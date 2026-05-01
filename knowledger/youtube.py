import html
import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from curl_cffi import requests

logger = logging.getLogger(__name__)

OEMBED_URL = "https://www.youtube.com/oembed"
WATCH_URL = "https://www.youtube.com/watch"


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    video_id: str
    title: str
    channel_name: str


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


def _fetch_page_title(video_id: str) -> str | None:
    """Fetch full title from og:title meta tag — oEmbed truncates long titles."""
    response = requests.get(
        WATCH_URL,
        params={"v": video_id},
        impersonate="chrome110",
    )
    response.raise_for_status()
    match = re.search(r'<meta property="og:title" content="([^"]+)"', response.text)
    return html.unescape(match.group(1)) if match else None


def fetch_video_metadata(url: str) -> VideoMetadata:
    video_id = extract_video_id(url)
    if not video_id:
        raise ValueError(f"Could not extract video ID from URL: {url}")

    response = requests.get(
        OEMBED_URL,
        params={"url": url, "format": "json"},
        impersonate="chrome110",
    )
    response.raise_for_status()
    data = response.json()

    try:
        title = _fetch_page_title(video_id) or data["title"]
    except Exception:
        logger.debug("Could not fetch full page title for %s, using oEmbed title", video_id)
        title = data["title"]

    return VideoMetadata(
        video_id=video_id,
        title=title,
        channel_name=data["author_name"],
    )


def sanitize_filename(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "", text).strip()
