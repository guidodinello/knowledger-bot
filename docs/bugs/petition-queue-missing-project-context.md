# Bug: Auth Errors Before Project Selection Skip the Retry Queue Entirely

**Severity:** Medium (silent data loss — a fetched-but-unsent request just vanishes)
**Files:** `knowledger/bot.py` (`handle_youtube_url`, `handle_project_selection`), `knowledger/claude_client.py`

## Description

The retry queue (`petition_queue.json`, drained by `/refresh` and the token-update HTTP
hook) only works if a `QueueEntry` — which requires a `project_id` — gets created in
the first place. Two places in the flow can hit `AuthError` **before** a project is
ever chosen, and both currently dead-end with an error message instead of falling
through to the point where queuing is possible:

**1. `handle_youtube_url` (bot.py:266-270)** — the very first step, before the project
keyboard is even shown:

```python
try:
    projects = await asyncio.to_thread(context.bot_data["claude_client"].list_projects)
except AuthError as e:
    await update.message.reply_text(f"Auth error: {e}")
    return
```

`ClaudeClient.list_projects()` (claude_client.py:85-97) caches its result **in memory
only** (`_projects_cache`). That cache is empty on a fresh process start and is
explicitly cleared by `/refresh` (`invalidate_projects()`) and `update_token()`. So the
exact moment the token goes bad — right when you'd want the queue to catch the
request — is also the moment there's no cached project list to fall back on. The user
never sees the project-selection keyboard, so no `project_id` is ever known, so
nothing can be queued. The transcript hasn't even been fetched yet at this point, so
there's genuinely nothing to save — but the *conversation* is lost too: the user has
to remember to resend the URL after fixing the token, instead of it just working via
`/refresh` like every other queued item.

**2. `handle_project_selection` (bot.py:333-344)** — after a project *has* been picked,
but before the transcript is fetched, a separate `list_docs` call powers the
duplicate-check ("already exists — skip or overwrite?") prompt:

```python
try:
    docs: list[Doc] = await asyncio.to_thread(
        context.bot_data["claude_client"].list_docs,
        project_id,
    )
except AuthError as e:
    await query.edit_message_text(f"Auth error: {e}")
    return
```

This one is more clearly a bug: `project_id` **is** already known at this point, and
the function's own `match outcome:` block a few lines below (bot.py:417-435) already
knows exactly how to build a `QueueEntry` and enqueue it on `DeferredForAuth` — that
code just isn't reachable from this earlier, separate `list_docs` call. The only thing
missing is the transcript, which hasn't been fetched yet purely because this
dedup-check call happens first (a deliberate optimization — skip fetching a transcript
for a video that turns out to already exist). When the token is already broken, that
optimization backfires: it aborts the whole flow before ever reaching the transcript
fetch + `service.upload()` call that would have queued it correctly.

## Impact

Medium. This is exactly the retry queue's reason for existing (queue-then-retry after
a token update), and it silently fails to engage for both entry points that lead into
it. In practice: any URL sent while the token is stale never gets queued, so `/refresh`
and the token-update webhook — the very mechanisms meant to make this recoverable —
have nothing to act on. The user has no visibility that this happened beyond the
one-off "Auth error" reply, which looks identical to a permanent failure. The
[`/inqueue` redesign](../features/inqueue-redesign.md) makes existing queue entries
easier to read, but can't help with requests that never made it into the queue.

## Fix

### 1. Persist the project list, not just cache it in memory

Mirror the existing session-token persistence pattern (`_load_persisted_token` /
`update_token` in `config.py` / `claude_client.py`): write the project list to
`config.storage.data_dir / "projects_cache.json"` on every successful
`list_projects()` fetch, and load it as a fallback.

In `handle_youtube_url`:

```python
try:
    projects = await asyncio.to_thread(context.bot_data["claude_client"].list_projects)
except AuthError:
    projects = load_persisted_projects(context.bot_data["config"].storage.data_dir)
    if not projects:
        await update.message.reply_text(f"Auth error: {e}")
        return
    # proceed to show the keyboard, with a caveat that the list may be stale
```

Only when there is truly no project list available anywhere (first-ever run with an
already-bad token) does the flow dead-end — every other case lets the user pick a
project and continue.

### 2. Let the dedup-check AuthError fall through instead of dead-ending

Only change the `AuthError` branch of the `list_docs` pre-check — the healthy-token
path (the common case) keeps its existing behavior and performance (skip the
transcript fetch on a known duplicate) unchanged:

```python
try:
    docs: list[Doc] | None = await asyncio.to_thread(
        context.bot_data["claude_client"].list_docs,
        project_id,
    )
except AuthError:
    docs = None  # can't dedupe right now — fall through to fetch + service.upload,
                 # which will hit the same AuthError again via list_docs(docs=None)
                 # and correctly return DeferredForAuth for the existing queuing logic below
```

`docs=None` is exactly the signal `TranscriptUploadService.upload()` already uses to
do its own `list_docs` + dedupe internally (upload_service.py:81-88), returning
`DeferredForAuth` on the same `AuthError` — which the `case DeferredForAuth():` block
already in `handle_project_selection` (bot.py:417) enqueues correctly. No new queuing
logic needed, just letting the existing code path be reached.

## Open question

For fix #1, when falling back to a persisted-but-possibly-stale project list: should
the bot show a caveat in the message (e.g. "⚠️ using last known project list — may not
include newly created projects")? Recommended yes, so the user isn't confused if a
project they created after the token broke doesn't appear yet — but flagging since
it's a UX call, not a functional requirement.

## Verification

1. `uv run ruff check .` clean.
2. With a valid persisted project cache on disk, force `AuthError` (e.g. temporarily
   corrupt the session token in `session_token.json` or set an invalid
   `CLAUDE_SESSION_TOKEN`) and confirm `handle_youtube_url` still shows the project
   keyboard using the persisted list.
3. Pick a project, let the dedup-check `list_docs` call hit the forced `AuthError`,
   and confirm the flow falls through to fetch the transcript, then queues via
   `DeferredForAuth` — check `/inqueue` (or `petition_queue.json` directly) shows the
   new entry with the correct `project_id`.
4. Restore a valid token and run `/refresh`; confirm the queued entry drains and
   uploads successfully.
