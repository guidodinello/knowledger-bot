from pathlib import Path
from typing import cast

import pytest
from curl_cffi.requests.exceptions import RequestException

from knowledger.claude_client import AuthError, ClaudeClient, Doc
from knowledger.history import UploadRecord, record_upload
from knowledger.upload_service import (
    AlreadyExists,
    DeferredForAuth,
    RetryPending,
    TranscriptUploadService,
    Uploaded,
)


class FakeClient:
    """Minimal stand-in for ClaudeClient, mirroring the style of FakeClaudeClient in
    tests/test_bot_queue.py but scoped to just what TranscriptUploadService calls."""

    def __init__(self) -> None:
        self.docs: list[Doc] = []
        self.deleted: list[str] = []
        self.uploaded: list[tuple[str, str, str]] = []
        self.list_docs_error: Exception | None = None
        self.delete_doc_error: Exception | None = None
        self.upload_content_error: Exception | None = None

    def list_docs(self, project_id: str) -> list[Doc]:
        if self.list_docs_error is not None:
            raise self.list_docs_error
        return self.docs

    def delete_doc(self, project_id: str, doc_uuid: str) -> None:
        if self.delete_doc_error is not None:
            raise self.delete_doc_error
        self.deleted.append(doc_uuid)
        self.docs = [d for d in self.docs if d["uuid"] != doc_uuid]

    def upload_content(self, project_id: str, content: str, file_name: str) -> None:
        if self.upload_content_error is not None:
            raise self.upload_content_error
        self.uploaded.append((project_id, content, file_name))


def test_uploads_when_no_duplicate() -> None:
    client = FakeClient()
    service = TranscriptUploadService(cast(ClaudeClient, client))

    outcome = service.upload("proj", "transcript", "f.md")

    assert outcome == Uploaded()
    assert client.uploaded == [("proj", "transcript", "f.md")]


def test_already_exists_when_file_name_present() -> None:
    client = FakeClient()
    client.docs = [{"uuid": "u1", "file_name": "f.md"}]
    service = TranscriptUploadService(cast(ClaudeClient, client))

    outcome = service.upload("proj", "transcript", "f.md")

    assert outcome == AlreadyExists()
    assert client.uploaded == []


def test_deferred_for_auth_on_list_docs_auth_error() -> None:
    client = FakeClient()
    client.list_docs_error = AuthError("expired")
    service = TranscriptUploadService(cast(ClaudeClient, client))

    outcome = service.upload("proj", "transcript", "f.md")

    assert isinstance(outcome, DeferredForAuth)
    assert "expired" in outcome.error


def test_retry_pending_on_list_docs_transient_error() -> None:
    client = FakeClient()
    client.list_docs_error = RequestException("boom")
    service = TranscriptUploadService(cast(ClaudeClient, client))

    outcome = service.upload("proj", "transcript", "f.md")

    assert isinstance(outcome, RetryPending)
    assert "boom" in outcome.error


def test_deferred_for_auth_on_upload_auth_error() -> None:
    client = FakeClient()
    client.upload_content_error = AuthError("expired")
    service = TranscriptUploadService(cast(ClaudeClient, client))

    outcome = service.upload("proj", "transcript", "f.md")

    assert isinstance(outcome, DeferredForAuth)


def test_retry_pending_on_upload_transient_error() -> None:
    client = FakeClient()
    client.upload_content_error = RequestException("boom")
    service = TranscriptUploadService(cast(ClaudeClient, client))

    outcome = service.upload("proj", "transcript", "f.md")

    assert isinstance(outcome, RetryPending)


def test_overwrite_deletes_then_uploads() -> None:
    client = FakeClient()
    client.docs = [{"uuid": "old", "file_name": "f.md"}]
    service = TranscriptUploadService(cast(ClaudeClient, client))

    outcome = service.upload("proj", "new-transcript", "f.md", overwrite_doc_uuid="old")

    assert outcome == Uploaded()
    assert client.deleted == ["old"]
    assert client.uploaded == [("proj", "new-transcript", "f.md")]


