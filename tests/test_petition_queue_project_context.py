import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telegram import Update

from knowledger.bot import CustomContext, handle_project_selection, handle_youtube_url
from knowledger.claude_client import AuthError, ClaudeClient
from knowledger.config import (
    ClaudeSettings,
    Config,
    LoggerConfig,
    StorageSettings,
    TelegramSettings,
    load_persisted_projects,
)
from knowledger.persistence import CorruptDataError
from knowledger.queue import Queue
from knowledger.youtube import VideoMetadata


class FakeCookies:
    """ClaudeClient reads `sessionKey` off every response to pick up session renewals;
    these fixtures carry none, so the client keeps the token it was constructed with."""

    def get(self, _name: str) -> str | None:
        return None


class FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload
        self.cookies = FakeCookies()

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        pass


class FakeMessage:
    def __init__(self, text: str, message_id: int = 1) -> None:
        self.text = text
        self.message_id = message_id
        self.replies: list[str] = []

    async def reply_text(self, text: str, parse_mode: str | None = None, reply_markup=None) -> None:
        self.replies.append(text)


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.edits: list[str] = []

    async def answer(self, *args, **kwargs) -> None:
        pass

    async def edit_message_text(
        self,
        text: str,
        parse_mode: str | None = None,
        reply_markup=None,
    ) -> None:
        self.edits.append(text)

    async def edit_message_reply_markup(self, reply_markup=None) -> None:
        pass


class FakeUpdate:
    def __init__(
        self,
        message=None,
        callback_query=None,
        user_id: int = 1,
        chat_id: int = 1,
    ) -> None:
        self.message = message
        self.callback_query = callback_query
        self.effective_user = SimpleNamespace(id=user_id)
        self.effective_chat = SimpleNamespace(id=chat_id)


class FakeContext:
    def __init__(self, bot_data: dict, user_data: dict | None = None) -> None:
        self.bot_data = bot_data
        self.user_data = user_data if user_data is not None else {}


class FakeClaudeClient:
    """Stands in for ClaudeClient in handler-level tests: separate knobs to fail
    list_projects / list_docs with AuthError, mirroring the real client's behavior of
    raising immediately rather than returning an error value."""

    def __init__(self) -> None:
        self.projects: list[dict] = [{"uuid": "p1", "name": "Proj"}]
        self.fail_list_projects = False
        self.docs: dict[str, list[dict]] = {}
        self.fail_list_docs = False
        self.upload_calls = 0

    def list_projects(self):
        if self.fail_list_projects:
            raise AuthError("token expired")
        return self.projects

    def list_docs(self, project_id: str):
        if self.fail_list_docs:
            raise AuthError("token expired")
        return self.docs.get(project_id, [])

    def upload_content(self, project_id: str, content: str, file_name: str) -> None:
        self.upload_calls += 1


def _config(tmp_path: Path) -> Config:
    return Config(
        logger=LoggerConfig(),
        telegram=TelegramSettings(bot_token="x", allowed_user_ids=frozenset({1})),
        claude=ClaudeSettings(session_token="x"),
        storage=StorageSettings(data_dir=tmp_path),
    )


# --- load_persisted_projects (config.py) -----------------------------------------


def test_load_persisted_projects_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_persisted_projects(tmp_path) is None


def test_load_persisted_projects_corrupt_json_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "projects_cache.json").write_text("not json")
    with pytest.raises(CorruptDataError):
        load_persisted_projects(tmp_path)


def test_load_persisted_projects_wrong_schema_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "projects_cache.json").write_text(json.dumps([{"uuid": "p1"}]))
    with pytest.raises(CorruptDataError):
        load_persisted_projects(tmp_path)


def test_load_persisted_projects_reads_written_value(tmp_path: Path) -> None:
    (tmp_path / "projects_cache.json").write_text(
        json.dumps([{"uuid": "p1", "name": "Cached Project"}]),
    )
    assert load_persisted_projects(tmp_path) == [{"uuid": "p1", "name": "Cached Project"}]


# --- ClaudeClient.list_projects persistence ---------------------------------------


def _patch_projects_endpoint(monkeypatch: pytest.MonkeyPatch, projects: list[dict]) -> None:
    def fake_request(_method: str, url: str, headers=None, impersonate=None, **_kw) -> FakeResponse:
        if url.endswith("/organizations"):
            return FakeResponse(200, [{"uuid": "org1", "capabilities": ["chat"]}])
        return FakeResponse(200, projects)

    monkeypatch.setattr("knowledger.claude_client.requests.request", fake_request)


def test_list_projects_persists_cache_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_projects_endpoint(monkeypatch, [{"uuid": "p1", "name": "Proj"}])
    cache_path = tmp_path / "projects_cache.json"
    client = ClaudeClient("token", projects_persist_path=cache_path)

    projects = client.list_projects()

    assert projects == [{"uuid": "p1", "name": "Proj"}]
    assert json.loads(cache_path.read_text()) == [{"uuid": "p1", "name": "Proj"}]


