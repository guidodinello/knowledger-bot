"""Proactive weekly digest of what got uploaded, grouped by project — visibility before
the user's manual weekend analysis session in Claude web (that session itself is
entirely out of scope; this only summarizes what already landed).

Same shape as ``run_pending_transcript_retrier``: an infinite loop, sleep until the
next scheduled moment, do the work, repeat. Self-correcting by recomputing "next
occurrence from now" every iteration (rather than a fixed 7-day sleep), so a restart
mid-week still lands on the right slot instead of drifting.
"""

import asyncio
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from telegram.ext import Application

from .claude_client import ClaudeClient
from .config import DAY_CODES, Config
from .history import UploadRecord, load_history
from .logger import get_logger
from .notify import notify

logger = get_logger(__name__)

RECAP_WINDOW = timedelta(days=7)


def _next_occurrence(day: str, hour: int, now: datetime) -> datetime:
    """Earliest slot with the given weekday+hour STRICTLY AFTER `now`.

    Pure and injected with `now` so it's unit-testable. Must be strictly future: a
    naive `>=` (or same-day-if-not-yet-passed) implementation returns *today's* slot
    again immediately after that slot just fired — the loop wakes, recomputes while
    `now` is still inside the target hour, gets a past-or-equal target, sleeps a
    non-positive duration, and fires again in a tight burst until the hour rolls over.
    Strict-future avoids that: asyncio.sleep never wakes early, so once a slot has
    fired, the next call to this function (with a *later* `now`) always lands beyond it
    or exactly a week later."""
    candidate = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    candidate += timedelta(days=(DAY_CODES[day] - now.weekday()) % 7)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def _format_recap(records: list[UploadRecord], project_names: dict[str, str]) -> str:
    if not records:
        return "No transcripts uploaded this week."

    by_project: dict[str, list[UploadRecord]] = defaultdict(list)
    for record in records:
        by_project[record.project_id].append(record)

    ordered = sorted(by_project.items(), key=lambda item: len(item[1]), reverse=True)

    lines = []
    for project_id, project_records in ordered:
        name = project_names.get(project_id, project_id)
        lines.append(f"{name} ({len(project_records)})")
        for record in project_records:
            suffix = f" — {record.channel_name}" if record.channel_name else ""
            lines.append(f"• “{record.video_title}”{suffix}")
        lines.append("")

    lines.append(f"{len(records)} transcripts uploaded this week.")
    return "\n".join(lines).strip()


async def _send_recap(app: Application, config: Config, client: ClaudeClient) -> None:
    now = datetime.now(UTC)
    all_records = load_history(config.storage.data_dir)
    cutoff = (now - RECAP_WINDOW).isoformat()
    recent = [r for r in all_records if r.uploaded_at >= cutoff]

    projects = await asyncio.to_thread(client.list_projects)
    project_names = {p["uuid"]: p["name"] for p in projects}

    window_start = (now - RECAP_WINDOW).date().isoformat()
    window_end = now.date().isoformat()
    header = f"\U0001f4c5 Weekly recap — {window_start} to {window_end}\n\n"
    await notify(app, config, header + _format_recap(recent, project_names))


async def run_weekly_recap(app: Application, config: Config) -> None:
    client: ClaudeClient = app.bot_data["claude_client"]
    logger.info(
        "Weekly recap started: %s %02d:00 UTC",
        config.weekly_recap.day,
        config.weekly_recap.hour,
    )
    while True:
        target = _next_occurrence(
            config.weekly_recap.day,
            config.weekly_recap.hour,
            datetime.now(UTC),
        )
        await asyncio.sleep((target - datetime.now(UTC)).total_seconds())
        try:
            await _send_recap(app, config, client)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Unlike the poller/pending-transcript retrier, PersistenceError here is
            # NOT re-raised as fatal: the recap is a best-effort visibility aid, and a
            # corrupt upload_history.json must not take down the whole bot. Log and
            # retry next week instead.
            logger.exception("Weekly recap failed; will retry next week")
