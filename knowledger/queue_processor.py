"""Single durable-queue processor shared by every drain trigger (/refresh, the
HTTP token-update endpoint, and the poller's auth-fallback retries all enqueue into the
same Queue) so overlapping drains cannot upload the same entry twice — the asyncio.Lock
makes the whole batch's network calls single-flight, while individual claim/ack/release
calls stay atomic on their own (see queue.py)."""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from curl_cffi.requests.exceptions import RequestException
from telegram.ext import Application
from telegram.helpers import escape_markdown

from .claude_client import AuthError, ClaudeClient, Doc
from .config import Config
from .history import UploadRecord, record_upload
from .logger import get_logger
from .notify import notify
from .queue import Queue, QueueEntry
from .upload_service import (
    AlreadyExists,
    DeferredForAuth,
    RetryPending,
    TranscriptUploadService,
    Uploaded,
)

logger = get_logger(__name__)

MAX_UPLOAD_ATTEMPTS = 5  # non-auth upload failures before alerting; also the re-alert cooldown


@dataclass(slots=True)
class DrainResult:
    uploaded: int = 0
    already_existed: int = 0
    failed_auth: int = 0
    failed_other: int = 0


class QueueProcessor:
    def __init__(self, queue: Queue) -> None:
        self._queue = queue
        self._lock = asyncio.Lock()

    async def drain(
        self,
        telegram_app: Application,
        config: Config,
        client: ClaudeClient,
    ) -> DrainResult:
        """Attempt every currently-queued entry once. Serialized by the lock so two
        concurrent triggers (e.g. /refresh and a token-update drain) never process the
        queue at the same time — the second call simply waits for the first to finish."""
        async with self._lock:
            return await self._drain_locked(telegram_app, config, client)

    async def _drain_locked(
        self,
        telegram_app: Application,
        config: Config,
        client: ClaudeClient,
    ) -> DrainResult:
        service = TranscriptUploadService(client)
        result = DrainResult()
        docs_by_project: dict[str, list[Doc]] = {}
        attempted: set[str] = set()
        while True:
            entry = self._queue.claim(exclude=frozenset(attempted))
            if entry is None:
                break
            attempted.add(entry.id)
            await self._process_entry(
                entry,
                telegram_app,
                config,
                client,
                service,
                docs_by_project,
                result,
            )
        return result

    async def _process_entry(
        self,
        entry: QueueEntry,
        telegram_app: Application,
        config: Config,
        client: ClaudeClient,
        service: TranscriptUploadService,
        docs_by_project: dict[str, list[Doc]],
        result: DrainResult,
    ) -> None:
        if entry.project_id not in docs_by_project:
            try:
                docs_by_project[entry.project_id] = await asyncio.to_thread(
                    client.list_docs,
                    entry.project_id,
                )
            except AuthError:
                self._queue.release(entry.id)
                result.failed_auth += 1
                return
            except RequestException:
                # Unknown state for this project — don't guess; release rather than
                # risk treating "listing failed" as "the doc doesn't exist" for every
                # entry sharing this project_id.
                logger.exception("Failed to list docs for project %s", entry.project_id)
                await self._release_with_alert(
                    telegram_app,
                    config,
                    entry,
                    result,
                    f"🛑 Queued upload stuck for “{entry.file_name}” — failed "
                    "{attempts}x in a row listing project docs. Check logs.",
                )
                return

        outcome = await asyncio.to_thread(
            service.upload,
            entry.project_id,
            entry.transcript,
            entry.file_name,
            overwrite_doc_uuid=entry.overwrite_doc_uuid,
            docs=docs_by_project[entry.project_id],
        )

        match outcome:
            case Uploaded():
                self._queue.ack(entry.id)
                result.uploaded += 1
                record_upload(
                    config.storage.data_dir,
                    UploadRecord(
                        project_id=entry.project_id,
                        file_name=entry.file_name,
                        video_title=entry.video_title,
                        channel_name=entry.channel_name,
                        uploaded_at=datetime.now(UTC).isoformat(),
                    ),
                )
                escaped = escape_markdown(entry.file_name, version=1)
                await telegram_app.bot.send_message(
                    entry.chat_id,
                    f"Queued upload saved: *{escaped}*",
                    parse_mode="Markdown",
                )
            case AlreadyExists():
                logger.info("Queued entry already uploaded, skipping: %s", entry.file_name)
                self._queue.ack(entry.id)
                result.already_existed += 1
            case DeferredForAuth():
                self._queue.release(entry.id)
                result.failed_auth += 1
            case RetryPending(step=step, error=error):
                logger.warning(
                    "Queue retry failed for %s while %s: %s",
                    entry.file_name,
                    step,
                    error,
                )
                await self._release_with_alert(
                    telegram_app,
                    config,
                    entry,
                    result,
                    f"🛑 Queued upload stuck for “{entry.file_name}” while {step} — failed "
                    "{attempts}x in a row. Check logs; it will keep retrying.",
                )

    async def _release_with_alert(
        self,
        telegram_app: Application,
        config: Config,
        entry: QueueEntry,
        result: DrainResult,
        message_template: str,
    ) -> None:
        """Release with an incremented attempt count, and alert every MAX_UPLOAD_ATTEMPTS
        attempts so a genuinely broken entry doesn't fail invisibly forever."""
        self._queue.release(entry.id, increment_attempts=True)
        attempts = entry.upload_attempts + 1
        if attempts % MAX_UPLOAD_ATTEMPTS == 0:
            await notify(telegram_app, config, message_template.format(attempts=attempts))
        result.failed_other += 1
