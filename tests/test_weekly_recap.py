import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from telegram.ext import Application

from knowledger.claude_client import ClaudeClient
from knowledger.config import (
    ClaudeSettings,
    Config,
    LoggerConfig,
    StorageSettings,
    TelegramSettings,
)
from knowledger.history import UploadRecord, record_upload
from knowledger.weekly_recap import _format_recap, _next_occurrence, _send_recap


def test_next_occurrence_at_exact_target_returns_following_week() -> None:
    # The critical regression: recomputing right as the target slot fires must NOT
    # return the same instant again (that would burst-refire until the hour rolls
    # over). Friday 2026-07-17 18:00:00 UTC is a Friday.
    now = datetime(2026, 7, 17, 18, 0, 0, tzinfo=UTC)
    result = _next_occurrence("FRI", 18, now)
    assert result == datetime(2026, 7, 24, 18, 0, 0, tzinfo=UTC)
    assert result > now


def test_next_occurrence_just_before_target_is_today() -> None:
    now = datetime(2026, 7, 17, 17, 59, 59, tzinfo=UTC)
    result = _next_occurrence("FRI", 18, now)
    assert result == datetime(2026, 7, 17, 18, 0, 0, tzinfo=UTC)


def test_next_occurrence_just_after_target_is_next_week() -> None:
    now = datetime(2026, 7, 17, 18, 0, 1, tzinfo=UTC)
    result = _next_occurrence("FRI", 18, now)
    assert result == datetime(2026, 7, 24, 18, 0, 0, tzinfo=UTC)


def test_next_occurrence_non_matching_weekday() -> None:
    # Monday 2026-07-13, looking for the next Friday.
    now = datetime(2026, 7, 13, 9, 0, 0, tzinfo=UTC)
    result = _next_occurrence("FRI", 18, now)
    assert result == datetime(2026, 7, 17, 18, 0, 0, tzinfo=UTC)


def test_format_recap_empty_week() -> None:
    assert _format_recap([], {}) == "No transcripts uploaded this week."


def test_format_recap_groups_by_project_sorted_by_count() -> None:
    records = [
        UploadRecord("p1", "f1", "T1", "C1", "2026-07-17T18:00:00+00:00"),
        UploadRecord("p2", "f2", "T2", "C2", "2026-07-17T18:00:00+00:00"),
        UploadRecord("p2", "f3", "T3", "C2", "2026-07-17T18:00:00+00:00"),
    ]
    text = _format_recap(records, {"p1": "Investments", "p2": "Exercise"})
    assert text.index("Exercise (2)") < text.index("Investments (1)")
    assert "“T2” — C2" in text
    assert "3 transcripts uploaded this week." in text


def test_format_recap_omits_channel_suffix_when_blank() -> None:
    records = [UploadRecord("p1", "f1", "T1", "", "2026-07-17T18:00:00+00:00")]
    text = _format_recap(records, {"p1": "Investments"})
    assert "“T1”" in text
    assert "—" not in text.split("\n", 1)[1]  # no channel suffix on the item line


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, parse_mode=None) -> None:
        self.sent.append((chat_id, text))


class FakeTelegramApp:
    def __init__(self) -> None:
        self.bot = FakeBot()


class FakeClaudeClient:
    """Only list_projects() is exercised by _send_recap — see FakeClaudeClient in
    tests/test_bot_queue.py for the equivalent used against the upload paths."""

    def __init__(self, projects: list[dict]) -> None:
        self._projects = projects

    def list_projects(self):
        return self._projects


def _config(data_dir: Path) -> Config:
    return Config(
        logger=LoggerConfig(),
        telegram=TelegramSettings(bot_token="x", allowed_user_ids=frozenset({1})),
        claude=ClaudeSettings(session_token="x"),
        storage=StorageSettings(data_dir=data_dir),
    )


def test_send_recap_groups_and_falls_back_to_raw_id_for_deleted_project(
    tmp_path: Path,
) -> None:
    """Exercises the full load -> filter -> map -> format -> notify path, including
    the plan's spec'd fallback: a project deleted after upload (its id no longer in
    list_projects()) must show the raw id rather than erroring."""
    now = datetime.now(UTC).isoformat()
    record_upload(tmp_path, UploadRecord("p1", "f1", "T1", "C1", now))
    record_upload(tmp_path, UploadRecord("p1", "f2", "T2", "C1", now))
    record_upload(tmp_path, UploadRecord("gone", "f3", "T3", "C2", now))

    app = FakeTelegramApp()
    config = _config(tmp_path)
    client = FakeClaudeClient([{"uuid": "p1", "name": "Investments"}])

    asyncio.run(_send_recap(cast(Application, app), config, cast(ClaudeClient, client)))

    assert len(app.bot.sent) == 1
    text = app.bot.sent[0][1]
    assert "Investments (2)" in text
    assert "gone (1)" in text  # deleted project: raw id shown, not an error
    assert "“T1”" in text
    assert "“T3” — C2" in text
    assert "3 transcripts uploaded this week." in text


def test_send_recap_filters_to_the_last_7_days(tmp_path: Path) -> None:
    stale = (datetime.now(UTC) - timedelta(days=8)).isoformat()
    fresh = datetime.now(UTC).isoformat()
    record_upload(tmp_path, UploadRecord("p1", "old", "Old", "C", stale))
    record_upload(tmp_path, UploadRecord("p1", "new", "New", "C", fresh))

    app = FakeTelegramApp()
    config = _config(tmp_path)
    client = FakeClaudeClient([{"uuid": "p1", "name": "Investments"}])

    asyncio.run(_send_recap(cast(Application, app), config, cast(ClaudeClient, client)))

    text = app.bot.sent[0][1]
    assert "“New”" in text
    assert "“Old”" not in text
    assert "1 transcripts uploaded this week." in text


def test_send_recap_empty_history_sends_no_uploads_message(tmp_path: Path) -> None:
    app = FakeTelegramApp()
    config = _config(tmp_path)
    client = FakeClaudeClient([])

    asyncio.run(_send_recap(cast(Application, app), config, cast(ClaudeClient, client)))

    assert "No transcripts uploaded this week." in app.bot.sent[0][1]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
