"""Durable retry list for the interactive upload flow's transcript fetch, kept
separate from ``petition_queue.json``'s ``Queue`` (see the open question in
docs/bugs/transcript-blocked-not-queued.md): that queue's claim/ack/release model
exists for entries that already have a transcript in hand and are only paused on
upload auth. A ``TranscriptTransportError`` happens earlier — there is no transcript
yet, so it gets its own small durable list instead of widening ``QueueEntry`` with an
optional transcript.

A periodic task (``run_pending_transcript_retrier``, started unconditionally in
main.py — this retry must work whether or not the auto-transcript poller feature is
enabled) drains the list by retrying ``fetch_transcript`` and, on success, falling
into the same ``TranscriptUploadService.upload()`` outcome handling already used by
the interactive flow and the poller.
"""

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from curl_cffi.requests.exceptions import RequestException
from telegram.ext import Application

from .claude_client import AuthError, ClaudeClient, Doc
from .config import Config
from .history import UploadRecord, record_upload
from .logger import get_logger
from .persistence import CorruptDataError, PersistenceError, atomic_write_json, load_json
from .queue import Queue, QueueEntry, build_auth_fallback_entry
from .telegram_format import NO_PREVIEW, PARSE_MODE, subject
from .transcript import TranscriptTransportError, TranscriptUnavailable, fetch_transcript
from .upload_service import (
    AlreadyExists,
    DeferredForAuth,
    RetryPending,
    TranscriptUploadService,
    Uploaded,
)

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PendingTranscript:
    chat_id: int
    project_id: str
    video_id: str
    file_name: str
    video_title: str
    queued_at: str  # ISO-8601, for display only
    channel_name: str  # for the weekly recap; required — see scripts/backfill_channel_name.py
    overwrite_doc_uuid: str | None = None  # set for a blocked overwrite retry


@dataclass(slots=True)
class PendingTranscriptStore:
    """Same load/mutate-in-place/save-atomically shape as ``Queue`` and
    ``PollerState``, minus their claim/ack semantics — a fetch retry has no in-flight
    state to protect, only pending-or-gone."""

    path: Path = field(default_factory=lambda: Path("pending_transcripts.json"))

    def load(self) -> list[PendingTranscript]:
        raw = load_json(self.path)
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise CorruptDataError(self.path, "expected a JSON array of pending transcripts")
        try:
            return [PendingTranscript(**p) for p in raw]
        except TypeError as e:
            raise CorruptDataError(
                self.path,
                f"malformed pending transcript entry: {e} — if this is a missing "
                "channel_name on an entry from before that field existed, run "
                "scripts/backfill_channel_name.py against this DATA_DIR",
            ) from e

    def add(self, entry: PendingTranscript) -> bool:
        """Persist entry, deduped on project_id+video_id. If an existing entry for the
        same video has a different ``overwrite_doc_uuid`` — the user's intent changed
        between attempts, e.g. a plain upload became an overwrite — it is replaced
        rather than silently dropped, since keeping the older intent would mean the
        drain later resolves the wrong outcome. Returns False only when an entry with
        the exact same intent is already queued."""
        entries = self.load()
        existing = next(
            (
                e
                for e in entries
                if e.project_id == entry.project_id and e.video_id == entry.video_id
            ),
            None,
        )
        if existing is not None:
            if existing.overwrite_doc_uuid == entry.overwrite_doc_uuid:
                return False
            self._save([entry if e is existing else e for e in entries])
            return True
        self._save(entries + [entry])
        return True

    def remove(self, project_id: str, video_id: str) -> None:
        entries = self.load()
        remaining = [
            e for e in entries if not (e.project_id == project_id and e.video_id == video_id)
        ]
        if len(remaining) != len(entries):
            self._save(remaining)

    def _save(self, entries: list[PendingTranscript]) -> None:
        atomic_write_json(self.path, [asdict(e) for e in entries])


def _enqueue_auth_fallback(
    queue: Queue,
    entry: PendingTranscript,
    transcript: str,
) -> QueueEntry | None:
    """The retried fetch succeeded but the upload itself is now auth-blocked — hand
    the now-in-hand transcript to the existing petition_queue mechanism (same
    fallback the poller already uses) rather than losing it. Returns the persisted
    entry, or None if this project_id+video_id was already queued (e.g. by an
    earlier, still-unresolved auth failure for the same video) — the caller uses
    this to tell the user whether this is newly queued or was already pending."""
    queue_entry = build_auth_fallback_entry(
        project_id=entry.project_id,
        video_id=entry.video_id,
        file_name=entry.file_name,
        transcript=transcript,
        chat_id=entry.chat_id,
        video_title=entry.video_title,
        channel_name=entry.channel_name,
        overwrite_doc_uuid=entry.overwrite_doc_uuid,
    )
    # Persistence failures propagate — silently discarding a fetched transcript here
    # would lose it with no record it ever existed.
    return queue.enqueue(queue_entry)


async def _notify_chat(
    app: Application,
    chat_id: int,
    text: str,
    *,
    parse_mode: str | None = None,
) -> None:
    """Best-effort: a failure here (e.g. this task's first tick racing the Telegram
    Application's own startup initialization) must not be treated as the drain
    itself failing — the transcript is already safely handled by this point, only
    the user-facing confirmation would be missing."""
    try:
        await app.bot.send_message(
            chat_id,
            text,
            parse_mode=parse_mode,
            link_preview_options=NO_PREVIEW,
        )
    except Exception:
        logger.warning("Failed to notify chat %d", chat_id, exc_info=True)


