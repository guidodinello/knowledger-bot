from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled, YouTubeTranscriptApi
from youtube_transcript_api._errors import RequestBlocked
from youtube_transcript_api.proxies import WebshareProxyConfig

from .config import ProxyConfig
from .invidious import fetch_transcript as fetch_transcript_invidious
from .logger import get_logger

logger = get_logger(__name__)


def fetch_transcript(video_id: str, proxy: ProxyConfig | None = None) -> str | None:
    proxy_config = (
        WebshareProxyConfig(proxy_username=proxy.username, proxy_password=proxy.password)
        if proxy is not None
        else None
    )
    try:
        api = YouTubeTranscriptApi(proxy_config=proxy_config)
        transcript_list = api.list(video_id)
        try:
            transcript = transcript_list.find_transcript(["en"]).fetch()
        except NoTranscriptFound:
            transcript = next(iter(transcript_list)).fetch()
        text = "\n".join(snippet.text.strip() for snippet in transcript.snippets)
        logger.info("Fetched transcript via YouTube API for %s", video_id)
        return text
    except RequestBlocked:
        logger.warning("YouTube blocked transcript request for %s, trying Invidious", video_id)
        return fetch_transcript_invidious(video_id)
    except (TranscriptsDisabled, NoTranscriptFound):
        logger.info("No transcript available for video %s", video_id)
        return None
