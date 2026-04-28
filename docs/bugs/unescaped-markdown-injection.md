# Bug: Unescaped Markdown in Bot Messages

**Severity:** High  
**Files:** `knowledger/bot.py:117-120`, `knowledger/bot.py:178`

## Description

Video titles and channel names are embedded directly into Telegram Markdown-formatted strings without escaping:

```python
# bot.py:117-120
f"*{metadata.title}*\n_{metadata.channel_name}_\n\nSelect a project:"

# bot.py:178
f"Saved *{file_name}* to project."
```

Telegram's Markdown parser is applied to the entire string. If `metadata.title`, `metadata.channel_name`, or `file_name` contain the characters `*`, `_`, `` ` ``, or `[`, Telegram rejects the message with a `BadRequest: Can't parse entities` error. The bot silently fails to reply — the user sees nothing.

## Impact

This is a high-frequency real-world failure. Many YouTube channels and video titles naturally contain underscores (e.g. `Tech_World`, `AI_Explained`) or other Markdown-active characters. Any such video will produce a broken interaction.

## Fix

Use `telegram.helpers.escape_markdown(text, version=1)` on every piece of user-supplied dynamic content before embedding it in a Markdown string:

```python
from telegram.helpers import escape_markdown

escaped_title = escape_markdown(metadata.title, version=1)
escaped_channel = escape_markdown(metadata.channel_name, version=1)
f"*{escaped_title}*\n_{escaped_channel}_\n\nSelect a project:"
```

Apply the same escaping to `file_name` in the success message at `bot.py:178`.
