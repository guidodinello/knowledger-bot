"""Formatting primitives for every Telegram message the bot sends.

This module owns *how* text is escaped, linked, and capped — never *what* it says.
Copy stays at its call site (see the Locality-of-Behavior guideline in CLAUDE.md);
centralising it here would only create a catch-all that readers have to chase.

Why HTML rather than Telegram's legacy Markdown: the escaping burden under Markdown
falls on every individual interpolation, and forgetting one makes Telegram reject the
whole message with `BadRequest: Can't parse entities` — the bot then silently replies
nothing. That has already happened twice (docs/bugs/unescaped-markdown-injection.md,
docs/bugs/inqueue-markdown-italics.md). HTML escaping is a single well-defined
transform, and it is the only parse mode that supports links.
"""

import html
import re
from datetime import UTC, datetime

from telegram import LinkPreviewOptions

PARSE_MODE = "HTML"

# Now that titles are links, Telegram would attach a preview card for the first one in
# every message — a large thumbnail under a queue listing or a weekly digest, for a video
# the user didn't ask to preview. Pass this to every send that can contain a link.
# (`disable_web_page_preview=True` is the older spelling of the same thing; this is the
# form the current Bot API uses.)
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
_TRUNCATION_NOTE = "\n… (truncated)"

TITLE_LIMIT = 70  # video titles are frequently 100+ chars and wrap into a wall of text

_TAG_RE = re.compile(r"<[^>]*>")
_TAG_NAME_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)")


def esc(text: str) -> str:
    """Escape text for interpolation into an HTML-parse-mode message.

    `quote=False`: Telegram only cares about `<`, `>` and `&` in text nodes, and
    escaping quotes would put visible `&quot;` in ordinary prose. Attribute values go
    through `link()`, which escapes quotes explicitly."""
    return html.escape(text, quote=False)


def bold(text: str) -> str:
    return f"<b>{esc(text)}</b>"


def italic(text: str) -> str:
    return f"<i>{esc(text)}</i>"


def code(text: str) -> str:
    return f"<code>{esc(text)}</code>"


