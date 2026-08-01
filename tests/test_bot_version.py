import asyncio
from pathlib import Path
from types import SimpleNamespace

from knowledger.bot import cmd_version
from knowledger.config import (
    ClaudeSettings,
    Config,
    LoggerConfig,
    StorageSettings,
    TelegramSettings,
    VersionSettings,
)


class FakeMessage:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply_text(self, text: str, parse_mode: str | None = None, **kwargs: object) -> None:
        self.replies.append(text)


class FakeUpdate:
    def __init__(self, message: FakeMessage, user_id: int = 1) -> None:
        self.message = message
        self.effective_user = SimpleNamespace(id=user_id)


class FakeContext:
    def __init__(self, bot_data: dict) -> None:
        self.bot_data = bot_data


def _config(data_dir: Path, version: VersionSettings) -> Config:
    return Config(
        logger=LoggerConfig(),
        telegram=TelegramSettings(bot_token="x", allowed_user_ids=frozenset({1})),
        claude=ClaudeSettings(session_token="x"),
        storage=StorageSettings(data_dir=data_dir),
        version=version,
    )


def test_version_reports_baked_sha_and_date(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        VersionSettings(commit_sha="a1b2c3d", commit_date="2026-07-24T21:36:16-03:00"),
    )
    message = FakeMessage()
    update = FakeUpdate(message)
    context = FakeContext({"config": config})

    asyncio.run(cmd_version(update, context))  # type: ignore[arg-type]

    assert len(message.replies) == 1
    reply = message.replies[0]
    assert "a1b2c3d" in reply
    # Humanised, not the raw ISO string with its timezone offset.
    assert "2026-07-24 21:36" in reply
    assert "T21:36:16-03:00" not in reply


def test_version_says_so_plainly_when_the_build_was_never_stamped(tmp_path: Path) -> None:
    """An unstamped build has no sha to report, so "Running unknown, committed unknown"
    told the reader nothing. Name the actual situation instead."""
    config = _config(tmp_path, VersionSettings())
    message = FakeMessage()
    update = FakeUpdate(message)
    context = FakeContext({"config": config})

    asyncio.run(cmd_version(update, context))  # type: ignore[arg-type]

    assert len(message.replies) == 1
    assert "unknown" not in message.replies[0]
    assert "wasn't stamped" in message.replies[0]