def token_expired_message(subject_line: str, *, newly_queued: bool) -> str:
    """The shared copy for every auth-blocked transcript that made it into Queue."""
    tail = (
        "Update your Claude session token, then run /refresh."
        if newly_queued
        else "It was already queued — update your token, then run /refresh."
    )
    return f"⏳ Waiting on a valid token\n{subject_line}\n\n{tail}"


async def _drain_one(
    app: Application,
    config: Config,
    client: ClaudeClient,
    service: TranscriptUploadService,
    queue: Queue,
    store: PendingTranscriptStore,
    entry: PendingTranscript,
    docs_by_project: dict[str, list[Doc]],
) -> None:
    try:
        transcript = await asyncio.to_thread(
            fetch_transcript,
            entry.video_id,
            proxy=config.transcript.proxy,
            cookies_path=config.transcript.youtube_cookies_path,
        )
    except TranscriptTransportError:
        logger.info("Transcript request still blocked for %s; will retry", entry.video_id)
        return
    except TranscriptUnavailable:
        logger.info("No captions available for %s on retry; giving up", entry.video_id)
        store.remove(entry.project_id, entry.video_id)
        await _notify_chat(
            app,
            entry.chat_id,
            subject(entry.video_title, entry.channel_name, entry.video_id)
            + "\n\nThis video has no captions after all, so there's nothing to save. "
            "I've stopped retrying it.",
            parse_mode=PARSE_MODE,
        )
        return

    # Cache doc listings per project for the whole batch — several pending entries
    # commonly share a project_id (the same block affecting several requests), and
    # TranscriptUploadService.upload() would otherwise re-list on every single call.
    # Mirrors QueueProcessor._process_entry's identical batching.
    if entry.project_id not in docs_by_project:
        try:
            docs_by_project[entry.project_id] = await asyncio.to_thread(
                client.list_docs,
                entry.project_id,
            )
        except AuthError:
            store.remove(entry.project_id, entry.video_id)
            added = _enqueue_auth_fallback(queue, entry, transcript)
            await _notify_chat(
                app,
                entry.chat_id,
                token_expired_message(
                    subject(entry.video_title, entry.channel_name, entry.video_id),
                    newly_queued=added is not None,
                ),
                parse_mode=PARSE_MODE,
            )
            return
        except RequestException:
            logger.warning(
                "Failed to list docs for project %s; will retry",
                entry.project_id,
                exc_info=True,
            )
            return

    outcome = await asyncio.to_thread(
        service.upload,
        entry.project_id,
        transcript,
        entry.file_name,
        overwrite_doc_uuid=entry.overwrite_doc_uuid,
        docs=docs_by_project[entry.project_id],
    )
    match outcome:
        case Uploaded():
            store.remove(entry.project_id, entry.video_id)
            logger.info(
                "Retried upload succeeded: %s -> project %s",
                entry.file_name,
                entry.project_id,
            )
            record_upload(
                config.storage.data_dir,
                UploadRecord(
                    project_id=entry.project_id,
                    file_name=entry.file_name,
                    video_title=entry.video_title,
                    channel_name=entry.channel_name,
                    uploaded_at=datetime.now(UTC).isoformat(),
                    video_id=entry.video_id,
                ),
            )
            await _notify_chat(
                app,
                entry.chat_id,
                "✅ Saved — YouTube stopped blocking the transcript\n"
                + subject(entry.video_title, entry.channel_name, entry.video_id),
                parse_mode=PARSE_MODE,
            )
        case AlreadyExists():
            logger.info("Doc already exists, dropping pending transcript: %s", entry.file_name)
            store.remove(entry.project_id, entry.video_id)
        case DeferredForAuth():
            store.remove(entry.project_id, entry.video_id)
            added = _enqueue_auth_fallback(queue, entry, transcript)
            await _notify_chat(
                app,
                entry.chat_id,
                token_expired_message(
                    subject(entry.video_title, entry.channel_name, entry.video_id),
                    newly_queued=added is not None,
                ),
                parse_mode=PARSE_MODE,
            )
        case RetryPending(step=step, error=error):
            logger.warning(
                "Retried upload failed for %s while %s: %s; will retry",
                entry.file_name,
                step,
                error,
            )


async def drain_pending_transcripts(
    app: Application,
    config: Config,
    client: ClaudeClient,
    queue: Queue,
    store: PendingTranscriptStore,
) -> None:
    service = TranscriptUploadService(client)
    docs_by_project: dict[str, list[Doc]] = {}
    for entry in store.load():
        await _drain_one(app, config, client, service, queue, store, entry, docs_by_project)


async def run_pending_transcript_retrier(app: Application, config: Config) -> None:
    """Started unconditionally alongside Telegram polling — unlike the auto-transcript
    poller, this retry backs the always-on interactive upload flow, not an opt-in
    feature, so it must not depend on AUTO_TRANSCRIPT_PROJECT being configured."""
    client: ClaudeClient = app.bot_data["claude_client"]
    queue: Queue = app.bot_data["queue"]
    store: PendingTranscriptStore = app.bot_data["pending_transcripts"]
    logger.info(
        "Pending-transcript retrier started: interval %ds",
        config.poller.poll_interval,
    )
    while True:
        try:
            await drain_pending_transcripts(app, config, client, queue, store)
        except asyncio.CancelledError:
            raise
        except PersistenceError:
            # Fatal: this store can no longer be trusted. Propagate so main.py's
            # structured supervision terminates the process visibly, same as the
            # poller does on its own PersistenceError.
            raise
        except Exception:
            logger.exception("Pending-transcript drain failed; continuing")
        await asyncio.sleep(config.poller.poll_interval)
