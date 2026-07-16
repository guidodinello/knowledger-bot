"""Shared "store a transcript as a Claude project doc" decision logic — list the
project's docs, detect a duplicate, upload or overwrite, and classify auth/transient
failures — reused by every entry point that stores a transcript (Telegram's normal
upload and its interactive overwrite, the durable-queue processor's retries, the
poller's auto-upload, and the CLI) instead of five independent copies of the same
list_docs -> dedupe -> upload/overwrite -> classify workflow.

Deliberately synchronous (this only calls ClaudeClient's own synchronous methods) —
async callers wrap the whole call in asyncio.to_thread, same as they already do for
the individual client calls this replaces. Telegram text, poller notifications, and
CLI output all stay outside this module; callers translate the returned outcome into
whatever their own adapter needs to say or do."""

from dataclasses import dataclass

from curl_cffi.requests.exceptions import RequestException

from .claude_client import AuthError, ClaudeClient, Doc


@dataclass(frozen=True, slots=True)
class Uploaded:
    """The transcript was uploaded (or the overwrite's replacement landed) just now."""


@dataclass(frozen=True, slots=True)
class AlreadyExists:
    """A doc with this file_name already exists — nothing was uploaded this call."""


@dataclass(frozen=True, slots=True)
class DeferredForAuth:
    """The Claude session token is invalid/expired. Nothing was uploaded; the caller
    decides how to persist this for retry (a fresh queue entry, or releasing one
    already claimed) — that's adapter-specific, not this module's concern."""

    step: str  # e.g. "listing project documents", "deleting the old document", "uploading..."
    error: str


@dataclass(frozen=True, slots=True)
class RetryPending:
    """A transient request failure (list/delete/upload). Nothing was uploaded; same
    step/retry-persistence notes as DeferredForAuth."""

    step: str
    error: str


UploadOutcome = Uploaded | AlreadyExists | DeferredForAuth | RetryPending


class TranscriptUploadService:
    def __init__(self, client: ClaudeClient) -> None:
        self._client = client

    def upload(
        self,
        project_id: str,
        transcript: str,
        file_name: str,
        *,
        overwrite_doc_uuid: str | None = None,
        docs: list[Doc] | None = None,
    ) -> UploadOutcome:
        """Store `transcript` as `file_name` in `project_id`.

        If `overwrite_doc_uuid` is given, delete that doc first (skipping the delete,
        idempotently, if it's already gone — e.g. a prior attempt's confirmation was
        lost) rather than uploading a second copy under the same name.

        `docs` lets a caller that's already listing many entries against the same
        project in one pass (the queue processor draining a batch) supply its own
        cached listing instead of triggering a redundant list_docs call here; other
        callers omit it and this method lists the project itself. After a successful
        delete, this method mutates that same list in place to drop the deleted entry
        — a caller reusing its own cached list across multiple calls for the same
        project (as the queue processor does) sees the update automatically instead of
        needing to know to invalidate its cache itself.
        """
        if docs is None:
            try:
                docs = self._client.list_docs(project_id)
            except AuthError as e:
                return DeferredForAuth(step="listing project documents", error=str(e))
            except RequestException as e:
                return RetryPending(step="listing project documents", error=str(e))

        if overwrite_doc_uuid is not None:
            if any(d["uuid"] == overwrite_doc_uuid for d in docs):
                try:
                    self._client.delete_doc(project_id, overwrite_doc_uuid)
                except AuthError as e:
                    return DeferredForAuth(step="deleting the old document", error=str(e))
                except RequestException as e:
                    return RetryPending(step="deleting the old document", error=str(e))
                docs[:] = [d for d in docs if d["uuid"] != overwrite_doc_uuid]
            elif any(d["file_name"] == file_name for d in docs):
                # The old doc is already gone AND a replacement with this name already
                # exists: a prior attempt landed and we just never saw the
                # confirmation. Uploading again would create a duplicate.
                return AlreadyExists()
        elif any(d["file_name"] == file_name for d in docs):
            return AlreadyExists()

        try:
            self._client.upload_content(project_id, transcript, file_name)
        except AuthError as e:
            return DeferredForAuth(step="uploading the document", error=str(e))
        except RequestException as e:
            return RetryPending(step="uploading the document", error=str(e))
        return Uploaded()
