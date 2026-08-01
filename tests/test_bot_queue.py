import asyncio
from pathlib import Path
from typing import cast

import pytest
from curl_cffi.requests.exceptions import RequestException
from telegram.ext import Application

from knowledger.bot import drain_queue
from knowledger.claude_client import AuthError, ClaudeClient
from knowledger.config import (
    ClaudeSettings,
    Config,
    LoggerConfig,
    StorageSettings,
    TelegramSettings,
)
from knowledger.queue import Queue, QueueEntry
from knowledger.queue_processor import MAX_UPLOAD_ATTEMPTS, DrainResult, QueueProcessor


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, parse_mode=None, **kwargs) -> None:
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

    def delete_doc(self, project_id, uuid):
        self.docs[project_id] = [d for d in self.docs.get(project_id, []) if d["uuid"] != uuid]


async def _drain(
    app: FakeTelegramApp,
    config: Config,
    client: FakeClaudeClient,
    queue: Queue,
    processor: QueueProcessor | None = None,
) -> DrainResult:
    """Cast the fakes to the real protocol types drain_queue expects — they satisfy the
    subset of the interface it actually uses (bot.send_message / list_docs / upload_content /
    delete_doc), but aren't nominal subclasses of Application / ClaudeClient."""
    return await drain_queue(
        cast(Application, app), config, cast(ClaudeClient, client), queue, processor
    )


def _config(data_dir: Path) -> Config:
    # data_dir must point at tmp_path, not the default "." — record_upload() now
    # writes upload_history.json as a side effect of a successful drain, and the
    # default would otherwise write into the real repo working directory.
    return Config(
        logger=LoggerConfig(),
        telegram=TelegramSettings(bot_token="x", allowed_user_ids=frozenset({1})),
        claude=ClaudeSettings(session_token="x"),
        storage=StorageSettings(data_dir=data_dir),
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
        "channel_name": "Ch",
        **overrides,
    }
    return QueueEntry(**fields)


def test_success_notifies_and_drops_entry(tmp_path: Path) -> None:
    queue = Queue(path=tmp_path / "q.json")
    queue.enqueue(_entry("v1", "f1"))
    client = FakeClaudeClient()
    app = FakeTelegramApp()

    result = asyncio.run(_drain(app, _config(tmp_path), client, queue))

    assert result.uploaded == 1
    assert queue.peek() == []
    chat_id, text = app.bot.sent[0]
    assert chat_id == 1
    assert "Saved" in text
    # The confirmation names the video, not the derived doc file name.
    assert "Title" in text
    assert "f1" not in text


def test_auth_error_releases_silently_then_succeeds(tmp_path: Path) -> None:
    queue = Queue(path=tmp_path / "q.json")
    queue.enqueue(_entry("v2", "f2"))
    client = FakeClaudeClient()
    client.fail_auth_times = 1
    app = FakeTelegramApp()

    result = asyncio.run(_drain(app, _config(tmp_path), client, queue))
    assert result.failed_auth == 1
    assert len(queue.peek()) == 1
    assert queue.peek()[0].state == "pending"  # released, not lost
    assert app.bot.sent == []  # no message on auth failure

    result = asyncio.run(_drain(app, _config(tmp_path), client, queue))
    assert result.uploaded == 1


def test_alert_fires_once_at_threshold_then_recovers(tmp_path: Path) -> None:
    queue = Queue(path=tmp_path / "q.json")
    queue.enqueue(_entry("v3", "f3"))
    client = FakeClaudeClient()
    client.fail_upload_times = MAX_UPLOAD_ATTEMPTS
    app = FakeTelegramApp()
    config = _config(tmp_path)

    for _ in range(MAX_UPLOAD_ATTEMPTS):
        asyncio.run(_drain(app, config, client, queue))
    assert len(app.bot.sent) == 1
    assert f"Stuck after {MAX_UPLOAD_ATTEMPTS} attempts" in app.bot.sent[0][1]

    result = asyncio.run(_drain(app, config, client, queue))
    assert result.uploaded == 1


def test_stuck_alert_survives_a_video_title_containing_braces(tmp_path: Path) -> None:
    """The alert used to be built as a template string with a literal `{attempts}`
    placeholder that `_release_with_alert` then `.format()`ed. Because the video title
    was interpolated into that template first, any title containing a brace raised
    KeyError/IndexError inside format() — losing the very alert meant to surface a
    permanently stuck entry."""
    queue = Queue(path=tmp_path / "q.json")
    queue.enqueue(_entry("v4", "f4", video_title="Ranking every {city} in 2026 {}"))
    client = FakeClaudeClient()
    client.fail_upload_times = MAX_UPLOAD_ATTEMPTS
    app = FakeTelegramApp()
    config = _config(tmp_path)

    for _ in range(MAX_UPLOAD_ATTEMPTS):
        asyncio.run(_drain(app, config, client, queue))

    assert len(app.bot.sent) == 1
    assert "Ranking every {city} in 2026 {}" in app.bot.sent[0][1]


def test_overwrite_deletes_old_doc_then_uploads(tmp_path: Path) -> None:
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()
    client.docs["p"] = [{"uuid": "old-uuid", "file_name": "f4"}]
    queue.enqueue(_entry("v4", "f4", transcript="new", overwrite_doc_uuid="old-uuid"))
    app = FakeTelegramApp()

    result = asyncio.run(_drain(app, _config(tmp_path), client, queue))

    assert result.uploaded == 1
    assert client.docs["p"] == [{"uuid": "u1", "file_name": "f4"}]


