"""Watch designated YouTube channels and auto-upload transcripts of new videos.

Runs as an in-process asyncio task inside the bot (see ``main.py``). Every
``config.poller.poll_interval`` seconds it polls each channel's Atom feed, enqueues
newly published videos, and once a video is 24h old fetches its transcript and uploads
it as a doc into ``config.poller.auto_transcript_project`` (or a channel's own
``project`` override in ``channels.json``) — reusing the shared
``TranscriptUploadService`` and the ``Queue`` auth-fallback.
"""

import asyncio
import fcntl
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException
from defusedxml import ElementTree as ET
from telegram.ext import Application

from .claude_client import AuthError, ClaudeClient, Project
from .config import Config, ProxyConfig
from .history import UploadRecord, record_upload
from .logger import get_logger
from .notify import notify
from .persistence import (
    CorruptDataError,
    PersistenceError,
    atomic_write_json,
    atomic_write_json_if_exists,
    load_json,
)
from .queue import Queue, build_auth_fallback_entry
from .queue_processor import MAX_UPLOAD_ATTEMPTS
from .telegram_format import bold, code, subject
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


def _video_subject(video: "PendingVideo") -> str:
    """Every notification about a pending video names it the same way."""
    return subject(video.title, video.channel_name, video.video_id)


def _fmt_days(delta: timedelta) -> str:
    """Render a give-up/delay window for user-facing copy. Derived from the constant
    rather than restated as a literal, so the copy can't drift when it changes."""
    days = round(delta.total_seconds() / 86400)
    if days < 1:
        hours = round(delta.total_seconds() / 3600)
        return f"{hours} hours"
    return "a day" if days == 1 else f"{days} days"


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
    project: str | None = None  # overrides PollerSettings.auto_transcript_project; name or uuid


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
    baseline_seeded: set[str] = field(default_factory=set)  # channel_ids already baseline-seeded
    auth_error_notified: bool = False  # in-memory only; not persisted to disk

    @classmethod
    def load(cls, path: Path) -> "PollerState":
        """Load state. A missing file is a valid first-run/empty state. A corrupt or
        unreadable existing file fails closed (raises) instead of silently resetting —
        that would re-run baseline seeding for every channel and re-detect every
        pending video as brand new."""
        raw = load_json(path)
        if raw is None:
            return cls(path=path)
        try:
            return cls(
                path=path,
                seen=set(raw["seen"]),
                pending=[PendingVideo(**p) for p in raw["pending"]],
                # Missing key: pre-existing state file from before per-channel baseline
                # seeding existed. Defaulting to empty re-seeds every channel it's
                # already tracking exactly once after upgrading, which is harmless —
                # baseline seeding only adds already-`seen` video ids back to `seen`.
                baseline_seeded=set(raw.get("baseline_seeded", [])),
            )
        except (KeyError, TypeError) as e:
            raise CorruptDataError(path, f"malformed poller state: {e}") from e

    def save(self) -> None:
        payload = {
            "seen": sorted(self.seen),
            "pending": [asdict(v) for v in self.pending],
            "baseline_seeded": sorted(self.baseline_seeded),
        }
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


def _channel_dicts(channels: list[Channel]) -> list[dict[str, str | None]]:
    """Serialize channels, omitting a null project so absence means inherit default."""
    channel_dicts = []
    for channel in channels:
        data = asdict(channel)
        if data["project"] is None:
            del data["project"]
        channel_dicts.append(data)
    return channel_dicts


def save_channels(path: Path, channels: list[Channel]) -> None:
    atomic_write_json(path, _channel_dicts(channels))


def _save_existing_channels(path: Path, channels: list[Channel]) -> bool:
    """Persist only if channels.json still exists at the atomic replacement point."""
    return atomic_write_json_if_exists(path, _channel_dicts(channels))


@contextmanager
def channels_file_lock(path: Path) -> Iterator[None]:
    """Serialize channels.json read-modify-write transactions across worker threads."""
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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
            ),
        )
    return videos


