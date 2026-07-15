import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlparse

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from .config import ProxyConfig

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


def extract_video_id(url: str) -> str | None:
    parsed = urlparse(url)

    if parsed.hostname in ("www.youtube.com", "youtube.com"):
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


def _fetch_page_data(
    video_id: str, proxy: ProxyConfig | None = None
) -> tuple[str | None, str | None]:
    """Return (title, upload_date) from the watch page. oEmbed truncates long titles."""
    proxies = {"http": proxy.url, "https": proxy.url} if proxy else None  # type: ignore[arg-type]
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

    try:
        page_title, upload_date = _fetch_page_data(video_id, proxy=proxy)
        title = page_title or data["title"]
    except (RequestException, ValueError):
        logger.exception("Could not fetch full page title for %s, using oEmbed title", video_id)
        title = data["title"]
        upload_date = None

    return VideoMetadata(
        video_id=video_id,
        title=title,
        channel_name=data["author_name"],
        upload_date=upload_date,
    )


def sanitize_filename(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "", text).strip()


def build_doc_name(channel_name: str, title: str, upload_date: str | None) -> str:
    """Build the canonical doc name: ``Youtube - {channel} - {title} - {date}``."""
    date_suffix = f" - {upload_date}" if upload_date else ""
    return f"Youtube - {sanitize_filename(channel_name)} - {sanitize_filename(title)}{date_suffix}"
