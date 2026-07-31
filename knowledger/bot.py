import asyncio
from collections.abc import Awaitable, Callable, Coroutine
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
from .config import Config, load_persisted_projects
from .history import UploadRecord, record_upload
from .logger import get_logger
from .pending_transcripts import PendingTranscript, PendingTranscriptStore
from .persistence import PersistenceError
from .poller import PollerState, load_channels
from .queue import Queue, QueueEntry
from .queue_processor import DrainResult, QueueProcessor
from .subscriptions import (
    ResolvedChannel,
    SubscriptionError,
    add_subscription,
    find_subscription,
    resolve_subscription,
)
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
    pending_transcripts: PendingTranscriptStore


class PendingUpload(TypedDict):
    project_id: str
    file_name: str
    video_id: str
    channel_name: str


CustomContext = CallbackContext[Any, dict, dict, BotData]

YOUTUBE_URL_PATTERN = r"https?://(www\.)?(youtube\.com/watch|youtu\.be/|youtube\.com/shorts/)\S+"


def _keyboard_for(
    projects: list[Project],
    msg_id: int | str,
    prefix: str = "",
) -> InlineKeyboardMarkup:
    """Every project, no "More..." row — used once the user has asked to see the full
    list, or when no whitelist is configured at all.

    `prefix` routes the callback to a different handler (see `_SUBSCRIBE_PREFIX`); the
    default empty prefix is the transcript-upload picker."""
    keyboard = [
        [InlineKeyboardButton(p["name"], callback_data=f"{prefix}{p['uuid']}:{msg_id}")]
        for p in projects
    ]
    return InlineKeyboardMarkup(keyboard)


def _build_keyboard(
    projects: list[Project],
    msg_id: int | str,
    whitelist: frozenset[str],
    prefix: str = "",
) -> InlineKeyboardMarkup:
    """Whitelist-filtered view, with a "More..." row when it hides any project — the
    caller wanting the unfiltered list uses `_keyboard_for` directly instead of passing
    an empty whitelist here."""
    if not whitelist:
        return _keyboard_for(projects, msg_id, prefix)
    visible = [p for p in projects if p["name"] in whitelist]
    keyboard = [
        [InlineKeyboardButton(p["name"], callback_data=f"{prefix}{p['uuid']}:{msg_id}")]
        for p in visible
    ]
    if len(visible) < len(projects):
        keyboard.append([InlineKeyboardButton("More...", callback_data=f"{prefix}more:{msg_id}")])
    return InlineKeyboardMarkup(keyboard)


async def _projects_for_picker(
    context: CustomContext,
    reply: Callable[[str], Awaitable[Any]],
) -> list[Project] | None:
    """The projects to offer in an inline picker: the live list, or the last known one
    when the token has gone bad. Returns None — after explaining why through `reply` —
    when there is nothing to show at all."""
    try:
        projects = await asyncio.to_thread(context.bot_data["claude_client"].list_projects)
    except AuthError as e:
        try:
            projects = load_persisted_projects(context.bot_data["config"].storage.data_dir)
        except PersistenceError as cache_err:
            logger.exception("Failed to load persisted project list")
            await reply(
                f"Auth error: {e}\nAlso failed to load the cached project list: {cache_err}",
            )
            return None
        if not projects:
            await reply(f"Auth error: {e}")
            return None
        await reply("⚠️ Using last known project list — may not include newly created projects.")
    if not projects:
        await reply("No projects found. Use /refresh to reload your Claude projects.")
        return None
    return projects


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
        "transcript to.\n\n"
        "Commands: /inqueue — show queue contents, /subscribed — list watched channels, "
        "/subscribe <link> — watch a channel, /refresh — reload project list, "
        "/version — show running build, /help — show this message",
    )


@_require_auth
async def cmd_help(update: Update, context: CustomContext, user: User) -> None:
    await cmd_start(update, context)


@_require_auth
async def cmd_version(update: Update, context: CustomContext, user: User) -> None:
    if update.message is None:
        return
    version = context.bot_data["config"].version
    sha = version.commit_sha or "unknown"
    date = version.commit_date or "unknown"
    await update.message.reply_text(f"Running {sha}, committed {date}.")


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
            context.application,
            context.bot_data["config"],
            client,
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


