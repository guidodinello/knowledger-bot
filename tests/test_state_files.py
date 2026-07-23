import json
from pathlib import Path

import pytest

from knowledger.persistence import CorruptDataError
from knowledger.poller import Channel, PollerState, load_channels
from knowledger.queue import OldQueueSchemaError, Queue, QueueEntry


def test_queue_missing_file_is_empty(tmp_path: Path) -> None:
    queue = Queue(path=tmp_path / "q.json")
    assert queue.peek() == []


def test_queue_rejects_old_array_schema(tmp_path: Path) -> None:
    path = tmp_path / "q.json"
    path.write_text(json.dumps([{"project_id": "p", "video_id": "v"}]))
    queue = Queue(path=path)
    with pytest.raises(OldQueueSchemaError):
        queue.peek()
    assert json.loads(path.read_text()) == [{"project_id": "p", "video_id": "v"}]  # untouched


def test_queue_rejects_corrupt_entry_schema(tmp_path: Path) -> None:
    path = tmp_path / "q.json"
    path.write_text(json.dumps({"version": 2, "entries": [{"project_id": "p"}]}))
    queue = Queue(path=path)
    with pytest.raises(CorruptDataError):
        queue.peek()


def test_queue_enqueue_dedups_on_project_and_video(tmp_path: Path) -> None:
    queue = Queue(path=tmp_path / "q.json")
    entry = QueueEntry(
        project_id="p",
        video_id="v",
        file_name="f",
        transcript="t",
        chat_id=1,
        video_title="T",
        queued_at="now",
    )
    first = queue.enqueue(entry)
    second = queue.enqueue(entry)
    assert first is not None
    assert second is None
    assert len(queue.peek()) == 1


def test_queue_entry_without_channel_name_defaults_blank(tmp_path: Path) -> None:
    # A version-2 queue file written before channel_name existed must still load —
    # the field is optional/defaulted, not a schema bump.
    path = tmp_path / "q.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "entries": [
                    {
                        "project_id": "p",
                        "video_id": "v",
                        "file_name": "f",
                        "transcript": "t",
                        "chat_id": 1,
                        "video_title": "T",
                        "queued_at": "now",
                    },
                ],
            },
        ),
    )
    entries = Queue(path=path).peek()
    assert entries[0].channel_name == ""


def test_poller_state_missing_file_is_empty(tmp_path: Path) -> None:
    state = PollerState.load(tmp_path / "state.json")
    assert state.seen == set()
    assert state.pending == []


def test_poller_state_corrupt_file_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("not json")
    with pytest.raises(CorruptDataError):
        PollerState.load(path)
    assert path.read_text() == "not json"  # untouched — no baseline reseed side effect


def test_poller_state_malformed_schema_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"seen": ["a"]}))  # missing "pending"
    with pytest.raises(CorruptDataError):
        PollerState.load(path)


def test_channels_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_channels(tmp_path / "channels.json") == []


def test_channels_corrupt_file_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "channels.json"
    path.write_text("not json")
    with pytest.raises(CorruptDataError):
        load_channels(path)


def test_channels_valid_file_loads(tmp_path: Path) -> None:
    path = tmp_path / "channels.json"
    path.write_text(json.dumps([{"handle": "@x", "name": "X"}]))
    channels = load_channels(path)
    assert channels == [Channel(handle="@x", name="X")]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