def test_overwrite_skips_delete_when_already_gone_and_replacement_missing() -> None:
    """Old doc already gone, no replacement yet: a fresh attempt should just upload,
    not error out trying to delete something that isn't there."""
    client = FakeClient()
    client.docs = []
    service = TranscriptUploadService(cast(ClaudeClient, client))

    outcome = service.upload("proj", "transcript", "f.md", overwrite_doc_uuid="already-gone")

    assert outcome == Uploaded()
    assert client.deleted == []
    assert client.uploaded == [("proj", "transcript", "f.md")]


def test_overwrite_already_landed_is_not_duplicated() -> None:
    """Old doc gone AND a replacement with this name already exists: a prior attempt's
    delete+upload landed but its confirmation was lost. Must not upload again."""
    client = FakeClient()
    client.docs = [{"uuid": "landed", "file_name": "f.md"}]
    service = TranscriptUploadService(cast(ClaudeClient, client))

    outcome = service.upload("proj", "transcript", "f.md", overwrite_doc_uuid="already-gone")

    assert outcome == AlreadyExists()
    assert client.uploaded == []


def test_overwrite_delete_auth_error_defers() -> None:
    client = FakeClient()
    client.docs = [{"uuid": "old", "file_name": "f.md"}]
    client.delete_doc_error = AuthError("expired")
    service = TranscriptUploadService(cast(ClaudeClient, client))

    outcome = service.upload("proj", "transcript", "f.md", overwrite_doc_uuid="old")

    assert isinstance(outcome, DeferredForAuth)
    assert client.uploaded == []


def test_pre_fetched_docs_skip_internal_list_call() -> None:
    """Passing `docs=` (the queue processor's per-drain cache) must not trigger an
    additional list_docs call."""
    client = FakeClient()
    client.list_docs_error = RuntimeError("should not be called")
    service = TranscriptUploadService(cast(ClaudeClient, client))

    outcome = service.upload("proj", "transcript", "f.md", docs=[])

    assert outcome == Uploaded()


def test_successful_overwrite_delete_invalidates_caller_supplied_docs_cache() -> None:
    """A caller reusing its own cached `docs` list across multiple upload() calls for
    the same project (the queue processor's docs_by_project) must see the deleted
    entry drop out automatically — the service mutates the list it was given in place
    rather than pushing cache-invalidation responsibility onto every caller."""
    client = FakeClient()
    client.docs = [{"uuid": "old", "file_name": "f.md"}]
    cached_docs = client.docs
    service = TranscriptUploadService(cast(ClaudeClient, client))

    outcome = service.upload(
        "proj", "new-transcript", "f.md", overwrite_doc_uuid="old", docs=cached_docs
    )

    assert outcome == Uploaded()
    assert cached_docs == []


# --- duplicate detection by video id ------------------------------------------------
#
# The two upload paths name the same video differently when the interactive flow can't
# reach the watch page for its upload date: it stores
# "Youtube - Ch - Title", the poller stores "Youtube - Ch - Title - 2026-08-04". Name
# equality alone therefore misses the duplicate and the transcript lands twice.

DATED = "Youtube - Ch - Title - 2026-08-04"
DATELESS = "Youtube - Ch - Title"


def _record(tmp_path: Path, file_name: str, video_id: str, project_id: str = "proj") -> None:
    record_upload(
        tmp_path,
        UploadRecord(
            project_id=project_id,
            file_name=file_name,
            video_title="Title",
            channel_name="Ch",
            uploaded_at="2026-08-04T23:27:00+00:00",
            video_id=video_id,
        ),
    )


def test_already_uploaded_video_is_not_duplicated_under_a_different_name(tmp_path: Path) -> None:
    client = FakeClient()
    client.docs = [{"uuid": "u1", "file_name": DATELESS}]
    _record(tmp_path, DATELESS, "vid1")
    service = TranscriptUploadService(cast(ClaudeClient, client), tmp_path)

    outcome = service.upload("proj", "transcript", DATED, video_id="vid1")

    assert outcome == AlreadyExists()
    assert client.uploaded == []


