"""Turn a YouTube link into a watched channel in ``channels.json``.

Backs the bot's ``/subscribe`` command: the user sends a link to *any* video from a
channel (or the channel's own URL/``@handle``), and this module works out which channel
that is — name, ``@handle`` and ``UCxxxx`` id — so the poller can start watching it.

Resolution is network-bound and synchronous (``curl_cffi``); callers in async code run
`resolve_subscription` through ``asyncio.to_thread``, as they already do for
``fetch_video_metadata``.
"""

from dataclasses import dataclass
from pathlib import Path

from curl_cffi.requests.exceptions import RequestException

from .config import ProxyConfig
from .logger import get_logger
from .poller import Channel, fetch_feed, load_channels, resolve_channel_id, save_channels
from .youtube import extract_channel_handle, extract_video_id, fetch_video_metadata

logger = get_logger(__name__)


class SubscriptionError(Exception):
    """A subscription could not be resolved. The message is user-facing — it is shown
    in Telegram verbatim, so it says what to do about it rather than what broke."""


@dataclass(frozen=True, slots=True)
class ResolvedChannel:
    handle: str  # "@Foo", or "channel/UC..." for a channel with no handle
    name: str
    channel_id: str


def _resolve_id(handle: str, proxy: ProxyConfig | None) -> str:
    """The ``channel/UC...`` form already carries the id — everything else costs a fetch
    of the channel page."""
    if handle.startswith("channel/"):
        return handle.removeprefix("channel/")
    channel_id = resolve_channel_id(handle, proxy)
    if channel_id is None:
        raise SubscriptionError(
            f"Couldn't find the channel id for {handle} — YouTube may be blocking the "
            "lookup. Try again in a minute.",
        )
    return channel_id


def _name_from_feed(channel_id: str, proxy: ProxyConfig | None) -> str | None:
    """The Atom feed names the channel in every entry's author element — the cheapest
    display name for a channel we reached by URL rather than through a video."""
    try:
        videos = fetch_feed(channel_id, proxy)
    except Exception:
        logger.warning("Could not read %s's feed while subscribing", channel_id, exc_info=True)
        return None
    return next((v.channel_name for v in videos if v.channel_name), None)


def resolve_subscription(url: str, proxy: ProxyConfig | None = None) -> ResolvedChannel:
    """Resolve a video URL, channel URL or bare ``@handle`` to the channel to watch.

    Raises SubscriptionError with a user-facing message if the link isn't YouTube, the
    video can't be read, or the channel id can't be resolved."""
    text = url.strip()

    if extract_video_id(text):
        try:
            metadata = fetch_video_metadata(text, proxy)
        except (RequestException, ValueError) as e:
            raise SubscriptionError(f"Couldn't read that video: {e}") from e
        handle = extract_channel_handle(metadata.channel_url or "")
        if handle is None:
            raise SubscriptionError(
                "YouTube didn't report a channel for that video. Send the channel's URL "
                "or @handle instead.",
            )
        return ResolvedChannel(
            handle=handle,
            name=metadata.channel_name,
            channel_id=_resolve_id(handle, proxy),
        )

    handle = extract_channel_handle(text)
    if handle is None:
        raise SubscriptionError(
            "That doesn't look like a YouTube link. Send a link to any video from the "
            "channel, or the channel's @handle.",
        )
    channel_id = _resolve_id(handle, proxy)
    return ResolvedChannel(
        handle=handle,
        name=_name_from_feed(channel_id, proxy) or handle.removeprefix("@"),
        channel_id=channel_id,
    )


def find_subscription(channels: list[Channel], resolved: ResolvedChannel) -> Channel | None:
    """Match on either identifier: an entry added by hand may still have a null
    `channel_id` (the poller backfills it), and an entry whose channel later renamed its
    handle still matches by id."""
    return next(
        (
            c
            for c in channels
            if c.channel_id == resolved.channel_id or c.handle.lower() == resolved.handle.lower()
        ),
        None,
    )


def add_subscription(
    path: Path,
    resolved: ResolvedChannel,
    project: str | None,
) -> Channel | None:
    """Append the channel to ``channels.json``, or return None if it's already there.

    Re-reads the file immediately before writing rather than trusting the copy the
    command handler read earlier, so a channel the poller resolved an id for in the
    meantime isn't clobbered. `project` is a Claude project uuid, or None to inherit
    `AUTO_TRANSCRIPT_PROJECT`."""
    channels = load_channels(path)
    if find_subscription(channels, resolved) is not None:
        return None
    channel = Channel(
        handle=resolved.handle,
        name=resolved.name,
        channel_id=resolved.channel_id,
        project=project,
    )
    channels.append(channel)
    save_channels(path, channels)
    return channel
