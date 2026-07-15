from unittest.mock import MagicMock, patch

import pytest
from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled
from youtube_transcript_api._errors import RequestBlocked

from knowledger.transcript import TranscriptTransportError, TranscriptUnavailable, fetch_transcript


def _fake_api(effect) -> MagicMock:
    """A YouTubeTranscriptApi stand-in whose .list() either raises `effect` or returns a
    transcript_list yielding one fetchable transcript."""
    api = MagicMock()
    if isinstance(effect, Exception):
        api.list.side_effect = effect
    else:
        snippet = MagicMock(text="hello")
        transcript = MagicMock(snippets=[snippet])
        found = MagicMock()
        found.fetch.return_value = transcript
        transcript_list = MagicMock()
        transcript_list.find_transcript.return_value = found
        api.list.return_value = transcript_list
    return api


def test_success_returns_text() -> None:
    api = _fake_api(None)
    with patch("knowledger.transcript.YouTubeTranscriptApi", return_value=api):
        assert fetch_transcript("vid") == "hello"


def test_transcripts_disabled_raises_unavailable() -> None:
    api = _fake_api(TranscriptsDisabled("vid"))
    with (
        patch("knowledger.transcript.YouTubeTranscriptApi", return_value=api),
        pytest.raises(TranscriptUnavailable),
    ):
        fetch_transcript("vid")


def test_no_transcript_found_at_list_stage_raises_unavailable() -> None:
    api = _fake_api(NoTranscriptFound("vid", ["en"], MagicMock()))
    with (
        patch("knowledger.transcript.YouTubeTranscriptApi", return_value=api),
        pytest.raises(TranscriptUnavailable),
    ):
        fetch_transcript("vid")


def test_request_blocked_raises_transport_error() -> None:
    api = _fake_api(RequestBlocked("vid"))
    with (
        patch("knowledger.transcript.YouTubeTranscriptApi", return_value=api),
        pytest.raises(TranscriptTransportError),
    ):
        fetch_transcript("vid")


def test_session_is_closed_on_success(tmp_path) -> None:
    cookies_path = tmp_path / "cookies.txt"
    cookies_path.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tFALSE\t0\tname\tvalue\n"
    )
    api = _fake_api(None)
    close_calls: list[bool] = []

    class TrackedSession:
        def __init__(self) -> None:
            self.cookies = None

        def close(self) -> None:
            close_calls.append(True)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    with (
        patch("knowledger.transcript.requests.Session", TrackedSession),
        patch("knowledger.transcript.YouTubeTranscriptApi", return_value=api),
    ):
        fetch_transcript("vid", cookies_path=cookies_path)

    assert close_calls == [True]


def test_session_is_closed_on_failure(tmp_path) -> None:
    cookies_path = tmp_path / "cookies.txt"
    cookies_path.write_text(
        "# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tFALSE\t0\tname\tvalue\n"
    )
    api = _fake_api(TranscriptsDisabled("vid"))
    close_calls: list[bool] = []

    class TrackedSession:
        def __init__(self) -> None:
            self.cookies = None

        def close(self) -> None:
            close_calls.append(True)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    with (
        patch("knowledger.transcript.requests.Session", TrackedSession),
        patch("knowledger.transcript.YouTubeTranscriptApi", return_value=api),
        pytest.raises(TranscriptUnavailable),
    ):
        fetch_transcript("vid", cookies_path=cookies_path)

    assert close_calls == [True]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
