import asyncio
import json
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from curl_cffi.requests.exceptions import RequestException
from telegram.ext import Application

from knowledger.claude_client import AuthError, ClaudeClient
from knowledger.config import (
    ClaudeSettings,
    Config,
    LoggerConfig,
    StorageSettings,
    TelegramSettings,
)
from knowledger.history import UploadRecord, load_history, record_upload
from knowledger.pending_transcripts import (
    PendingTranscript,
    PendingTranscriptStore,
    drain_pending_transcripts,
)
from knowledger.persistence import CorruptDataError
from knowledger.queue import Queue, QueueEntry
from knowledger.telegram_format import NO_PREVIEW
from knowledger.transcript import TranscriptTransportError, TranscriptUnavailable
from knowledger.youtube import VideoMetadata, build_doc_name


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.options: list[dict] = []

    async def send_message(self, chat_id, text, parse_mode=None, **kwargs) -> None:
        self.sent.append((chat_id, text))
        self.options.append(kwargs)


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


def _config(data_dir: Path) -> Config:
    # data_dir must point at tmp_path, not the default "." — record_upload() now
    # writes upload_history.json as a side effect of a successful retry, and the
    # default would otherwise write into the real repo working directory.
    return Config(
        logger=LoggerConfig(),
        telegram=TelegramSettings(bot_token="x", allowed_user_ids=frozenset({1})),
        claude=ClaudeSettings(session_token="x"),
        storage=StorageSettings(data_dir=data_dir),
    )


def _entry(
    video_id: str,
    file_name: str = "f",
    project_id: str = "p",
    **overrides,
) -> PendingTranscript:
    fields = {
        "chat_id": 1,
        "project_id": project_id,
        "video_id": video_id,
        "file_name": file_name,
        "video_title": "Title",
        "queued_at": "now",
        "channel_name": "Ch",
        **overrides,
    }
    return PendingTranscript(**fields)


@pytest.fixture(autouse=True)
def _no_metadata_refetch():
    """A drained entry carrying the dateless form of its doc name re-resolves the
    video's metadata before uploading (see `_resolve_doc_name`). Tests must not reach
    YouTube for that, so the default here is the "still can't reach it" branch — which
    keeps the queued name, exactly what the tests below assert. Tests that care about
    the re-resolution patch this with their own return value."""
    with patch(
        "knowledger.pending_transcripts.fetch_video_metadata",
        side_effect=RequestException("no network in tests"),
    ) as mock:
        yield mock


async def _drain(app, config, client, queue, store) -> None:
    await drain_pending_transcripts(
        cast(Application, app),
        config,
        cast(ClaudeClient, client),
        queue,
        store,
    )


# --- PendingTranscriptStore persistence -------------------------------------------


def test_missing_file_is_empty(tmp_path: Path) -> None:
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    assert store.load() == []


def test_entry_missing_channel_name_fails_closed(tmp_path: Path) -> None:
    # channel_name is a required field (no default) so that no upload ever silently
    # goes unrecorded in the weekly recap — a pending_transcripts.json array written
    # before the field existed must be fixed up (see scripts/backfill_channel_name.py)
    # rather than loading with a silently blank value.
    path = tmp_path / "p.json"
    path.write_text(
        json.dumps(
            [
                {
                    "chat_id": 1,
                    "project_id": "p",
                    "video_id": "v1",
                    "file_name": "f1",
                    "video_title": "T1",
                    "queued_at": "now",
                },
            ],
        ),
    )
    with pytest.raises(CorruptDataError):
        PendingTranscriptStore(path=path).load()


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


