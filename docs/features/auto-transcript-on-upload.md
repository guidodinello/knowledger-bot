# Feature: Auto-transcript on New Upload

**Value:** High
**Effort:** Medium
**Touches:** `knowledger/poller.py` (new), `channels.json` (new), `knowledger/config.py`, `main.py`, `knowledger/youtube.py`

## Problem

Adding a transcript is a manual step: send a YouTube URL to the bot, pick a project, wait.
For a fixed set of "review" channels we watch every week, that step is pure toil — the
same channels, the same target project, every time. We want new videos from those channels
transcribed and uploaded automatically, with no human in the loop.

## Proposed Solution

An **in-process asyncio background task** inside the existing bot process polls each
channel's Atom feed (`https://www.youtube.com/feeds/videos.xml?channel_id=UCxxxx`), and
**24 hours after a video is published** fetches its transcript and uploads it as a doc into
a designated Claude project — reusing the manual flow's dedup + upload path.

Key decisions (from `auto-transcript-on-upload-chat.md`):

- **Polling, not WebSub.** No public callback server; just diff each feed against a
  seen-set. "New video within the hour" is plenty for a weekly review cadence.
- **24h delay, keyed off publish time.** YouTube first publishes a rough *draft* caption
  and swaps in a polished pass within hours; waiting a day avoids grabbing the draft. Using
  the feed's real `<published>` timestamp (not a detection timestamp) means the delay is
  correct even across bot restarts.
- **In-process, not cron.** The bot already runs persistently with a live, `/refresh`-able
  `ClaudeClient`. A cron process would carry its own token that constantly goes stale — the
  exact failure the persistent upload queue exists to work around. Reusing the live client
  avoids that entirely.
- **Handles → channel IDs resolved once.** The user lists `@handles`; the poller scrapes
  each channel's `UCxxxx` id on first run and caches it back into `channels.json`.

## Implementation

### 1. `build_doc_name` — SSOT for the filename (`knowledger/youtube.py`)

The `Youtube - {channel} - {title} - {date}` expression was duplicated in `bot.py` and
`cli.py` (the latter missing the date). Extracted into one helper reused by the bot, the
CLI, and the poller:

```python
def build_doc_name(channel_name: str, title: str, upload_date: str | None) -> str:
    date_suffix = f" - {upload_date}" if upload_date else ""
    return f"Youtube - {sanitize_filename(channel_name)} - {sanitize_filename(title)}{date_suffix}"
```

### 2. Channel config — `channels.json`

A committed JSON array the user edits (public handles, ships in the Docker image).
`channel_id` starts `null` and is backfilled on first run:

```json
[ { "handle": "@NeuronaFinanciera", "name": "Neurona Financiera", "channel_id": null } ]
```

### 3. `knowledger/poller.py`

Follows `queue.py` conventions: frozen-dataclass entries, atomic `.tmp`+`os.replace`
writes, missing/corrupt state → empty + WARNING. Blocking I/O runs via `asyncio.to_thread`
(as in `bot.py`). XML parsed with `defusedxml`.

- `resolve_channel_id(handle, proxy)` — scrape `UCxxxx` from the channel page.
- `fetch_feed(channel_id, proxy)` — GET + parse the Atom feed into `PendingVideo`
  (`video_id, title, channel_name, published` tz-aware, `first_seen`). **Direct-first**
  HTTP: the Decodo proxy exists for the transcript API's Oracle-IP block; the feed usually
  works direct, so we only fall back to the proxy on failure (saves paid bandwidth).
- `PollerState` (`poller_state.json`) — `{ seen: [video_id…], pending: [PendingVideo…] }`.
- `run_poller(app, config)` — the loop:
  1. **First run only:** baseline-seed `seen` from current feeds *without enqueueing* — so
     only videos published after startup are ever processed.
  2. Each tick (`config.poll_interval`, default 3600s): detect new videos → `pending`;
     for each pending video ≥24h past publish, fetch the transcript, dedup via `list_docs`,
     `upload_content` into the target project, notify allowed users on Telegram.
  On `AuthError` during upload, the transcript is parked on the shared `Queue`
  (`petition_queue.json`) so `/refresh` uploads it — the video stays pending until the
  upload is confirmed on a later tick.

### 4. Wiring (`knowledger/config.py`, `main.py`)

- Config: `auto_transcript_project` (`AUTO_TRANSCRIPT_PROJECT`, project *name*),
  `channels_path` (`CHANNELS_PATH`), `poll_interval` (`POLL_INTERVAL_SECONDS`).
- The poller is **enabled only when `AUTO_TRANSCRIPT_PROJECT` is set**; otherwise the bot
  behaves exactly as before. When enabled, `main_async` starts `run_poller` as an extra
  task alongside polling and the token server, with identical cancel-on-shutdown handling.

## Edge Cases

| Scenario | Behaviour |
|----------|-----------|
| First run / fresh state | Baseline-seed `seen` from current feeds without enqueueing — no back-catalogue upload storm |
| Captions not ready at 24h | Stay pending, retried each tick until 72h **after first detection**, then dropped with a "no captions" notice |
| YouTube blocks feed/transcript (Oracle IP) | Feed fetch falls back to the proxy; a blocked transcript returns `None` and is treated as "not ready" (retried) |
| Token expired mid-upload | Transcript parked on `petition_queue.json`, drained by `/refresh`; video stays pending until upload confirmed |
| Bot down for hours/days | Publish-time gate + `seen` set → nothing missed, nothing double-uploaded |
| Duplicate already uploaded | `list_docs` check before upload — skipped |
| `channel_id` unresolved / handle renamed | Logged WARNING, that channel skipped this tick; others proceed |

## Setup

1. Add the review-channel `@handles` to `channels.json` (`channel_id: null`).
2. Set `AUTO_TRANSCRIPT_PROJECT` in `.env` to the target Claude project name.
3. Restart the bot. On first start it resolves channel IDs, baseline-seeds, and begins
   watching; only videos published after that point are auto-transcribed.
