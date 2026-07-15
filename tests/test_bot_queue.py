import asyncio
from pathlib import Path

import pytest
from curl_cffi.requests.exceptions import RequestException

from knowledger.bot import MAX_UPLOAD_ATTEMPTS, drain_queue
from knowledger.claude_client import AuthError
from knowledger.config import Config, LoggerConfig
from knowledger.queue import Queue, QueueEntry


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, parse_mode=None) -> None:
        self.sent.append((chat_id, text))


class FakeTelegramApp:
    def __init__(self) -> None:
        self.bot = FakeBot()


class FakeClaudeClient:
    """Minimal stand-in for ClaudeClient: an in-memory doc store per project_id, with
    knobs to fail the next N calls of a given kind."""

    def __init__(self) -> None:
        self.docs: dict[str, list[dict]] = {}
        self.upload_calls = 0
        self.fail_upload_times = 0
        self.fail_auth_times = 0
        self.fail_list_times = 0

    def list_docs(self, project_id):
        if self.fail_list_times > 0:
            self.fail_list_times -= 1
            raise RequestException("list boom")
        return self.docs.get(project_id, [])

    def upload_content(self, project_id, content, file_name):
        self.upload_calls += 1
        if self.fail_auth_times > 0:
            self.fail_auth_times -= 1
            raise AuthError("expired")
        if self.fail_upload_times > 0:
            self.fail_upload_times -= 1
            raise RequestException("boom")
        self.docs.setdefault(project_id, []).append({"uuid": "u1", "file_name": file_name})
        return {}

    def delete_doc(self, project_id, uuid):
        self.docs[project_id] = [d for d in self.docs.get(project_id, []) if d["uuid"] != uuid]


def _config() -> Config:
    return Config(
        logger=LoggerConfig(),
        telegram_bot_token="x",
        claude_session_token="x",
        allowed_user_ids=frozenset({1}),
    )


def _entry(video_id: str, file_name: str, project_id: str = "p", **overrides) -> QueueEntry:
    fields = {
        "project_id": project_id,
        "video_id": video_id,
        "file_name": file_name,
        "transcript": "transcript",
        "chat_id": 1,
        "video_title": "Title",
        "queued_at": "now",
        **overrides,
    }
    return QueueEntry(**fields)


def test_success_notifies_and_drops_entry(tmp_path: Path) -> None:
    queue = Queue(path=tmp_path / "q.json")
    queue.enqueue(_entry("v1", "f1"))
    client = FakeClaudeClient()
    app = FakeTelegramApp()

    result = asyncio.run(drain_queue(app, _config(), client, queue))

    assert result.uploaded == 1
    assert queue.peek() == []
    assert app.bot.sent == [(1, "Queued upload saved: *f1*")]


def test_auth_error_requeues_silently_then_succeeds(tmp_path: Path) -> None:
    queue = Queue(path=tmp_path / "q.json")
    queue.enqueue(_entry("v2", "f2"))
    client = FakeClaudeClient()
    client.fail_auth_times = 1
    app = FakeTelegramApp()

    result = asyncio.run(drain_queue(app, _config(), client, queue))
    assert result.failed_auth == 1
    assert len(queue.peek()) == 1
    assert app.bot.sent == []  # no message on auth failure

    result = asyncio.run(drain_queue(app, _config(), client, queue))
    assert result.uploaded == 1


def test_alert_fires_once_at_threshold_then_recovers(tmp_path: Path) -> None:
    queue = Queue(path=tmp_path / "q.json")
    queue.enqueue(_entry("v3", "f3"))
    client = FakeClaudeClient()
    client.fail_upload_times = MAX_UPLOAD_ATTEMPTS
    app = FakeTelegramApp()
    config = _config()

    for _ in range(MAX_UPLOAD_ATTEMPTS):
        asyncio.run(drain_queue(app, config, client, queue))
    assert len(app.bot.sent) == 1
    assert "stuck" in app.bot.sent[0][1]

    result = asyncio.run(drain_queue(app, config, client, queue))
    assert result.uploaded == 1


