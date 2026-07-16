# Bug: /refresh Drops Non-Auth Network Errors

**Severity:** Low
**File:** `knowledger/bot.py:78-84`

## Description

`cmd_refresh` only catches `AuthError`:

```python
try:
    context.bot_data["projects"] = context.bot_data["claude_client"].list_projects()
    await update.message.reply_text(f"Done. {len(...)} project(s) loaded.")
except AuthError as e:
    await update.message.reply_text(f"Auth error: {e}")
```

`list_projects()` can also raise `RequestException` (network timeout, connection error) or any other unexpected exception. These propagate uncaught, producing only a server-side log entry. The user receives no Telegram reply and has no way to know the refresh failed.

## Impact

Low — this is a personal bot with reliable network conditions most of the time. But when a transient failure does occur, the user sees their "Refreshing project list..." message with no follow-up, leaving the interaction in an ambiguous state.

## Fix

Add a fallback handler to surface the error to the user:

```python
except AuthError as e:
    await update.message.reply_text(f"Auth error: {e}")
except Exception as e:
    logger.exception("Failed to refresh project list")
    await update.message.reply_text(f"Refresh failed: {e}")
```