_TELEGRAM_MAX_MESSAGE_LENGTH = 4096
_INQUEUE_MAX_ENTRIES = 10  # per-section cap before a "+N more" trailer
_STUCK_MARKER = "⚠️ "  # prefix for entries with upload_attempts > 0


def _capped(text: str) -> str:
    """Telegram rejects anything longer than 4096 characters outright, so a long listing
    is truncated with a visible marker rather than being lost to a send failure."""
    if len(text) <= _TELEGRAM_MAX_MESSAGE_LENGTH:
        return text
    note = "\n… (truncated)"
    return text[: _TELEGRAM_MAX_MESSAGE_LENGTH - len(note)] + note


def _fmt_ts(iso: str) -> str:
    """Absolute 'YYYY-MM-DD HH:MM' slice of an ISO-8601 string. Absolute, not
    relative — a relative "2h ago" goes stale/misleading if the message sits unread."""
    return iso[:16].replace("T", " ")


def _cap_entries(
    entry_lines: list[list[str]],
    max_entries: int = _INQUEUE_MAX_ENTRIES,
) -> list[str]:
    """Flatten up to max_entries formatted entries (each a list of its own lines: a
    bullet line plus any indented detail lines) and append a '+N more' trailer instead
    of hard-truncating mid-bullet."""
    capped = entry_lines[:max_entries]
    lines = [line for entry in capped for line in entry]
    remaining = len(entry_lines) - len(capped)
    if remaining > 0:
        lines.append(f"+{remaining} more")
    return lines


@_require_auth
async def cmd_inqueue(update: Update, context: CustomContext, user: User) -> None:
    if update.message is None:
        return

    lines: list[str] = ["📊 Queue status", ""]

    # 🔁 Retry queue — QueueEntry: has upload_attempts and is the only section /refresh drains.
    queue: Queue = context.bot_data["queue"]
    retry_entries = queue.peek()
    if retry_entries:
        lines.append(f"🔁 Retry queue — {len(retry_entries)} queued")
        entry_lines = []
        for e in retry_entries:
            marker = _STUCK_MARKER if e.upload_attempts else ""
            title = escape_markdown(e.video_title or e.file_name, version=1)
            attempts = f" — {e.upload_attempts} failed attempts" if e.upload_attempts else ""
            detail = f"  queued {_fmt_ts(e.queued_at)}"
            if e.upload_attempts:
                detail += " — run /refresh to retry"
            entry_lines.append([f"• {marker}{title}{attempts}", detail])
        lines.extend(_cap_entries(entry_lines))
    else:
        lines.append("🔁 Retry queue: empty")

    lines.append("")

    # ⏳ Poller pending — PendingVideo: has upload_attempts and channel_name, no /refresh.
    state_path = context.bot_data["config"].storage.data_dir / "poller_state.json"
    try:
        state = PollerState.load(state_path)
    except PersistenceError as e:
        logger.exception("Failed to read poller state")
        lines.append(f"⏳ Poller: (error: {escape_markdown(str(e), version=1)})")
    else:
        if state.pending:
            lines.append(f"⏳ Poller — {len(state.pending)} pending")
            entry_lines = []
            for v in state.pending:
                marker = _STUCK_MARKER if v.upload_attempts else ""
                title = escape_markdown(v.title, version=1)
                channel_name = escape_markdown(v.channel_name, version=1)
                attempts = f" — {v.upload_attempts} failed attempts" if v.upload_attempts else ""
                detail = f"  seen {_fmt_ts(v.first_seen)}"
                entry_lines.append([f"• {marker}{title} — {channel_name}{attempts}", detail])
            lines.extend(_cap_entries(entry_lines))
        else:
            lines.append("⏳ Poller: empty")
        if state.seen:
            lines.append(f"{len(state.seen)} videos seen total")

    lines.append("")

    # 📥 Blocked transcripts — PendingTranscript: has channel_name, no upload_attempts, no
    # /refresh hint (drained automatically by the periodic retrier, not by /refresh).
    lines.append("📥 Blocked transcripts")
    try:
        pending_transcripts = context.bot_data["pending_transcripts"].load()
    except PersistenceError as e:
        logger.exception("Failed to read pending transcripts")
        lines[-1] += f": (error: {escape_markdown(str(e), version=1)})"
    else:
        if pending_transcripts:
            lines[-1] += f" — {len(pending_transcripts)} blocked"
            entry_lines = [
                [
                    f"• {escape_markdown(t.video_title, version=1)}"
                    f" — {escape_markdown(t.channel_name, version=1)}",
                    f"  queued {_fmt_ts(t.queued_at)}",
                ]
                for t in pending_transcripts
            ]
            lines.extend(_cap_entries(entry_lines))
        else:
            lines[-1] += ": empty"

    await update.message.reply_text(_capped("\n".join(lines)), parse_mode="Markdown")


