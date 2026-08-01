import asyncio
from pathlib import Path
from types import SimpleNamespace

from knowledger.bot import cmd_inqueue
from knowledger.config import (
    ClaudeSettings,
    Config,
    LoggerConfig,
    StorageSettings,
    TelegramSettings,
)
from knowledger.pending_transcripts import PendingTranscriptStore
from knowledger.poller import PendingVideo, PollerState
from knowledger.queue import Queue, QueueEntry


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[tuple[str, str | None]] = []

    async def reply_text(self, text: str, parse_mode: str | None = None, **kwargs) -> None:
        self.replies.append((text, parse_mode))


class FakeUpdate:
    def __init__(self, message: FakeMessage, user_id: int = 1) -> None:
        self.message = message
        self.effective_user = SimpleNamespace(id=user_id)


class FakeContext:
    def __init__(self, bot_data: dict) -> None:
        self.bot_data = bot_data


def _config(data_dir: Path) -> Config:
    return Config(
        logger=LoggerConfig(),
        telegram=TelegramSettings(bot_token="x", allowed_user_ids=frozenset({1})),
        claude=ClaudeSettings(session_token="x"),
        storage=StorageSettings(data_dir=data_dir),
    )


def test_inqueue_with_nothing_queued_says_so_once(tmp_path: Path) -> None:
    """Three section headers whose only content was the word "empty" told the reader
    nothing; the single empty state does (UH35). Also still a regression test for
    docs/bugs/inqueue-markdown-italics.md — no raw on-disk filenames leak into the copy.
    """
    bot_data = {
        "queue": Queue(path=tmp_path / "petition_queue.json"),
        "config": _config(tmp_path),
        "pending_transcripts": PendingTranscriptStore(path=tmp_path / "pending_transcripts.json"),
    }
    message = FakeMessage()
    update = FakeUpdate(message)
    context = FakeContext(bot_data)

    asyncio.run(cmd_inqueue(update, context))  # type: ignore[arg-type]

    assert len(message.replies) == 1
    text, parse_mode = message.replies[0]
    assert parse_mode == "HTML"
    assert "petition_queue.json" not in text
    assert "poller_state.json" not in text
    assert "pending_transcripts.json" not in text
    assert text == "✅ Nothing queued — everything's up to date."


def test_inqueue_shows_counts_stuck_marker_and_caps_long_lists(tmp_path: Path) -> None:
    queue = Queue(path=tmp_path / "petition_queue.json")
    for i in range(2):
        queue.enqueue(
            QueueEntry(
                project_id="p",
                video_id=f"v{i}",
                file_name=f"file{i}",
                transcript="t",
                chat_id=1,
                video_title=f"Title {i}",
                queued_at="2026-07-17T09:12:00+00:00",
                channel_name=f"Channel {i}",
                upload_attempts=4 if i == 0 else 0,
            )
        )

    state_path = tmp_path / "poller_state.json"
    state = PollerState(path=state_path)
    for i in range(12):
        state.pending.append(
            PendingVideo(
                channel_id="c",
                video_id=f"pv{i}",
                title=f"Video {i}",
                channel_name="Channel A",
                published="2026-07-18T08:15:00+00:00",
                first_seen="2026-07-18T08:15:00+00:00",
            )
        )
        state.seen.add(f"pv{i}")
    state.save()

    bot_data = {
        "queue": queue,
        "config": _config(tmp_path),
        "pending_transcripts": PendingTranscriptStore(path=tmp_path / "pending_transcripts.json"),
    }
    message = FakeMessage()
    update = FakeUpdate(message)
    context = FakeContext(bot_data)

    asyncio.run(cmd_inqueue(update, context))  # type: ignore[arg-type]

    text, _ = message.replies[0]
    assert "Retry queue — 2 queued" in text
    assert "🛑" in text  # stuck marker; same emoji as the stuck alert
    assert "4 failed attempts" in text
    assert "run /refresh to retry" in text
    assert "Poller — 12 waiting to upload" in text
    assert "+2 more" in text
    assert "12 videos seen since the poller started." in text
    # Titles link back to the video rather than sitting there as dead text.
    assert '<a href="https://youtu.be/v0">Title 0</a>' in text
    assert '<a href="https://youtu.be/pv0">Video 0</a>' in text
