from unittest.mock import MagicMock, patch

import pytest
from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled
from youtube_transcript_api._errors import RequestBlocked

from knowledger.transcript import (
    TranscriptTransportError,
    TranscriptUnavailable,
    _pick_transcript,
    fetch_transcript,
)


def _track(language_code: str, *, is_generated: bool, text: str = "hello") -> MagicMock:
    track = MagicMock(language_code=language_code, is_generated=is_generated)
    track.fetch.return_value = MagicMock(snippets=[MagicMock(text=text)])
    return track


def _fake_list(*tracks: MagicMock) -> MagicMock:
    """A TranscriptList stand-in iterating like the real one: manual tracks first."""
    transcript_list = MagicMock(video_id="vid")
    ordered = [t for t in tracks if not t.is_generated] + [t for t in tracks if t.is_generated]
    transcript_list.__iter__.side_effect = lambda: iter(ordered)
    return transcript_list


def _fake_api(effect) -> MagicMock:
    """A YouTubeTranscriptApi stand-in whose .list() either raises `effect` or returns a
    transcript_list yielding one fetchable transcript."""
    api = MagicMock()
    if isinstance(effect, Exception):
        api.list.side_effect = effect
    else:
        api.list.return_value = _fake_list(_track("en", is_generated=True))
    return api


def test_success_returns_text() -> None:
    api = _fake_api(None)
    with patch("knowledger.transcript.YouTubeTranscriptApi", return_value=api):
        assert fetch_transcript("vid") == "hello"


def test_prefers_original_language_over_manual_foreign_track() -> None:
    # The reported bug: a Spanish video with a manual Arabic track came back in Arabic.
    arabic = _track("ar", is_generated=False)
    spanish = _track("es", is_generated=True)
    assert _pick_transcript(_fake_list(arabic, spanish)) is spanish


def test_prefers_original_language_over_english() -> None:
    english = _track("en", is_generated=False)
    spanish = _track("es", is_generated=True)
    assert _pick_transcript(_fake_list(english, spanish)) is spanish


def test_manual_track_wins_within_original_language() -> None:
    manual = _track("es-419", is_generated=False)
    generated = _track("es", is_generated=True)
    assert _pick_transcript(_fake_list(manual, generated)) is manual


def test_without_generated_track_falls_back_to_english_then_spanish() -> None:
    arabic = _track("ar", is_generated=False)
    spanish = _track("es", is_generated=False)
    english = _track("en-US", is_generated=False)
    assert _pick_transcript(_fake_list(arabic, spanish, english)) is english
    assert _pick_transcript(_fake_list(arabic, spanish)) is spanish


def test_falls_back_to_any_track() -> None:
    arabic = _track("ar", is_generated=False)
    french = _track("fr", is_generated=False)
    assert _pick_transcript(_fake_list(arabic, french)) is arabic


def test_empty_list_raises_unavailable() -> None:
    api = MagicMock()
    api.list.return_value = _fake_list()
    with (
        patch("knowledger.transcript.YouTubeTranscriptApi", return_value=api),
        pytest.raises(TranscriptUnavailable),
    ):
        fetch_transcript("vid")


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