_SUBSCRIBE_PREFIX = "sub:"  # routes a project-picker callback to the subscribe handler
_SUBSCRIBE_DEFAULT = "default"  # picker choice: inherit AUTO_TRANSCRIPT_PROJECT


def _project_label(value: str, names: dict[str, str]) -> str:
    """Channel entries and AUTO_TRANSCRIPT_PROJECT may hold either a project name or a
    uuid — show the name where we can resolve one, the raw value otherwise."""
    return names.get(value, value)


async def _project_names(context: CustomContext) -> dict[str, str]:
    """uuid -> project name, best effort. Display-only, so every failure degrades to an
    empty mapping (callers fall back to printing the raw value) rather than an error:
    not being able to name a project is no reason to refuse to list the channels."""
    projects: list[Project] | None
    try:
        projects = await asyncio.to_thread(context.bot_data["claude_client"].list_projects)
    except Exception:
        logger.warning("Could not load the live project list for display", exc_info=True)
        try:
            projects = load_persisted_projects(context.bot_data["config"].storage.data_dir)
        except PersistenceError:
            logger.exception("Could not load the cached project list for display")
            projects = None
    return {p["uuid"]: p["name"] for p in projects or []}


def _fmt_interval(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes // 60}h" if minutes >= 60 and minutes % 60 == 0 else f"{minutes} min"


@_require_auth
async def cmd_subscribed(update: Update, context: CustomContext, user: User) -> None:
    if update.message is None:
        return

    config = context.bot_data["config"]
    try:
        channels = load_channels(config.poller.channels_path)
    except PersistenceError as e:
        logger.exception("Failed to read the channel list")
        await update.message.reply_text(f"Couldn't read the channel list: {e}")
        return

    if not channels:
        await update.message.reply_text(
            "Not watching any channels yet. Add one with /subscribe <youtube link>.",
        )
        return

    names = await _project_names(context)
    default = config.poller.auto_transcript_project

    lines = [f"📺 Watching {len(channels)} channel(s)", ""]
    for ch in channels:
        lines.append(f"• {ch.name} ({ch.handle})")
        if ch.project:
            lines.append(f"  → {_project_label(ch.project, names)}")
        elif default:
            lines.append(f"  → {_project_label(default, names)} (default)")
        else:
            lines.append("  → no project")

    lines.append("")
    if default:
        lines.append(
            f"Checked every {_fmt_interval(config.poller.poll_interval)}; "
            "transcripts upload 24h after a video is published.",
        )
    else:
        lines.append("⚠️ AUTO_TRANSCRIPT_PROJECT is not set — auto-upload is off.")

    # Plain text, no parse_mode: channel names are arbitrary user-facing strings and this
    # listing gains nothing from Markdown (see docs/bugs/unescaped-markdown-injection.md).
    await update.message.reply_text(_capped("\n".join(lines)))


def _subscribe_keyboard(
    projects: list[Project],
    msg_id: int | str,
    config: Config,
    *,
    show_all: bool = False,
) -> InlineKeyboardMarkup:
    """The upload picker's keyboard, prefixed for the subscribe flow and topped with a
    row for inheriting AUTO_TRANSCRIPT_PROJECT — the common case, since most channels
    are watched for the same project."""
    keyboard = (
        _keyboard_for(projects, msg_id, _SUBSCRIBE_PREFIX)
        if show_all
        else _build_keyboard(
            projects,
            msg_id,
            config.telegram.project_whitelist,
            _SUBSCRIBE_PREFIX,
        )
    )
    rows = [list(row) for row in keyboard.inline_keyboard]
    default = config.poller.auto_transcript_project
    if default:
        names = {p["uuid"]: p["name"] for p in projects}
        rows.insert(
            0,
            [
                InlineKeyboardButton(
                    f"Default ({_project_label(default, names)})",
                    callback_data=f"{_SUBSCRIBE_PREFIX}{_SUBSCRIBE_DEFAULT}:{msg_id}",
                ),
            ],
        )
    return InlineKeyboardMarkup(rows)


@_require_auth
async def cmd_subscribe(update: Update, context: CustomContext, user: User) -> None:
    if update.message is None or context.user_data is None:
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /subscribe <youtube link>\n\n"
            "Send a link to any video from the channel (or the channel's @handle) and "
            "I'll watch it for new uploads.",
        )
        return

    config = context.bot_data["config"]
    await update.message.reply_text("Looking up the channel...")
    try:
        resolved = await asyncio.to_thread(resolve_subscription, args[0], config.transcript.proxy)
    except SubscriptionError as e:
        await update.message.reply_text(str(e))
        return

    try:
        channels = load_channels(config.poller.channels_path)
    except PersistenceError as e:
        logger.exception("Failed to read the channel list")
        await update.message.reply_text(f"Couldn't read the channel list: {e}")
        return
    existing = find_subscription(channels, resolved)
    if existing is not None:
        await update.message.reply_text(
            f"Already watching {existing.name} ({existing.handle}). See /subscribed.",
        )
        return

    projects = await _projects_for_picker(context, update.message.reply_text)
    if projects is None:
        return

    msg_id = update.message.message_id
    context.user_data[f"subscribe_{msg_id}"] = resolved
    await update.message.reply_text(
        f"Found {resolved.name} ({resolved.handle}).\n\nWhere should its transcripts go?",
        reply_markup=_subscribe_keyboard(projects, msg_id, config),
    )