def _resolve_missing_ids(channels: list[Channel], path: Path, proxy: ProxyConfig | None) -> None:
    """Backfill any null channel_id from its handle and persist the result.

    Resolution is network-bound and takes seconds per channel, during which `/subscribe`
    can append to the same file. The final re-read and save share a filesystem lock with
    that command's whole mutation transaction, so neither writer can drop the other's
    changes. If the file disappeared during resolution, it is deliberately not recreated;
    the caller's in-memory list still gets the resolved ids either way."""
    resolved: dict[str, str] = {}
    for ch in channels:
        if ch.channel_id:
            continue
        cid = resolve_channel_id(ch.handle, proxy)
        if cid:
            ch.channel_id = cid
            resolved[ch.handle.lower()] = cid
            logger.info("Resolved %s -> %s", ch.handle, cid)
    if not resolved:
        return
    with channels_file_lock(path):
        latest = load_channels(path)
        for ch in latest:
            if not ch.channel_id:
                ch.channel_id = resolved.get(ch.handle.lower())
        if not _save_existing_channels(path, latest):
            logger.warning(
                "Channels file %s disappeared during id resolution; not recreating it",
                path,
            )


def _baseline_seed(
    channels: list[Channel],
    state: PollerState,
    proxy: ProxyConfig | None,
) -> set[str]:
    """Mark every current feed video of the given channels as seen WITHOUT enqueueing it,
    so the poller only ever processes videos published after a channel starts being
    watched (no back-catalogue storm). Called both on first run (all channels) and
    whenever a channel is newly added to channels.json (just that channel).

    Returns the ids of the channels whose feed was actually read. A channel whose fetch
    failed is deliberately left out: marking it seeded anyway would make the next tick
    treat its whole back catalogue as new — the exact flood this seeding prevents — and
    YouTube blocking a datacenter IP makes that failure routine, not exceptional."""
    seeded: set[str] = set()
    for ch in channels:
        if not ch.channel_id:
            continue
        try:
            videos = fetch_feed(ch.channel_id, proxy)
        except Exception:
            logger.warning("Baseline fetch failed for %s", ch.handle, exc_info=True)
            continue
        # An empty feed is a successful read (a channel with no videos yet) — seeded.
        for video in videos:
            state.seen.add(video.video_id)
        seeded.add(ch.channel_id)
    logger.info("Baseline-seeded %d of %d channels", len(seeded), len(channels))
    return seeded


async def sync_channels(
    path: Path,
    state: PollerState,
    proxy: ProxyConfig | None,
    current: list[Channel],
) -> list[Channel]:
    """Load the watch list, backfill any missing `channel_id`, and baseline-seed channels
    that have never been seeded. This is the poller's startup sequence, re-run at the top
    of every tick so a channel added by `/subscribe` starts being watched without a
    restart — and so it goes through the same baseline seeding as any other new channel,
    rather than flooding the queue with its back catalogue.

    An unreadable watch list (missing file, corrupt JSON) keeps `current` instead of
    clearing it: losing the file is a reason to keep watching what we already loaded, not
    to silently stop watching everything. A file that legitimately parses to an empty
    list *does* clear the watch list — that's an edit, not a failure."""
    if not path.exists():
        if current:
            logger.warning("Channels file %s disappeared; keeping the loaded channels", path)
        return current
    try:
        channels = load_channels(path)
    except PersistenceError:
        logger.exception("Could not reload %s; keeping the loaded channels", path)
        return current

    await asyncio.to_thread(_resolve_missing_ids, channels, path, proxy)

    # Drop seeded-markers for channels no longer watched. Removing a channel by hand and
    # re-adding it later is the documented flow (there is no /unsubscribe), and a stale
    # marker would skip seeding on the way back in — uploading the whole gap period.
    watched = {ch.channel_id for ch in channels if ch.channel_id}
    pruned = state.baseline_seeded - watched
    if pruned:
        logger.info("Dropping baseline markers for %d unwatched channels", len(pruned))
        state.baseline_seeded &= watched

    unseeded = [
        ch for ch in channels if ch.channel_id and ch.channel_id not in state.baseline_seeded
    ]
    if unseeded:
        state.baseline_seeded |= await asyncio.to_thread(_baseline_seed, unseeded, state, proxy)
    # A prune alone changes state without producing anything to seed — still persist it.
    if unseeded or pruned:
        state.save()
    return channels


def _resolve_project(client: ClaudeClient, name_or_uuid: str) -> Project | None:
    """Resolve AUTO_TRANSCRIPT_PROJECT — accepts either a project uuid or a project name.

    Returns the whole project, not just its uuid: the configured value may be either
    form, so it is not something to show a user. Echoing it verbatim is what put a bare
    uuid in the "Auto-saved to …" notification, and the resolved name was right here
    all along."""
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
    return match


