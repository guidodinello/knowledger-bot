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
from knowledger.queue import Queue


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[tuple[str, str | None]] = []

    async def reply_text(self, text: str, parse_mode: str | None = None) -> None:
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


def test_inqueue_wraps_literal_filenames_in_backticks_not_underscores(tmp_path: Path) -> None:
    """Regression test for docs/bugs/inqueue-markdown-italics.md: the hardcoded
    filenames each contain a single underscore, which Telegram's legacy Markdown
    parser treats as an italics toggle across the whole message. Backticks sidestep
    that instead of leaving the raw, unescaped names in the message."""
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
    assert parse_mode == "Markdown"
    assert "`petition_queue.json`" in text
    assert "`poller_state.json`" in text
    assert "`pending_transcripts.json`" in text
    assert "petition_queue.json (retry/upload queue)" not in text
    assert "poller_state.json (seen + pending videos)" not in text
    assert "pending_transcripts.json (transcript fetches blocked" not in text
