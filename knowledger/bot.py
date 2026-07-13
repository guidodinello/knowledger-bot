import asyncio
from datetime import UTC, datetime
from functools import wraps
from typing import Any, TypedDict

from curl_cffi.requests.exceptions import RequestException
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
from .queue import Queue, QueueEntry
from .transcript import fetch_transcript
from .youtube import VideoMetadata, build_doc_name, extract_video_id, fetch_video_metadata

logger = get_logger(__name__)


class BotData(TypedDict):
    config: Config
    claude_client: ClaudeClient
    queue: Queue


class PendingUpload(TypedDict):
    project_id: str
    file_name: str
    video_id: str


CustomContext = CallbackContext[Any, dict, dict, BotData]

YOUTUBE_URL_PATTERN = r"https?://(www\.)?(youtube\.com/watch|youtu\.be/|youtube\.com/shorts/)\S+"


def _build_keyboard(
    projects: list[Project],
    msg_id: int | str,
    whitelist: frozenset[str],
    show_all: bool = False,
) -> InlineKeyboardMarkup:
    if whitelist and not show_all:
        visible = [p for p in projects if p["name"] in whitelist]
        has_more = len(visible) < len(projects)
    else:
        visible = projects
        has_more = False
    keyboard = [
        [InlineKeyboardButton(p["name"], callback_data=f"{p['uuid']}:{msg_id}")] for p in visible
    ]
    if has_more:
        keyboard.append([InlineKeyboardButton("More...", callback_data=f"more:{msg_id}")])
    return InlineKeyboardMarkup(keyboard)


def _is_allowed(update: Update, config: Config) -> bool:
    user = update.effective_user
    if user is None or user.id not in config.allowed_user_ids:
        logger.warning("Unauthorized access attempt from user %s", user)
        return False
    return True


def _require_auth(handler):
    @wraps(handler)
    async def wrapper(update: Update, context: CustomContext):
        if not _is_allowed(update, context.bot_data["config"]):
            if update.callback_query:
                await update.callback_query.answer("Access denied.")
            return
        return await handler(update, context)

    return wrapper


@_require_auth
async def cmd_start(update: Update, context: CustomContext) -> None:
    if update.message is None:
        return
    await update.message.reply_text(
        "Send me a YouTube URL and I'll let you pick a Claude project to save the "
        "transcript to.\n\nCommands: /refresh — reload project list, /help — show this message"
    )


@_require_auth
async def cmd_help(update: Update, context: CustomContext) -> None:
    await cmd_start(update, context)


@_require_auth
async def cmd_refresh(update: Update, context: CustomContext) -> None:
    if update.message is None:
        return
    await update.message.reply_text("Refreshing project list...")
    client = context.bot_data["claude_client"]
    client.invalidate_projects()
    try:
        projects = await asyncio.to_thread(lambda: client.projects)
        await update.message.reply_text(f"Done. {len(projects)} project(s) loaded.")
    except AuthError as e:
        await update.message.reply_text(f"Auth error: {e}")
        return
    except Exception as e:
        logger.exception("Failed to refresh project list")
        await update.message.reply_text(f"Refresh failed: {e}")
        return

    await _drain_and_retry_queue(update.effective_chat.id, context)


async def _drain_and_retry_queue(chat_id: int, context: CustomContext) -> None:
    entries = context.bot_data["queue"].drain()
    if not entries:
        return

    failed = []
    for entry in entries:
        try:
            await asyncio.to_thread(
                context.bot_data["claude_client"].upload_content,
                entry.project_id,
                entry.transcript,
                entry.file_name,
            )
        except AuthError:
            failed.append(entry)
            continue
        except Exception:
            logger.exception("Queue retry failed for %s", entry.file_name)
            failed.append(entry)
            continue
        escaped = escape_markdown(entry.file_name, version=1)
        await context.bot.send_message(
            entry.chat_id,
            f"Queued upload saved: *{escaped}*",
            parse_mode="Markdown",
        )

    could_not_reenqueue = []
    for entry in failed:
        try:
            context.bot_data["queue"].enqueue(entry)
        except Exception:
            logger.exception("Failed to re-enqueue %s", entry.file_name)
            could_not_reenqueue.append(entry)

    summary = f"{len(entries) - len(failed)}/{len(entries)} queued upload(s) processed."
    if failed:
        summary += f" {len(failed)} failed and re-queued."
    if could_not_reenqueue:
        summary += f" Warning: {len(could_not_reenqueue)} could not be re-queued (storage error)."
    await context.bot.send_message(chat_id, summary)


