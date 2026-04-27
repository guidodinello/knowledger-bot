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

from .claude_client import AuthError, ClaudeClient, Doc, Project
from .config import Config
from .logger import get_logger
from .transcript import fetch_transcript
from .youtube import VideoMetadata, extract_video_id, fetch_video_metadata, sanitize_filename

logger = get_logger(__name__)


class BotData(TypedDict):
    config: Config
    claude_client: ClaudeClient
    projects: list[Project]


class PendingUpload(TypedDict):
    project_id: str
    file_name: str
    transcript: str


CustomContext = CallbackContext[Any, dict, dict, BotData]

YOUTUBE_URL_PATTERN = r"https?://(www\.)?(youtube\.com/watch|youtu\.be/|youtube\.com/shorts/)\S+"


def _build_keyboard(
    projects: list[Project], msg_id: int | str, whitelist: frozenset[str], show_all: bool = False
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


async def cmd_start(update: Update, context: CustomContext) -> None:
    if not _is_allowed(update, context.bot_data["config"]):
        return
    await update.message.reply_text(
        "Send me a YouTube URL and I'll let you pick a Claude project to save the "
        "transcript to.\n\nCommands: /refresh — reload project list, /help — show this message"
    )


async def cmd_help(update: Update, context: CustomContext) -> None:
    await cmd_start(update, context)


async def cmd_refresh(update: Update, context: CustomContext) -> None:
    if not _is_allowed(update, context.bot_data["config"]):
        return

    await update.message.reply_text("Refreshing project list...")
    try:
        context.bot_data["projects"] = context.bot_data["claude_client"].list_projects()
        await update.message.reply_text(
            f"Done. {len(context.bot_data['projects'])} project(s) loaded."
        )
    except AuthError as e:
        await update.message.reply_text(f"Auth error: {e}")


async def handle_youtube_url(update: Update, context: CustomContext) -> None:
    if not _is_allowed(update, context.bot_data["config"]):
        return

    url = update.message.text.strip()
    if not extract_video_id(url):
        await update.message.reply_text("Couldn't parse a video ID from that URL.")
        return

    await update.message.reply_text("Fetching video info...")

    try:
        metadata: VideoMetadata = fetch_video_metadata(url)
    except (RequestException, ValueError) as e:
        logger.exception("Failed to fetch metadata for %s", url)
        await update.message.reply_text(f"Failed to fetch video info: {e}")
        return

    projects = context.bot_data["projects"]
    if not projects:
        await update.message.reply_text(
            "No projects loaded. Use /refresh to load your Claude projects."
        )
        return

    msg_id = update.message.message_id
    context.user_data[f"video_{msg_id}"] = metadata

    keyboard = _build_keyboard(projects, msg_id, context.bot_data["config"].project_whitelist)

    await update.message.reply_text(
        f"*{metadata.title}*\n_{metadata.channel_name}_\n\nSelect a project:",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


async def handle_project_selection(update: Update, context: CustomContext) -> None:
    query = update.callback_query

    if not _is_allowed(update, context.bot_data["config"]):
        await query.answer("Access denied.")
        return

    await query.answer()

    parts = query.data.split(":", 1)
    if len(parts) != 2:
        await query.edit_message_text("Invalid selection data.")
        return

    project_id, msg_id_str = parts

    if project_id == "more":
        keyboard = _build_keyboard(
            context.bot_data["projects"], msg_id_str, frozenset(), show_all=True
        )
        await query.edit_message_reply_markup(reply_markup=keyboard)
        return

    metadata: VideoMetadata | None = context.user_data.get(f"video_{msg_id_str}")

    if metadata is None:
        await query.edit_message_text("Session expired. Please send the URL again.")
        return

    await query.edit_message_text("Fetching transcript...")

    transcript = fetch_transcript(metadata.video_id)
    if transcript is None:
        await query.edit_message_text(
            f"No captions available for *{metadata.title}*.", parse_mode="Markdown"
        )
        return

    channel = sanitize_filename(metadata.channel_name)
    title = sanitize_filename(metadata.title)
    file_name = f"Youtube - {channel} - {title}"

    try:
        docs: list[Doc] = context.bot_data["claude_client"].list_docs(project_id)
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
            project_id=project_id, file_name=file_name, transcript=transcript
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
        await query.edit_message_text(
            f"⚠️ *{file_name}* already exists in this project.\n\nSkip or overwrite?",
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return

    try:
        context.bot_data["claude_client"].upload_content(project_id, transcript, file_name)
    except AuthError as e:
        await query.edit_message_text(f"Auth error: {e}")
        return
    except Exception as e:
        logger.exception("Upload failed for %s", file_name)
        await query.edit_message_text(f"Upload failed: {e}")
        return

    context.user_data.pop(f"video_{msg_id_str}", None)

    await query.edit_message_text(f"Saved *{file_name}* to project.", parse_mode="Markdown")


async def handle_duplicate_choice(update: Update, context: CustomContext) -> None:
    query = update.callback_query

    if not _is_allowed(update, context.bot_data["config"]):
        await query.answer("Access denied.")
        return

    await query.answer()

    action, *rest = query.data.split(":")

    if action == "skip":
        msg_id_str = rest[0]
        context.user_data.pop(f"video_{msg_id_str}", None)
        context.user_data.pop(f"pending_{msg_id_str}", None)
        await query.edit_message_text("Already in project — skipped.")
        return

    doc_uuid, msg_id_str = rest[0], rest[1]
    pending: PendingUpload | None = context.user_data.get(f"pending_{msg_id_str}")
    if pending is None:
        await query.edit_message_text("Session expired. Please send the URL again.")
        return

    await query.edit_message_text("Overwriting...")

    try:
        context.bot_data["claude_client"].delete_doc(pending["project_id"], doc_uuid)
        context.bot_data["claude_client"].upload_content(
            pending["project_id"], pending["transcript"], pending["file_name"]
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

    await query.edit_message_text(
        f"Saved *{pending['file_name']}* to project.", parse_mode="Markdown"
    )


def build_application(config: Config) -> Application:
    client = ClaudeClient(config.claude_session_token)
    projects = client.list_projects()
    logger.info("Loaded %d Claude project(s)", len(projects))

    app = (
        Application.builder()
        .token(config.telegram_bot_token)
        .context_types(ContextTypes(bot_data=BotData))
        .build()
    )
    app.bot_data["config"] = config
    app.bot_data["claude_client"] = client
    app.bot_data["projects"] = projects

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(YOUTUBE_URL_PATTERN), handle_youtube_url)
    )
    app.add_handler(CallbackQueryHandler(handle_duplicate_choice, pattern=r"^(skip|overwrite):"))
    app.add_handler(CallbackQueryHandler(handle_project_selection))

    return app
