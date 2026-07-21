# Feature: Redesign `/inqueue` Output for Readability

**Status:** Proposed.
**Value:** Low–Medium (quality-of-life for the one person who reads this message)
**Effort:** Low
**Touches:** `knowledger/bot.py` (`cmd_inqueue`)

## Problem

The current `/inqueue` reply (see screenshot in `docs/PENDING/`) has several UX
issues on top of the [Markdown italics bug](../bugs/inqueue-markdown-italics.md):

- Section headers are raw on-disk filenames (`petition_queue.json`,
  `poller_state.json`) — internal implementation detail, meaningless to the human
  reading a Telegram message.
- No counts up front — you have to read the whole message to know if anything needs
  attention.
- No timestamps — a video queued 10 minutes ago and one stuck for 3 days look
  identical.
- An empty section still gets a full header block plus a `(empty)` line.
- Entries with rising `upload_attempts` (the stuck-video signal already used
  elsewhere, see `MAX_UPLOAD_ATTEMPTS` in `queue_processor.py`) aren't visually
  distinguished from healthy ones.
- Long lists hard-truncate at `_TELEGRAM_MAX_MESSAGE_LENGTH` mid-bullet instead of
  ending cleanly.

## Design decisions

- **Timestamps: absolute, not relative.** `YYYY-MM-DD HH:MM` sliced from the stored
  ISO-8601 string. A relative "2h ago" goes stale/misleading if the message sits
  unread in the chat.
- **Truncation: cap entries per section (~10) + a `+N more` line**, instead of the
  current hard character-limit cut. Never ends mid-bullet.
- **Drop the raw filenames entirely.** They're an implementation detail; the human
  section labels ("Retry queue", "Poller") are what matter. (If you're SSH'd in
  debugging the actual files, you know their names already.)
- **Human section labels + emoji anchors** so the message is scannable without
  reading every line: 🔁 Retry queue, ⏳ Poller pending, ⚠️ on any entry whose
  `upload_attempts` > 0.
- **Empty sections collapse to one line** (`🔁 Retry queue: empty`) instead of a
  header + separate `(empty)` line.
- **Counts in section headers** (`⏳ Poller — 2 pending`) so the summary is visible
  without reading the bullets.
- **`/refresh` hint only appears when there's something to retry** — an actionable
  nudge, not a static instruction shown even when the queue is empty.

## Mockup

Empty retry queue, 2 poller-pending videos:

```
📊 Queue status

🔁 Retry queue: empty

⏳ Poller — 2 pending
• NO es AGOTAMIENTO, es ACUMULACIÓN de energía — José Luis Cava
  seen 2026-07-18 03:00
• ¿Qué opciones para invertir en dólares...? — Rodrigo Álvarez
  seen 2026-07-18 08:15

76 videos seen total
```

Non-empty retry queue with a stuck entry:

```
📊 Queue status

🔁 Retry queue — 2 queued
• ⚠️ "Título..." — 4 failed attempts
  queued 2026-07-17 09:12 — run /refresh to retry
• "Otro título..."
  queued 2026-07-18 03:00

⏳ Poller — 1 pending
• "Title" — José Luis Cava
  seen 2026-07-18 08:15

77 videos seen total
```

Long list, capped:

```
⏳ Poller — 14 pending
• "Title 1" — Channel A
  seen 2026-07-18 08:15
... (9 more entries)
• "Title 10" — Channel J
  seen 2026-07-10 12:00
+4 more
```

## Fix

Rewrite `cmd_inqueue` (bot.py:195-239) to:

1. Build each section (`retry queue`, `poller pending`) as a list of formatted
   entry strings first, so count and cap logic is shared between sections instead of
   duplicated inline.
2. A small local helper for the timestamp slice (`ts[:16].replace("T", " ")`) reused
   for both `QueueEntry.queued_at` and `PendingVideo.first_seen`.
3. A shared cap helper: given a list of formatted entries and a max (e.g. 10), return
   the capped list plus an optional `+N more` trailer line.
4. Emoji/label constants for the two sections and the ⚠️ stuck marker (attempts > 0),
   defined next to `cmd_inqueue` since nothing else uses them.
5. Keep `escape_markdown` on every user-supplied field (title, channel name) exactly
   as today — only the internal filenames are being removed, not the existing
   Markdown-safety handling for real user content.

This also happens to fix the [Markdown italics bug](../bugs/inqueue-markdown-italics.md)
as a side effect, since the raw filenames causing it are removed entirely — that bug
doc can be closed once this ships instead of needing its own separate fix.

## Verification

1. `uv run ruff check .` clean.
2. Manually populate a retry-queue entry with `upload_attempts > 0` and more than 10
   poller-pending videos; call `/inqueue` in a real Telegram client and confirm:
   - No unintended italics.
   - Counts in headers match actual list lengths.
   - The stuck entry shows the ⚠️ marker.
   - The long list caps at 10 with a correct `+N more` line, not a mid-bullet cutoff.
   - Empty retry queue collapses to a single line.