def _enqueue_auth_fallback(
    queue: Queue,
    project_id: str,
    video: PendingVideo,
    transcript: str,
    file_name: str,
    config: Config,
) -> None:
    """On token expiry, park the transcript in petition_queue.json so /refresh uploads it."""
    entry = build_auth_fallback_entry(
        project_id=project_id,
        video_id=video.video_id,
        file_name=file_name,
        transcript=transcript,
        chat_id=next(iter(config.telegram.allowed_user_ids), 0),
        video_title=video.title,
        channel_name=video.channel_name,
    )
    # Persistence failures propagate rather than being logged and swallowed — silently
    # discarding a fetched transcript here would lose it with no record it ever existed.
    queue.enqueue(entry)


class TranscriptPoller:
    """Holds the poller's stable dependencies for its whole run — replaces threading
    app/config/client/queue/channels/state/default_project through every call as
    positional arguments."""

    def __init__(
        self,
        app: Application,
        config: Config,
        client: ClaudeClient,
        queue: Queue,
        channels: list[Channel],
        state: PollerState,
        default_project: str,
    ) -> None:
        self._app = app
        self._config = config
        self._client = client
        self._queue = queue
        self._service = TranscriptUploadService(client)
        self._channels = channels
        self._state = state
        # The raw AUTO_TRANSCRIPT_PROJECT setting: a name *or* a uuid, and never shown
        # to a user — only fed to _resolve_project, which yields both.
        self._default_project = default_project

    async def _process_video(
        self,
        project: Project,
        video: PendingVideo,
        now: datetime,
    ) -> PendingVideo | None:
        """Fetch + upload one due video. Returns the (possibly updated) video to keep
        it in the pending list, or None once it's done — confirmed uploaded, or
        permanently given up on."""
        project_id, project_name = project["uuid"], project["name"]
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
                    self._app,
                    self._config,
                    _video_subject(video) + "\n\nStill no captions after "
                    f"{_fmt_days(GIVE_UP_AFTER)}, so I've stopped waiting for them.",
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
                record_upload(
                    self._config.storage.data_dir,
                    UploadRecord(
                        project_id=project_id,
                        file_name=file_name,
                        video_title=video.title,
                        channel_name=video.channel_name,
                        uploaded_at=datetime.now(UTC).isoformat(),
                        video_id=video.video_id,
                    ),
                )
                await notify(
                    self._app,
                    self._config,
                    f"✅ Auto-saved to {bold(project_name)}\n{_video_subject(video)}",
                )
                return None
            case AlreadyExists():
                logger.info("Doc already exists, skipping: %s", file_name)
                self._queue.remove(project_id, video.video_id)
                return None
            case DeferredForAuth():
                _enqueue_auth_fallback(
                    self._queue,
                    project_id,
                    video,
                    transcript,
                    file_name,
                    self._config,
                )
                await notify(
                    self._app,
                    self._config,
                    f"⏳ Waiting on a valid token\n{_video_subject(video)}\n\n"
                    "The transcript is queued. Update your Claude session token, "
                    "then run /refresh.",
                )
                return video  # keep pending until we confirm the upload landed
            case RetryPending(step=step, error=error):
                # Covers both a failed doc listing and a failed upload — previously
                # only upload failures counted toward the stuck-video alert; a
                # transient listing failure silently retried unnoticed. Folding both
                # into the same retry-classification closes that gap.
                logger.warning(
                    "Upload attempt failed for %s while %s: %s; will retry",
                    file_name,
                    step,
                    error,
                )
                attempts = video.upload_attempts + 1
                if attempts % MAX_UPLOAD_ATTEMPTS == 0:
                    await notify(
                        self._app,
                        self._config,
                        f"🛑 Stuck after {attempts} attempts — the upload to "
                        f"{bold(project_name)} keeps failing.\n{_video_subject(video)}\n\n"
                        "It stays queued and keeps retrying; check the logs if it "
                        "doesn't clear.",
                    )
                return replace(video, upload_attempts=attempts)

    async def _tick(self) -> None:
        # 0. Re-read the watch list so channels added since the last tick (via
        # /subscribe, or by editing channels.json directly) are picked up — seeded
        # first, so this tick treats their back catalogue as already seen.
        self._channels = await sync_channels(
            self._config.poller.channels_path,
            self._state,
            self._config.transcript.proxy,
            self._channels,
        )

        # 1. Detect new videos across all channels. A channel whose baseline seed failed
        # above is skipped: its feed has never been read, so every entry would look new
        # and its back catalogue would be enqueued wholesale. Seeding is retried next tick.
        for ch in self._channels:
            if not ch.channel_id:
                continue
            if ch.channel_id not in self._state.baseline_seeded:
                logger.info("Skipping %s until its baseline seed succeeds", ch.handle)
                continue
            try:
                videos = await asyncio.to_thread(
                    fetch_feed,
                    ch.channel_id,
                    self._config.transcript.proxy,
                )
            except Exception:
                logger.warning(
                    "Feed fetch failed for %s; skipping this tick",
                    ch.handle,
                    exc_info=True,
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

        # 2. Resolve each distinct configured project setting once per tick (cached on
        # the client), then map each channel to its resolved project. A channel with no
        # `project` override falls back to the global default.
        channel_project_setting = {
            ch.channel_id: ch.project or self._default_project
            for ch in self._channels
            if ch.channel_id
        }
        # The global default is always resolved, even when every channel overrides it —
        # it's the fallback for a pending video whose channel is no longer configured.
        settings = set(channel_project_setting.values()) | {self._default_project}
        try:
            projects = await self._resolve_projects(settings)
        except AuthError:
            logger.warning("Auth error resolving projects; skipping processing this tick")
            if not self._state.auth_error_notified:
                self._state.auth_error_notified = True
                await notify(
                    self._app,
                    self._config,
                    "⚠️ Auto-upload is paused: your Claude session token has expired.\n\n"
                    f"Send a new one to {code('POST /update-token')} and I'll resume "
                    "on the next check. Nothing is lost in the meantime — new videos "
                    "stay queued (see /inqueue).",
                )
            return
        if self._state.auth_error_notified:
            self._state.auth_error_notified = False
            await notify(
                self._app,
                self._config,
                "✅ Token accepted — auto-upload has resumed.",
            )

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
            # A pending video whose channel has since been removed from channels.json
            # still gets uploaded, to the global default project — it was already
            # detected and paid for, and dropping it here would be a silent data loss.
            setting = channel_project_setting.get(video.channel_id, self._default_project)
            project = projects.get(setting)
            if project is None:
                # Misconfigured project name (typo, wrong org) — leave it pending,
                # don't drop it; _resolve_project already logged the error.
                settled.append(video)
                continue
            result = await self._process_video(project, video, now)
            if result is not None:
                settled.append(result)
            self._state.pending = settled + original_pending[i + 1 :]
            self._state.save()
        self._state.pending = settled
        self._state.save()

    async def _resolve_projects(self, settings: set[str]) -> dict[str, Project | None]:
        """One _resolve_project call per distinct configured project setting, keyed by
        that setting so callers can look up by whatever channels.json holds.
        Each call hits the client's cached project list, so there's at most one
        Claude API round trip total per tick."""
        results = {}
        for setting in settings:
            try:
                results[setting] = await asyncio.to_thread(_resolve_project, self._client, setting)
            except AuthError:
                raise  # let _tick() handle it
        return results

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
    default_project = config.poller.auto_transcript_project
    if not default_project:
        logger.info("AUTO_TRANSCRIPT_PROJECT not set; poller disabled")
        return

    client: ClaudeClient = app.bot_data["claude_client"]
    queue: Queue = app.bot_data["queue"]

    config.storage.data_dir.mkdir(parents=True, exist_ok=True)
    state_path = config.storage.data_dir / "poller_state.json"
    state = PollerState.load(state_path)

    # Startup stays fail-closed on a corrupt watch list — that's a configuration error,
    # and starting up watching nothing would hide it. Only the per-tick reload degrades
    # gracefully (sync_channels keeps the channels it already had).
    channels = load_channels(config.poller.channels_path)
    channels = await sync_channels(
        config.poller.channels_path,
        state,
        config.transcript.proxy,
        channels,
    )
    if not channels:
        # Not fatal any more: every tick re-reads the file, so a watch list that is
        # empty (or missing) at startup starts working the moment /subscribe writes one.
        logger.warning(
            "No channels configured (%s); poller idle until one is added",
            config.poller.channels_path,
        )

    poller = TranscriptPoller(app, config, client, queue, channels, state, default_project)
    await poller.run()
