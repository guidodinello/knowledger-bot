"""Watch designated YouTube channels and auto-upload transcripts of new videos.

Runs as an in-process asyncio task inside the bot (see ``main.py``). Every
``config.poll_interval`` seconds it polls each channel's Atom feed, enqueues newly
published videos, and once a video is 24h old fetches its transcript and uploads it as a
doc into ``config.auto_transcript_project`` — reusing the manual flow's dedup + upload
path (``ClaudeClient.list_docs`` / ``upload_content``) and the ``Queue`` auth-fallback.
"""

import asyncio
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException
from defusedxml import ElementTree as ET
from telegram.ext import Application

from .claude_client import AuthError, ClaudeClient
from .config import Config, ProxyConfig
from .logger import get_logger
from .queue import Queue, QueueEntry
from .transcript import fetch_transcript
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


@dataclass(slots=True)
class PollerState:
    path: Path
    seen: set[str] = field(default_factory=set)
    pending: list[PendingVideo] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "PollerState":
        """Load state. Missing or corrupt file is treated as empty (logged at WARNING)."""
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                path=path,
                seen=set(data["seen"]),
                pending=[PendingVideo(**p) for p in data["pending"]],
            )
        except FileNotFoundError:
            return cls(path=path)
        except Exception:
            logger.warning(
                "State file %s is corrupt or unreadable; treating as empty", path, exc_info=True
            )
            return cls(path=path)

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        payload = {"seen": sorted(self.seen), "pending": [asdict(v) for v in self.pending]}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def load_channels(path: Path) -> list[Channel]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("Channels file %s not found; poller has nothing to watch", path)
        return []
    except Exception:
        logger.warning("Channels file %s is corrupt or unreadable", path, exc_info=True)
        return []
    return [Channel(**c) for c in data]


def _save_channels(path: Path, channels: list[Channel]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(c) for c in channels], indent=2), encoding="utf-8")
    os.replace(tmp, path)


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
        (p for p in client.projects if name_or_uuid == p["uuid"] or target == p["name"].lower()),
        None,
    )
    if match is None:
        logger.error("Auto-transcript project '%s' not found in Claude account", name_or_uuid)
        return None
    return match["uuid"]


async def _notify(app: Application, config: Config, text: str) -> None:
    for uid in config.allowed_user_ids:
        try:
            await app.bot.send_message(chat_id=uid, text=text)
        except Exception:
            logger.warning("Failed to notify user %d", uid, exc_info=True)


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
        chat_id=next(iter(config.allowed_user_ids), 0),
        video_title=video.title,
        queued_at=datetime.now(UTC).isoformat(),
    )
    try:
        queue.enqueue(entry)
    except Exception:
        logger.exception("Failed to enqueue %s after auth error", file_name)


async def _process_video(
    app: Application,
    config: Config,
    client: ClaudeClient,
    queue: Queue,
    project_id: str,
    video: PendingVideo,
    now: datetime,
) -> bool:
    """Fetch + upload one due video. Returns True if it should leave the pending list."""
    transcript = await asyncio.to_thread(
        fetch_transcript, video.video_id, config.proxy, config.youtube_cookies_path
    )
    if transcript is None:
        first_seen = datetime.fromisoformat(video.first_seen)
        if now - first_seen >= GIVE_UP_AFTER:
            logger.info("Giving up on %s — no captions after %s", video.video_id, GIVE_UP_AFTER)
            await _notify(app, config, f"⚠️ No captions for “{video.title}” — gave up.")
            return True
        logger.info("Transcript not ready for %s; will retry", video.video_id)
        return False

    file_name = build_doc_name(video.channel_name, video.title, video.published[:10])

    try:
        docs = await asyncio.to_thread(client.list_docs, project_id)
    except AuthError:
        _enqueue_auth_fallback(queue, project_id, video, transcript, file_name, config)
        await _notify(app, config, f"Token expired — “{file_name}” queued. Run /refresh.")
        return False  # keep pending until we confirm the upload landed

    if any(d["file_name"] == file_name for d in docs):
        logger.info("Doc already exists, skipping: %s", file_name)
        return True

    try:
        await asyncio.to_thread(client.upload_content, project_id, transcript, file_name)
    except AuthError:
        _enqueue_auth_fallback(queue, project_id, video, transcript, file_name, config)
        await _notify(app, config, f"Token expired — “{file_name}” queued. Run /refresh.")
        return False
    except Exception:
        logger.exception("Upload failed for %s; will retry", file_name)
        return False

    logger.info("Auto-uploaded %s to project %s", file_name, project_id)
    await _notify(app, config, f"✅ Auto-uploaded “{file_name}”")
    return True


async def _tick(
    app: Application,
    config: Config,
    client: ClaudeClient,
    queue: Queue,
    channels: list[Channel],
    state: PollerState,
    project_name: str,
) -> None:
    # 1. Detect new videos across all channels.
    for ch in channels:
        if not ch.channel_id:
            continue
        try:
            videos = await asyncio.to_thread(fetch_feed, ch.channel_id, config.proxy)
        except Exception:
            logger.warning("Feed fetch failed for %s; skipping this tick", ch.handle, exc_info=True)
            continue
        for video in videos:
            if video.video_id not in state.seen:
                state.seen.add(video.video_id)
                state.pending.append(video)
                logger.info("Detected new video %s (%s)", video.video_id, video.channel_name)
    state.save()

    if not state.pending:
        return

    # 2. Resolve the target project once per tick (cached on the client).
    try:
        project_id = await asyncio.to_thread(_resolve_project, client, project_name)
    except AuthError:
        logger.warning("Auth error resolving project; skipping processing this tick")
        return
    if project_id is None:
        return

    # 3. Process every pending video whose publish time is at least UPLOAD_DELAY ago.
    now = datetime.now(UTC)
    still_pending: list[PendingVideo] = []
    for video in state.pending:
        if now - datetime.fromisoformat(video.published) < UPLOAD_DELAY:
            still_pending.append(video)
            continue
        done = await _process_video(app, config, client, queue, project_id, video, now)
        if not done:
            still_pending.append(video)
    state.pending = still_pending
    state.save()


async def run_poller(app: Application, config: Config) -> None:
    project_name = config.auto_transcript_project
    if not project_name:
        logger.info("AUTO_TRANSCRIPT_PROJECT not set; poller disabled")
        return

    client: ClaudeClient = app.bot_data["claude_client"]
    queue: Queue = app.bot_data["queue"]

    channels = load_channels(config.channels_path)
    if not channels:
        logger.warning("No channels configured (%s); poller idle", config.channels_path)
        return

    await asyncio.to_thread(_resolve_missing_ids, channels, config.channels_path, config.proxy)

    config.data_dir.mkdir(parents=True, exist_ok=True)
    state_path = config.data_dir / "poller_state.json"
    first_run = not state_path.exists()
    state = PollerState.load(state_path)
    if first_run:
        await asyncio.to_thread(_baseline_seed, channels, state, config.proxy)
        state.save()

    logger.info("Poller started: %d channel(s), interval %ds", len(channels), config.poll_interval)
    while True:
        try:
            await _tick(app, config, client, queue, channels, state, project_name)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Poller tick failed; continuing")
        await asyncio.sleep(config.poll_interval)