@_require_auth
async def handle_subscribe_selection(update: Update, context: CustomContext, user: User) -> None:
    query = update.callback_query
    if query is None or context.user_data is None or query.data is None:
        return

    await query.answer()

    parts = query.data.removeprefix(_SUBSCRIBE_PREFIX).split(":", 1)
    if len(parts) != 2:
        await query.edit_message_text("Invalid selection data.")
        return
    choice, msg_id_str = parts
    config = context.bot_data["config"]

    if choice == "more":
        try:
            projects = await asyncio.to_thread(context.bot_data["claude_client"].list_projects)
        except AuthError as e:
            await query.answer(str(e)[:200])
            return
        await query.edit_message_reply_markup(
            reply_markup=_subscribe_keyboard(projects, msg_id_str, config, show_all=True),
        )
        return

    resolved: ResolvedChannel | None = context.user_data.get(f"subscribe_{msg_id_str}")
    if resolved is None:
        await query.edit_message_text("Session expired. Please run /subscribe again.")
        return

    project = None if choice == _SUBSCRIBE_DEFAULT else choice
    try:
        added = await asyncio.to_thread(
            add_subscription,
            config.poller.channels_path,
            resolved,
            project,
        )
    except PersistenceError as e:
        logger.exception("Failed to add %s to the channel list", resolved.handle)
        await query.edit_message_text(f"Couldn't save the subscription: {e}")
        return

    context.user_data.pop(f"subscribe_{msg_id_str}", None)
    if added is None:
        # Someone (or something) added the same channel between the /subscribe check
        # and this tap — the end state is what the user wanted either way.
        await query.edit_message_text(f"Already watching {resolved.name}.")
        return

    logger.info(
        "Subscribed to %s (%s) -> project %s",
        added.name,
        added.channel_id,
        project or "default",
    )
    lines = [f"✅ Watching {added.name} ({added.handle})."]
    default = config.poller.auto_transcript_project
    if default is None:
        # Without it the poller task never starts at all (see main.py), so a per-channel
        # project of its own wouldn't get this channel uploaded either.
        lines.append("⚠️ AUTO_TRANSCRIPT_PROJECT is not set — auto-upload is off until it is.")
    else:
        names = await _project_names(context)
        lines.append(
            f"New videos go to {_project_label(project or default, names)}, "
            f"picked up within {_fmt_interval(config.poller.poll_interval)} and uploaded "
            "24h after publication.",
        )
    await query.edit_message_text("\n".join(lines))


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
            fetch_video_metadata,
            url,
            context.bot_data["config"].transcript.proxy,
        )
    except (RequestException, ValueError) as e:
        logger.exception("Failed to fetch metadata for %s", url)
        await update.message.reply_text(f"Failed to fetch video info: {e}")
        return

    projects = await _projects_for_picker(context, update.message.reply_text)
    if projects is None:
        return

    msg_id = update.message.message_id
    context.user_data[f"video_{msg_id}"] = metadata

    keyboard = _build_keyboard(
        projects,
        msg_id,
        context.bot_data["config"].telegram.project_whitelist,
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

    docs: list[Doc] | None
    try:
        docs = await asyncio.to_thread(
            context.bot_data["claude_client"].list_docs,
            project_id,
        )
    except AuthError:
        # Can't dedupe right now — fall through to fetch + service.upload, which will
        # hit the same AuthError again via its own internal list_docs(docs=None) call
        # and correctly return DeferredForAuth for the queuing logic below, instead of
        # dead-ending here before a transcript was ever fetched.
        docs = None
    except Exception as e:
        logger.exception("Failed to list docs for project %s", project_id)
        await query.edit_message_text(f"Failed to check for duplicates: {e}")
        return

    existing = (
        next((d for d in docs if d["file_name"] == file_name), None) if docs is not None else None
    )
    if existing:
        context.user_data[f"pending_{msg_id_str}"] = PendingUpload(
            project_id=project_id,
            file_name=file_name,
            video_id=metadata.video_id,
            channel_name=metadata.channel_name,
        )
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Skip", callback_data=f"skip:{msg_id_str}"),
                    InlineKeyboardButton(
                        "Overwrite",
                        callback_data=f"overwrite:{existing['uuid']}:{msg_id_str}",
                    ),
                ],
            ],
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
        if update.effective_chat is None:
            return
        pending_entry = PendingTranscript(
            chat_id=update.effective_chat.id,
            project_id=project_id,
            video_id=metadata.video_id,
            file_name=file_name,
            video_title=metadata.title,
            queued_at=datetime.now(UTC).isoformat(),
            channel_name=metadata.channel_name,
        )
        try:
            context.bot_data["pending_transcripts"].add(pending_entry)
        except PersistenceError:
            logger.exception("Failed to persist pending transcript for %s", file_name)
            await query.edit_message_text(
                "Transcript request was blocked and could not be queued for retry — "
                "please resend the URL shortly.",
            )
            return
        await query.edit_message_text(
            "Transcript request was blocked — this is usually temporary. It's been "
            "queued and will retry automatically.",
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
            record_upload(
                context.bot_data["config"].storage.data_dir,
                UploadRecord(
                    project_id=project_id,
                    file_name=file_name,
                    video_title=metadata.title,
                    channel_name=metadata.channel_name,
                    uploaded_at=datetime.now(UTC).isoformat(),
                ),
            )
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
                channel_name=metadata.channel_name,
            )
            try:
                added = context.bot_data["queue"].enqueue(entry)
            except PersistenceError:
                logger.exception("Failed to enqueue %s", file_name)
                await query.edit_message_text(
                    "Token expired and queuing failed — please resend the URL after "
                    "updating the token.",
                )
                return
            escaped = escape_markdown(file_name, version=1)
            if added:
                msg = f"Token expired — *{escaped}* queued. Run /refresh after updating the token."
            else:
                msg = f"Token expired — *{escaped}* was already queued."
            await query.edit_message_text(msg, parse_mode="Markdown")
        case RetryPending(step=step, error=error):
            logger.warning("Upload failed for %s while %s: %s", file_name, step, error)
            await query.edit_message_text(f"Upload failed while {step}: {error}")


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
        if update.effective_chat is None:
            return
        pending_entry = PendingTranscript(
            chat_id=update.effective_chat.id,
            project_id=pending["project_id"],
            video_id=pending["video_id"],
            file_name=pending["file_name"],
            video_title=pending["file_name"],
            queued_at=datetime.now(UTC).isoformat(),
            channel_name=pending["channel_name"],
            overwrite_doc_uuid=doc_uuid,
        )
        try:
            context.bot_data["pending_transcripts"].add(pending_entry)
        except PersistenceError:
            logger.exception("Failed to persist pending transcript for %s", pending["file_name"])
            await query.edit_message_text(
                "Transcript request was blocked and could not be queued for retry — "
                "please retry the overwrite shortly.",
            )
            return
        await query.edit_message_text(
            "Transcript request was blocked — this is usually temporary. It's been "
            "queued and will retry automatically.",
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
        channel_name=pending["channel_name"],
        overwrite_doc_uuid=doc_uuid,
    )
    queue: Queue = context.bot_data["queue"]
    escaped = escape_markdown(pending["file_name"], version=1)
    try:
        persisted = queue.enqueue(draft)
    except PersistenceError:
        logger.exception("Failed to durably queue overwrite for %s", pending["file_name"])
        await query.edit_message_text(
            "Overwrite failed to queue durably — please retry the overwrite.",
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
                record_upload(
                    context.bot_data["config"].storage.data_dir,
                    UploadRecord(
                        project_id=claimed.project_id,
                        file_name=claimed.file_name,
                        video_title=claimed.video_title,
                        channel_name=claimed.channel_name,
                        uploaded_at=datetime.now(UTC).isoformat(),
                    ),
                )
                await query.edit_message_text(
                    f"Saved *{escaped}* to project.",
                    parse_mode="Markdown",
                )
            case AlreadyExists():
                # Old doc already gone and a replacement already exists: a prior
                # attempt landed and we just never saw the confirmation.
                queue.ack(claimed.id)
                await query.edit_message_text(
                    f"*{escaped}* was already overwritten.",
                    parse_mode="Markdown",
                )
            case DeferredForAuth(step=step, error=error):
                queue.release(claimed.id)
                await query.edit_message_text(
                    f"Auth error while {step} — overwrite queued for retry: {error}",
                )
            case RetryPending(step=step, error=error):
                queue.release(claimed.id, increment_attempts=True)
                await query.edit_message_text(
                    f"Overwrite failed while {step}: {error}. It has been queued and will "
                    "retry automatically.",
                )
    except Exception:
        # Anything unexpected (a programming error, a malformed API response) must
        # still release the claim — otherwise the entry is stuck in_flight until the
        # next process restart's recover_abandoned(), instead of being retried by the
        # very next drain.
        logger.exception("Unexpected error during overwrite for %s", claimed.file_name)
        queue.release(claimed.id, increment_attempts=True)
        await query.edit_message_text(
            "Unexpected error during overwrite — it has been queued and will retry automatically.",
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
        config.claude.session_token,
        persist_path=config.storage.data_dir / "session_token.json",
        projects_persist_path=config.storage.data_dir / "projects_cache.json",
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
    app.bot_data["pending_transcripts"] = PendingTranscriptStore(
        path=config.storage.data_dir / "pending_transcripts.json",
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("inqueue", cmd_inqueue))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CommandHandler("version", cmd_version))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("subscribed", cmd_subscribed))
    app.add_handler(
        MessageHandler(filters.TEXT & filters.Regex(YOUTUBE_URL_PATTERN), handle_youtube_url),
    )
    # Both prefixed handlers must be registered before the catch-all project picker,
    # which matches any callback data.
    app.add_handler(CallbackQueryHandler(handle_duplicate_choice, pattern=r"^(skip|overwrite):"))
    app.add_handler(
        CallbackQueryHandler(handle_subscribe_selection, pattern=f"^{_SUBSCRIBE_PREFIX}"),
    )
    app.add_handler(CallbackQueryHandler(handle_project_selection))

    return app