@_require_auth
async def handle_youtube_url(update: Update, context: CustomContext) -> None:
    if update.message is None or update.message.text is None or context.user_data is None:
        return

    url = update.message.text.strip()
    if not extract_video_id(url):
        await update.message.reply_text("Couldn't parse a video ID from that URL.")
        return

    logger.info("New request from user %s: %s", update.effective_user.id, url)
    await update.message.reply_text("Fetching video info...")

    try:
        metadata = await asyncio.to_thread(
            fetch_video_metadata, url, context.bot_data["config"].proxy
        )
    except (RequestException, ValueError) as e:
        logger.exception("Failed to fetch metadata for %s", url)
        await update.message.reply_text(f"Failed to fetch video info: {e}")
        return

    try:
        projects = await asyncio.to_thread(lambda: context.bot_data["claude_client"].projects)
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

    keyboard = _build_keyboard(projects, msg_id, context.bot_data["config"].project_whitelist)

    safe_title = escape_markdown(metadata.title, version=1)
    safe_channel = escape_markdown(metadata.channel_name, version=1)
    await update.message.reply_text(
        f"*{safe_title}*\n_{safe_channel}_\n\nSelect a project:",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


@_require_auth
async def handle_project_selection(update: Update, context: CustomContext) -> None:
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
                projects = await asyncio.to_thread(
                    lambda: context.bot_data["claude_client"].projects
                )
            except AuthError as e:
                await query.answer(str(e)[:200])
                return
            keyboard = _build_keyboard(projects, msg_id_str, frozenset(), show_all=True)
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
    transcript = await asyncio.to_thread(
        fetch_transcript,
        metadata.video_id,
        proxy=context.bot_data["config"].proxy,
        cookies_path=context.bot_data["config"].youtube_cookies_path,
    )
    if transcript is None:
        await query.edit_message_text(
            f"No captions available for *{escape_markdown(metadata.title, version=1)}*.",
            parse_mode="Markdown",
        )
        return

    logger.info("Uploading %s to project %s", file_name, project_id)
    try:
        await asyncio.to_thread(
            context.bot_data["claude_client"].upload_content, project_id, transcript, file_name
        )
    except AuthError:
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
        except Exception:
            logger.exception("Failed to enqueue %s", file_name)
            await query.edit_message_text(
                "Token expired and queuing failed — please resend the URL after updating the token."
            )
            return
        escaped = escape_markdown(file_name, version=1)
        if added:
            msg = f"Token expired — *{escaped}* queued. Run /refresh after updating the token."
        else:
            msg = f"Token expired — *{escaped}* was already queued."
        await query.edit_message_text(msg, parse_mode="Markdown")
        return
    except Exception as e:
        logger.exception("Upload failed for %s", file_name)
        await query.edit_message_text(f"Upload failed: {e}")
        return

    context.user_data.pop(f"video_{msg_id_str}", None)

    logger.info("Upload complete: %s -> project %s", file_name, project_id)
    await query.edit_message_text(
        f"Saved *{escape_markdown(file_name, version=1)}* to project.", parse_mode="Markdown"
    )


@_require_auth
async def handle_duplicate_choice(update: Update, context: CustomContext) -> None:
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
    transcript = await asyncio.to_thread(
        fetch_transcript,
        pending["video_id"],
        proxy=context.bot_data["config"].proxy,
        cookies_path=context.bot_data["config"].youtube_cookies_path,
    )
    if transcript is None:
        await query.edit_message_text(
            f"No captions available for *{escape_markdown(pending['file_name'], version=1)}*.",
            parse_mode="Markdown",
        )
        return

    logger.info("Overwriting %s in project %s", pending["file_name"], pending["project_id"])
    await query.edit_message_text("Overwriting...")

    try:
        await asyncio.to_thread(
            context.bot_data["claude_client"].delete_doc, pending["project_id"], doc_uuid
        )
        await asyncio.to_thread(
            context.bot_data["claude_client"].upload_content,
            pending["project_id"],
            transcript,
            pending["file_name"],
        )
    except AuthError as e:
        await query.edit_message_text(f"Auth error: {e}")
        return
    except Exception as e:
        logger.exception("Overwrite failed for %s", pending["file_name"])
        await query.edit_message_text(f"Overwrite failed: {e}")
        return

    context.user_data.pop(f"video_{msg_id_str}", None)
    context.user_data.pop(f"pending_{msg_id_str}", None)

    logger.info("Overwrite complete: %s -> project %s", pending["file_name"], pending["project_id"])
    await query.edit_message_text(
        f"Saved *{escape_markdown(pending['file_name'], version=1)}* to project.",
        parse_mode="Markdown",
    )


def build_application(config: Config) -> Application:
    app = (
        Application.builder()
        .token(config.telegram_bot_token)
        .context_types(ContextTypes(bot_data=BotData))
        .build()
    )
    app.bot_data["config"] = config
    app.bot_data["claude_client"] = ClaudeClient(config.claude_session_token)
    app.bot_data["queue"] = Queue(path=config.data_dir / "petition_queue.json")

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(YOUTUBE_URL_PATTERN), handle_youtube_url)
    )
    app.add_handler(CallbackQueryHandler(handle_duplicate_choice, pattern=r"^(skip|overwrite):"))
    app.add_handler(CallbackQueryHandler(handle_project_selection))

    return app