def test_list_projects_persist_failure_does_not_break_the_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_projects_endpoint(monkeypatch, [{"uuid": "p1", "name": "Proj"}])
    unwritable = tmp_path / "missing-parent-dir" / "projects_cache.json"
    client = ClaudeClient("token", projects_persist_path=unwritable)

    projects = client.list_projects()

    assert projects == [{"uuid": "p1", "name": "Proj"}]
    assert not unwritable.exists()


# --- handle_youtube_url: AuthError-before-project-selection fallback --------------


def test_handle_youtube_url_falls_back_to_persisted_projects_on_auth_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "projects_cache.json").write_text(
        json.dumps([{"uuid": "p1", "name": "Cached Project"}]),
    )
    monkeypatch.setattr(
        "knowledger.bot.fetch_video_metadata",
        lambda url, proxy: VideoMetadata(
            video_id="v1",
            title="Title",
            channel_name="Channel",
            upload_date=None,
        ),
    )
    client = FakeClaudeClient()
    client.fail_list_projects = True
    message = FakeMessage("https://youtu.be/v1")
    update = FakeUpdate(message=message)
    context = FakeContext(bot_data={"config": _config(tmp_path), "claude_client": client})

    asyncio.run(handle_youtube_url(cast(Update, update), cast(CustomContext, context)))

    assert any("Using last known project list" in r for r in message.replies)
    assert any("Select a project" in r for r in message.replies)
    assert context.user_data["video_1"].video_id == "v1"


def test_handle_youtube_url_dead_ends_without_any_persisted_projects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "knowledger.bot.fetch_video_metadata",
        lambda url, proxy: VideoMetadata(
            video_id="v1",
            title="Title",
            channel_name="Channel",
            upload_date=None,
        ),
    )
    client = FakeClaudeClient()
    client.fail_list_projects = True
    message = FakeMessage("https://youtu.be/v1")
    update = FakeUpdate(message=message)
    context = FakeContext(bot_data={"config": _config(tmp_path), "claude_client": client})

    asyncio.run(handle_youtube_url(cast(Update, update), cast(CustomContext, context)))

    assert message.replies[-1].startswith("Auth error:")
    assert "video_1" not in context.user_data


def test_handle_youtube_url_fails_closed_on_corrupt_persisted_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "projects_cache.json").write_text("not json")
    monkeypatch.setattr(
        "knowledger.bot.fetch_video_metadata",
        lambda url, proxy: VideoMetadata(
            video_id="v1",
            title="Title",
            channel_name="Channel",
            upload_date=None,
        ),
    )
    client = FakeClaudeClient()
    client.fail_list_projects = True
    message = FakeMessage("https://youtu.be/v1")
    update = FakeUpdate(message=message)
    context = FakeContext(bot_data={"config": _config(tmp_path), "claude_client": client})

    asyncio.run(handle_youtube_url(cast(Update, update), cast(CustomContext, context)))

    assert message.replies[-1].startswith("Auth error:")
    assert "cached project list" in message.replies[-1]


# --- handle_project_selection: dedup-check AuthError falls through ----------------


def test_dedup_check_auth_error_falls_through_to_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "knowledger.bot.fetch_transcript",
        lambda video_id, proxy=None, cookies_path=None: "transcript text",
    )
    client = FakeClaudeClient()
    client.fail_list_docs = True
    queue = Queue(path=tmp_path / "petition_queue.json")
    query = FakeQuery(data="p1:1")
    update = FakeUpdate(callback_query=query)
    context = FakeContext(
        bot_data={
            "config": _config(tmp_path),
            "claude_client": client,
            "queue": queue,
        },
        user_data={
            "video_1": VideoMetadata(
                video_id="v1",
                title="Title",
                channel_name="Channel",
                upload_date="20260101",
            ),
        },
    )

    asyncio.run(handle_project_selection(cast(Update, update), cast(CustomContext, context)))

    assert "expired" in query.edits[-1].lower()
    entries = queue.peek()
    assert len(entries) == 1
    assert entries[0].project_id == "p1"
    assert entries[0].video_id == "v1"
    assert entries[0].transcript == "transcript text"


def test_dedup_check_still_finds_duplicates_when_token_is_healthy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unaffected control case: a healthy list_docs call still detects an existing doc
    and prompts Skip/Overwrite instead of always falling through to upload."""
    monkeypatch.setattr(
        "knowledger.bot.fetch_transcript",
        lambda video_id, proxy=None, cookies_path=None: "transcript text",
    )
    client = FakeClaudeClient()
    metadata = VideoMetadata(
        video_id="v1",
        title="Title",
        channel_name="Channel",
        upload_date="20260101",
    )
    from knowledger.youtube import build_doc_name

    file_name = build_doc_name(metadata.channel_name, metadata.title, metadata.upload_date)
    client.docs["p1"] = [{"uuid": "existing-uuid", "file_name": file_name}]
    queue = Queue(path=tmp_path / "petition_queue.json")
    query = FakeQuery(data="p1:1")
    update = FakeUpdate(callback_query=query)
    context = FakeContext(
        bot_data={
            "config": _config(tmp_path),
            "claude_client": client,
            "queue": queue,
        },
        user_data={"video_1": metadata},
    )

    asyncio.run(handle_project_selection(cast(Update, update), cast(CustomContext, context)))

    assert "skip or overwrite" in query.edits[-1].lower()
    assert queue.peek() == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
