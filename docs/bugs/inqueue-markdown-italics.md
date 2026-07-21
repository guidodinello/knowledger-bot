# Bug: `/inqueue` Filenames Break Markdown Rendering

**Severity:** Low
**File:** `knowledger/bot.py:195-239` (`cmd_inqueue`)

## Description

`cmd_inqueue` sends its reply with `parse_mode="Markdown"` but emits the literal
filenames `petition_queue.json` and `poller_state.json` unescaped:

```python
lines.append("petition_queue.json (retry/upload queue)")
...
lines.append("poller_state.json (seen + pending videos)")
```

Telegram's legacy Markdown parser treats `_` as an italics toggle across the whole
message, not per-word. The underscore in `petition_queue` opens italics and the
underscore in `poller_state` closes it — everything in between (the queue section,
the `(empty)` marker, and the start of the poller section) renders italicized.
Confirmed via screenshot. Same bug class as the already-fixed
[unescaped Markdown injection bug](unescaped-markdown-injection.md), just a spot that
fix didn't cover since these two strings are hardcoded literals, not user-supplied text.

## Impact

Low — cosmetic only, but the italic corruption makes the message genuinely hard to
read.

## Fix

Wrap the two literal filenames in backticks instead of leaving them as plain
Markdown-unsafe text:

```python
lines.append("`petition_queue.json` (retry/upload queue)")
...
lines.append("`poller_state.json` (seen + pending videos)")
```

Superseded by whatever section headers the [`/inqueue` redesign](../features/inqueue-redesign.md)
lands on — if that ships first, this fix is already included and this doc can be
closed without separate action.

## Verification

1. `uv run ruff check .` clean.
2. Run the bot, populate at least one queue entry, call `/inqueue`, and confirm in a
   real Telegram client that no unintended italics appear.
