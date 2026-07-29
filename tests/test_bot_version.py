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

    async def reply_text(self, text: str) -> None:
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

    assert message.replies == ["Running a1b2c3d, committed 2026-07-24T21:36:16-03:00."]


def test_version_reports_unknown_when_unbaked(tmp_path: Path) -> None:
    config = _config(tmp_path, VersionSettings())
    message = FakeMessage()
    update = FakeUpdate(message)
    context = FakeContext({"config": config})

    asyncio.run(cmd_version(update, context))  # type: ignore[arg-type]

    assert message.replies == ["Running unknown, committed unknown."]
