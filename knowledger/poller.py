"""Watch designated YouTube channels and auto-upload transcripts of new videos.

Runs as an in-process asyncio task inside the bot (see ``main.py``). Every
``config.poller.poll_interval`` seconds it polls each channel's Atom feed, enqueues
newly published videos, and once a video is 24h old fetches its transcript and uploads
it as a doc into ``config.poller.auto_transcript_project`` — reusing the shared
``TranscriptUploadService`` and the ``Queue`` auth-fallback.
"""

import asyncio
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException
from defusedxml import ElementTree as ET
from telegram.ext import Application

from .claude_client import AuthError, ClaudeClient
from .config import Config, ProxyConfig
from .logger import get_logger
from .notify import notify
from .persistence import CorruptDataError, PersistenceError, atomic_write_json, load_json
from .queue import Queue, QueueEntry
from .queue_processor import MAX_UPLOAD_ATTEMPTS
from .transcript import TranscriptTransportError, TranscriptUnavailable, fetch_transcript
from .upload_service import (
    AlreadyExists,
    DeferredForAuth,
    RetryPending,
    TranscriptUploadService,
    Uploaded,
)
from .youtube import build_doc_name

logger = get_logger(__name__)

FEED_URL = "https://www.youtube.com/feeds/videos.xml"
ATOM_NS = "{http://www.w3.org/2005/Atom}"
YT_NS = "{http://www.youtube.com/xml/schemas/2015}"

UPLOAD_DELAY = timedelta(hours=24)  # wait for YouTube's polished captions to replace the draft
GIVE_UP_AFTER = timedelta(hours=72)  # measured from first detection, not publish time
# Priority order: a channel page carries its OWN id as "externalId" / the canonical
# /channel/ link, but also embeds OTHER channels' "channelId" (recommendations, etc.) —
# so match the authoritative fields first and fall back to a bare channelId only last.
_CHANNEL_ID_PATTERNS = (
    re.compile(r'"externalId":"(UC[\w-]+)"'),
    re.compile(r'<link rel="canonical" href="https://www\.youtube\.com/channel/(UC[\w-]+)"'),
    re.compile(r'"channelId":"(UC[\w-]+)"'),
)


@dataclass(slots=True)
class Channel:
    handle: str  # e.g. "@NeuronaFinanciera"
    name: str
    channel_id: str | None = None


@dataclass(frozen=True, slots=True)
class PendingVideo:
    channel_id: str
    video_id: str
    title: str
    channel_name: str
    published: str  # ISO-8601, tz-aware (from the feed)
    first_seen: str  # ISO-8601 UTC, when the poller first enqueued it
    upload_attempts: int = 0  # consecutive non-auth upload failures, for the stuck-video alert


@dataclass(slots=True)
class PollerState:
    path: Path
    seen: set[str] = field(default_factory=set)
    pending: list[PendingVideo] = field(default_factory=list)
    auth_error_notified: bool = False  # in-memory only; not persisted to disk

    @classmethod
    def load(cls, path: Path) -> "PollerState":
        """Load state. A missing file is a valid first-run/empty state. A corrupt or
        unreadable existing file fails closed (raises) instead of silently resetting —
        that would re-run first-run baseline seeding and re-detect every pending video
        as brand new."""
        raw = load_json(path)
        if raw is None:
            return cls(path=path)
        try:
            return cls(
                path=path,
                seen=set(raw["seen"]),
                pending=[PendingVideo(**p) for p in raw["pending"]],
            )
        except (KeyError, TypeError) as e:
            raise CorruptDataError(path, f"malformed poller state: {e}") from e

    def save(self) -> None:
        payload = {"seen": sorted(self.seen), "pending": [asdict(v) for v in self.pending]}
        atomic_write_json(self.path, payload)


def load_channels(path: Path) -> list[Channel]:
    """Missing channels file: valid empty state (poller idle). An existing but corrupt
    or unreadable file fails closed instead of silently disabling the poller."""
    raw = load_json(path)
    if raw is None:
        logger.warning("Channels file %s not found; poller has nothing to watch", path)
        return []
    if not isinstance(raw, list):
        raise CorruptDataError(path, "expected a JSON array of channels")
    try:
        return [Channel(**c) for c in raw]
    except TypeError as e:
        raise CorruptDataError(path, f"malformed channel entry: {e}") from e


