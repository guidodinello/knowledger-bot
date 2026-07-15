import http.cookiejar
from contextlib import ExitStack
from pathlib import Path

import requests
from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled, YouTubeTranscriptApi
from youtube_transcript_api._errors import RequestBlocked
from youtube_transcript_api.proxies import GenericProxyConfig

from .config import ProxyConfig
from .logger import get_logger

logger = get_logger(__name__)


class TranscriptError(Exception):
    def __init__(self, video_id: str) -> None:
        super().__init__(f"video {video_id}")
        self.video_id = video_id


class TranscriptUnavailable(TranscriptError):
    """Authoritative: no transcript exists for this video (captions disabled or none
    found in any language). Safe to permanently give up on."""


class TranscriptTransportError(TranscriptError):
    """Transient: the request was blocked, rate-limited, or the connection failed.
    Must NOT be treated the same as TranscriptUnavailable — it should stay retryable."""


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
) -> str:
    """Fetch a transcript's plain text. Raises TranscriptUnavailable if the video
    authoritatively has none, or TranscriptTransportError on a blocked/connection
    failure that should be retried instead."""
    proxy_config = GenericProxyConfig(http_url=proxy.url) if proxy is not None else None
    session = _build_session(cookies_path)
    try:
        with ExitStack() as stack:
            # Only a session we created ourselves needs closing; a None http_client
            # makes the library create and own its own internal session.
            if session is not None:
                stack.enter_context(session)
            api = YouTubeTranscriptApi(proxy_config=proxy_config, http_client=session)
            transcript_list = api.list(video_id)
            try:
                transcript = transcript_list.find_transcript(["en"]).fetch()
            except NoTranscriptFound:
                transcript = next(iter(transcript_list)).fetch()
            text = "\n".join(snippet.text.strip() for snippet in transcript.snippets)
            logger.info("Fetched transcript via YouTube API for %s", video_id)
            return text
    except (
        RequestBlocked,
        requests.exceptions.RetryError,
        requests.exceptions.ConnectionError,
    ) as e:
        logger.warning("YouTube blocked transcript request for %s", video_id)
        raise TranscriptTransportError(video_id) from e
    except (TranscriptsDisabled, NoTranscriptFound) as e:
        logger.info("No transcript available for video %s", video_id)
        raise TranscriptUnavailable(video_id) from e