def test_add_supersedes_when_overwrite_intent_changes(tmp_path: Path) -> None:
    """A plain-upload entry blocked, then a later overwrite attempt for the same
    video also blocked, must not silently keep the stale plain-upload intent — the
    newer overwrite intent has to win, or the drain would later resolve the wrong
    outcome (AlreadyExists instead of actually overwriting)."""
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    plain = _entry("v1", file_name="f1")
    overwrite = _entry("v1", file_name="f1", overwrite_doc_uuid="old-uuid")

    assert store.add(plain) is True
    assert store.add(overwrite) is True  # intent changed -> superseded, not dropped
    assert store.load() == [overwrite]

    # Re-adding the exact same (now current) intent is a true no-op duplicate.
    assert store.add(overwrite) is False
    assert store.load() == [overwrite]


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
        asyncio.run(_drain(app, _config(tmp_path), client, queue, store))

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
        asyncio.run(_drain(app, _config(tmp_path), client, queue, store))

    assert store.load() == []
    assert len(app.bot.sent) == 1
    assert "no captions" in app.bot.sent[0][1]


def test_successful_retry_uploads_and_notifies_and_drops_entry(tmp_path: Path) -> None:
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    store.add(_entry("v1", file_name="f1", channel_name="Ch1"))
    app = FakeTelegramApp()
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()

    with patch("knowledger.pending_transcripts.fetch_transcript", return_value="transcript text"):
        asyncio.run(_drain(app, _config(tmp_path), client, queue, store))

    assert store.load() == []
    assert client.docs["p"] == [{"uuid": "u1", "file_name": "f1"}]
    assert len(app.bot.sent) == 1
    assert "Title" in app.bot.sent[0][1]
    assert app.bot.options[0]["link_preview_options"] == NO_PREVIEW

    # A transcript that was transiently blocked and later retried successfully must
    # still show up in the weekly recap history — this is a real upload, just a
    # delayed one.
    history = load_history(tmp_path)
    assert len(history) == 1
    assert history[0].file_name == "f1"
    assert history[0].channel_name == "Ch1"


def test_already_exists_drops_entry_silently(tmp_path: Path) -> None:
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    store.add(_entry("v1", file_name="f1"))
    app = FakeTelegramApp()
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()
    client.docs["p"] = [{"uuid": "existing", "file_name": "f1"}]

    with patch("knowledger.pending_transcripts.fetch_transcript", return_value="transcript text"):
        asyncio.run(_drain(app, _config(tmp_path), client, queue, store))

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
        asyncio.run(_drain(app, _config(tmp_path), client, queue, store))

    assert store.load() == []
    queued = queue.peek()
    assert len(queued) == 1
    assert queued[0].video_id == "v1"
    assert queued[0].transcript == "transcript text"
    assert queued[0].overwrite_doc_uuid == "old-uuid"
    assert len(app.bot.sent) == 1
    assert "Waiting on a valid token" in app.bot.sent[0][1]


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
        asyncio.run(_drain(app, _config(tmp_path), client, queue, store))

    assert len(store.load()) == 1
    assert queue.peek() == []
    assert app.bot.sent == []


def test_auth_error_listing_docs_moves_entry_into_petition_queue(tmp_path: Path) -> None:
    """AuthError can surface while listing docs (before upload() is even reached),
    not just from within upload() — that path must also fall back into the durable
    queue instead of dropping the freshly-fetched transcript."""
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    store.add(_entry("v1", file_name="f1"))
    app = FakeTelegramApp()
    queue = Queue(path=tmp_path / "q.json")

    class AuthFailingClient(FakeClaudeClient):
        def list_docs(self, project_id):
            raise AuthError("expired")

    client = AuthFailingClient()

    with patch("knowledger.pending_transcripts.fetch_transcript", return_value="transcript text"):
        asyncio.run(_drain(app, _config(tmp_path), client, queue, store))

    assert store.load() == []
    queued = queue.peek()
    assert len(queued) == 1
    assert queued[0].transcript == "transcript text"
    assert "Waiting on a valid token" in app.bot.sent[0][1]


