import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from telegram.ext import Application

from knowledger.claude_client import AuthError, ClaudeClient
from knowledger.config import (
    ClaudeSettings,
    Config,
    LoggerConfig,
    PollerSettings,
    StorageSettings,
    TelegramSettings,
)
from knowledger.poller import (
    UPLOAD_DELAY,
    Channel,
    PendingVideo,
    PollerState,
    TranscriptPoller,
    run_poller,
)
from knowledger.queue import Queue


class FakeClaudeClient:
    def __init__(self, projects: list[dict]) -> None:
        self.projects = projects
        self.uploads: list[tuple[str, str]] = []

    def list_projects(self) -> list[dict]:
        return self.projects

    def list_docs(self, project_id: str) -> list[dict]:
        return []

    def upload_content(self, project_id: str, content: str, file_name: str) -> None:
        self.uploads.append((project_id, file_name))


def _config(tmp_path: Path, **poller_kwargs: object) -> Config:
    return Config(
        logger=LoggerConfig(),
        telegram=TelegramSettings(bot_token="x", allowed_user_ids=frozenset({1})),
        claude=ClaudeSettings(session_token="x"),
        storage=StorageSettings(data_dir=tmp_path),
        poller=PollerSettings(**poller_kwargs) if poller_kwargs else PollerSettings(),
    )


def _old_video(channel_id: str, video_id: str) -> PendingVideo:
    published = (datetime.now(UTC) - UPLOAD_DELAY - timedelta(hours=1)).isoformat()
    return PendingVideo(
        channel_id=channel_id,
        video_id=video_id,
        title="T",
        channel_name="Ch",
        published=published,
        first_seen=published,
    )


def _poller(
    tmp_path: Path,
    client: FakeClaudeClient,
    channels: list[Channel],
    state: PollerState,
    project_name: str = "Default",
) -> TranscriptPoller:
    return TranscriptPoller(
        app=cast(Application, object()),
        config=_config(tmp_path),
        client=cast(ClaudeClient, client),
        queue=Queue(path=tmp_path / "q.json"),
        channels=channels,
        state=state,
        project_name=project_name,
    )


@pytest.fixture(autouse=True)
def _no_notify_no_feed():
    with (
        patch("knowledger.poller.notify", new=AsyncMock()),
        patch("knowledger.poller.fetch_feed", return_value=[]),
        patch("knowledger.poller.fetch_transcript", return_value="transcript text"),
    ):
        yield


# --- Part A: per-channel project routing ------------------------------------------


def test_tick_routes_each_channels_video_to_its_own_project(tmp_path: Path) -> None:
    client = FakeClaudeClient(
        [
            {"uuid": "default-id", "name": "Default"},
            {"uuid": "exercise-id", "name": "Exercise"},
        ],
    )
    channels = [
        Channel(handle="@a", name="A", channel_id="chan-a"),  # uses the global default
        Channel(handle="@b", name="B", channel_id="chan-b", project="Exercise"),
    ]
    state = PollerState(path=tmp_path / "state.json")
    state.pending = [_old_video("chan-a", "va"), _old_video("chan-b", "vb")]
    poller = _poller(tmp_path, client, channels, state)

    asyncio.run(poller._tick())

    assert state.pending == []
    project_ids_used = {project_id for project_id, _ in client.uploads}
    assert project_ids_used == {"default-id", "exercise-id"}


def test_tick_leaves_video_pending_when_channel_project_is_misconfigured(
    tmp_path: Path,
) -> None:
    client = FakeClaudeClient([{"uuid": "default-id", "name": "Default"}])
    channels = [
        Channel(handle="@a", name="A", channel_id="chan-a"),
        Channel(handle="@b", name="B", channel_id="chan-b", project="Nonexistent"),
    ]
    state = PollerState(path=tmp_path / "state.json")
    state.pending = [_old_video("chan-a", "va"), _old_video("chan-b", "vb")]
    poller = _poller(tmp_path, client, channels, state)

    asyncio.run(poller._tick())

    # chan-a's video uploaded normally; chan-b's stays pending, not dropped.
    assert [v.video_id for v in state.pending] == ["vb"]
    assert {pid for pid, _ in client.uploads} == {"default-id"}


