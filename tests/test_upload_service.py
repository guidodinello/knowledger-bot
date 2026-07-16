from curl_cffi.requests.exceptions import RequestException

from knowledger.claude_client import AuthError
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
        self.docs: list[dict] = []
        self.deleted: list[str] = []
        self.uploaded: list[tuple[str, str, str]] = []
        self.list_docs_error: Exception | None = None
        self.delete_doc_error: Exception | None = None
        self.upload_content_error: Exception | None = None

    def list_docs(self, project_id: str) -> list[dict]:
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
    service = TranscriptUploadService(client)

    outcome = service.upload("proj", "transcript", "f.md")

    assert outcome == Uploaded()
    assert client.uploaded == [("proj", "transcript", "f.md")]


def test_already_exists_when_file_name_present() -> None:
    client = FakeClient()
    client.docs = [{"uuid": "u1", "file_name": "f.md"}]
    service = TranscriptUploadService(client)

    outcome = service.upload("proj", "transcript", "f.md")

    assert outcome == AlreadyExists()
    assert client.uploaded == []


def test_deferred_for_auth_on_list_docs_auth_error() -> None:
    client = FakeClient()
    client.list_docs_error = AuthError("expired")
    service = TranscriptUploadService(client)

    outcome = service.upload("proj", "transcript", "f.md")

    assert isinstance(outcome, DeferredForAuth)
    assert "expired" in outcome.error


def test_retry_pending_on_list_docs_transient_error() -> None:
    client = FakeClient()
    client.list_docs_error = RequestException("boom")
    service = TranscriptUploadService(client)

    outcome = service.upload("proj", "transcript", "f.md")

    assert isinstance(outcome, RetryPending)
    assert "boom" in outcome.error


def test_deferred_for_auth_on_upload_auth_error() -> None:
    client = FakeClient()
    client.upload_content_error = AuthError("expired")
    service = TranscriptUploadService(client)

    outcome = service.upload("proj", "transcript", "f.md")

    assert isinstance(outcome, DeferredForAuth)


def test_retry_pending_on_upload_transient_error() -> None:
    client = FakeClient()
    client.upload_content_error = RequestException("boom")
    service = TranscriptUploadService(client)

    outcome = service.upload("proj", "transcript", "f.md")

    assert isinstance(outcome, RetryPending)


def test_overwrite_deletes_then_uploads() -> None:
    client = FakeClient()
    client.docs = [{"uuid": "old", "file_name": "f.md"}]
    service = TranscriptUploadService(client)

    outcome = service.upload("proj", "new-transcript", "f.md", overwrite_doc_uuid="old")

    assert outcome == Uploaded()
    assert client.deleted == ["old"]
    assert client.uploaded == [("proj", "new-transcript", "f.md")]


def test_overwrite_skips_delete_when_already_gone_and_replacement_missing() -> None:
    """Old doc already gone, no replacement yet: a fresh attempt should just upload,
    not error out trying to delete something that isn't there."""
    client = FakeClient()
    client.docs = []
    service = TranscriptUploadService(client)

    outcome = service.upload("proj", "transcript", "f.md", overwrite_doc_uuid="already-gone")

    assert outcome == Uploaded()
    assert client.deleted == []
    assert client.uploaded == [("proj", "transcript", "f.md")]


def test_overwrite_already_landed_is_not_duplicated() -> None:
    """Old doc gone AND a replacement with this name already exists: a prior attempt's
    delete+upload landed but its confirmation was lost. Must not upload again."""
    client = FakeClient()
    client.docs = [{"uuid": "landed", "file_name": "f.md"}]
    service = TranscriptUploadService(client)

    outcome = service.upload("proj", "transcript", "f.md", overwrite_doc_uuid="already-gone")

    assert outcome == AlreadyExists()
    assert client.uploaded == []


def test_overwrite_delete_auth_error_defers() -> None:
    client = FakeClient()
    client.docs = [{"uuid": "old", "file_name": "f.md"}]
    client.delete_doc_error = AuthError("expired")
    service = TranscriptUploadService(client)

    outcome = service.upload("proj", "transcript", "f.md", overwrite_doc_uuid="old")

    assert isinstance(outcome, DeferredForAuth)
    assert client.uploaded == []


def test_pre_fetched_docs_skip_internal_list_call() -> None:
    """Passing `docs=` (the queue processor's per-drain cache) must not trigger an
    additional list_docs call."""
    client = FakeClient()
    client.list_docs_error = RuntimeError("should not be called")
    service = TranscriptUploadService(client)

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
    service = TranscriptUploadService(client)

    outcome = service.upload(
        "proj", "new-transcript", "f.md", overwrite_doc_uuid="old", docs=cached_docs
    )

    assert outcome == Uploaded()
    assert cached_docs == []