def test_second_auth_failure_for_same_video_reports_already_queued(tmp_path: Path) -> None:
    """If the video is already sitting in petition_queue.json (e.g. from an earlier
    auth failure), a second auth-blocked retry for the same project+video must not
    silently discard the newly re-fetched transcript nor claim it was freshly
    queued — Queue.enqueue() no-ops on the duplicate, and the message must reflect
    that instead of the generic 'queued' text."""
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    store.add(_entry("v1", file_name="f1"))
    app = FakeTelegramApp()
    queue = Queue(path=tmp_path / "q.json")
    queue.enqueue(
        QueueEntry(
            project_id="p",
            video_id="v1",
            file_name="f1",
            transcript="already queued transcript",
            chat_id=1,
            video_title="Title",
            queued_at="earlier",
            channel_name="Ch",
        ),
    )

    class AuthFailingClient(FakeClaudeClient):
        def list_docs(self, project_id):
            raise AuthError("expired")

    client = AuthFailingClient()

    with patch("knowledger.pending_transcripts.fetch_transcript", return_value="transcript text"):
        asyncio.run(_drain(app, _config(tmp_path), client, queue, store))

    assert store.load() == []
    queued = queue.peek()
    assert len(queued) == 1
    assert queued[0].transcript == "already queued transcript"  # untouched, not overwritten
    assert "already queued" in app.bot.sent[0][1]


def test_docs_are_listed_once_per_project_across_a_batch(tmp_path: Path) -> None:
    """Several pending entries sharing a project_id must share one list_docs() call
    for the whole drain batch, not one call each."""
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    store.add(_entry("v1", file_name="f1", project_id="shared"))
    store.add(_entry("v2", file_name="f2", project_id="shared"))
    app = FakeTelegramApp()
    queue = Queue(path=tmp_path / "q.json")

    class CountingClient(FakeClaudeClient):
        def __init__(self) -> None:
            super().__init__()
            self.list_docs_calls = 0

        def list_docs(self, project_id):
            self.list_docs_calls += 1
            return super().list_docs(project_id)

    client = CountingClient()

    with patch("knowledger.pending_transcripts.fetch_transcript", return_value="transcript text"):
        asyncio.run(_drain(app, _config(tmp_path), client, queue, store))

    assert client.list_docs_calls == 1
    assert store.load() == []
    assert client.docs["shared"] == [
        {"uuid": "u1", "file_name": "f1"},
        {"uuid": "u1", "file_name": "f2"},
    ]


def test_notify_failure_does_not_abort_remaining_entries_in_batch(tmp_path: Path) -> None:
    """A best-effort notification failure (e.g. the Telegram Application racing its
    own startup init) for one entry must not stop the rest of the batch from being
    processed — the transcript is already safely handled by that point."""
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    store.add(_entry("v1", file_name="f1", project_id="p1", chat_id=1))
    store.add(_entry("v2", file_name="f2", project_id="p2", chat_id=2))
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()

    class FlakyBot(FakeBot):
        async def send_message(self, chat_id, text, parse_mode=None, **kwargs) -> None:
            if chat_id == 1:
                raise RuntimeError("bot not initialized yet")
            await super().send_message(chat_id, text, parse_mode=parse_mode)

    app = FakeTelegramApp()
    app.bot = FlakyBot()

    with patch("knowledger.pending_transcripts.fetch_transcript", return_value="transcript text"):
        asyncio.run(_drain(app, _config(tmp_path), client, queue, store))

    assert store.load() == []  # both entries resolved despite the first notify failing
    chat_id, text = app.bot.sent[0]
    assert chat_id == 2
    assert "Saved" in text
    assert "Title" in text


# --- doc-name re-resolution --------------------------------------------------------


def _metadata(upload_date: str | None) -> VideoMetadata:
    return VideoMetadata(video_id="v1", title=TITLE, channel_name=CHANNEL, upload_date=upload_date)


CHANNEL = "On-Chain Mind"
TITLE = "Bitcoin's Top Buyers Are Finally Capitulating"
DATED_NAME = build_doc_name(CHANNEL, TITLE, "2026-08-04")
DATELESS_NAME = build_doc_name(CHANNEL, TITLE, None)


def _named_entry(file_name: str) -> PendingTranscript:
    """An entry whose channel/title actually match its doc name — what
    `is_undated_doc_name` compares against to decide whether the name is degraded."""
    return _entry("v1", file_name=file_name, channel_name=CHANNEL, video_title=TITLE)


