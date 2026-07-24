# Feature: Weekly Upload Recap

**Status:** Implemented — [PR #49](https://github.com/guidodinello/knowledger-bot/pull/49).
**Value:** Medium (visibility into what landed in each project before the weekend
analysis session)
**Effort:** Medium
**Touches:** `knowledger/config.py`, `knowledger/poller.py`, `knowledger/bot.py`,
`knowledger/queue_processor.py`, `knowledger/weekly_recap.py` (new), `main.py`

## Problem

The bot uploads transcripts to Claude projects (via the poller, manual Telegram
uploads, and queued retries), and the user does a manual weekend analysis pass in
Claude web against those transcripts — that manual step stays exactly as-is, this
feature does not touch it. What's missing is visibility *before* that session: a
proactive summary of what got uploaded that week and to which project, instead of
having to remember or scroll back through the Telegram chat history.

There is currently no persisted record of successful uploads to build such a summary
from. Each of the three places that handle a successful upload only does an
ephemeral, one-off notification at the time it happens:

- `knowledger/poller.py` `_process_video`, `case Uploaded():` (poller.py:326-331)
- `knowledger/bot.py` `handle_project_selection`, `case Uploaded():` (bot.py:400-407)
- `knowledger/queue_processor.py` `_process_entry`, `case Uploaded():` (queue_processor.py:127-136)

None of these write anything durable — `poller_state.json` and `petition_queue.json`
both *remove* an entry once it's uploaded, by design (they track "not yet done," not
history). So a weekly digest needs a new, append-only record of what happened.

## Design

### 1. Append-only upload history

A new small module, `knowledger/history.py`:

```python
@dataclass(frozen=True, slots=True)
class UploadRecord:
    project_id: str
    file_name: str
    video_title: str
    uploaded_at: str  # ISO-8601 UTC

def record_upload(data_dir: Path, record: UploadRecord) -> None:
    """Append one record to upload_history.json. Read-modify-write under the
    same atomic_write_json pattern already used for poller_state.json /
    petition_queue.json — safe for the low, bursty write frequency of uploads
    (nothing here needs a real append-only log format)."""

def load_history(data_dir: Path) -> list[UploadRecord]:
    """Missing file: empty history (valid — no uploads yet). Corrupt file: fails
    closed via CorruptDataError, same policy as poller_state.json / channels.json."""
```

Call `record_upload` right next to the existing notification/reply logic at each of
the three `case Uploaded():` sites — same pattern as those sites already follow for
their own side effects on that outcome (log + notify), just one more side effect
alongside them.

No pruning logic needed for v1: the file only grows by one small JSON object per
upload, which for a personal bot's volume (a handful of transcripts a week) is
negligible for years. Revisit only if it actually becomes a problem.

### 2. Schedule config

```python
@dataclass(frozen=True, slots=True)
class WeeklyRecapSettings:
    enabled: bool = False
    day: str = "FRI"   # Mon..Sun three-letter code, UTC
    hour: int = 18     # UTC
```

Read from `WEEKLY_RECAP_ENABLED` / `WEEKLY_RECAP_DAY` / `WEEKLY_RECAP_HOUR`, same
env-var-with-defaults pattern as `POLL_INTERVAL_SECONDS`. Off by default, like the
token server and poller, which also require explicit opt-in.

UTC (not local time) to stay consistent with the rest of the poller subsystem, which
already timestamps everything in UTC and has no timezone-conversion dependency —
default `FRI 18:00 UTC` is the evening before the usual weekend analysis session.

### 3. The recap task itself — `knowledger/weekly_recap.py`

Same shape as `run_poller`: an infinite loop, sleep until the next scheduled moment,
do the work, repeat. Self-correcting by recomputing "next occurrence from now" every
iteration (rather than a fixed 7-day sleep), so a restart mid-week still lands on the
right slot instead of drifting:

```python
async def run_weekly_recap(app: Application, config: Config) -> None:
    client: ClaudeClient = app.bot_data["claude_client"]
    while True:
        target = _next_occurrence(config.weekly_recap.day, config.weekly_recap.hour)
        await asyncio.sleep((target - datetime.now(UTC)).total_seconds())
        try:
            await _send_recap(app, config, client)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Weekly recap failed; will retry next week")
```

`_send_recap`:

1. `load_history(config.storage.data_dir)`, filter to `uploaded_at >= now - 7 days`.
2. Resolve each `project_id` to a display name via `client.list_projects()` (already
   cached — no extra API cost beyond whatever's already cached this run). A
   project that no longer exists (deleted after upload) falls back to showing the
   raw id rather than erroring.
3. Group by project name, sorted by upload count descending (busiest project first).
4. `notify()` the formatted message to every allowed user — same delivery mechanism
   already used by the poller's alerts.

### Mockup

```
📅 Weekly recap — 2026-07-12 to 2026-07-19

Investments (4)
• "Influencia: psicología de la persuasión" — José Luis Cava
• "¿Ha llegado YA el momento de VENDER semiconductores?" — José Luis Cava
• "NO es AGOTAMIENTO, es ACUMULACIÓN de energía" — José Luis Cava
• "¿Qué opciones para invertir en dólares...?" — Rodrigo Álvarez

Exercise (1)
• "Rutina de movilidad para corredores" — Dr. La Rosa

5 transcripts uploaded this week.
```

Empty week:

```
📅 Weekly recap — 2026-07-12 to 2026-07-19

No transcripts uploaded this week.
```

### 4. Wiring — `main.py`

Same conditional-task pattern already used for the poller and HTTP server:

```python
if config.weekly_recap.enabled:
    from knowledger.weekly_recap import run_weekly_recap
    tasks.append(tg.create_task(run_weekly_recap(app, config)))
```

## Out of scope

- The manual analysis prompt/workflow itself (deciding hold/invest/etc.) — stays
  exactly as-is, entirely outside the bot.
- Per-project custom schedules (e.g. Investments recapped weekly, Exercise
  monthly) — not asked for; one global schedule for all projects is enough for now.
- Retroactive backfill of history for uploads that happened before this feature
  ships — the history starts empty; the first recap after deploying will only cover
  uploads from that point forward.

## Verification

1. `uv run ruff check .` clean.
2. Trigger a manual Telegram upload and a poller auto-upload; confirm both append to
   `upload_history.json` with the correct `project_id` and timestamp.
3. Temporarily set `WEEKLY_RECAP_DAY`/`WEEKLY_RECAP_HOUR` to a few minutes in the
   future, run the bot, and confirm the recap message arrives at the right time,
   grouped correctly by project, with accurate counts.
4. Delete `upload_history.json` (or point `DATA_DIR` at an empty dir) and confirm the
   recap sends the "No transcripts uploaded this week" message instead of erroring.
5. Restart the process mid-week and confirm the next recap still lands on the
   configured day/hour rather than drifting from the restart time.