def _save_channels(path: Path, channels: list[Channel]) -> None:
    atomic_write_json(path, [asdict(c) for c in channels])


def _proxies(proxy: ProxyConfig | None) -> dict[str, str] | None:
    return {"http": proxy.url, "https": proxy.url} if proxy else None


def _http_get(url: str, params: dict[str, str], proxy: ProxyConfig | None) -> str:
    """GET as a browser. Try direct first; fall back to the proxy only if direct fails.

    YouTube blocks Oracle datacenter IPs for the transcript API, but the RSS feed and
    channel pages often work direct — so we avoid paid proxy bandwidth unless we must.
    """
    try:
        response = requests.get(url, params=params, impersonate="chrome110")
        response.raise_for_status()
        return response.text
    except RequestException:
        if proxy is None:
            raise
        logger.info("Direct fetch failed for %s; retrying via proxy", url)
        response = requests.get(
            url,
            params=params,
            impersonate="chrome110",
            proxies=_proxies(proxy),  # type: ignore[arg-type]
        )
        response.raise_for_status()
        return response.text


def resolve_channel_id(handle: str, proxy: ProxyConfig | None = None) -> str | None:
    """Resolve an ``@handle`` to its ``UCxxxx`` channel id by scraping the channel page."""
    url = f"https://www.youtube.com/{handle.lstrip('/')}"
    try:
        html = _http_get(url, {}, proxy)
    except RequestException:
        logger.warning("Failed to fetch channel page for %s", handle, exc_info=True)
        return None
    for pattern in _CHANNEL_ID_PATTERNS:
        match = pattern.search(html)
        if match is not None:
            return match.group(1)
    logger.warning("Could not resolve channel_id for %s", handle)
    return None


def fetch_feed(channel_id: str, proxy: ProxyConfig | None = None) -> list[PendingVideo]:
    """Fetch and parse a channel's Atom feed into PendingVideo entries (first_seen = now)."""
    xml = _http_get(FEED_URL, {"channel_id": channel_id}, proxy)
    root = ET.fromstring(xml)
    now = datetime.now(UTC).isoformat()
    videos: list[PendingVideo] = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        video_id = entry.findtext(f"{YT_NS}videoId")
        title = entry.findtext(f"{ATOM_NS}title")
        published = entry.findtext(f"{ATOM_NS}published")
        author = entry.find(f"{ATOM_NS}author")
        channel_name = author.findtext(f"{ATOM_NS}name") if author is not None else None
        if not (video_id and title and published):
            continue
        videos.append(
            PendingVideo(
                channel_id=channel_id,
                video_id=video_id,
                title=title,
                channel_name=channel_name or "",
                published=published,
                first_seen=now,
            )
        )
    return videos


def _resolve_missing_ids(channels: list[Channel], path: Path, proxy: ProxyConfig | None) -> None:
    """Backfill any null channel_id from its handle and persist the result."""
    changed = False
    for ch in channels:
        if ch.channel_id:
            continue
        cid = resolve_channel_id(ch.handle, proxy)
        if cid:
            ch.channel_id = cid
            changed = True
            logger.info("Resolved %s -> %s", ch.handle, cid)
    if changed:
        _save_channels(path, channels)


def _baseline_seed(channels: list[Channel], state: PollerState, proxy: ProxyConfig | None) -> None:
    """First-run only: mark every current feed video as seen WITHOUT enqueueing it, so the
    poller only ever processes videos published after it starts (no back-catalogue storm)."""
    for ch in channels:
        if not ch.channel_id:
            continue
        try:
            for video in fetch_feed(ch.channel_id, proxy):
                state.seen.add(video.video_id)
        except Exception:
            logger.warning("Baseline fetch failed for %s", ch.handle, exc_info=True)
    logger.info("Baseline-seeded %d existing videos", len(state.seen))