def test_dateless_name_is_re_resolved_before_upload(tmp_path: Path, _no_metadata_refetch) -> None:
    """The entry was named while YouTube was blocking this bot, so the watch page — the
    only source of the upload date — was unreachable and the name came out dateless. The
    poller names the same video from the channel feed and always has a date, so the two
    would store the same transcript twice. By retry time the block has lifted: re-resolve
    and upload under the canonical name both paths agree on."""
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    store.add(_named_entry(DATELESS_NAME))
    app = FakeTelegramApp()
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()
    _no_metadata_refetch.side_effect = None
    _no_metadata_refetch.return_value = _metadata("2026-08-04")

    with patch("knowledger.pending_transcripts.fetch_transcript", return_value="transcript text"):
        asyncio.run(_drain(app, _config(tmp_path), client, queue, store))

    assert client.docs["p"] == [{"uuid": "u1", "file_name": DATED_NAME}]
    assert load_history(tmp_path)[0].file_name == DATED_NAME


def test_already_dated_name_is_not_re_resolved(tmp_path: Path, _no_metadata_refetch) -> None:
    """Nothing to recover, so don't spend a request on it."""
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    store.add(_named_entry(DATED_NAME))
    app = FakeTelegramApp()
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()

    with patch("knowledger.pending_transcripts.fetch_transcript", return_value="transcript text"):
        asyncio.run(_drain(app, _config(tmp_path), client, queue, store))

    assert _no_metadata_refetch.call_count == 0
    assert client.docs["p"] == [{"uuid": "u1", "file_name": DATED_NAME}]


def test_re_resolution_failure_keeps_the_queued_name(tmp_path: Path) -> None:
    """Still blocked at retry time: upload under the name we already have rather than
    losing the transcript over a cosmetic detail. The video-id duplicate check still
    protects against a second copy."""
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    store.add(_named_entry(DATELESS_NAME))
    app = FakeTelegramApp()
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()

    with patch("knowledger.pending_transcripts.fetch_transcript", return_value="transcript text"):
        asyncio.run(_drain(app, _config(tmp_path), client, queue, store))

    assert client.docs["p"] == [{"uuid": "u1", "file_name": DATELESS_NAME}]


def test_poller_upload_of_the_same_video_is_recognised(tmp_path: Path) -> None:
    """The poller got there first and stored the video under its own (dated) name. The
    retry must recognise that as this video and drop the entry, not add a second copy
    under the dateless name it is still carrying."""
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    store.add(_named_entry(DATELESS_NAME))
    app = FakeTelegramApp()
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()
    client.docs["p"] = [{"uuid": "from-poller", "file_name": DATED_NAME}]
    record_upload(
        tmp_path,
        UploadRecord(
            project_id="p",
            file_name=DATED_NAME,
            video_title="Title",
            channel_name="Ch",
            uploaded_at="2026-08-05T08:32:00+00:00",
            video_id="v1",
        ),
    )

    with patch("knowledger.pending_transcripts.fetch_transcript", return_value="transcript text"):
        asyncio.run(_drain(app, _config(tmp_path), client, queue, store))

    assert store.load() == []
    assert client.upload_calls == 0


def test_re_resolved_name_is_used_for_the_auth_fallback_entry(tmp_path: Path) -> None:
    """A retry that succeeds on the transcript but hits an expired token hands off to
    petition_queue.json — with the corrected name, so /refresh doesn't reintroduce the
    duplicate the re-resolution just avoided."""
    store = PendingTranscriptStore(path=tmp_path / "p.json")
    store.add(_named_entry(DATELESS_NAME))
    app = FakeTelegramApp()
    queue = Queue(path=tmp_path / "q.json")
    client = FakeClaudeClient()
    client.fail_auth_times = 1

    with (
        patch("knowledger.pending_transcripts.fetch_transcript", return_value="transcript text"),
        patch(
            "knowledger.pending_transcripts.fetch_video_metadata",
            return_value=_metadata("2026-08-04"),
        ),
    ):
        asyncio.run(_drain(app, _config(tmp_path), client, queue, store))

    assert [e.file_name for e in queue.peek()] == [DATED_NAME]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
