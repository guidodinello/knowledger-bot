# Bug: Blocking I/O Inside Async Handlers

**Severity:** Medium  
**File:** `knowledger/bot.py:155`, `knowledger/bot.py:167`

## Description

`handle_project_selection` is an `async` function running inside the `asyncio` event loop managed by `python-telegram-bot`. Two synchronous, potentially slow operations are called directly without offloading to a thread:

```python
# bot.py:155 — can take 10–30 s when Invidious fallback is used
transcript = fetch_transcript(metadata.video_id)

# bot.py:167 — network I/O to claude.ai
context.bot_data["claude_client"].upload_content(project_id, transcript, file_name)
```

Calling blocking code from an async handler stalls the entire event loop. No other Telegram updates (messages, button presses) can be processed until the call completes.

## Impact

For a personal single-user bot this causes temporary unresponsiveness. If Invidious fallback iterates through multiple slow instances, the bot can freeze for 30 seconds or more. The bot gives no indication of progress during this time.

## Fix

Offload blocking calls to a thread with `asyncio.to_thread`:

```python
import asyncio

transcript = await asyncio.to_thread(fetch_transcript, metadata.video_id)

await asyncio.to_thread(
    context.bot_data["claude_client"].upload_content,
    project_id, transcript, file_name,
)
```

This keeps the event loop free while the blocking work runs in a thread pool.
