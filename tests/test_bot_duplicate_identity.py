"""End-to-end cover for the interactive flow's half of identity-based duplicate
detection: sending a link for a video the poller has already stored.

The service layer is covered in tests/test_upload_service.py and the retry path in
tests/test_pending_transcripts.py; what these exercise is `handle_project_selection`'s
wiring of `find_existing` — the path a user actually walks — and which name the
Overwrite branch ends up writing.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telegram import Update

from knowledger.bot import CustomContext, handle_project_selection
from knowledger.config import (
    ClaudeSettings,
    Config,
    LoggerConfig,
    StorageSettings,
    TelegramSettings,
)
from knowledger.history import UploadRecord, record_upload
from knowledger.queue import Queue
from knowledger.youtube import VideoMetadata, build_doc_name

CHANNEL = "On-Chain Mind"
TITLE = "Bitcoin's Top Buyers Are Finally Capitulating"
DATED_NAME = build_doc_name(CHANNEL, TITLE, "2026-08-04")
DATELESS_NAME = build_doc_name(CHANNEL, TITLE, None)


class FakeQuery:
    def __init__(self, data: str) -> None:
        self.data = data
        self.edits: list[str] = []

    async def answer(self, *args, **kwargs) -> None:
        pass

    async def edit_message_text(self, text: str, parse_mode=None, reply_markup=None, **kw) -> None:
        self.edits.append(text)

    async def edit_message_reply_markup(self, reply_markup=None) -> None:
        pass


class FakeUpdate:
    def __init__(self, callback_query) -> None:
        self.message = None
        self.callback_query = callback_query
        self.effective_user = SimpleNamespace(id=1)
        self.effective_chat = SimpleNamespace(id=1)


class FakeContext:
    def __init__(self, bot_data: dict, user_data: dict) -> None:
        self.bot_data = bot_data
        self.user_data = user_data


class FakeClaudeClient:
    def __init__(self) -> None:
        self.projects = [{"uuid": "p1", "name": "Investments"}]
        self.docs: dict[str, list[dict]] = {}
        self.uploaded: list[tuple[str, str]] = []

    def list_projects(self):
        return self.projects

    def list_docs(self, project_id: str):
        return list(self.docs.get(project_id, []))

    def upload_content(self, project_id: str, content: str, file_name: str) -> None:
        self.uploaded.append((project_id, file_name))


def _config(tmp_path: Path) -> Config:
    return Config(
        logger=LoggerConfig(),
        telegram=TelegramSettings(bot_token="x", allowed_user_ids=frozenset({1})),
        claude=ClaudeSettings(session_token="x"),
        storage=StorageSettings(data_dir=tmp_path),
    )


def _run(
    tmp_path: Path,
    client: FakeClaudeClient,
    metadata: VideoMetadata,
) -> tuple[FakeQuery, FakeContext]:
    query = FakeQuery(data="p1:1")
    context = FakeContext(
        bot_data={
            "config": _config(tmp_path),
            "claude_client": client,
            "queue": Queue(path=tmp_path / "petition_queue.json"),
        },
        user_data={"video_1": metadata},
    )
    asyncio.run(
        handle_project_selection(
            cast(Update, FakeUpdate(callback_query=query)),
            cast(CustomContext, context),
        ),
    )
    return query, context


def _metadata(upload_date: str | None) -> VideoMetadata:
    return VideoMetadata(video_id="v1", title=TITLE, channel_name=CHANNEL, upload_date=upload_date)


def _seed_history(tmp_path: Path, file_name: str) -> None:
    record_upload(
        tmp_path,
        UploadRecord(
            project_id="p1",
            file_name=file_name,
            video_title=TITLE,
            channel_name=CHANNEL,
            uploaded_at="2026-08-05T08:32:00+00:00",
            video_id="v1",
        ),
    )


@pytest.fixture(autouse=True)
def _transcript(monkeypatch: pytest.MonkeyPatch):
    """Any test here that reaches a transcript fetch has already failed its point —
    the duplicate check runs first and is supposed to stop the flow before this."""
    monkeypatch.setattr(
        "knowledger.bot.fetch_transcript",
        lambda video_id, proxy=None, cookies_path=None: "transcript text",
    )


def test_link_for_a_poller_uploaded_video_prompts_instead_of_uploading(tmp_path: Path) -> None:
    """The bug this whole change exists for, walked end to end: the poller stored the
    video under its feed-derived dated name, and the watch page is unreachable now, so
    this flow computes the dateless one. The names don't match, but the video id does —
    the user gets the Skip/Overwrite prompt rather than a silent second copy."""
    client = FakeClaudeClient()
    client.docs["p1"] = [{"uuid": "from-poller", "file_name": DATED_NAME}]
    _seed_history(tmp_path, DATED_NAME)

    query, _ = _run(tmp_path, client, _metadata(None))

    assert "overwrite it with a fresh transcript, or skip?" in query.edits[-1].lower()
    assert client.uploaded == []


def test_overwrite_of_an_id_matched_doc_keeps_its_canonical_name(tmp_path: Path) -> None:
    """Overwrite replaces the doc that was found, so it has to write that doc's name.
    Writing the name this flow computed would delete the poller's correctly dated doc
    and put the dateless one back in its place."""
    client = FakeClaudeClient()
    client.docs["p1"] = [{"uuid": "from-poller", "file_name": DATED_NAME}]
    _seed_history(tmp_path, DATED_NAME)

    _, context = _run(tmp_path, client, _metadata(None))

    assert context.user_data["pending_1"]["file_name"] == DATED_NAME


def test_overwrite_heals_a_dateless_doc_when_the_date_is_known_now(tmp_path: Path) -> None:
    """The mirror case: the existing doc is the degraded one from an earlier blocked
    attempt and this flow does have an upload date. Then the replacement is the chance
    to move the video onto its canonical name, so this flow's name wins."""
    client = FakeClaudeClient()
    client.docs["p1"] = [{"uuid": "from-earlier-block", "file_name": DATELESS_NAME}]
    _seed_history(tmp_path, DATELESS_NAME)

    query, context = _run(tmp_path, client, _metadata("2026-08-04"))

    assert "overwrite it with a fresh transcript, or skip?" in query.edits[-1].lower()
    assert context.user_data["pending_1"]["file_name"] == DATED_NAME


def test_an_unrelated_video_in_the_project_still_uploads(tmp_path: Path) -> None:
    """Control: history for a different video must not make this one look present."""
    client = FakeClaudeClient()
    client.docs["p1"] = [{"uuid": "other", "file_name": build_doc_name(CHANNEL, "Other", None)}]
    _seed_history(tmp_path, DATED_NAME)

    _run(tmp_path, client, _metadata(None))

    assert client.uploaded == [("p1", DATELESS_NAME)]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