def test_tick_auth_error_resolving_projects_skips_processing_and_notifies(
    tmp_path: Path,
) -> None:
    class FailingClient(FakeClaudeClient):
        def list_projects(self) -> list[dict]:
            raise AuthError("token expired")

    client = FailingClient([])
    channels = [Channel(handle="@a", name="A", channel_id="chan-a")]
    state = PollerState(path=tmp_path / "state.json")
    state.pending = [_old_video("chan-a", "va")]
    poller = _poller(tmp_path, client, channels, state)

    asyncio.run(poller._tick())

    assert state.auth_error_notified is True
    assert [v.video_id for v in state.pending] == ["va"]  # left untouched


# --- Part B: per-channel baseline seeding -------------------------------------------


def _feed_video(channel_id: str, video_id: str) -> PendingVideo:
    now = datetime.now(UTC).isoformat()
    return PendingVideo(
        channel_id=channel_id,
        video_id=video_id,
        title="T",
        channel_name="Ch",
        published=now,
        first_seen=now,
    )


def test_run_poller_only_baseline_seeds_the_newly_added_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(
        json.dumps(
            [
                {"handle": "@a", "name": "A", "channel_id": "chan-a"},
                {"handle": "@b", "name": "B", "channel_id": "chan-b", "project": "Exercise"},
            ],
        ),
    )
    state_path = tmp_path / "poller_state.json"
    PollerState(path=state_path, seen={"old-a"}, baseline_seeded={"chan-a"}).save()

    def fake_fetch_feed(channel_id: str, proxy=None) -> list[PendingVideo]:
        return [_feed_video(channel_id, f"new-{channel_id}")]

    monkeypatch.setattr("knowledger.poller.fetch_feed", fake_fetch_feed)
    monkeypatch.setattr(TranscriptPoller, "run", AsyncMock())

    config = _config(
        tmp_path,
        auto_transcript_project="Default",
        channels_path=channels_path,
    )
    app = cast(
        Application,
        SimpleNamespace(
            bot_data={"claude_client": object(), "queue": Queue(path=tmp_path / "q.json")},
        ),
    )

    asyncio.run(run_poller(app, config))

    final_state = PollerState.load(state_path)
    assert final_state.baseline_seeded == {"chan-a", "chan-b"}
    assert "new-chan-b" in final_state.seen  # newly-added channel got baseline-seeded
    assert "new-chan-a" not in final_state.seen  # already-seeded channel untouched
    assert final_state.pending == []  # baseline seeding never enqueues


def test_run_poller_backward_compat_upgrade_reseeds_without_flooding_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A state file written before per-channel baseline seeding existed has no
    `baseline_seeded` key. On upgrade, every already-tracked channel gets
    baseline-seeded once — but since their current videos are already in `seen`,
    this is a no-op: nothing new gets enqueued."""
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(
        json.dumps([{"handle": "@a", "name": "A", "channel_id": "chan-a"}]),
    )
    state_path = tmp_path / "poller_state.json"
    state_path.write_text(json.dumps({"seen": ["existing-a"], "pending": []}))

    def fake_fetch_feed(channel_id: str, proxy=None) -> list[PendingVideo]:
        return [_feed_video("chan-a", "existing-a")]  # same video already seen

    monkeypatch.setattr("knowledger.poller.fetch_feed", fake_fetch_feed)
    monkeypatch.setattr(TranscriptPoller, "run", AsyncMock())

    config = _config(
        tmp_path,
        auto_transcript_project="Default",
        channels_path=channels_path,
    )
    app = cast(
        Application,
        SimpleNamespace(
            bot_data={"claude_client": object(), "queue": Queue(path=tmp_path / "q.json")},
        ),
    )

    asyncio.run(run_poller(app, config))

    final_state = PollerState.load(state_path)
    assert final_state.baseline_seeded == {"chan-a"}
    assert final_state.seen == {"existing-a"}
    assert final_state.pending == []


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
