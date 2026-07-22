import asyncio
import json
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from curl_cffi.requests.exceptions import RequestException
from telegram.ext import Application

from knowledger.claude_client import AuthError, ClaudeClient
from knowledger.config import ClaudeSettings, Config, LoggerConfig, TelegramSettings
from knowledger.pending_transcripts import (
    PendingTranscript,
    PendingTranscriptStore,
    drain_pending_transcripts,
)
from knowledger.persistence import CorruptDataError
from knowledger.queue import Queue
from knowledger.transcript import TranscriptTransportError, TranscriptUnavailable


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, parse_mode=None) -> None:
        self.sent.append((chat_id, text))


class FakeTelegramApp:
    def __init__(self) -> None:
        self.bot = FakeBot()


class FakeClaudeClient:
    def __init__(self) -> None:
        self.docs: dict[str, list[dict]] = {}
        self.upload_calls = 0
        self.fail_auth_times = 0

    def list_docs(self, project_id):
        return self.docs.get(project_id, [])

    def upload_content(self, project_id, content, file_name):
        self.upload_calls += 1
        if self.fail_auth_times > 0:
            self.fail_auth_times -= 1
            raise AuthError("expired")
        self.docs.setdefault(project_id, []).append({"uuid": "u1", "file_name": file_name})

    def delete_doc(self, project_id, uuid):
        self.docs[project_id] = [d for d in self.docs.get(project_id, []) if d["uuid"] != uuid]


def _config() -> Config:
    return Config(
        logger=LoggerConfig(),
        telegram=TelegramSettings(bot_token="x", allowed_user_ids=frozenset({1})),
        claude=ClaudeSettings(session_token="x"),
    )


def _entry(
    video_id: str, file_name: str = "f", project_id: str = "p", **overrides,
) -> PendingTranscript:
    fields = {
        "chat_id": 1,
        "project_id": project_id,
        "video_id": video_id,
        "file_name": file_name,
        "video_title": "Title",
        "queued_at": "now",
        **overrides,
    }
    return PendingTranscript(**fields)


async def _drain(app, config, client, queue, store) -> None:
    await drain_pending_transcripts(
        cast(Application, app), config, cast(ClaudeClient, client), queue, store,
    )


# --- PendingTranscriptStore persistence -------------------------------------------


def test_missing_file_is_empty(tmp_path: Path) -> None:
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    assert store.load() == []


def test_add_persists_and_dedups_on_project_and_video(tmp_path: Path) -> None:
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    entry = _entry("v1")
    assert store.add(entry) is True
    assert store.add(entry) is False
    assert store.load() == [entry]


def test_remove_drops_matching_entry_only(tmp_path: Path) -> None:
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    store.add(_entry("v1", file_name="f1"))
    store.add(_entry("v2", file_name="f2"))
    store.remove("p", "v1")
    remaining = store.load()
    assert len(remaining) == 1
    assert remaining[0].video_id == "v2"


def test_corrupt_file_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text("not json")
    store = PendingTranscriptStore(path=path)
    with pytest.raises(CorruptDataError):
        store.load()


def test_malformed_schema_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"not": "a list"}))
    store = PendingTranscriptStore(path=path)
    with pytest.raises(CorruptDataError):
        store.load()


def test_round_trip_preserves_overwrite_doc_uuid(tmp_path: Path) -> None:
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    entry = _entry("v1", overwrite_doc_uuid="old-uuid")
    store.add(entry)
    assert store.load() == [entry]


# --- drain_pending_transcripts -----------------------------------------------------


def test_still_blocked_stays_pending(tmp_path: Path) -> None:
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    store.add(_entry("v1"))
    app = FakeTelegramApp()
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()

    with patch(
        "knowledger.pending_transcripts.fetch_transcript",
        side_effect=TranscriptTransportError("v1"),
    ):
        asyncio.run(_drain(app, _config(), client, queue, store))

    assert len(store.load()) == 1
    assert app.bot.sent == []


def test_still_unavailable_gives_up_and_notifies(tmp_path: Path) -> None:
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    store.add(_entry("v1"))
    app = FakeTelegramApp()
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()

    with patch(
        "knowledger.pending_transcripts.fetch_transcript",
        side_effect=TranscriptUnavailable("v1"),
    ):
        asyncio.run(_drain(app, _config(), client, queue, store))

    assert store.load() == []
    assert len(app.bot.sent) == 1
    assert "No captions" in app.bot.sent[0][1]


def test_successful_retry_uploads_and_notifies_and_drops_entry(tmp_path: Path) -> None:
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    store.add(_entry("v1", file_name="f1"))
    app = FakeTelegramApp()
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()

    with patch("knowledger.pending_transcripts.fetch_transcript", return_value="transcript text"):
        asyncio.run(_drain(app, _config(), client, queue, store))

    assert store.load() == []
    assert client.docs["p"] == [{"uuid": "u1", "file_name": "f1"}]
    assert len(app.bot.sent) == 1
    assert "f1" in app.bot.sent[0][1]


def test_already_exists_drops_entry_silently(tmp_path: Path) -> None:
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    store.add(_entry("v1", file_name="f1"))
    app = FakeTelegramApp()
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()
    client.docs["p"] = [{"uuid": "existing", "file_name": "f1"}]

    with patch("knowledger.pending_transcripts.fetch_transcript", return_value="transcript text"):
        asyncio.run(_drain(app, _config(), client, queue, store))

    assert store.load() == []
    assert app.bot.sent == []
    assert client.upload_calls == 0


def test_deferred_for_auth_moves_entry_into_petition_queue(tmp_path: Path) -> None:
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    store.add(_entry("v1", file_name="f1", overwrite_doc_uuid="old-uuid"))
    app = FakeTelegramApp()
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()
    client.fail_auth_times = 1
    client.docs["p"] = [{"uuid": "old-uuid", "file_name": "f1"}]

    with patch("knowledger.pending_transcripts.fetch_transcript", return_value="transcript text"):
        asyncio.run(_drain(app, _config(), client, queue, store))

    assert store.load() == []
    queued = queue.peek()
    assert len(queued) == 1
    assert queued[0].video_id == "v1"
    assert queued[0].transcript == "transcript text"
    assert queued[0].overwrite_doc_uuid == "old-uuid"
    assert len(app.bot.sent) == 1
    assert "Token expired" in app.bot.sent[0][1]


def test_transient_upload_failure_stays_pending(tmp_path: Path) -> None:
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    store.add(_entry("v1", file_name="f1"))
    app = FakeTelegramApp()
    queue = Queue(path=tmp_path / "q.json")

    class FlakyClient(FakeClaudeClient):
        def list_docs(self, project_id):
            raise RequestException("boom")

    client = FlakyClient()

    with patch("knowledger.pending_transcripts.fetch_transcript", return_value="transcript text"):
        asyncio.run(_drain(app, _config(), client, queue, store))

    assert len(store.load()) == 1
    assert queue.peek() == []
    assert app.bot.sent == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
