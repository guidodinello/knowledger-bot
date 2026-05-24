import http.cookiejar
from pathlib import Path

import requests
from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled, YouTubeTranscriptApi
from youtube_transcript_api._errors import RequestBlocked
from youtube_transcript_api.proxies import GenericProxyConfig

from .config import ProxyConfig
from .logger import get_logger

logger = get_logger(__name__)


def _build_session(cookies_path: Path | None) -> requests.Session | None:
    if cookies_path is None:
        return None
    jar = http.cookiejar.MozillaCookieJar(str(cookies_path))
    jar.load(ignore_discard=True, ignore_expires=True)
    session = requests.Session()
    session.cookies = jar  # type: ignore[assignment]
    return session


def fetch_transcript(
    video_id: str,
    proxy: ProxyConfig | None = None,
    cookies_path: Path | None = None,
) -> str | None:
    proxy_config = GenericProxyConfig(http_url=proxy.url) if proxy is not None else None
    try:
        api = YouTubeTranscriptApi(
            proxy_config=proxy_config,
            http_client=_build_session(cookies_path),
        )
        transcript_list = api.list(video_id)
        try:
            transcript = transcript_list.find_transcript(["en"]).fetch()
        except NoTranscriptFound:
            transcript = next(iter(transcript_list)).fetch()
        text = "\n".join(snippet.text.strip() for snippet in transcript.snippets)
        logger.info("Fetched transcript via YouTube API for %s", video_id)
        return text
    except (RequestBlocked, requests.exceptions.RetryError, requests.exceptions.ConnectionError):
        logger.warning("YouTube blocked transcript request for %s", video_id)
        return None
    except (TranscriptsDisabled, NoTranscriptFound):
        logger.info("No transcript available for video %s", video_id)
        return None
