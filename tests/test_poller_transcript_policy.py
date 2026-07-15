import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from knowledger.config import Config, LoggerConfig
from knowledger.poller import GIVE_UP_AFTER, PendingVideo, _process_video
from knowledger.queue import Queue
from knowledger.transcript import TranscriptTransportError, TranscriptUnavailable


def _config() -> Config:
    return Config(
        logger=LoggerConfig(),
        telegram_bot_token="x",
        claude_session_token="x",
        allowed_user_ids=frozenset({1}),
    )


def _video(first_seen: datetime) -> PendingVideo:
    return PendingVideo(
        channel_id="c",
        video_id="v",
        title="T",
        channel_name="Ch",
        published=first_seen.isoformat(),
        first_seen=first_seen.isoformat(),
    )


@pytest.fixture(autouse=True)
def _no_notify():
    with patch("knowledger.poller.notify", new=AsyncMock()):
        yield


def test_transport_error_never_ages_into_give_up_even_past_the_window(tmp_path) -> None:
    """A transcript request that keeps getting blocked must stay retryable forever —
    it must never be reclassified as 'no captions' just because time has passed."""
    old_enough = datetime.now(UTC) - GIVE_UP_AFTER - timedelta(hours=1)
    video = _video(old_enough)
    queue = Queue(path=tmp_path / "q.json")
    client = object()  # unused: fetch fails before any client call
    app = object()

    with patch("knowledger.poller.fetch_transcript", side_effect=TranscriptTransportError("v")):
        result = asyncio.run(
            _process_video(app, _config(), client, queue, "proj", video, datetime.now(UTC))
        )

    assert result is not None  # kept pending, not given up on
    assert result.video_id == "v"


def test_unavailable_gives_up_after_window(tmp_path) -> None:
    old_enough = datetime.now(UTC) - GIVE_UP_AFTER - timedelta(hours=1)
    video = _video(old_enough)
    queue = Queue(path=tmp_path / "q.json")
    client = object()
    app = object()

    with patch("knowledger.poller.fetch_transcript", side_effect=TranscriptUnavailable("v")):
        result = asyncio.run(
            _process_video(app, _config(), client, queue, "proj", video, datetime.now(UTC))
        )

    assert result is None  # given up


def test_unavailable_within_window_stays_pending(tmp_path) -> None:
    recent = datetime.now(UTC) - timedelta(hours=1)
    video = _video(recent)
    queue = Queue(path=tmp_path / "q.json")
    client = object()
    app = object()

    with patch("knowledger.poller.fetch_transcript", side_effect=TranscriptUnavailable("v")):
        result = asyncio.run(
            _process_video(app, _config(), client, queue, "proj", video, datetime.now(UTC))
        )

    assert result is not None
    assert result.video_id == "v"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
