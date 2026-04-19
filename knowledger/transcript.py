import os

from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled, YouTubeTranscriptApi
from youtube_transcript_api.proxies import GenericProxyConfig

from .logger import get_logger

logger = get_logger(__name__)


def fetch_transcript(video_id: str) -> str | None:
    proxy_url = os.getenv("YOUTUBE_PROXY")
    proxy_config = GenericProxyConfig(http=proxy_url, https=proxy_url) if proxy_url else None
    try:
        api = YouTubeTranscriptApi(proxy_config=proxy_config)
        transcript_list = api.list(video_id)
        try:
            transcript = transcript_list.find_transcript(["en"]).fetch()
        except NoTranscriptFound:
            transcript = next(iter(transcript_list)).fetch()
        return "\n".join(snippet.text for snippet in transcript.snippets)
    except (TranscriptsDisabled, NoTranscriptFound):
        logger.info("No transcript available for video %s", video_id)
        return None
