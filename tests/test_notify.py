import asyncio
from pathlib import Path
from typing import cast

from telegram.ext import Application

from knowledger.config import (
    ClaudeSettings,
    Config,
    LoggerConfig,
    StorageSettings,
    TelegramSettings,
)
from knowledger.notify import notify
from knowledger.telegram_format import TELEGRAM_MAX_MESSAGE_LENGTH


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
        self.sent.append(text)


class FakeApplication:
    def __init__(self) -> None:
        self.bot = FakeBot()


def _config(tmp_path: Path) -> Config:
    return Config(
        logger=LoggerConfig(),
        telegram=TelegramSettings(bot_token="x", allowed_user_ids=frozenset({1})),
        claude=ClaudeSettings(session_token="x"),
        storage=StorageSettings(data_dir=tmp_path),
    )


def test_notify_caps_an_overlong_broadcast(tmp_path: Path) -> None:
    app = FakeApplication()

    asyncio.run(notify(cast(Application, app), _config(tmp_path), "x" * 5000))

    assert len(app.bot.sent) == 1
    assert len(app.bot.sent[0]) <= TELEGRAM_MAX_MESSAGE_LENGTH
    assert app.bot.sent[0].endswith("… (truncated)")


def test_notify_preserves_literal_text_when_parse_mode_is_none(tmp_path: Path) -> None:
    app = FakeApplication()
    text = "A <literal> & B " * 500

    asyncio.run(notify(cast(Application, app), _config(tmp_path), text, parse_mode=None))

    sent = app.bot.sent[0]
    assert len(sent) <= TELEGRAM_MAX_MESSAGE_LENGTH
    assert "<literal>" in sent
    assert "&amp;" not in sent
    assert sent.endswith("… (truncated)")