def test_a_different_video_with_the_same_stem_still_uploads(tmp_path: Path) -> None:
    """A channel that publishes under a recurring title (a daily livestream) produces
    doc names differing only in the date. Those are genuinely different videos and both
    must be stored — the reason detection keys on the video id and not on a
    strip-the-date name comparison."""
    client = FakeClient()
    client.docs = [{"uuid": "u1", "file_name": "Youtube - Ch - Title - 2026-08-03"}]
    _record(tmp_path, "Youtube - Ch - Title - 2026-08-03", "yesterday")
    service = TranscriptUploadService(cast(ClaudeClient, client), tmp_path)

    outcome = service.upload("proj", "transcript", DATED, video_id="today")

    assert outcome == Uploaded()
    assert client.uploaded == [("proj", "transcript", DATED)]


def test_history_for_another_project_does_not_block_the_upload(tmp_path: Path) -> None:
    client = FakeClient()
    client.docs = [{"uuid": "u1", "file_name": DATELESS}]
    _record(tmp_path, DATELESS, "vid1", project_id="other-project")
    service = TranscriptUploadService(cast(ClaudeClient, client), tmp_path)

    outcome = service.upload("proj", "transcript", DATED, video_id="vid1")

    assert outcome == Uploaded()


def test_history_record_whose_doc_was_deleted_does_not_block_the_upload(tmp_path: Path) -> None:
    """The video was uploaded once and the user has since removed the doc — history
    still remembers it, but the project doesn't have it any more, so re-uploading is
    exactly right. Only a doc that is actually still there counts as a duplicate."""
    client = FakeClient()
    client.docs = []
    _record(tmp_path, DATELESS, "vid1")
    service = TranscriptUploadService(cast(ClaudeClient, client), tmp_path)

    outcome = service.upload("proj", "transcript", DATED, video_id="vid1")

    assert outcome == Uploaded()


def test_unreadable_history_falls_back_to_name_matching(tmp_path: Path) -> None:
    """History is best-effort. A corrupt file degrades detection to what it was before
    identity matching existed; it must never fail the upload."""
    (tmp_path / "upload_history.json").write_text("{ not json")
    client = FakeClient()
    client.docs = [{"uuid": "u1", "file_name": DATELESS}]
    service = TranscriptUploadService(cast(ClaudeClient, client), tmp_path)

    assert service.upload("proj", "transcript", DATED, video_id="vid1") == Uploaded()
    assert service.upload("proj", "transcript", DATELESS, video_id="vid1") == AlreadyExists()


def test_without_a_data_dir_detection_is_by_name_only(tmp_path: Path) -> None:
    """cli.py has no DATA_DIR to read a history from — it keeps the original behaviour
    rather than gaining a half-working identity check."""
    client = FakeClient()
    client.docs = [{"uuid": "u1", "file_name": DATELESS}]
    _record(tmp_path, DATELESS, "vid1")
    service = TranscriptUploadService(cast(ClaudeClient, client))

    assert service.upload("proj", "transcript", DATED, video_id="vid1") == Uploaded()


def test_overwrite_that_already_landed_under_a_different_name_is_not_duplicated(
    tmp_path: Path,
) -> None:
    """Same lost-confirmation case as the name-matched one, but the replacement landed
    under the re-resolved canonical name while this retry still carries the old one."""
    client = FakeClient()
    client.docs = [{"uuid": "landed", "file_name": DATED}]
    _record(tmp_path, DATED, "vid1")
    service = TranscriptUploadService(cast(ClaudeClient, client), tmp_path)

    outcome = service.upload(
        "proj", "transcript", DATELESS, overwrite_doc_uuid="already-gone", video_id="vid1"
    )

    assert outcome == AlreadyExists()
    assert client.uploaded == []


def test_find_existing_returns_the_doc_stored_under_the_other_name(tmp_path: Path) -> None:
    """bot.py's pre-upload check needs the doc itself, not just a boolean: its uuid is
    what the Overwrite button deletes."""
    client = FakeClient()
    docs: list[Doc] = [{"uuid": "u1", "file_name": DATELESS}]
    _record(tmp_path, DATELESS, "vid1")
    service = TranscriptUploadService(cast(ClaudeClient, client), tmp_path)

    assert service.find_existing("proj", DATED, docs, video_id="vid1") == docs[0]
    assert service.find_existing("proj", DATED, docs, video_id="other") is None
    assert service.find_existing("proj", DATED, docs) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
