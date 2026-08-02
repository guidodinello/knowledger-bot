import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime
from functools import wraps
from typing import Any, TypedDict

from curl_cffi.requests.exceptions import RequestException
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update, User
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
from .config import Config, load_persisted_projects
from .history import UploadRecord, record_upload
from .logger import get_logger
from .pending_transcripts import (
    PendingTranscript,
    PendingTranscriptStore,
    token_expired_message,
)
from .persistence import PersistenceError
from .poller import UPLOAD_DELAY, PollerState, load_channels
from .queue import Queue, QueueEntry
from .queue_processor import DrainResult, QueueProcessor
from .subscriptions import (
    ResolvedChannel,
    SubscriptionError,
    add_subscription,
    find_subscription,
    resolve_subscription,
)
from .telegram_format import (
    NO_PREVIEW,
    PARSE_MODE,
    bold,
    cap_entries,
    cap_message,
    code,
    esc,
    fmt_age,
    fmt_ts,
    subject,
    video_link,
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
    video_title: str  # the real title; file_name is the derived doc name, not user-facing


CustomContext = CallbackContext[Any, dict, dict, BotData]

YOUTUBE_URL_PATTERN = r"https?://(?:(?:www|m)\.)?(?:youtube\.com/(?:watch|shorts/)|youtu\.be/)\S+"


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


# Shared error copy. These conditions surface from several handlers, and the recovery
# step is identical every time — stating it two different ways would be its own defect.
# The underlying exception is never shown: every site below already logs it, and a raw
# curl/HTTP string tells the reader nothing they can act on (UH33).
_AUTH_EXPIRED = (
    "Your Claude session token has expired, so I can't reach your projects. "
    "Update the token, then run /refresh."
)
_PROJECTS_UNREACHABLE = "Couldn't reach Claude to load your projects. Try again in a moment."
# Two variants rather than one shared string: the recovery step differs by flow, and
# telling someone in the subscribe flow to "send the link again" sends them somewhere
# that won't finish what they started.
_STALE_UPLOAD_SESSION = "That button belongs to an older message. Send the link again."
_STALE_SUBSCRIBE_SESSION = "That button belongs to an older message. Run /subscribe again."
_STALE_LIST_WARNING = (
    "⚠️ Your session token has expired, so this is the last project list I saw. "
    "Projects created since then won't be here."
)


async def _projects_for_picker(
    context: CustomContext,
    reply: Callable[[str], Awaitable[Any]],
) -> tuple[list[Project], str | None] | None:
    """The projects to offer in an inline picker, plus a caveat to show above it (None
    when the list is fresh). Returns None — after explaining why through `reply` — when
    there is nothing to show at all.

    The caveat is returned rather than sent here on purpose: callers render the picker
    by *editing* their status message, so a warning emitted as its own edit would be
    overwritten by the picker a moment later and the user would never see it."""
    try:
        projects = await asyncio.to_thread(context.bot_data["claude_client"].list_projects)
    except AuthError:
        try:
            projects = load_persisted_projects(context.bot_data["config"].storage.data_dir)
        except PersistenceError:
            logger.exception("Failed to load persisted project list")
            await reply(_AUTH_EXPIRED)
            return None
        if not projects:
            await reply(_AUTH_EXPIRED)
            return None
        return projects, _STALE_LIST_WARNING
    except RequestException:
        logger.exception("Failed to load project list for picker")
        await reply(_PROJECTS_UNREACHABLE)
        return None
    if not projects:
        await reply("No projects found. Run /refresh to reload them from Claude.")
        return None
    return projects, None


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


# Single source of truth for the command list: both the /help text and Telegram's
# native command menu are derived from it, so the two can never drift. `usage` is the
# argument hint shown in /help only — set_my_commands takes the bare name.
_COMMANDS: list[tuple[str, str, str]] = [
    ("subscribe", "<link>", "watch a channel for new uploads"),
    ("subscribed", "", "list watched channels"),
    ("inqueue", "", "what's waiting to upload"),
    ("refresh", "", "reload the project list and retry the queue"),
    ("version", "", "show the running build"),
    ("help", "", "show this message"),
]


def _help_text() -> str:
    lines = ["Send me a YouTube URL and I'll save its transcript to a Claude project.", ""]
    for name, usage, description in _COMMANDS:
        command = f"/{name} {usage}".rstrip()
        lines.append(f"{esc(command)} — {esc(description)}")
    return "\n".join(lines)


@_require_auth
async def cmd_start(update: Update, context: CustomContext, user: User) -> None:
    if update.message is None:
        return
    await update.message.reply_text(_help_text(), parse_mode=PARSE_MODE)


@_require_auth
async def cmd_help(update: Update, context: CustomContext, user: User) -> None:
    await cmd_start(update, context)


@_require_auth
async def cmd_version(update: Update, context: CustomContext, user: User) -> None:
    if update.message is None:
        return
    version = context.bot_data["config"].version
    if version.commit_sha is None:
        await update.message.reply_text(
            "This build wasn't stamped with a commit — it was most likely run from a "
            "working copy rather than a release image.",
        )
        return
    line = f"Running {code(version.commit_sha)}"
    if version.commit_date:
        age = fmt_age(version.commit_date)
        committed = fmt_ts(version.commit_date)
        line += f", committed {esc(committed)}"
        if age:
            line += f" ({esc(age)})"
    await update.message.reply_text(line + ".", parse_mode=PARSE_MODE)


@_require_auth
async def cmd_refresh(update: Update, context: CustomContext, user: User) -> None:
    if update.message is None:
        return
    # One status message, edited in place as the work progresses, rather than a new
    # message per stage — otherwise the chat accumulates dead "Refreshing..." lines.
    status = await update.message.reply_text("Reloading your projects…")
    client = context.bot_data["claude_client"]
    client.invalidate_projects()
    try:
        projects = await asyncio.to_thread(client.list_projects)
    except AuthError:
        await status.edit_text(_AUTH_EXPIRED)
        return
    except Exception:
        logger.exception("Failed to refresh project list")
        await status.edit_text(
            "Couldn't reach Claude to reload your projects. Try /refresh again in a moment.",
        )
        return
    await status.edit_text(f"{len(projects)} project(s) loaded. Retrying the queue…")

    try:
        result = await context.bot_data["queue_processor"].drain(
            context.application,
            context.bot_data["config"],
            client,
        )
    except Exception:
        logger.exception("Queue drain failed")
        await status.edit_text(
            f"{len(projects)} project(s) loaded, but the queued uploads couldn't be "
            "retried. They're still queued — run /refresh again in a moment.",
        )
        return
    if result.uploaded or result.already_existed or result.failed_auth or result.failed_other:
        parts = [f"{result.uploaded} queued upload(s) saved"]
        if result.already_existed:
            parts.append(f"{result.already_existed} already existed")
        if result.failed_auth:
            parts.append(f"{result.failed_auth} still waiting on a valid token")
        if result.failed_other:
            parts.append(f"{result.failed_other} still failing — see /inqueue")
        await status.edit_text(f"{len(projects)} project(s) loaded. " + ", ".join(parts) + ".")
    else:
        await status.edit_text(f"{len(projects)} project(s) loaded. Nothing was queued.")


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


_INQUEUE_MAX_ENTRIES = 10  # per-section cap before a "+N more" trailer
_STUCK_MARKER = "🛑 "  # prefix for entries with upload_attempts > 0; matches the stuck alert


@_require_auth
async def cmd_inqueue(update: Update, context: CustomContext, user: User) -> None:
    if update.message is None:
        return

    sections: list[list[str]] = []  # one block per non-empty subsystem
    footers: list[str] = []  # standing facts that are true even when nothing is queued
    problems: list[str] = []  # subsystems whose state we couldn't read at all

    # 🔁 Retry queue — QueueEntry: has upload_attempts and is the only section /refresh drains.
    queue: Queue = context.bot_data["queue"]
    retry_entries = queue.peek()
    if retry_entries:
        block = [f"🔁 {bold(f'Retry queue — {len(retry_entries)} queued')}"]
        entry_lines = []
        for e in retry_entries:
            marker = _STUCK_MARKER if e.upload_attempts else ""
            title = video_link(e.video_title, e.video_id)
            attempts = f" — {e.upload_attempts} failed attempts" if e.upload_attempts else ""
            detail = f"  queued {fmt_ts(e.queued_at)}"
            if e.upload_attempts:
                detail += " — run /refresh to retry"
            entry_lines.append([f"• {marker}{title}{attempts}", detail])
        block.extend(cap_entries(entry_lines, _INQUEUE_MAX_ENTRIES))
        sections.append(block)

    # ⏳ Poller pending — PendingVideo: has upload_attempts and channel_name, no /refresh.
    state_path = context.bot_data["config"].storage.data_dir / "poller_state.json"
    try:
        state = PollerState.load(state_path)
    except PersistenceError:
        logger.exception("Failed to read poller state")
        problems.append("⏳ Couldn't read the poller's state — check the logs.")
    else:
        if state.pending:
            header = f"Poller — {len(state.pending)} waiting to upload"
            block = [f"⏳ {bold(header)}"]
            entry_lines = []
            for v in state.pending:
                marker = _STUCK_MARKER if v.upload_attempts else ""
                title = video_link(v.title, v.video_id)
                attempts = f" — {v.upload_attempts} failed attempts" if v.upload_attempts else ""
                detail = f"  seen {fmt_ts(v.first_seen)}"
                entry_lines.append([f"• {marker}{title} — {esc(v.channel_name)}{attempts}", detail])
            block.extend(cap_entries(entry_lines, _INQUEUE_MAX_ENTRIES))
            sections.append(block)
        if state.seen:
            footers.append(f"{len(state.seen)} videos seen since the poller started.")

    # 📥 Blocked transcripts — PendingTranscript: has channel_name, no upload_attempts, no
    # /refresh hint (drained automatically by the periodic retrier, not by /refresh).
    try:
        pending_transcripts = context.bot_data["pending_transcripts"].load()
    except PersistenceError:
        logger.exception("Failed to read pending transcripts")
        problems.append("📥 Couldn't read the blocked-transcript list — check the logs.")
    else:
        if pending_transcripts:
            header = f"Blocked transcripts — {len(pending_transcripts)}"
            block = [f"📥 {bold(header)}", "Retrying automatically; nothing to do."]
            block.extend(
                cap_entries(
                    [
                        [
                            f"• {video_link(t.video_title, t.video_id)} — {esc(t.channel_name)}",
                            f"  queued {fmt_ts(t.queued_at)}",
                        ]
                        for t in pending_transcripts
                    ],
                    _INQUEUE_MAX_ENTRIES,
                ),
            )
            sections.append(block)

    # Empty state (UH35): say the useful thing once instead of printing three headers
    # whose only content is the word "empty".
    if not sections and not problems:
        lines = ["✅ Nothing queued — everything's up to date."]
    else:
        lines = ["📊 " + bold("Queue status")]
        for block in [*sections, *([p] for p in problems)]:
            lines.append("")  # one blank line between blocks, never two
            lines.extend(block)
    if footers:
        lines.append("")
        lines.extend(footers)

    await update.message.reply_text(
        cap_message("\n".join(lines).strip()),
        parse_mode=PARSE_MODE,
        link_preview_options=NO_PREVIEW,
    )


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
    except (AuthError, RequestException):
        logger.warning("Could not load the live project list for display", exc_info=True)
        try:
            projects = load_persisted_projects(context.bot_data["config"].storage.data_dir)
        except PersistenceError:
            logger.exception("Could not load the cached project list for display")
            projects = None
    return {p["uuid"]: p["name"] for p in projects or []}


def _fmt_interval(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes // 60}h" if minutes >= 60 and minutes % 60 == 0 else f"{minutes} min"


def _fmt_upload_delay() -> str:
    """The poller's UPLOAD_DELAY is the authoritative definition — never restate it as a
    literal in user-facing copy, or the bot starts lying when the delay changes."""
    return _fmt_interval(UPLOAD_DELAY.total_seconds())


@_require_auth
async def cmd_subscribed(update: Update, context: CustomContext, user: User) -> None:
    if update.message is None:
        return

    config = context.bot_data["config"]
    try:
        channels = load_channels(config.poller.channels_path)
    except PersistenceError:
        logger.exception("Failed to read the channel list")
        await update.message.reply_text(
            "Couldn't read the list of watched channels — check the logs.",
        )
        return

    if not channels:
        await update.message.reply_text(
            "Not watching any channels yet.\n\n"
            "Add one with /subscribe followed by a link to any of its videos.",
        )
        return

    names = await _project_names(context)
    default = config.poller.auto_transcript_project

    lines = [f"📺 {bold(f'Watching {len(channels)} channel(s)')}", ""]
    for ch in channels:
        lines.append(f"• {esc(ch.name)} ({esc(ch.handle)})")
        if ch.project:
            lines.append(f"  → {esc(_project_label(ch.project, names))}")
        elif default:
            lines.append(f"  → {esc(_project_label(default, names))} (default)")
        else:
            lines.append("  → no project")

    lines.append("")
    if default:
        lines.append(
            f"Checked every {_fmt_interval(config.poller.poll_interval)}; "
            f"transcripts upload {_fmt_upload_delay()} after a video is published.",
        )
    else:
        lines.append(
            "⚠️ Auto-upload is off: no default project is configured "
            f"({code('AUTO_TRANSCRIPT_PROJECT')} is unset), so nothing here is being "
            "picked up.",
        )

    await update.message.reply_text(cap_message("\n".join(lines)), parse_mode=PARSE_MODE)


def _with_subscribe_default(
    keyboard: InlineKeyboardMarkup,
    projects: list[Project],
    msg_id: int | str,
    config: Config,
) -> InlineKeyboardMarkup:
    """Top a subscribe-flow project keyboard with the global-default choice."""
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


def _subscribe_keyboard(
    projects: list[Project],
    msg_id: int | str,
    config: Config,
) -> InlineKeyboardMarkup:
    """Whitelist-filtered subscribe picker, including the global-default choice."""
    keyboard = _build_keyboard(
        projects,
        msg_id,
        config.telegram.project_whitelist,
        _SUBSCRIBE_PREFIX,
    )
    return _with_subscribe_default(keyboard, projects, msg_id, config)


def _all_subscribe_keyboard(
    projects: list[Project],
    msg_id: int | str,
    config: Config,
) -> InlineKeyboardMarkup:
    """Unfiltered subscribe picker shown after the user taps “More…”."""
    keyboard = _keyboard_for(projects, msg_id, _SUBSCRIBE_PREFIX)
    return _with_subscribe_default(keyboard, projects, msg_id, config)


@_require_auth
async def cmd_subscribe(update: Update, context: CustomContext, user: User) -> None:
    if update.message is None or context.user_data is None:
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Send a link to any video from the channel — or the channel's @handle — and "
            "I'll watch it for new uploads.\n\n"
            "For example: /subscribe @veritasium",
        )
        return

    config = context.bot_data["config"]
    # One status message, edited in place through the lookup and into the picker, so the
    # chat doesn't accumulate a dead "Looking up..." line on every subscribe.
    status = await update.message.reply_text("Looking up the channel…")
    try:
        resolved = await asyncio.to_thread(resolve_subscription, args[0], config.transcript.proxy)
    except SubscriptionError as e:
        # SubscriptionError messages are written as user-facing copy (see subscriptions.py).
        await status.edit_text(esc(str(e)), parse_mode=PARSE_MODE)
        return

    try:
        channels = load_channels(config.poller.channels_path)
    except PersistenceError:
        logger.exception("Failed to read the channel list")
        await status.edit_text("Couldn't read the list of watched channels — check the logs.")
        return
    existing = find_subscription(channels, resolved)
    if existing is not None:
        await status.edit_text(
            f"Already watching {bold(existing.name)} ({esc(existing.handle)}).\n"
            "See /subscribed for where its transcripts go.",
            parse_mode=PARSE_MODE,
        )
        return

    picker = await _projects_for_picker(context, status.edit_text)
    if picker is None:
        return
    projects, warning = picker

    msg_id = update.message.message_id
    context.user_data[f"subscribe_{msg_id}"] = resolved
    prefix = f"{esc(warning)}\n\n" if warning else ""
    await status.edit_text(
        f"{prefix}Found {bold(resolved.name)} ({esc(resolved.handle)}).\n\n"
        "Where should its transcripts go?",
        parse_mode=PARSE_MODE,
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
        await query.edit_message_text(_STALE_SUBSCRIBE_SESSION)
        return
    choice, msg_id_str = parts
    config = context.bot_data["config"]

    if choice == "more":
        try:
            projects = await asyncio.to_thread(context.bot_data["claude_client"].list_projects)
        except AuthError:
            # Not query.answer(): the handler already answered above, and Telegram
            # delivers only the first answer — a second one is silently dropped, so a
            # dead-token tap would appear to do nothing. Report in the message instead.
            await query.edit_message_text(_AUTH_EXPIRED)
            return
        except RequestException:
            logger.exception("Failed to load full project list for subscribe picker")
            await query.edit_message_text(_PROJECTS_UNREACHABLE)
            return
        await query.edit_message_reply_markup(
            reply_markup=_all_subscribe_keyboard(projects, msg_id_str, config),
        )
        return

    resolved: ResolvedChannel | None = context.user_data.get(f"subscribe_{msg_id_str}")
    if resolved is None:
        await query.edit_message_text(_STALE_SUBSCRIBE_SESSION)
        return

    project = None if choice == _SUBSCRIBE_DEFAULT else choice
    try:
        added = await asyncio.to_thread(
            add_subscription,
            config.poller.channels_path,
            resolved,
            project,
        )
    except PersistenceError:
        logger.exception("Failed to add %s to the channel list", resolved.handle)
        await query.edit_message_text(
            "Couldn't save the subscription — check the logs, then try /subscribe again.",
        )
        return

    context.user_data.pop(f"subscribe_{msg_id_str}", None)
    if added is None:
        # Someone (or something) added the same channel between the /subscribe check
        # and this tap — the end state is what the user wanted either way.
        await query.edit_message_text(
            f"Already watching {bold(resolved.name)}.",
            parse_mode=PARSE_MODE,
        )
        return

    logger.info(
        "Subscribed to %s (%s) -> project %s",
        added.name,
        added.channel_id,
        project or "default",
    )
    lines = [f"✅ Watching {bold(added.name)} ({esc(added.handle)})."]
    default = config.poller.auto_transcript_project
    if default is None:
        # Without it the poller task never starts at all (see main.py), so a per-channel
        # project of its own wouldn't get this channel uploaded either.
        lines.append(
            f"⚠️ Nothing will upload yet: {code('AUTO_TRANSCRIPT_PROJECT')} is unset, which "
            "keeps the poller switched off entirely.",
        )
    else:
        names = await _project_names(context)
        lines.append(
            f"New videos go to {bold(_project_label(project or default, names))}, "
            f"picked up within {_fmt_interval(config.poller.poll_interval)} and uploaded "
            f"{_fmt_upload_delay()} after publication.",
        )
    await query.edit_message_text("\n".join(lines), parse_mode=PARSE_MODE)


@_require_auth
async def handle_youtube_url(update: Update, context: CustomContext, user: User) -> None:
    if update.message is None or update.message.text is None or context.user_data is None:
        return

    url = update.message.text.strip()
    if not extract_video_id(url):
        await update.message.reply_text(
            "I couldn't find a video ID in that link. Send a normal youtube.com/watch, "
            "youtu.be or /shorts link.",
        )
        return

    logger.info("New request from user %s: %s", user.id, url)
    # This status message becomes the project picker below — one message for the whole
    # interaction, instead of leaving a stale "Fetching..." line behind in the chat.
    status = await update.message.reply_text("Reading the video's details…")

    try:
        metadata = await asyncio.to_thread(
            fetch_video_metadata,
            url,
            context.bot_data["config"].transcript.proxy,
        )
    except (RequestException, ValueError):
        logger.exception("Failed to fetch metadata for %s", url)
        await status.edit_text(
            "Couldn't read that video's details. It may be private, deleted or "
            "region-locked — check the link and try again.",
        )
        return

    picker = await _projects_for_picker(context, status.edit_text)
    if picker is None:
        return
    projects, warning = picker

    msg_id = update.message.message_id
    context.user_data[f"video_{msg_id}"] = metadata

    keyboard = _build_keyboard(
        projects,
        msg_id,
        context.bot_data["config"].telegram.project_whitelist,
    )

    prefix = f"{esc(warning)}\n\n" if warning else ""
    await status.edit_text(
        f"{prefix}{bold(metadata.title)}\n{esc(metadata.channel_name)}\n\nWhich project?",
        parse_mode=PARSE_MODE,
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
        await query.edit_message_text(_STALE_UPLOAD_SESSION)
        return

    project_id, msg_id_str = parts

    match project_id:
        case "more":
            try:
                projects = await asyncio.to_thread(context.bot_data["claude_client"].list_projects)
            except AuthError:
                await query.edit_message_text(_AUTH_EXPIRED)
                return
            except RequestException:
                logger.exception("Failed to load full project list for upload picker")
                await query.edit_message_text(_PROJECTS_UNREACHABLE)
                return
            keyboard = _keyboard_for(projects, msg_id_str)
            await query.edit_message_reply_markup(reply_markup=keyboard)
            return

    metadata: VideoMetadata | None = context.user_data.get(f"video_{msg_id_str}")

    if metadata is None:
        await query.edit_message_text(_STALE_UPLOAD_SESSION)
        return

    file_name = build_doc_name(metadata.channel_name, metadata.title, metadata.upload_date)
    project_name = _project_label(project_id, await _project_names(context))
    subject_line = subject(metadata.title, metadata.channel_name, metadata.video_id)

    await query.edit_message_text("Checking the project for a copy…")

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
    except Exception:
        logger.exception("Failed to list docs for project %s", project_id)
        await query.edit_message_text(
            "Couldn't reach Claude to check whether this is already saved. "
            "Send the link again in a moment.",
        )
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
            video_title=metadata.title,
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
        await query.edit_message_text(
            f"⚠️ Already in {bold(project_name)}\n{subject_line}\n\n"
            "Overwrite it with a fresh transcript, or skip?",
            parse_mode=PARSE_MODE,
            reply_markup=keyboard,
            link_preview_options=NO_PREVIEW,
        )
        return

    await query.edit_message_text("Fetching the transcript…")

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
            f"{subject_line}\n\nThis video has no captions, so there's nothing to save.",
            parse_mode=PARSE_MODE,
            link_preview_options=NO_PREVIEW,
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
                "YouTube blocked the transcript request, and I couldn't queue a retry. "
                "Send the link again in a few minutes.",
            )
            return
        await query.edit_message_text(
            f"⏳ Queued for {bold(project_name)}\n{subject_line}\n\n"
            "YouTube blocked the transcript request — usually temporary. "
            "I'll keep retrying and confirm when it lands.",
            parse_mode=PARSE_MODE,
            link_preview_options=NO_PREVIEW,
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
                    video_id=metadata.video_id,
                ),
            )
            await query.edit_message_text(
                f"✅ Saved to {bold(project_name)}\n{subject_line}",
                parse_mode=PARSE_MODE,
                link_preview_options=NO_PREVIEW,
            )
        case AlreadyExists():
            # Rare race: a doc with this name appeared between the check above and
            # this upload attempt. Same "nothing to do" outcome as the check finding
            # it up front.
            await query.edit_message_text(
                f"Already in {bold(project_name)}\n{subject_line}\n\nNothing uploaded.",
                parse_mode=PARSE_MODE,
                link_preview_options=NO_PREVIEW,
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
                    "Your session token has expired and I couldn't hold the transcript "
                    "for later. Update the token and send the link again.",
                )
                return
            await query.edit_message_text(
                token_expired_message(subject_line, newly_queued=added is not None),
                parse_mode=PARSE_MODE,
                link_preview_options=NO_PREVIEW,
            )
        case RetryPending(step=step, error=error):
            logger.warning("Upload failed for %s while %s: %s", file_name, step, error)
            await query.edit_message_text(
                f"Couldn't save this to {bold(project_name)}.\n{subject_line}\n\n"
                "Nothing was queued — send the link again to retry.",
                parse_mode=PARSE_MODE,
                link_preview_options=NO_PREVIEW,
            )


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
            await query.edit_message_text(_STALE_UPLOAD_SESSION)
            return
        msg_id_str = rest[0]
        context.user_data.pop(f"video_{msg_id_str}", None)
        context.user_data.pop(f"pending_{msg_id_str}", None)
        await query.edit_message_text("Skipped — the existing copy was left alone.")
        return

    if len(rest) < 2:
        await query.edit_message_text(_STALE_UPLOAD_SESSION)
        return
    doc_uuid, msg_id_str = rest[0], rest[1]
    pending: PendingUpload | None = context.user_data.get(f"pending_{msg_id_str}")
    if pending is None:
        await query.edit_message_text(_STALE_UPLOAD_SESSION)
        return

    subject_line = subject(pending["video_title"], pending["channel_name"], pending["video_id"])
    project_name = _project_label(pending["project_id"], await _project_names(context))

    await query.edit_message_text("Fetching the transcript…")

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
            f"{subject_line}\n\nThis video has no captions, so there's nothing to "
            "overwrite the existing copy with.",
            parse_mode=PARSE_MODE,
            link_preview_options=NO_PREVIEW,
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
            video_title=pending["video_title"],
            queued_at=datetime.now(UTC).isoformat(),
            channel_name=pending["channel_name"],
            overwrite_doc_uuid=doc_uuid,
        )
        try:
            context.bot_data["pending_transcripts"].add(pending_entry)
        except PersistenceError:
            logger.exception("Failed to persist pending transcript for %s", pending["file_name"])
            await query.edit_message_text(
                "YouTube blocked the transcript request, and I couldn't queue a retry. "
                "Send the link again in a few minutes.",
            )
            return
        await query.edit_message_text(
            f"⏳ Overwrite queued for {bold(project_name)}\n{subject_line}\n\n"
            "YouTube blocked the transcript request — usually temporary. "
            "I'll keep retrying and confirm when it lands.",
            parse_mode=PARSE_MODE,
            link_preview_options=NO_PREVIEW,
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
        video_title=pending["video_title"],
        queued_at=datetime.now(UTC).isoformat(),
        channel_name=pending["channel_name"],
        overwrite_doc_uuid=doc_uuid,
    )
    queue: Queue = context.bot_data["queue"]
    try:
        persisted = queue.enqueue(draft)
    except PersistenceError:
        logger.exception("Failed to durably queue overwrite for %s", pending["file_name"])
        await query.edit_message_text(
            "I couldn't record the overwrite safely, so I stopped before touching the "
            "existing copy. It's untouched — send the link again to retry.",
        )
        return
    if persisted is None:
        await query.edit_message_text(
            f"{subject_line}\n\nThis overwrite is already queued — run /refresh once "
            "your token is valid again.",
            parse_mode=PARSE_MODE,
            link_preview_options=NO_PREVIEW,
        )
        return

    claimed = queue.claim_by_id(persisted.id)
    if claimed is None:
        # A concurrent /refresh or token-update drain already claimed it — it will
        # complete there; nothing left for this handler to do.
        await query.edit_message_text(
            f"{subject_line}\n\nThis overwrite is already running — I'll confirm shortly.",
            parse_mode=PARSE_MODE,
            link_preview_options=NO_PREVIEW,
        )
        return

    logger.info("Overwriting %s in project %s", pending["file_name"], pending["project_id"])
    await query.edit_message_text("Replacing the existing copy…")

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
                        video_id=claimed.video_id,
                    ),
                )
                await query.edit_message_text(
                    f"✅ Replaced in {bold(project_name)}\n{subject_line}",
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_PREVIEW,
                )
            case AlreadyExists():
                # Old doc already gone and a replacement already exists: a prior
                # attempt landed and we just never saw the confirmation.
                queue.ack(claimed.id)
                await query.edit_message_text(
                    f"✅ Already replaced in {bold(project_name)}\n{subject_line}",
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_PREVIEW,
                )
            case DeferredForAuth():
                queue.release(claimed.id)
                await query.edit_message_text(
                    f"⏳ Waiting on a valid token\n{subject_line}\n\n"
                    "The replacement is queued. Update your Claude session token, "
                    "then run /refresh.",
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_PREVIEW,
                )
            case RetryPending(step=step, error=error):
                logger.warning(
                    "Overwrite failed for %s while %s: %s",
                    claimed.file_name,
                    step,
                    error,
                )
                queue.release(claimed.id, increment_attempts=True)
                await query.edit_message_text(
                    f"⏳ Couldn't replace it just now\n{subject_line}\n\n"
                    "It's queued and will retry automatically.",
                    parse_mode=PARSE_MODE,
                    link_preview_options=NO_PREVIEW,
                )
    except Exception:
        # Anything unexpected (a programming error, a malformed API response) must
        # still release the claim — otherwise the entry is stuck in_flight until the
        # next process restart's recover_abandoned(), instead of being retried by the
        # very next drain.
        logger.exception("Unexpected error during overwrite for %s", claimed.file_name)
        queue.release(claimed.id, increment_attempts=True)
        await query.edit_message_text(
            "Something went wrong while replacing it. The replacement is queued and "
            "will retry automatically.",
        )
    finally:
        # The durable QueueEntry (not this dict) now owns retry state regardless of
        # outcome, so this Telegram-interaction bookkeeping is stale the moment we
        # reach here on any exit path — success, failure, or unexpected exception.
        context.user_data.pop(f"video_{msg_id_str}", None)
        context.user_data.pop(f"pending_{msg_id_str}", None)


async def _register_commands(app: Application) -> None:
    """Populate Telegram's native command menu, so the commands are discoverable and
    autocompleted in the client instead of only existing in the /help text.

    Best-effort: a failure here is cosmetic, and must not stop the bot from starting."""
    try:
        await app.bot.set_my_commands(
            [BotCommand(name, description) for name, _usage, description in _COMMANDS],
        )
    except Exception:
        logger.warning("Could not register the Telegram command menu", exc_info=True)


def build_application(config: Config) -> Application:
    app = (
        Application.builder()
        .token(config.telegram.bot_token)
        .context_types(ContextTypes(bot_data=BotData))
        .post_init(_register_commands)
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