def test_overwrite_deletes_old_doc_then_uploads(tmp_path: Path) -> None:
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()
    client.docs["p"] = [{"uuid": "old-uuid", "file_name": "f4"}]
    queue.enqueue(_entry("v4", "f4", transcript="new", overwrite_doc_uuid="old-uuid"))
    app = FakeTelegramApp()

    result = asyncio.run(drain_queue(app, _config(), client, queue))

    assert result.uploaded == 1
    assert client.docs["p"] == [{"uuid": "u1", "file_name": "f4"}]


def test_overwrite_retry_after_delete_already_landed_is_idempotent(tmp_path: Path) -> None:
    """The old doc is already gone (deleted in a prior attempt) and no replacement exists
    yet — retry should just upload, not try to delete an already-gone doc."""
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()
    queue.enqueue(_entry("v5", "f5", overwrite_doc_uuid="already-gone-uuid"))
    app = FakeTelegramApp()

    result = asyncio.run(drain_queue(app, _config(), client, queue))

    assert result.uploaded == 1


def test_overwrite_retry_after_lost_confirmation_is_not_duplicated(tmp_path: Path) -> None:
    """The old doc is gone AND a doc with the target name already exists — the previous
    attempt's delete+upload actually landed, we just never saw the confirmation. Retrying
    must not upload a second copy."""
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()
    client.docs["p"] = [{"uuid": "landed-uuid", "file_name": "f6"}]
    queue.enqueue(_entry("v6", "f6", overwrite_doc_uuid="gone-already"))
    app = FakeTelegramApp()

    calls_before = client.upload_calls
    result = asyncio.run(drain_queue(app, _config(), client, queue))

    assert result.already_existed == 1
    assert client.upload_calls == calls_before
    assert len(client.docs["p"]) == 1


def test_transient_list_docs_failure_requeues_overwrite_without_skipping_delete(
    tmp_path: Path,
) -> None:
    """A transient list_docs failure must not be mistaken for 'the old doc is gone' — that
    would skip the delete and leave the stale doc in place forever."""
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()
    client.docs["p"] = [{"uuid": "still-there", "file_name": "f7"}]
    client.fail_list_times = 1
    queue.enqueue(_entry("v7", "f7", overwrite_doc_uuid="still-there"))
    app = FakeTelegramApp()
    config = _config()

    result = asyncio.run(drain_queue(app, config, client, queue))
    assert result.failed_other == 1
    assert len(queue.peek()) == 1
    assert any(d["uuid"] == "still-there" for d in client.docs["p"])  # delete NOT skipped

    result = asyncio.run(drain_queue(app, config, client, queue))
    assert result.uploaded == 1
    assert not any(d["uuid"] == "still-there" for d in client.docs["p"])


def test_one_entrys_storage_failure_does_not_lose_the_rest_of_the_batch(tmp_path: Path) -> None:
    """A per-entry requeue failure (e.g. disk full) must not abort processing of the other
    entries already read from the queue this round."""

    class FlakyQueue(Queue):
        def __init__(self, path: Path) -> None:
            super().__init__(path=path)
            self.fail_next_enqueue = False

        def enqueue(self, entry: QueueEntry) -> bool:
            if self.fail_next_enqueue:
                self.fail_next_enqueue = False
                raise OSError("disk full")
            return super().enqueue(entry)

    queue = FlakyQueue(path=tmp_path / "q.json")
    client = FakeClaudeClient()
    client.fail_auth_times = 2  # both entries fail with auth error -> both attempt requeue
    queue.enqueue(_entry("vA", "fA"))
    queue.enqueue(_entry("vB", "fB"))
    queue.fail_next_enqueue = True  # first requeue attempt (entry A) fails
    app = FakeTelegramApp()

    result = asyncio.run(drain_queue(app, _config(), client, queue))

    assert result.failed_auth == 2
    remaining = queue.peek()
    assert len(remaining) == 1
    assert remaining[0].video_id == "vB"  # A was lost, B survived


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
