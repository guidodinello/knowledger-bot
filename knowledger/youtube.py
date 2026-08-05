import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from .config import ProxyConfig

if TYPE_CHECKING:
    from curl_cffi.requests.session import ProxySpec

logger = logging.getLogger(__name__)

OEMBED_URL = "https://www.youtube.com/oembed"
WATCH_URL = "https://www.youtube.com/watch"


class _OgTitleParser(HTMLParser):
    """Extract <meta property="og:title" content="..."> — tolerant of attribute order,
    case, and extra attributes, unlike an exact-order regex."""

    def __init__(self) -> None:
        super().__init__()
        self.title: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.title is not None or tag != "meta":
            return
        attr_map = dict(attrs)
        if attr_map.get("property") == "og:title" and attr_map.get("content") is not None:
            self.title = attr_map["content"]


def _extract_og_title(html_text: str) -> str | None:
    parser = _OgTitleParser()
    parser.feed(html_text)
    return parser.title


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    video_id: str
    title: str
    channel_name: str
    upload_date: str | None
    # oEmbed's `author_url` — the channel the video belongs to (e.g.
    # "https://www.youtube.com/@Foo"). Optional because nothing in the upload path needs
    # it; /subscribe uses it to derive the channel to watch from a single video link.
    channel_url: str | None = None


_YOUTUBE_HOSTS = ("www.youtube.com", "youtube.com", "m.youtube.com")


def extract_video_id(url: str) -> str | None:
    parsed = urlparse(url)

    if parsed.hostname in _YOUTUBE_HOSTS:
        if parsed.path == "/watch":
            qs = parse_qs(parsed.query)
            ids = qs.get("v")
            return ids[0] if ids else None
        if parsed.path.startswith("/shorts/"):
            return parsed.path.split("/shorts/")[1].split("/")[0] or None

    if parsed.hostname == "youtu.be":
        vid = parsed.path.lstrip("/").split("/")[0]
        return vid or None

    return None


def extract_channel_handle(url: str) -> str | None:
    """Return the channel-identifying path segment of a YouTube channel URL, in the form
    `resolve_channel_id` expects to append to `https://www.youtube.com/`: `"@handle"`,
    or the legacy/id forms `"channel/UC..."`, `"c/Name"`, `"user/Name"`.

    A bare `"@handle"` (no URL around it) is accepted too — that's what a user is most
    likely to type. Returns None for anything that isn't a channel reference, including
    video URLs (use `extract_video_id` for those)."""
    text = url.strip()
    if text.startswith("@"):
        return text if "/" not in text else None

    parsed = urlparse(text if "//" in text else f"https://{text}")
    if parsed.hostname not in _YOUTUBE_HOSTS:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        return None
    if parts[0].startswith("@"):
        return parts[0]
    if len(parts) >= 2 and parts[0] in ("channel", "c", "user"):
        return f"{parts[0]}/{parts[1]}"
    return None


def _fetch_page_data(
    video_id: str,
    proxy: ProxyConfig | None = None,
) -> tuple[str | None, str | None]:
    """Return (title, upload_date) from the watch page. oEmbed truncates long titles."""
    proxies: ProxySpec | None = {"http": proxy.url, "https": proxy.url} if proxy else None
    response = requests.get(
        WATCH_URL,
        params={"v": video_id},
        impersonate="chrome110",
        proxies=proxies,
    )
    response.raise_for_status()
    title = _extract_og_title(response.text)
    date_match = re.search(r'"uploadDate"\s*:\s*"(\d{4}-\d{2}-\d{2})', response.text)
    upload_date = date_match.group(1) if date_match else None
    return title, upload_date


def fetch_video_metadata(url: str, proxy: ProxyConfig | None = None) -> VideoMetadata:
    video_id = extract_video_id(url)
    if not video_id:
        raise ValueError(f"Could not extract video ID from URL: {url}")

    response = requests.get(
        OEMBED_URL,
        params={"url": url, "format": "json"},
        impersonate="chrome110",
    )
    if response.status_code == 401:
        raise ValueError("This video is private, age-restricted, or unavailable.")
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("YouTube returned malformed video metadata.")
    oembed_title = data.get("title")
    channel_name = data.get("author_name")
    if not isinstance(oembed_title, str) or not isinstance(channel_name, str):
        raise ValueError("YouTube returned incomplete video metadata.")

    try:
        page_title, upload_date = _fetch_page_data(video_id, proxy=proxy)
        title = page_title or oembed_title
    except (RequestException, ValueError):
        logger.exception("Could not fetch full page title for %s, using oEmbed title", video_id)
        title = oembed_title
        upload_date = None

    channel_url = data.get("author_url")
    return VideoMetadata(
        video_id=video_id,
        title=title,
        channel_name=channel_name,
        upload_date=upload_date,
        channel_url=channel_url if isinstance(channel_url, str) else None,
    )


def watch_url(video_id: str) -> str:
    """The canonical watch URL for a video id — for callers that hold an id (a feed
    entry, a queued retry) and need the URL form `fetch_video_metadata` takes."""
    return f"{WATCH_URL}?v={video_id}"


def sanitize_filename(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "", text).strip()


def build_doc_name(channel_name: str, title: str, upload_date: str | None) -> str:
    """Build the canonical doc name: ``Youtube - {channel} - {title} - {date}``."""
    date_suffix = f" - {upload_date}" if upload_date else ""
    return f"Youtube - {sanitize_filename(channel_name)} - {sanitize_filename(title)}{date_suffix}"


_DATE_SUFFIX_RE = re.compile(r" - \d{4}-\d{2}-\d{2}$")


def has_date_suffix(doc_name: str) -> bool:
    """Whether a doc name carries `build_doc_name`'s date suffix.

    A name without one was built from metadata whose `upload_date` came back None —
    the degraded form, and the reason two upload paths for the same video can disagree
    on its name (see `pending_transcripts._resolve_doc_name`)."""
    return _DATE_SUFFIX_RE.search(doc_name) is not None
