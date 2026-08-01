import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from telegram import InlineKeyboardMarkup

from knowledger.bot import cmd_subscribe, cmd_subscribed, handle_subscribe_selection
from knowledger.claude_client import AuthError
from knowledger.config import (
    ClaudeSettings,
    Config,
    LoggerConfig,
    PollerSettings,
    StorageSettings,
    TelegramSettings,
)
from knowledger.subscriptions import ResolvedChannel

PROJECTS = [
    {"uuid": "inv-uuid", "name": "Investments"},
    {"uuid": "gym-uuid", "name": "Exercise"},
]


class FakeMessage:
    def __init__(self, message_id: int = 7) -> None:
        self.message_id = message_id
        self.replies: list[str] = []
        self.markups: list[InlineKeyboardMarkup | None] = []

    async def reply_text(
        self,
        text: str,
        parse_mode: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        self.replies.append(text)
        self.markups.append(reply_markup)


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.answers: list[str | None] = []
        self.edits: list[str] = []
        self.markups: list[InlineKeyboardMarkup | None] = []

    async def answer(self, text: str | None = None) -> None:
        self.answers.append(text)

    async def edit_message_text(self, text: str, parse_mode: str | None = None) -> None:
        self.edits.append(text)

    async def edit_message_reply_markup(
        self,
        reply_markup: InlineKeyboardMarkup | None = None,
    ) -> None:
        self.markups.append(reply_markup)


class FakeUpdate:
    def __init__(
        self,
        message: FakeMessage | None = None,
        query: FakeQuery | None = None,
        user_id: int = 1,
    ) -> None:
        self.message = message
        self.callback_query = query
        self.effective_user = SimpleNamespace(id=user_id)


class FakeClient:
    def list_projects(self) -> list[dict]:
        return PROJECTS


class FakeContext:
    def __init__(self, config: Config, args: list[str] | None = None) -> None:
        self.bot_data = {"config": config, "claude_client": FakeClient()}
        self.user_data: dict[str, Any] = {}
        self.args = args


def _config(
    tmp_path: Path,
    *,
    whitelist: frozenset[str] = frozenset(),
    **poller_kwargs: Any,
) -> Config:
    poller_kwargs.setdefault("channels_path", tmp_path / "channels.json")
    return Config(
        logger=LoggerConfig(),
        telegram=TelegramSettings(
            bot_token="x",
            allowed_user_ids=frozenset({1}),
            project_whitelist=whitelist,
        ),
        claude=ClaudeSettings(session_token="x"),
        storage=StorageSettings(data_dir=tmp_path),
        poller=PollerSettings(**poller_kwargs),
    )


def _write_channels(config: Config, entries: list[dict]) -> None:
    config.poller.channels_path.write_text(json.dumps(entries))


def _callbacks(markup: InlineKeyboardMarkup | None) -> list[str]:
    assert markup is not None
    return [str(button.callback_data) for row in markup.inline_keyboard for button in row]


# --- /subscribed ---------------------------------------------------------------------


def test_subscribed_reports_an_empty_watch_list(tmp_path: Path) -> None:
    message = FakeMessage()
    context = FakeContext(_config(tmp_path, auto_transcript_project="Investments"))

    asyncio.run(cmd_subscribed(FakeUpdate(message), context))  # type: ignore[arg-type]

    assert "Not watching any channels yet" in message.replies[0]


def test_subscribed_lists_channels_with_resolved_project_names(tmp_path: Path) -> None:
    config = _config(tmp_path, auto_transcript_project="inv-uuid", poll_interval=3600)
    _write_channels(
        config,
        [
            {"handle": "@Cava", "name": "José Luis Cava", "channel_id": "UC1"},
            {"handle": "@Rosa", "name": "Dr. La Rosa", "channel_id": "UC2", "project": "gym-uuid"},
        ],
    )
    message = FakeMessage()

    asyncio.run(cmd_subscribed(FakeUpdate(message), FakeContext(config)))  # type: ignore[arg-type]

    text = message.replies[0]
    assert "📺 Watching 2 channel(s)" in text
    assert "• José Luis Cava (@Cava)" in text
    assert "→ Investments (default)" in text  # inherits AUTO_TRANSCRIPT_PROJECT, shown by name
    assert "• Dr. La Rosa (@Rosa)" in text
    assert "→ Exercise" in text  # per-channel override, uuid resolved to its name
    assert "Checked every 1h" in text


def test_subscribed_warns_when_auto_upload_is_off(tmp_path: Path) -> None:
    config = _config(tmp_path)  # no auto_transcript_project
    _write_channels(config, [{"handle": "@Cava", "name": "Cava", "channel_id": "UC1"}])
    message = FakeMessage()

    asyncio.run(cmd_subscribed(FakeUpdate(message), FakeContext(config)))  # type: ignore[arg-type]

    assert "AUTO_TRANSCRIPT_PROJECT is not set" in message.replies[0]


def test_subscribed_reports_a_corrupt_watch_list_instead_of_crashing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.poller.channels_path.write_text("{not json")
    message = FakeMessage()

    asyncio.run(cmd_subscribed(FakeUpdate(message), FakeContext(config)))  # type: ignore[arg-type]

    assert "Couldn't read the channel list" in message.replies[0]


def test_subscribed_survives_an_unusable_project_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Naming projects is display-only: a dead token downgrades to raw uuids rather
    than refusing to show which channels are watched."""
    config = _config(tmp_path, auto_transcript_project="inv-uuid")
    _write_channels(config, [{"handle": "@Cava", "name": "Cava", "channel_id": "UC1"}])
    context = FakeContext(config)
    monkeypatch.setattr(
        context.bot_data["claude_client"],
        "list_projects",
        lambda: (_ for _ in ()).throw(AuthError("no token")),
    )
    message = FakeMessage()

    asyncio.run(cmd_subscribed(FakeUpdate(message), context))  # type: ignore[arg-type]

    assert "• Cava (@Cava)" in message.replies[0]
    assert "→ inv-uuid (default)" in message.replies[0]


# --- /subscribe ----------------------------------------------------------------------


def test_subscribe_without_arguments_explains_itself(tmp_path: Path) -> None:
    message = FakeMessage()
    context = FakeContext(_config(tmp_path), args=[])

    asyncio.run(cmd_subscribe(FakeUpdate(message), context))  # type: ignore[arg-type]

    assert "Usage: /subscribe" in message.replies[0]


def test_subscribe_rejects_a_link_that_isnt_youtube(tmp_path: Path) -> None:
    message = FakeMessage()
    context = FakeContext(_config(tmp_path), args=["https://example.com/x"])

    asyncio.run(cmd_subscribe(FakeUpdate(message), context))  # type: ignore[arg-type]

    assert "doesn't look like a YouTube link" in message.replies[-1]


def _patch_resolution(monkeypatch: pytest.MonkeyPatch, resolved: ResolvedChannel) -> None:
    monkeypatch.setattr("knowledger.bot.resolve_subscription", lambda url, proxy: resolved)


def test_subscribe_offers_a_project_picker_with_a_default_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, auto_transcript_project="inv-uuid")
    _patch_resolution(monkeypatch, ResolvedChannel("@Cava", "José Luis Cava", "UC1"))
    message = FakeMessage(message_id=42)
    context = FakeContext(config, args=["https://www.youtube.com/watch?v=abc"])

    asyncio.run(cmd_subscribe(FakeUpdate(message), context))  # type: ignore[arg-type]

    assert "Found José Luis Cava (@Cava)" in message.replies[-1]
    assert _callbacks(message.markups[-1]) == [
        "sub:default:42",
        "sub:inv-uuid:42",
        "sub:gym-uuid:42",
    ]
    assert context.user_data["subscribe_42"].channel_id == "UC1"


def test_subscribe_recognises_a_channel_already_watched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, auto_transcript_project="inv-uuid")
    _write_channels(config, [{"handle": "@Cava", "name": "Cava", "channel_id": "UC1"}])
    _patch_resolution(monkeypatch, ResolvedChannel("@Cava", "José Luis Cava", "UC1"))
    message = FakeMessage()
    context = FakeContext(config, args=["https://www.youtube.com/watch?v=abc"])

    asyncio.run(cmd_subscribe(FakeUpdate(message), context))  # type: ignore[arg-type]

    assert "Already watching Cava (@Cava)" in message.replies[-1]
    assert message.markups[-1] is None  # no picker; nothing to choose


# --- picker callback ------------------------------------------------------------------


def _select(config: Config, choice: str, resolved: ResolvedChannel) -> FakeQuery:
    query = FakeQuery(f"sub:{choice}:42")
    context = FakeContext(config)
    context.user_data["subscribe_42"] = resolved
    asyncio.run(handle_subscribe_selection(FakeUpdate(query=query), context))  # type: ignore[arg-type]
    return query


def test_selecting_a_project_writes_the_channel(tmp_path: Path) -> None:
    config = _config(tmp_path, auto_transcript_project="inv-uuid", poll_interval=1800)

    query = _select(config, "gym-uuid", ResolvedChannel("@Rosa", "Dr. La Rosa", "UC2"))

    assert json.loads(config.poller.channels_path.read_text()) == [
        {"handle": "@Rosa", "name": "Dr. La Rosa", "channel_id": "UC2", "project": "gym-uuid"},
    ]
    assert "✅ Watching Dr. La Rosa (@Rosa)." in query.edits[-1]
    assert "New videos go to Exercise" in query.edits[-1]
    assert "within 30 min" in query.edits[-1]


def test_selecting_the_default_inherits_the_global_project(tmp_path: Path) -> None:
    config = _config(tmp_path, auto_transcript_project="inv-uuid")

    query = _select(config, "default", ResolvedChannel("@Rosa", "Dr. La Rosa", "UC2"))

    assert json.loads(config.poller.channels_path.read_text()) == [
        {"handle": "@Rosa", "name": "Dr. La Rosa", "channel_id": "UC2"},
    ]
    assert "New videos go to Investments" in query.edits[-1]


def test_selection_appends_to_an_existing_watch_list(tmp_path: Path) -> None:
    config = _config(tmp_path, auto_transcript_project="inv-uuid")
    _write_channels(config, [{"handle": "@Cava", "name": "Cava", "channel_id": "UC1"}])

    _select(config, "default", ResolvedChannel("@Rosa", "Dr. La Rosa", "UC2"))

    assert [c["handle"] for c in json.loads(config.poller.channels_path.read_text())] == [
        "@Cava",
        "@Rosa",
    ]


def test_selecting_a_project_still_warns_when_auto_upload_is_off(tmp_path: Path) -> None:
    """Without AUTO_TRANSCRIPT_PROJECT the poller task never starts, so picking an
    explicit project for the channel doesn't get it uploaded either."""
    config = _config(tmp_path)  # no auto_transcript_project

    query = _select(config, "gym-uuid", ResolvedChannel("@Rosa", "Dr. La Rosa", "UC2"))

    assert "✅ Watching Dr. La Rosa (@Rosa)." in query.edits[-1]
    assert "AUTO_TRANSCRIPT_PROJECT is not set" in query.edits[-1]


def test_selection_after_a_restart_asks_for_the_command_again(tmp_path: Path) -> None:
    """user_data is in-memory only, so a tap on yesterday's picker has nothing to add."""
    query = FakeQuery("sub:gym-uuid:42")
    context = FakeContext(_config(tmp_path))

    asyncio.run(handle_subscribe_selection(FakeUpdate(query=query), context))  # type: ignore[arg-type]

    assert "Session expired" in query.edits[-1]
    assert not (tmp_path / "channels.json").exists()


def test_more_reveals_every_project_without_writing_anything(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        auto_transcript_project="inv-uuid",
        whitelist=frozenset({"Investments"}),
    )
    query = FakeQuery("sub:more:42")
    context = FakeContext(config)
    context.user_data["subscribe_42"] = ResolvedChannel("@Rosa", "Dr. La Rosa", "UC2")

    asyncio.run(handle_subscribe_selection(FakeUpdate(query=query), context))  # type: ignore[arg-type]

    assert _callbacks(query.markups[-1]) == ["sub:default:42", "sub:inv-uuid:42", "sub:gym-uuid:42"]
    assert not config.poller.channels_path.exists()
    assert "subscribe_42" in context.user_data  # still waiting for a real choice


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