def _resolve_project(client: ClaudeClient, name_or_uuid: str) -> str | None:
    """Resolve AUTO_TRANSCRIPT_PROJECT — accepts either a project uuid or a project name."""
    target = name_or_uuid.lower()
    match = next(
        (
            p
            for p in client.list_projects()
            if name_or_uuid == p["uuid"] or target == p["name"].lower()
        ),
        None,
    )
    if match is None:
        logger.error("Auto-transcript project '%s' not found in Claude account", name_or_uuid)
        return None
    return match["uuid"]


def _enqueue_auth_fallback(
    queue: Queue,
    project_id: str,
    video: PendingVideo,
    transcript: str,
    file_name: str,
    config: Config,
) -> None:
    """On token expiry, park the transcript in petition_queue.json so /refresh uploads it."""
    entry = QueueEntry(
        project_id=project_id,
        video_id=video.video_id,
        file_name=file_name,
        transcript=transcript,
        chat_id=next(iter(config.telegram.allowed_user_ids), 0),
        video_title=video.title,
        queued_at=datetime.now(UTC).isoformat(),
    )
    # Persistence failures propagate rather than being logged and swallowed — silently
    # discarding a fetched transcript here would lose it with no record it ever existed.
    queue.enqueue(entry)


class TranscriptPoller:
    """Holds the poller's stable dependencies for its whole run — replaces threading
    app/config/client/queue/channels/state/project_name through every call as
    positional arguments."""

    def __init__(
        self,
        app: Application,
        config: Config,
        client: ClaudeClient,
        queue: Queue,
        channels: list[Channel],
        state: PollerState,
        project_name: str,
    ) -> None:
        self._app = app
        self._config = config
        self._client = client
        self._queue = queue
        self._service = TranscriptUploadService(client)
        self._channels = channels
        self._state = state
        self._project_name = project_name

    async def _process_video(
        self, project_id: str, video: PendingVideo, now: datetime
    ) -> PendingVideo | None:
        """Fetch + upload one due video. Returns the (possibly updated) video to keep
        it in the pending list, or None once it's done — confirmed uploaded, or
        permanently given up on."""
        try:
            transcript = await asyncio.to_thread(
                fetch_transcript,
                video.video_id,
                self._config.transcript.proxy,
                self._config.transcript.youtube_cookies_path,
            )
        except TranscriptUnavailable:
            # Only an authoritative "no transcript" result may age into the give-up
            # policy — a transient transport failure must stay retryable indefinitely,
            # or a temporary block during the 72h window would get permanently
            # misclassified as no captions.
            first_seen = datetime.fromisoformat(video.first_seen)
            if now - first_seen >= GIVE_UP_AFTER:
                logger.info("Giving up on %s — no captions after %s", video.video_id, GIVE_UP_AFTER)
                await notify(
                    self._app, self._config, f"⚠️ No captions for “{video.title}” — gave up."
                )
                return None
            logger.info("Transcript not ready for %s; will retry", video.video_id)
            return video
        except TranscriptTransportError:
            logger.info("Transcript request blocked for %s; will retry", video.video_id)
            return video

        file_name = build_doc_name(video.channel_name, video.title, video.published[:10])

        outcome = await asyncio.to_thread(self._service.upload, project_id, transcript, file_name)
        match outcome:
            case Uploaded():
                logger.info("Auto-uploaded %s to project %s", file_name, project_id)
                self._queue.remove(project_id, video.video_id)
                await notify(self._app, self._config, f"✅ Auto-uploaded “{file_name}”")
                return None
            case AlreadyExists():
                logger.info("Doc already exists, skipping: %s", file_name)
                self._queue.remove(project_id, video.video_id)
                return None
            case DeferredForAuth():
                _enqueue_auth_fallback(
                    self._queue, project_id, video, transcript, file_name, self._config
                )
                await notify(
                    self._app, self._config, f"Token expired — “{file_name}” queued. Run /refresh."
                )
                return video  # keep pending until we confirm the upload landed
            case RetryPending(step=step, error=error):
                # Covers both a failed doc listing and a failed upload — previously
                # only upload failures counted toward the stuck-video alert; a
                # transient listing failure silently retried unnoticed. Folding both
                # into the same retry-classification closes that gap.
                logger.warning(
                    "Upload attempt failed for %s while %s: %s; will retry", file_name, step, error
                )
                attempts = video.upload_attempts + 1
                if attempts % MAX_UPLOAD_ATTEMPTS == 0:
                    await notify(
                        self._app,
                        self._config,
                        f"🛑 Upload stuck for “{file_name}” while {step} — failed {attempts}x in "
                        "a row. Check logs; it will keep retrying.",
                    )
                return replace(video, upload_attempts=attempts)

    async def _tick(self) -> None:
        # 1. Detect new videos across all channels.
        for ch in self._channels:
            if not ch.channel_id:
                continue
            try:
                videos = await asyncio.to_thread(
                    fetch_feed, ch.channel_id, self._config.transcript.proxy
                )
            except Exception:
                logger.warning(
                    "Feed fetch failed for %s; skipping this tick", ch.handle, exc_info=True
                )
                continue
            for video in videos:
                if video.video_id not in self._state.seen:
                    self._state.seen.add(video.video_id)
                    self._state.pending.append(video)
                    logger.info("Detected new video %s (%s)", video.video_id, video.channel_name)
        self._state.save()

        if not self._state.pending:
            return

        # 2. Resolve the target project once per tick (cached on the client).
        try:
            project_id = await asyncio.to_thread(_resolve_project, self._client, self._project_name)
        except AuthError:
            logger.warning("Auth error resolving project; skipping processing this tick")
            if not self._state.auth_error_notified:
                self._state.auth_error_notified = True
                await notify(
                    self._app,
                    self._config,
                    "⚠️ Claude session token expired — poller is paused. "
                    "Update the token (POST /update-token) to resume.",
                )
            return
        if self._state.auth_error_notified:
            self._state.auth_error_notified = False
            await notify(
                self._app, self._config, "✅ Claude session token restored — poller resumed."
            )
        if project_id is None:
            return

        # 3. Process every pending video whose publish time is at least UPLOAD_DELAY
        # ago. Persist after each video so a crash/exception mid-batch can't discard
        # already-confirmed progress on videos processed earlier in the same tick
        # (state.pending is always the already-settled prefix plus the untouched
        # remainder, never in-memory-only).
        now = datetime.now(UTC)
        original_pending = list(self._state.pending)
        settled: list[PendingVideo] = []
        for i, video in enumerate(original_pending):
            if now - datetime.fromisoformat(video.published) < UPLOAD_DELAY:
                settled.append(video)
                continue
            result = await self._process_video(project_id, video, now)
            if result is not None:
                settled.append(result)
            self._state.pending = settled + original_pending[i + 1 :]
            self._state.save()
        self._state.pending = settled
        self._state.save()

    async def run(self) -> None:
        logger.info(
            "Poller started: %d channel(s), interval %ds",
            len(self._channels),
            self._config.poller.poll_interval,
        )
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except PersistenceError:
                # Fatal: state/queue can no longer be trusted. Let this propagate out
                # of the poller task so structured supervision (see main.py)
                # terminates the whole process visibly instead of continuing to run on
                # unreliable persistence.
                raise
            except Exception:
                logger.exception("Poller tick failed; continuing")
            await asyncio.sleep(self._config.poller.poll_interval)


async def run_poller(app: Application, config: Config) -> None:
    project_name = config.poller.auto_transcript_project
    if not project_name:
        logger.info("AUTO_TRANSCRIPT_PROJECT not set; poller disabled")
        return

    client: ClaudeClient = app.bot_data["claude_client"]
    queue: Queue = app.bot_data["queue"]

    channels = load_channels(config.poller.channels_path)
    if not channels:
        logger.warning("No channels configured (%s); poller idle", config.poller.channels_path)
        return

    await asyncio.to_thread(
        _resolve_missing_ids, channels, config.poller.channels_path, config.transcript.proxy
    )

    config.storage.data_dir.mkdir(parents=True, exist_ok=True)
    state_path = config.storage.data_dir / "poller_state.json"
    first_run = not state_path.exists()
    state = PollerState.load(state_path)
    if first_run:
        await asyncio.to_thread(_baseline_seed, channels, state, config.transcript.proxy)
        state.save()

    poller = TranscriptPoller(app, config, client, queue, channels, state, project_name)
    await poller.run()
