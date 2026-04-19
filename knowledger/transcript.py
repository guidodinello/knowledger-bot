import os

from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled, YouTubeTranscriptApi
from youtube_transcript_api.proxies import WebshareProxyConfig

from .logger import get_logger

logger = get_logger(__name__)


def fetch_transcript(video_id: str) -> str | None:
    username = os.getenv("WEBSHARE_PROXY_USERNAME")
    password = os.getenv("WEBSHARE_PROXY_PASSWORD")
    proxy_config = (
        WebshareProxyConfig(proxy_username=username, proxy_password=password) if username else None
    )
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
