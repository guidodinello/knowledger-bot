# Bug: Blocked Transcript Fetches Are Silently Dropped, Not Retried

**Severity:** Medium (silent data loss — a video the user explicitly asked to save
just vanishes unless they notice and manually resend the URL)
**Files:** `knowledger/bot.py` (`handle_project_selection`, `handle_duplicate_choice`)

## Description

The manual, interactive `/start`-URL-paste flow has two call sites that fetch a
transcript and, on `TranscriptTransportError` (YouTube blocked the request — see
[docs/PENDING evidence], confirmed transient via a live proxy/logs check on
2026-07-21), just edit the message and `return`:

**1. `handle_project_selection` (bot.py:375-392)** — the normal upload path:

```python
try:
    transcript = await asyncio.to_thread(
        fetch_transcript,
        metadata.video_id,
        proxy=context.bot_data["config"].transcript.proxy,
        cookies_path=context.bot_data["config"].transcript.youtube_cookies_path,
    )
except TranscriptUnavailable:
    ...
except TranscriptTransportError:
    await query.edit_message_text(
        "Transcript request was blocked — this is usually temporary. Please try again shortly.",
    )
    return
```

**2. `handle_duplicate_choice` (bot.py:483-500)** — the overwrite path — has the
identical pattern.

In both cases the video is gone from the bot's perspective the moment the message is
sent: nothing is persisted, nothing is scheduled for retry. The user has to notice the
message, remember which video it was for, and manually resend the URL.

This is a different failure shape from the already-documented
[petition-queue-missing-project-context.md](petition-queue-missing-project-context.md)
bug: that one is about `AuthError` short-circuiting *before* a `QueueEntry` can be
built. Here, the problem is upstream of that — there is no transcript yet to put in a
`QueueEntry` (`transcript: str` is a required field), so the existing
`petition_queue.json` mechanism can't be reused as-is for this case.

The poller (`knowledger/poller.py:319-321`) already handles the exact same exception
correctly, because it has a durable `PendingVideo` list to fall back into:

```python
except TranscriptTransportError:
    logger.info("Transcript request blocked for %s; will retry", video.video_id)
    return video  # stays in PollerState.pending, retried next tick
```

The bot's interactive flow has no equivalent "pending" concept — a Telegram callback
handler is fire-and-forget with no next tick to retry on.

Note this is specifically about the *transcript* fetch inside a user-triggered
request. The poller's *feed* fetches (`fetch_feed`/`_http_get` in `poller.py`, which
also failed with a run of 404s during the same investigation) don't need an
equivalent fix: `TranscriptPoller.run()` (poller.py:444-463) is an infinite loop that
re-fetches every channel's feed on every tick regardless of the previous tick's
outcome, so a feed failure is already retried automatically `poll_interval` seconds
later — no queue required there.

## Impact

Medium. Confirmed to happen in production (two videos back-to-back on 2026-07-21,
both showed this message) and, per the same investigation, this is *expected*
behavior from YouTube/the residential proxy occasionally — not a rare edge case. Every
occurrence currently means: video silently not saved, no record it was ever
requested, and the only recovery is the user remembering to resend the link.

## Fix options

### Option A — give the interactive flow its own small pending list

Add a `pending_transcripts.json`-style durable list (same shape as
`PollerState.pending`, keyed by `chat_id` + `video_id` + `project_id` + `file_name`)
that a lightweight periodic task (piggybacking on the poller's existing tick loop)
drains by retrying `fetch_transcript` and, on success, falling into the same
`service.upload()` + `QueueEntry`-on-`DeferredForAuth` logic already in
`handle_project_selection`. Mirrors the poller's proven pattern exactly.

The drain should call `service.upload()` bare (no pre-fetched `docs`, no
`overwrite_doc_uuid`) rather than reconstruct the interactive Skip/Overwrite prompt —
there's no user actively waiting on a callback for a background retry. If the video
landed in the project some other way while the retry was pending, `upload()`'s own
dedup check returns `AlreadyExists()` and the entry is silently dropped, exactly like
the poller already does on `AlreadyExists()` (`poller.py:332-335`: log + remove from
queue, no user-facing message). Skip/Overwrite stays exclusive to the live interactive
flow, where `list_docs` runs *before* `fetch_transcript` specifically so the user can
be asked.

### Option B — bounded in-request retry with backoff

Before giving up, retry `fetch_transcript` a couple of times in-process (e.g. 3
attempts, a few seconds apart) inside the existing `asyncio.to_thread` call. Simpler,
no new persistence — but doesn't help if the block outlasts the retry window (proxy
IP blocks have been observed to last minutes), and ties up the handler/event loop
thread for longer.

Recommended: **Option A**. The poller already proves the pattern works and the
"usually temporary" message implies to the user that a retry *will* happen — Option B
only sometimes delivers on that promise.

## Open question

Should Option A's pending list share `petition_queue.json`/`Queue` (with `transcript`
made optional, `None` while still fetching) or be a separate file? Sharing would let
`/inqueue` show both kinds of pending work in one place; separate keeps the two
independent (`QueueEntry` is upload-paused-on-auth, this is fetch-paused-on-transport)
and avoids widening `Queue`'s claim/ack semantics for a different lifecycle. Leaning
separate file, since `Queue.enqueue`'s dedupe-on-`project_id`+`video_id` and
claim/ack/release model was built for the upload case and folding fetch-retry into it
adds branching for little reuse.

## Verification

1. `uv run ruff check .` clean.
2. Force a `TranscriptTransportError` (e.g. temporarily point `YOUTUBE_PROXY_URL` at
   an unreachable host, or monkeypatch `fetch_transcript` to raise in a test) and send
   a URL through `/start`; confirm the video is persisted somewhere retryable instead
   of just showing the message and disappearing.
3. Restore transcript fetching and confirm the pending entry drains automatically
   (poller tick or `/refresh`, per whichever fix option is implemented) and the video
   uploads.
