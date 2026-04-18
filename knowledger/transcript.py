from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled, YouTubeTranscriptApi

from .logger import get_logger

logger = get_logger(__name__)


def fetch_transcript(video_id: str) -> str | None:
    try:
        api = YouTubeTranscriptApi()
        transcript_list = api.list(video_id)
        try:
            transcript = transcript_list.find_transcript(["en"]).fetch()
        except NoTranscriptFound:
            transcript = next(iter(transcript_list)).fetch()
        return "\n".join(snippet.text for snippet in transcript.snippets)
    except (TranscriptsDisabled, NoTranscriptFound):
        logger.info("No transcript available for video %s", video_id)
        return None