def link(text: str, url: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{esc(text)}</a>'


def blockquote(lines: list[str]) -> list[str]:
    """Wrap already-formatted lines in Telegram's blockquote, which draws them indented
    behind a vertical rule.

    This is the only real hierarchy lever the Bot API offers: HTML parse mode has no
    font sizes, so a section header can't be made *bigger* than its entries — it can
    only be made structurally superior to them by subordinating what follows. Bold
    alone loses to a blue link one line below it.

    Takes and returns whole lines because callers assemble messages line by line and
    `cap_message` truncates the same way; the open tag therefore has to live on the
    first line and the close on the last, never on lines of their own."""
    if not lines:
        return []
    opened = [f"<blockquote>{lines[0]}", *lines[1:]]
    return [*opened[:-1], f"{opened[-1]}</blockquote>"]


def video_link(title: str, video_id: str | None, *, limit: int = TITLE_LIMIT) -> str:
    """A video title, tappable when we know which video it is. `video_id` is None for
    upload-history records written before the field existed — those degrade to plain
    text rather than to a broken link."""
    shown = truncate(title, limit)
    if video_id is None:
        return esc(shown)
    return link(shown, f"https://youtu.be/{video_id}")


def subject(title: str, channel_name: str | None, video_id: str | None) -> str:
    """The bot's core noun, rendered the same way everywhere: a linked title followed by
    its channel.

    Every user-facing mention of a video goes through this. The alternative — showing
    `build_doc_name`'s output — leaks an internal identifier (`Youtube - CHANNEL -
    Title - 2026-07-30`) that the user never chose and can't act on."""
    line = video_link(title, video_id)
    if channel_name:
        line += f" — {esc(channel_name)}"
    return line


def truncate(text: str, limit: int = TITLE_LIMIT) -> str:
    """Shorten raw text. Must be applied BEFORE esc()/link() — cutting escaped text
    could slice an entity like `&amp;` in half."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _strip_tags(markup: str) -> str:
    """Reduce a fragment of our own markup back to raw plain text."""
    return html.unescape(_TAG_RE.sub("", markup))


def _unclosed_tags(markup: str) -> str:
    """The closing tags needed to make `markup` well-formed again, outermost last.

    Line-oriented truncation is safe only while every tag opens and closes on the same
    line. `blockquote()` breaks that — it spans a whole section — so dropping trailing
    lines can strand an open `<blockquote>`, and Telegram rejects the *entire* message
    for one unbalanced tag. Rather than forbid multi-line markup, close whatever is
    still open at the cut.

    Only ever sees markup this module produced, so a plain stack is enough: no void
    elements, no attributes containing `<`, no interleaved tags."""
    stack: list[str] = []
    for match in _TAG_NAME_RE.finditer(markup):
        is_closing, name = match.group(1), match.group(2).lower()
        if not is_closing:
            stack.append(name)
        elif name in stack:
            # Discard everything opened inside the tag being closed; unbalanced input
            # would otherwise leave phantom entries pinned at the bottom of the stack.
            while stack.pop() != name:
                pass
    return "".join(f"</{name}>" for name in reversed(stack))


def _escape_prefix(text: str, budget: int) -> str:
    """Escape as much raw text as fits without ever splitting an HTML entity."""
    parts: list[str] = []
    used = 0
    for char in text:
        escaped = esc(char)
        if used + len(escaped) > budget:
            break
        parts.append(escaped)
        used += len(escaped)
    return "".join(parts)


def cap_plain_message(text: str) -> str:
    """Fit unparsed text inside Telegram's limit without HTML transformations."""
    if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
        return text
    budget = TELEGRAM_MAX_MESSAGE_LENGTH - len(_TRUNCATION_NOTE)
    return text[:budget] + _TRUNCATION_NOTE


def cap_message(text: str) -> str:
    """Fit an HTML message inside Telegram's hard 4096-character limit.

    Drops whole lines from the end rather than slicing at a byte offset: under HTML a
    cut through `<a href="...">` makes Telegram reject the *entire* message, so a
    naive slice trades "truncated but readable" for "nothing arrives at all". Any tag
    still open at the cut (a multi-line `blockquote`) is closed rather than stranded,
    for the same reason."""
    if len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
        return text

    budget = TELEGRAM_MAX_MESSAGE_LENGTH - len(_TRUNCATION_NOTE)
    kept: list[str] = []
    used = 0
    for line in text.split("\n"):
        extra = len(line) + (1 if kept else 0)
        if used + extra > budget:
            break
        kept.append(line)
        used += extra

    # The closing tags are themselves part of the message, so they have to come out of
    # the same budget — drop further lines until both fit.
    while kept:
        body = "\n".join(kept)
        closers = _unclosed_tags(body)
        if len(body) + len(closers) <= budget:
            return body + closers + _TRUNCATION_NOTE
        kept.pop()

    # Degenerate case: even the first line overflows on its own. Strip our tags, then
    # escape raw characters into the remaining budget so no entity is severed.
    return _escape_prefix(_strip_tags(text), budget) + _TRUNCATION_NOTE


def fmt_ts(iso: str) -> str:
    """Absolute 'YYYY-MM-DD HH:MM' slice of an ISO-8601 string. Absolute, not
    relative — a relative "2h ago" goes stale/misleading if the message sits unread."""
    return iso[:16].replace("T", " ")


def fmt_age(iso: str) -> str:
    """Relative age, for the one place the question genuinely is "how old is this":
    /version, which is answered on demand and read immediately. Everything else uses
    fmt_ts — see its docstring."""
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - then
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return "yesterday" if days == 1 else f"{days} days ago"


def cap_entries(entry_lines: list[list[str]], max_entries: int) -> list[str]:
    """Flatten up to max_entries formatted entries (each a list of its own lines: a
    bullet line plus any indented detail lines) and append a '+N more' trailer instead
    of hard-truncating mid-bullet."""
    capped = entry_lines[:max_entries]
    lines = [line for entry in capped for line in entry]
    remaining = len(entry_lines) - len(capped)
    if remaining > 0:
        lines.append(f"+{remaining} more")
    return lines
