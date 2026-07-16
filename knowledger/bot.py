import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from functools import wraps
from typing import Any, TypedDict

from curl_cffi.requests.exceptions import RequestException
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, User
from telegram.ext import (
    Application,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

from .claude_client import AuthError, ClaudeClient, Doc, Project
from .config import Config
from .logger import get_logger
from .persistence import PersistenceError
from .queue import Queue, QueueEntry
from .queue_processor import DrainResult, QueueProcessor
from .transcript import TranscriptTransportError, TranscriptUnavailable, fetch_transcript
from .upload_service import (
    AlreadyExists,
    DeferredForAuth,
    RetryPending,
    TranscriptUploadService,
    Uploaded,
)
from .youtube import VideoMetadata, build_doc_name, extract_video_id, fetch_video_metadata

logger = get_logger(__name__)


class BotData(TypedDict):
    config: Config
    claude_client: ClaudeClient
    queue: Queue
    queue_processor: QueueProcessor


class PendingUpload(TypedDict):
    project_id: str
    file_name: str
    video_id: str


CustomContext = CallbackContext[Any, dict, dict, BotData]

YOUTUBE_URL_PATTERN = r"https?://(www\.)?(youtube\.com/watch|youtu\.be/|youtube\.com/shorts/)\S+"


def _keyboard_for(projects: list[Project], msg_id: int | str) -> InlineKeyboardMarkup:
    """Every project, no "More..." row — used once the user has asked to see the full
    list, or when no whitelist is configured at all."""
    keyboard = [
        [InlineKeyboardButton(p["name"], callback_data=f"{p['uuid']}:{msg_id}")] for p in projects
    ]
    return InlineKeyboardMarkup(keyboard)


def _build_keyboard(
    projects: list[Project], msg_id: int | str, whitelist: frozenset[str]
) -> InlineKeyboardMarkup:
    """Whitelist-filtered view, with a "More..." row when it hides any project — the
    caller wanting the unfiltered list uses `_keyboard_for` directly instead of passing
    an empty whitelist here."""
    if not whitelist:
        return _keyboard_for(projects, msg_id)
    visible = [p for p in projects if p["name"] in whitelist]
    keyboard = [
        [InlineKeyboardButton(p["name"], callback_data=f"{p['uuid']}:{msg_id}")] for p in visible
    ]
    if len(visible) < len(projects):
        keyboard.append([InlineKeyboardButton("More...", callback_data=f"more:{msg_id}")])
    return InlineKeyboardMarkup(keyboard)


def _authenticated_user(update: Update, config: Config) -> User | None:
    user = update.effective_user
    if user is None or user.id not in config.telegram.allowed_user_ids:
        logger.warning("Unauthorized access attempt from user %s", user)
        return None
    return user


_AuthedHandler = Callable[[Update, CustomContext, User], Coroutine[Any, Any, None]]
_Handler = Callable[[Update, CustomContext], Coroutine[Any, Any, None]]


def _require_auth(handler: _AuthedHandler) -> _Handler:
    """Wraps a handler so it only ever runs for an allowed user, and passes that user
    to the handler explicitly (rather than leaving it to re-derive `update.effective_user`
    as `User | None`, which is always non-None at that point but not statically so).

    Explicitly typed (rather than left for inference) so callers — both python-telegram-bot's
    handler registration and direct calls like cmd_help -> cmd_start — see the wrapped
    two-argument signature, not the inner handler's three-argument one."""

    @wraps(handler)
    async def wrapper(update: Update, context: CustomContext) -> None:
        user = _authenticated_user(update, context.bot_data["config"])
        if user is None:
            if update.callback_query:
                await update.callback_query.answer("Access denied.")
            return
        await handler(update, context, user)

    return wrapper


@_require_auth
async def cmd_start(update: Update, context: CustomContext, user: User) -> None:
    if update.message is None:
        return
    await update.message.reply_text(
        "Send me a YouTube URL and I'll let you pick a Claude project to save the "
        "transcript to.\n\nCommands: /refresh — reload project list, /help — show this message"
    )


@_require_auth
async def cmd_help(update: Update, context: CustomContext, user: User) -> None:
    await cmd_start(update, context)


@_require_auth
async def cmd_refresh(update: Update, context: CustomContext, user: User) -> None:
    if update.message is None:
        return
    await update.message.reply_text("Refreshing project list...")
    client = context.bot_data["claude_client"]
    client.invalidate_projects()
    try:
        projects = await asyncio.to_thread(client.list_projects)
        await update.message.reply_text(f"Done. {len(projects)} project(s) loaded.")
    except AuthError as e:
        await update.message.reply_text(f"Auth error: {e}")
        return
    except Exception as e:
        logger.exception("Failed to refresh project list")
        await update.message.reply_text(f"Refresh failed: {e}")
        return

    try:
        result = await context.bot_data["queue_processor"].drain(
            context.application, context.bot_data["config"], client
        )
    except Exception as e:
        logger.exception("Queue drain failed")
        await update.message.reply_text(f"Queue drain failed: {e}. It will retry on next /refresh.")
        return
    if result.uploaded or result.already_existed or result.failed_auth or result.failed_other:
        parts = [f"{result.uploaded} queued upload(s) saved"]
        if result.already_existed:
            parts.append(f"{result.already_existed} already existed")
        if result.failed_auth:
            parts.append(f"{result.failed_auth} still failing (auth)")
        if result.failed_other:
            parts.append(f"{result.failed_other} still failing (other)")
        await update.message.reply_text(", ".join(parts) + ".")


async def drain_queue(
    telegram_app: Application,
    config: Config,
    client: ClaudeClient,
    queue: Queue,
    processor: QueueProcessor | None = None,
) -> DrainResult:
    """Attempt every currently-queued entry once via QueueProcessor. In the running
    application, /refresh and the HTTP token-update drain always pass the SAME shared
    processor (see build_application / bot_data["queue_processor"]) so overlapping
    triggers cannot double-upload; a fresh throwaway processor is used only when none is
    supplied (e.g. direct/manual calls in tests)."""
    proc = processor if processor is not None else QueueProcessor(queue)
    return await proc.drain(telegram_app, config, client)


@_require_auth
async def handle_youtube_url(update: Update, context: CustomContext, user: User) -> None:
    if update.message is None or update.message.text is None or context.user_data is None:
        return

    url = update.message.text.strip()
    if not extract_video_id(url):
        await update.message.reply_text("Couldn't parse a video ID from that URL.")
        return

    logger.info("New request from user %s: %s", user.id, url)
    await update.message.reply_text("Fetching video info...")

    try:
        metadata = await asyncio.to_thread(
            fetch_video_metadata, url, context.bot_data["config"].transcript.proxy
        )
    except (RequestException, ValueError) as e:
        logger.exception("Failed to fetch metadata for %s", url)
        await update.message.reply_text(f"Failed to fetch video info: {e}")
        return

    try:
        projects = await asyncio.to_thread(context.bot_data["claude_client"].list_projects)
    except AuthError as e:
        await update.message.reply_text(f"Auth error: {e}")
        return
    if not projects:
        await update.message.reply_text(
            "No projects found. Use /refresh to reload your Claude projects."
        )
        return

    msg_id = update.message.message_id
    context.user_data[f"video_{msg_id}"] = metadata

    keyboard = _build_keyboard(
        projects, msg_id, context.bot_data["config"].telegram.project_whitelist
    )

    safe_title = escape_markdown(metadata.title, version=1)
    safe_channel = escape_markdown(metadata.channel_name, version=1)
    await update.message.reply_text(
        f"*{safe_title}*\n_{safe_channel}_\n\nSelect a project:",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@_require_auth
async def handle_project_selection(update: Update, context: CustomContext, user: User) -> None:
    query = update.callback_query
    if query is None or context.user_data is None:
        return

    await query.answer()

    if query.data is None:
        return
    parts = query.data.split(":", 1)
    if len(parts) != 2:
        await query.edit_message_text("Invalid selection data.")
        return

    project_id, msg_id_str = parts

    match project_id:
        case "more":
            try:
                projects = await asyncio.to_thread(context.bot_data["claude_client"].list_projects)
            except AuthError as e:
                await query.answer(str(e)[:200])
                return
            keyboard = _keyboard_for(projects, msg_id_str)
            await query.edit_message_reply_markup(reply_markup=keyboard)
            return

    metadata: VideoMetadata | None = context.user_data.get(f"video_{msg_id_str}")

    if metadata is None:
        await query.edit_message_text("Session expired. Please send the URL again.")
        return

    file_name = build_doc_name(metadata.channel_name, metadata.title, metadata.upload_date)

    await query.edit_message_text("Checking project...")

    try:
        docs: list[Doc] = await asyncio.to_thread(
            context.bot_data["claude_client"].list_docs, project_id
        )
    except AuthError as e:
        await query.edit_message_text(f"Auth error: {e}")
        return
    except Exception as e:
        logger.exception("Failed to list docs for project %s", project_id)
        await query.edit_message_text(f"Failed to check for duplicates: {e}")
        return

    existing = next((d for d in docs if d["file_name"] == file_name), None)
    if existing:
        context.user_data[f"pending_{msg_id_str}"] = PendingUpload(
            project_id=project_id, file_name=file_name, video_id=metadata.video_id
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Skip", callback_data=f"skip:{msg_id_str}"),
                    InlineKeyboardButton(
                        "Overwrite", callback_data=f"overwrite:{existing['uuid']}:{msg_id_str}"
                    ),
                ]
            ]
        )
        safe_name = escape_markdown(file_name, version=1)
        await query.edit_message_text(
            f"⚠️ *{safe_name}* already exists in this project.\n\nSkip or overwrite?",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return

    await query.edit_message_text("Fetching transcript...")

    logger.info("Fetching transcript for %s", file_name)
    try:
        transcript = await asyncio.to_thread(
            fetch_transcript,
            metadata.video_id,
            proxy=context.bot_data["config"].transcript.proxy,
            cookies_path=context.bot_data["config"].transcript.youtube_cookies_path,
        )
    except TranscriptUnavailable:
        await query.edit_message_text(
            f"No captions available for *{escape_markdown(metadata.title, version=1)}*.",
            parse_mode="Markdown",
        )
        return
    except TranscriptTransportError:
        await query.edit_message_text(
            "Transcript request was blocked — this is usually temporary. Please try again shortly."
        )
        return

    logger.info("Uploading %s to project %s", file_name, project_id)
    service = TranscriptUploadService(context.bot_data["claude_client"])
    # `docs` was just fetched above (for the duplicate check) and no upload happens
    # here unless that check found nothing — reuse it instead of listing again.
    outcome = await asyncio.to_thread(service.upload, project_id, transcript, file_name, docs=docs)

    match outcome:
        case Uploaded():
            context.user_data.pop(f"video_{msg_id_str}", None)
            logger.info("Upload complete: %s -> project %s", file_name, project_id)
            await query.edit_message_text(
                f"Saved *{escape_markdown(file_name, version=1)}* to project.",
                parse_mode="Markdown",
            )
        case AlreadyExists():
            # Rare race: a doc with this name appeared between the check above and
            # this upload attempt. Same "nothing to do" outcome as the check finding
            # it up front.
            await query.edit_message_text(
                f"*{escape_markdown(file_name, version=1)}* already exists in this "
                "project — nothing uploaded.",
                parse_mode="Markdown",
            )
        case DeferredForAuth():
            if update.effective_chat is None:
                return
            entry = QueueEntry(
                project_id=project_id,
                video_id=metadata.video_id,
                file_name=file_name,
                transcript=transcript,
                chat_id=update.effective_chat.id,
                video_title=metadata.title,
                queued_at=datetime.now(UTC).isoformat(),
            )
            try:
                added = context.bot_data["queue"].enqueue(entry)
            except PersistenceError:
                logger.exception("Failed to enqueue %s", file_name)
                await query.edit_message_text(
                    "Token expired and queuing failed — please resend the URL after "
                    "updating the token."
                )
                return
            escaped = escape_markdown(file_name, version=1)
            if added:
                msg = f"Token expired — *{escaped}* queued. Run /refresh after updating the token."
            else:
                msg = f"Token expired — *{escaped}* was already queued."
            await query.edit_message_text(msg, parse_mode="Markdown")
        case RetryPending(error):
            logger.warning("Upload failed for %s: %s", file_name, error)
            await query.edit_message_text(f"Upload failed: {error}")


@_require_auth
async def handle_duplicate_choice(update: Update, context: CustomContext, user: User) -> None:
    query = update.callback_query
    if query is None or context.user_data is None:
        return

    await query.answer()

    if query.data is None:
        return
    action, *rest = query.data.split(":")

    if action == "skip":
        if not rest:
            await query.edit_message_text("Invalid choice data.")
            return
        msg_id_str = rest[0]
        context.user_data.pop(f"video_{msg_id_str}", None)
        context.user_data.pop(f"pending_{msg_id_str}", None)
        await query.edit_message_text("Already in project — skipped.")
        return

    if len(rest) < 2:
        await query.edit_message_text("Invalid choice data.")
        return
    doc_uuid, msg_id_str = rest[0], rest[1]
    pending: PendingUpload | None = context.user_data.get(f"pending_{msg_id_str}")
    if pending is None:
        await query.edit_message_text("Session expired. Please send the URL again.")
        return

    await query.edit_message_text("Fetching transcript...")

    logger.info("Fetching transcript for overwrite: %s", pending["file_name"])
    try:
        transcript = await asyncio.to_thread(
            fetch_transcript,
            pending["video_id"],
            proxy=context.bot_data["config"].transcript.proxy,
            cookies_path=context.bot_data["config"].transcript.youtube_cookies_path,
        )
    except TranscriptUnavailable:
        await query.edit_message_text(
            f"No captions available for *{escape_markdown(pending['file_name'], version=1)}*.",
            parse_mode="Markdown",
        )
        return
    except TranscriptTransportError:
        await query.edit_message_text(
            "Transcript request was blocked — this is usually temporary. Please try again shortly."
        )
        return

    if update.effective_chat is None:
        return

    # Durability fix (audit finding 1): record the replacement BEFORE deleting the old
    # doc. If the upload fails after the delete lands, the replacement stays durably
    # queued (claimed, then released rather than acked) instead of being lost — the
    # queue processor's own idempotency check (old doc already gone) picks it up later
    # without re-attempting the delete.
    draft = QueueEntry(
        project_id=pending["project_id"],
        video_id=pending["video_id"],
        file_name=pending["file_name"],
        transcript=transcript,
        chat_id=update.effective_chat.id,
        video_title=pending["file_name"],
        queued_at=datetime.now(UTC).isoformat(),
        overwrite_doc_uuid=doc_uuid,
    )
    queue: Queue = context.bot_data["queue"]
    escaped = escape_markdown(pending["file_name"], version=1)
    try:
        persisted = queue.enqueue(draft)
    except PersistenceError:
        logger.exception("Failed to durably queue overwrite for %s", pending["file_name"])
        await query.edit_message_text(
            "Overwrite failed to queue durably — please retry the overwrite."
        )
        return
    if persisted is None:
        await query.edit_message_text(
            f"Overwrite of *{escaped}* is already queued — run /refresh once it completes.",
            parse_mode="Markdown",
        )
        return

    claimed = queue.claim_by_id(persisted.id)
    if claimed is None:
        # A concurrent /refresh or token-update drain already claimed it — it will
        # complete there; nothing left for this handler to do.
        await query.edit_message_text(
            f"Overwrite of *{escaped}* is already in progress — will confirm shortly.",
            parse_mode="Markdown",
        )
        return

    logger.info("Overwriting %s in project %s", pending["file_name"], pending["project_id"])
    await query.edit_message_text("Overwriting...")

    service = TranscriptUploadService(context.bot_data["claude_client"])
    try:
        outcome = await asyncio.to_thread(
            service.upload,
            claimed.project_id,
            claimed.transcript,
            claimed.file_name,
            overwrite_doc_uuid=doc_uuid,
        )
        match outcome:
            case Uploaded():
                queue.ack(claimed.id)
                logger.info(
                    "Overwrite complete: %s -> project %s",
                    pending["file_name"],
                    pending["project_id"],
                )
                await query.edit_message_text(
                    f"Saved *{escaped}* to project.", parse_mode="Markdown"
                )
            case AlreadyExists():
                # Old doc already gone and a replacement already exists: a prior
                # attempt landed and we just never saw the confirmation.
                queue.ack(claimed.id)
                await query.edit_message_text(
                    f"*{escaped}* was already overwritten.", parse_mode="Markdown"
                )
            case DeferredForAuth(error):
                queue.release(claimed.id)
                await query.edit_message_text(f"Auth error — overwrite queued for retry: {error}")
            case RetryPending(error):
                queue.release(claimed.id, increment_attempts=True)
                await query.edit_message_text(
                    f"Overwrite failed: {error}. It has been queued and will retry automatically."
                )
    except Exception:
        # Anything unexpected (a programming error, a malformed API response) must
        # still release the claim — otherwise the entry is stuck in_flight until the
        # next process restart's recover_abandoned(), instead of being retried by the
        # very next drain.
        logger.exception("Unexpected error during overwrite for %s", claimed.file_name)
        queue.release(claimed.id, increment_attempts=True)
        await query.edit_message_text(
            "Unexpected error during overwrite — it has been queued and will retry automatically."
        )
    finally:
        # The durable QueueEntry (not this dict) now owns retry state regardless of
        # outcome, so this Telegram-interaction bookkeeping is stale the moment we
        # reach here on any exit path — success, failure, or unexpected exception.
        context.user_data.pop(f"video_{msg_id_str}", None)
        context.user_data.pop(f"pending_{msg_id_str}", None)


def build_application(config: Config) -> Application:
    app = (
        Application.builder()
        .token(config.telegram.bot_token)
        .context_types(ContextTypes(bot_data=BotData))
        .build()
    )
    app.bot_data["config"] = config
    app.bot_data["claude_client"] = ClaudeClient(
        config.claude.session_token, persist_path=config.storage.data_dir / "session_token.json"
    )
    queue = Queue(path=config.storage.data_dir / "petition_queue.json")
    recovered = queue.recover_abandoned()
    if recovered:
        logger.warning(
            "Recovered %d abandoned in_flight queue entr%s from a prior run",
            recovered,
            "y" if recovered == 1 else "ies",
        )
    app.bot_data["queue"] = queue
    app.bot_data["queue_processor"] = QueueProcessor(queue)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(YOUTUBE_URL_PATTERN), handle_youtube_url)
    )
    app.add_handler(CallbackQueryHandler(handle_duplicate_choice, pattern=r"^(skip|overwrite):"))
    app.add_handler(CallbackQueryHandler(handle_project_selection))

    return app
