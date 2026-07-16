# Feature: Persistent Upload Queue on Token Failure

**Value:** High
**Effort:** Low-Medium
**Touches:** `knowledger/queue.py` (new), `knowledger/bot.py`

## Problem

When the Claude session token expires, `upload_content` raises `AuthError`. The user loses the in-progress petition entirely — the URL must be resent after refreshing the token. If the token expires while multiple uploads are in flight, each one is silently discarded. This is especially frustrating because the slow part (fetching the transcript) has already completed successfully.

## Proposed Solution

On `AuthError` during upload, serialize the petition — project ID, filename, and the already-fetched transcript — to a local JSON file queue. When `/refresh` succeeds, drain the queue by retrying all stored uploads and notifying each user in their chat.

The bot's reply on failure changes from "Auth error: …" to:

> Token expired. *Youtube - Channel - Title* has been queued — run /refresh after updating the token and it will upload automatically.

## Implementation

### New module: `knowledger/queue.py`

```
QueueEntry  (frozen dataclass)
  project_id: str
  file_name: str
  transcript: str
  chat_id: int
  video_title: str
  queued_at: str   # ISO-8601, for display only
```

`enqueue(entry: QueueEntry, path: Path = QUEUE_FILE) -> bool`
Load the current queue from `path`. Check for an existing entry with the same `(project_id, file_name)` pair. If a duplicate is found, return `False` without writing. Otherwise append and save, return `True`.

`drain_queue(path: Path = QUEUE_FILE) -> list[QueueEntry]`
Read all entries from `path`, atomically replace the file with an empty list, and return the entries. Treats a missing or corrupt file as an empty queue (log a warning).

`QUEUE_FILE = Path("petition_queue.json")`

File format: a JSON array of serialized `QueueEntry` objects, human-readable.

### Changes to `bot.py`

**`handle_project_selection`** — in the `except AuthError` branch:

```python
except AuthError:
    entry = QueueEntry(
        project_id=project_id,
        file_name=file_name,
        transcript=transcript,
        chat_id=update.effective_chat.id,
        video_title=metadata.title,
        queued_at=datetime.now(UTC).isoformat(),
    )
    added = enqueue(entry)
    if added:
        msg = f"Token expired — *{file_name}* queued. Run /refresh after updating the token."
    else:
        msg = f"Token expired — *{file_name}* was already queued."
    await query.edit_message_text(msg, parse_mode="Markdown")
    return
```

**`cmd_refresh`** — after successfully reloading projects, drain and retry the queue:

```python
entries = drain_queue()
if entries:
    failed = []
    for entry in entries:
        try:
            context.bot_data["claude_client"].upload_content(
                entry.project_id, entry.transcript, entry.file_name
            )
            await context.bot.send_message(
                entry.chat_id,
                f"Queued upload saved: *{entry.file_name}*",
                parse_mode="Markdown",
            )
        except AuthError:
            failed.append(entry)
        except Exception:
            logger.exception("Queue retry failed for %s", entry.file_name)
            failed.append(entry)
    for entry in failed:
        enqueue(entry)   # put failures back
    summary = f"{len(entries) - len(failed)}/{len(entries)} queued upload(s) processed."
    if failed:
        summary += f" {len(failed)} failed and re-queued."
    await update.message.reply_text(summary)
```

## Deduplication

The dedup key is `(project_id, file_name)`. The `file_name` encodes channel, title, and implicitly the video ID, so the same video sent to the same project while the token is expired is enqueued exactly once. The user is told on the second attempt that the video is already queued.

## Edge Cases

| Scenario | Behaviour |
|----------|-----------|
| Token still invalid when `/refresh` is run | Failed entries are re-enqueued; caller sees failure count |
| Bot restarts with a non-empty queue | Entries persist in the JSON file; processed on the next `/refresh` |
| Queue file missing or corrupt | Treated as empty queue; logged at WARNING level |
| Same video, different projects | Two separate entries (distinct `project_id`) — both processed |
| `/refresh` called with no queue | No extra output; existing refresh messages unchanged |

## Why This Produces the Most Value

Token expiration is the single most disruptive event in the current workflow. The transcript fetch — the slow, failure-prone operation — has already completed by the time `AuthError` is raised. Discarding it is wasteful. Persisting the already-fetched content to a file queue costs almost nothing and turns a full-loss failure into a simple deferred success.