def test_overwrite_retry_after_delete_already_landed_is_idempotent(tmp_path: Path) -> None:
    """The old doc is already gone (deleted in a prior attempt) and no replacement exists
    yet — retry should just upload, not try to delete an already-gone doc."""
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()
    queue.enqueue(_entry("v5", "f5", overwrite_doc_uuid="already-gone-uuid"))
    app = FakeTelegramApp()

    result = asyncio.run(_drain(app, _config(tmp_path), client, queue))

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
    result = asyncio.run(_drain(app, _config(tmp_path), client, queue))

    assert result.already_existed == 1
    assert client.upload_calls == calls_before
    assert len(client.docs["p"]) == 1


def test_transient_list_docs_failure_releases_overwrite_without_skipping_delete(
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
    config = _config(tmp_path)

    result = asyncio.run(_drain(app, config, client, queue))
    assert result.failed_other == 1
    assert len(queue.peek()) == 1
    assert any(d["uuid"] == "still-there" for d in client.docs["p"])  # delete NOT skipped

    result = asyncio.run(_drain(app, config, client, queue))
    assert result.uploaded == 1
    assert not any(d["uuid"] == "still-there" for d in client.docs["p"])


def test_two_concurrent_drains_upload_an_entry_once(tmp_path: Path) -> None:
    """A durable queue processor shared between callers (e.g. /refresh and a
    token-update-triggered drain) must serialize whole-batch draining so overlapping
    triggers cannot both claim and upload the same entry."""
    queue = Queue(path=tmp_path / "q.json")
    queue.enqueue(_entry("v8", "f8"))
    client = FakeClaudeClient()
    app = FakeTelegramApp()
    processor = QueueProcessor(queue)

    async def _run_both() -> tuple:
        return await asyncio.gather(
            processor.drain(cast(Application, app), _config(tmp_path), cast(ClaudeClient, client)),
            processor.drain(cast(Application, app), _config(tmp_path), cast(ClaudeClient, client)),
        )

    results = asyncio.run(_run_both())

    assert sum(r.uploaded for r in results) == 1
    assert client.upload_calls == 1
    assert queue.peek() == []


def test_release_failure_propagates_and_does_not_lose_other_entries(tmp_path: Path) -> None:
    """A persistence failure while releasing a claimed entry must propagate (not be
    logged and swallowed) — but must not corrupt or drop entries that were never
    touched this round; they remain durably queued for the next drain."""

    class FlakyQueue(Queue):
        def __init__(self, path: Path) -> None:
            super().__init__(path=path)
            self.fail_next_release = False

        def release(self, entry_id: str, *, increment_attempts: bool = False) -> None:
            if self.fail_next_release:
                self.fail_next_release = False
                raise OSError("disk full")
            super().release(entry_id, increment_attempts=increment_attempts)

    queue = FlakyQueue(path=tmp_path / "q.json")
    client = FakeClaudeClient()
    client.fail_auth_times = 2  # both entries would fail with auth error -> release
    queue.enqueue(_entry("vA", "fA"))
    queue.enqueue(_entry("vB", "fB"))
    queue.fail_next_release = True  # entry A's release fails
    app = FakeTelegramApp()

    with pytest.raises(OSError):
        asyncio.run(_drain(app, _config(tmp_path), client, queue))

    # Nothing was lost: A is still claimed (in_flight) as of the crash, B untouched.
    remaining = {e.video_id: e for e in queue.peek()}
    assert set(remaining) == {"vA", "vB"}

    # A fresh drain recovers the abandoned claim and both entries eventually succeed.
    queue.recover_abandoned()
    client.fail_auth_times = 0
    result = asyncio.run(_drain(app, _config(tmp_path), client, queue))
    assert result.uploaded == 2


def test_claim_recovers_abandoned_in_flight_entry_after_simulated_crash(tmp_path: Path) -> None:
    """A process restart (a fresh Queue instance over the same file) must recover a
    claim left in_flight by a crash — not treat it as invisible/stuck forever."""
    path = tmp_path / "q.json"
    queue = Queue(path=path)
    queue.enqueue(_entry("v9", "f9"))
    claimed = queue.claim()
    assert claimed is not None
    assert claimed.state == "in_flight"

    # Simulate the process crashing here (never ack'd/released) and restarting.
    restarted = Queue(path=path)
    recovered_count = restarted.recover_abandoned()
    assert recovered_count == 1
    assert restarted.peek()[0].state == "pending"

    reclaimed = restarted.claim()
    assert reclaimed is not None
    assert reclaimed.video_id == "v9"


def test_claim_does_not_reset_an_entry_in_flight_via_claim_by_id(tmp_path: Path) -> None:
    """claim() must never touch an entry already legitimately in_flight elsewhere in
    the same process (e.g. bot.py's interactive-overwrite path calls claim_by_id()
    outside the QueueProcessor lock). Resetting it back to pending — as a prior,
    buggy version of claim() did on every call to self-heal crash leftovers — would
    let a concurrent drain re-claim and double-process the same entry."""
    path = tmp_path / "q.json"
    queue = Queue(path=path)
    persisted = queue.enqueue(_entry("v9", "f9"))
    assert persisted is not None

    held = queue.claim_by_id(persisted.id)
    assert held is not None
    assert held.state == "in_flight"

    # A concurrent drain's claim() call must find nothing pending — not reset and
    # re-claim the entry `held` is still working on.
    assert queue.claim() is None
    assert queue.peek()[0].state == "in_flight"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
