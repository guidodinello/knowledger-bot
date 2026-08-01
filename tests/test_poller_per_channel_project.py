import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from curl_cffi.requests.exceptions import RequestException
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
from knowledger.persistence import CorruptDataError
from knowledger.poller import (
    UPLOAD_DELAY,
    Channel,
    PendingVideo,
    PollerState,
    TranscriptPoller,
    _resolve_missing_ids,
    _save_existing_channels,
    load_channels,
    run_poller,
    save_channels,
)
from knowledger.queue import Queue
from knowledger.subscriptions import ResolvedChannel, add_subscription


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


def _config(tmp_path: Path, **poller_kwargs: Any) -> Config:
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
    # Every tick re-reads channels_path (so /subscribe takes effect without a restart),
    # so the watch list has to exist on disk here — otherwise the default relative
    # "channels.json" would make these tests depend on the repo's real one.
    channels_path = tmp_path / "channels.json"
    save_channels(channels_path, channels)
    return TranscriptPoller(
        app=cast(Application, object()),
        config=_config(tmp_path, channels_path=channels_path),
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


# --- Part C: picking up watch-list edits without a restart ---------------------------


def test_tick_picks_up_a_newly_subscribed_channel_and_seeds_its_back_catalogue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/subscribe writes channels.json while the poller is running — the next tick must
    start watching the channel, and treat everything already on it as seen so the back
    catalogue is never uploaded."""
    client = FakeClaudeClient([{"uuid": "default-id", "name": "Default"}])
    state = PollerState(path=tmp_path / "state.json")
    poller = _poller(tmp_path, client, [], state)

    back_catalogue = [_feed_video("chan-new", "old-video")]
    monkeypatch.setattr("knowledger.poller.fetch_feed", lambda cid, proxy=None: back_catalogue)
    save_channels(
        tmp_path / "channels.json",
        [Channel(handle="@new", name="New", channel_id="chan-new")],
    )

    asyncio.run(poller._tick())

    assert state.baseline_seeded == {"chan-new"}
    assert state.seen == {"old-video"}
    assert state.pending == []  # seeded, not enqueued

    # A video published after subscribing is picked up normally on the following tick.
    back_catalogue.append(_feed_video("chan-new", "brand-new"))
    asyncio.run(poller._tick())

    assert [v.video_id for v in state.pending] == ["brand-new"]


def test_tick_keeps_watching_when_the_channel_list_becomes_unreadable(
    tmp_path: Path,
) -> None:
    """A corrupt channels.json is a reason to keep watching what we already loaded —
    not to silently stop watching everything."""
    client = FakeClaudeClient([{"uuid": "default-id", "name": "Default"}])
    channels = [Channel(handle="@a", name="A", channel_id="chan-a")]
    state = PollerState(path=tmp_path / "state.json", baseline_seeded={"chan-a"})
    poller = _poller(tmp_path, client, channels, state)
    (tmp_path / "channels.json").write_text("{ not json")

    asyncio.run(poller._tick())

    assert poller._channels == channels


def test_run_poller_still_fails_closed_on_a_corrupt_watch_list(tmp_path: Path) -> None:
    """Softening the *reload* must not soften startup: a corrupt file at boot is a
    configuration error, and starting up watching nothing would hide it."""
    channels_path = tmp_path / "channels.json"
    channels_path.write_text("{ not json")
    config = _config(tmp_path, auto_transcript_project="Default", channels_path=channels_path)
    app = cast(
        Application,
        SimpleNamespace(
            bot_data={"claude_client": object(), "queue": Queue(path=tmp_path / "q.json")},
        ),
    )

    with pytest.raises(CorruptDataError):
        asyncio.run(run_poller(app, config))


def test_tick_uploads_a_pending_video_whose_channel_was_unsubscribed(tmp_path: Path) -> None:
    """Removing a channel from channels.json must not strand videos already detected
    from it — they finish on the global default project."""
    client = FakeClaudeClient([{"uuid": "default-id", "name": "Default"}])
    state = PollerState(path=tmp_path / "state.json")
    state.pending = [_old_video("chan-gone", "v1")]
    poller = _poller(tmp_path, client, [], state)

    asyncio.run(poller._tick())

    assert state.pending == []
    assert [pid for pid, _ in client.uploads] == ["default-id"]


# --- Part C: baseline seeding is not credited unless the feed was actually read -------


def test_tick_does_not_mark_a_channel_seeded_when_its_baseline_fetch_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed baseline fetch must not be recorded as seeded, and the channel must not
    be detected from in the same tick — otherwise its whole back catalogue is enqueued."""
    state = PollerState(path=tmp_path / "state.json")
    channels = [Channel(handle="@a", name="A", channel_id="chan-a")]

    def failing_feed(channel_id: str, proxy=None) -> list[PendingVideo]:
        raise RequestException("YouTube blocked us")

    monkeypatch.setattr("knowledger.poller.fetch_feed", failing_feed)
    poller = _poller(tmp_path, FakeClaudeClient([]), channels, state)

    asyncio.run(poller._tick())

    assert state.baseline_seeded == set()
    assert state.pending == []


def test_tick_skips_detection_for_a_channel_whose_seed_failed_but_whose_feed_then_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flood window: seeding fails, then the very next fetch in the same tick
    succeeds. Every entry would look new, so detection has to skip the channel."""
    state = PollerState(path=tmp_path / "state.json")
    channels = [Channel(handle="@a", name="A", channel_id="chan-a")]
    calls = {"n": 0}

    def flaky_feed(channel_id: str, proxy=None) -> list[PendingVideo]:
        calls["n"] += 1
        if calls["n"] == 1:  # the baseline seed
            raise RequestException("transient block")
        return [_feed_video(channel_id, f"back-catalogue-{i}") for i in range(5)]

    monkeypatch.setattr("knowledger.poller.fetch_feed", flaky_feed)
    poller = _poller(tmp_path, FakeClaudeClient([]), channels, state)

    asyncio.run(poller._tick())

    assert state.pending == []  # no back-catalogue flood
    assert state.seen == set()


def test_tick_reseeds_a_channel_that_was_removed_and_re_added(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing a channel by hand and re-adding it via /subscribe must seed it again —
    a stale `baseline_seeded` marker would upload the whole gap period."""
    state = PollerState(path=tmp_path / "state.json", baseline_seeded={"chan-a"})
    monkeypatch.setattr(
        "knowledger.poller.fetch_feed",
        lambda channel_id, proxy=None: [_feed_video(channel_id, "gap-video")],
    )
    poller = _poller(tmp_path, FakeClaudeClient([]), [], state)  # @a removed by hand
    channels_path = tmp_path / "channels.json"

    asyncio.run(poller._tick())
    assert state.baseline_seeded == set()  # stale marker pruned
    assert PollerState.load(state.path).baseline_seeded == set()  # and persisted

    save_channels(channels_path, [Channel(handle="@a", name="A", channel_id="chan-a")])
    asyncio.run(poller._tick())

    assert state.baseline_seeded == {"chan-a"}
    assert "gap-video" in state.seen
    assert state.pending == []  # seeded, not enqueued


def test_resolve_missing_ids_does_not_clobber_a_concurrent_subscribe(tmp_path: Path) -> None:
    """`_resolve_missing_ids` scrapes for seconds while /subscribe can append to the same
    file. Writing back its stale snapshot would drop the new channel silently."""
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(json.dumps([{"handle": "@slow", "name": "Slow", "channel_id": None}]))
    stale = load_channels(channels_path)

    def resolve_and_meanwhile_subscribe(handle: str, proxy=None) -> str:
        # Simulates /subscribe landing while the scrape is in flight.
        channels_path.write_text(
            json.dumps(
                [
                    {"handle": "@slow", "name": "Slow", "channel_id": None},
                    {"handle": "@new", "name": "New", "channel_id": "chan-new"},
                ],
            ),
        )
        return "chan-slow"

    with patch("knowledger.poller.resolve_channel_id", resolve_and_meanwhile_subscribe):
        _resolve_missing_ids(stale, channels_path, None)

    final = {c.handle: c.channel_id for c in load_channels(channels_path)}
    assert final == {"@slow": "chan-slow", "@new": "chan-new"}


def test_id_backfill_and_subscribe_serialize_their_save_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(
        json.dumps([{"handle": "@slow", "name": "Slow", "channel_id": None}]),
    )
    stale = load_channels(channels_path)
    save_started = Event()
    allow_save = Event()
    subscription_finished = Event()
    real_save = _save_existing_channels

    def paused_save(path: Path, channels: list[Channel]) -> bool:
        save_started.set()
        assert allow_save.wait(timeout=2)
        return real_save(path, channels)

    def subscribe() -> None:
        add_subscription(
            channels_path,
            ResolvedChannel(handle="@new", name="New", channel_id="chan-new"),
            project=None,
        )
        subscription_finished.set()

    monkeypatch.setattr("knowledger.poller.resolve_channel_id", lambda handle, proxy: "chan-slow")
    monkeypatch.setattr("knowledger.poller._save_existing_channels", paused_save)

    with ThreadPoolExecutor(max_workers=2) as executor:
        backfill = executor.submit(_resolve_missing_ids, stale, channels_path, None)
        assert save_started.wait(timeout=2)
        subscription = executor.submit(subscribe)
        assert not subscription_finished.wait(timeout=0.05)
        allow_save.set()
        backfill.result(timeout=2)
        subscription.result(timeout=2)

    final = {c.handle: c.channel_id for c in load_channels(channels_path)}
    assert final == {"@slow": "chan-slow", "@new": "chan-new"}


def test_id_backfill_does_not_recreate_a_deleted_channels_file(tmp_path: Path) -> None:
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(
        json.dumps([{"handle": "@slow", "name": "Slow", "channel_id": None}]),
    )
    stale = load_channels(channels_path)

    real_save = _save_existing_channels

    def delete_before_atomic_swap(path: Path, channels: list[Channel]) -> bool:
        path.unlink()
        return real_save(path, channels)

    with (
        patch("knowledger.poller.resolve_channel_id", return_value="chan-slow"),
        patch("knowledger.poller._save_existing_channels", delete_before_atomic_swap),
    ):
        _resolve_missing_ids(stale, channels_path, None)

    assert stale[0].channel_id == "chan-slow"
    assert not channels_path.exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
