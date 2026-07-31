import json
from pathlib import Path

import pytest

from knowledger.poller import Channel, PendingVideo, load_channels
from knowledger.subscriptions import (
    ResolvedChannel,
    SubscriptionError,
    add_subscription,
    find_subscription,
    resolve_subscription,
)
from knowledger.youtube import VideoMetadata, extract_channel_handle


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("@Foo", "@Foo"),
        ("  @Foo  ", "@Foo"),
        ("https://www.youtube.com/@Foo", "@Foo"),
        ("https://youtube.com/@Foo/videos", "@Foo"),
        ("youtube.com/@Foo", "@Foo"),
        ("https://m.youtube.com/@Foo", "@Foo"),
        ("https://www.youtube.com/channel/UCabc123", "channel/UCabc123"),
        ("https://www.youtube.com/user/Legacy", "user/Legacy"),
        ("https://www.youtube.com/c/Legacy", "c/Legacy"),
        # Not channel references.
        ("https://www.youtube.com/watch?v=abc", None),
        ("https://example.com/@Foo", None),
        ("https://www.youtube.com/", None),
        ("just some text", None),
        ("@Foo/bar", None),
    ],
)
def test_extract_channel_handle(text: str, expected: str | None) -> None:
    assert extract_channel_handle(text) == expected


def test_resolve_subscription_from_video_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented path: any video link identifies the channel that published it."""
    monkeypatch.setattr(
        "knowledger.subscriptions.fetch_video_metadata",
        lambda url, proxy: VideoMetadata(
            video_id="v1",
            title="T",
            channel_name="Neurona Financiera",
            upload_date="2026-07-01",
            channel_url="https://www.youtube.com/@NeuronaFinancieraOK",
        ),
    )
    monkeypatch.setattr("knowledger.subscriptions.resolve_channel_id", lambda h, p: "UCabc")

    resolved = resolve_subscription("https://www.youtube.com/watch?v=v1")

    assert resolved == ResolvedChannel(
        handle="@NeuronaFinancieraOK",
        name="Neurona Financiera",
        channel_id="UCabc",
    )


def test_resolve_subscription_from_handle_names_the_channel_from_its_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("knowledger.subscriptions.resolve_channel_id", lambda h, p: "UCabc")
    monkeypatch.setattr(
        "knowledger.subscriptions.fetch_feed",
        lambda channel_id, proxy: [
            PendingVideo(
                channel_id=channel_id,
                video_id="v1",
                title="T",
                channel_name="On Chain Mind",
                published="2026-07-01T00:00:00+00:00",
                first_seen="2026-07-01T00:00:00+00:00",
            ),
        ],
    )

    assert resolve_subscription("@OnChainMind").name == "On Chain Mind"


def test_resolve_subscription_falls_back_to_the_handle_when_the_feed_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(channel_id: str, proxy: object) -> list[PendingVideo]:
        raise RuntimeError("feed down")

    monkeypatch.setattr("knowledger.subscriptions.resolve_channel_id", lambda h, p: "UCabc")
    monkeypatch.setattr("knowledger.subscriptions.fetch_feed", boom)

    resolved = resolve_subscription("@OnChainMind")

    assert resolved.name == "OnChainMind"
    assert resolved.channel_id == "UCabc"


def test_resolve_subscription_channel_id_url_needs_no_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise AssertionError("the id is already in the URL; no lookup should happen")

    monkeypatch.setattr("knowledger.subscriptions.resolve_channel_id", fail)
    monkeypatch.setattr("knowledger.subscriptions.fetch_feed", lambda channel_id, proxy: [])

    resolved = resolve_subscription("https://www.youtube.com/channel/UCabc")

    assert resolved.channel_id == "UCabc"


def test_resolve_subscription_rejects_a_non_youtube_link() -> None:
    with pytest.raises(SubscriptionError, match="doesn't look like a YouTube link"):
        resolve_subscription("https://example.com/whatever")


def test_resolve_subscription_reports_an_unresolvable_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("knowledger.subscriptions.resolve_channel_id", lambda h, p: None)

    with pytest.raises(SubscriptionError, match="Couldn't find the channel id"):
        resolve_subscription("@Ghost")


def _resolved(handle: str = "@Foo", channel_id: str = "UCabc") -> ResolvedChannel:
    return ResolvedChannel(handle=handle, name="Foo", channel_id=channel_id)


def test_add_subscription_appends_to_the_watch_list(tmp_path: Path) -> None:
    path = tmp_path / "channels.json"
    path.write_text(json.dumps([{"handle": "@Old", "name": "Old", "channel_id": "UCold"}]))

    added = add_subscription(path, _resolved(), project="proj-uuid")

    assert added == Channel(handle="@Foo", name="Foo", channel_id="UCabc", project="proj-uuid")
    assert load_channels(path) == [
        Channel(handle="@Old", name="Old", channel_id="UCold"),
        Channel(handle="@Foo", name="Foo", channel_id="UCabc", project="proj-uuid"),
    ]


def test_add_subscription_omits_the_project_key_when_inheriting_the_default(
    tmp_path: Path,
) -> None:
    """A channel on the default project must not write "project": null — poller entries
    distinguish "no override" by the key being absent."""
    path = tmp_path / "channels.json"

    add_subscription(path, _resolved(), project=None)

    assert json.loads(path.read_text()) == [
        {"handle": "@Foo", "name": "Foo", "channel_id": "UCabc"},
    ]


@pytest.mark.parametrize(
    "existing",
    [
        {"handle": "@Other", "name": "Other", "channel_id": "UCabc"},  # same id
        {"handle": "@foo", "name": "Foo", "channel_id": None},  # same handle, id not resolved yet
    ],
)
def test_add_subscription_refuses_duplicates(tmp_path: Path, existing: dict) -> None:
    path = tmp_path / "channels.json"
    path.write_text(json.dumps([existing]))

    assert add_subscription(path, _resolved(), project=None) is None
    assert len(load_channels(path)) == 1


def test_find_subscription_ignores_unrelated_channels() -> None:
    channels = [Channel(handle="@Other", name="Other", channel_id="UCother")]
    assert find_subscription(channels, _resolved()) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
