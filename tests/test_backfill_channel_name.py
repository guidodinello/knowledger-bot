import json
from pathlib import Path

import pytest

from knowledger.persistence import CorruptDataError
from scripts.backfill_channel_name import backfill_pending_transcripts, backfill_queue


def test_backfill_queue_missing_file_is_noop(tmp_path: Path) -> None:
    assert backfill_queue(tmp_path / "q.json", dry_run=False) == 0


def test_backfill_queue_patches_entries_missing_channel_name(tmp_path: Path) -> None:
    path = tmp_path / "q.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "entries": [
                    {"project_id": "p", "video_id": "v1"},
                    {"project_id": "p", "video_id": "v2", "channel_name": "already set"},
                ],
            },
        ),
    )

    patched = backfill_queue(path, dry_run=False)

    assert patched == 1
    result = json.loads(path.read_text())
    assert result["entries"][0]["channel_name"] == ""
    assert result["entries"][1]["channel_name"] == "already set"
    assert path.with_suffix(".json.bak").exists()


def test_backfill_queue_dry_run_does_not_write(tmp_path: Path) -> None:
    path = tmp_path / "q.json"
    path.write_text(json.dumps({"version": 2, "entries": [{"project_id": "p"}]}))

    patched = backfill_queue(path, dry_run=True)

    assert patched == 1
    assert "channel_name" not in json.loads(path.read_text())["entries"][0]
    assert not path.with_suffix(".json.bak").exists()


def test_backfill_queue_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "q.json"
    path.write_text(json.dumps({"version": 2, "entries": [{"project_id": "p"}]}))

    backfill_queue(path, dry_run=False)
    second_run_patched = backfill_queue(path, dry_run=False)

    assert second_run_patched == 0


def test_backfill_queue_rejects_unrecognized_schema(tmp_path: Path) -> None:
    path = tmp_path / "q.json"
    path.write_text(json.dumps([{"project_id": "p"}]))  # pre-durable-queue array format
    with pytest.raises(CorruptDataError):
        backfill_queue(path, dry_run=False)


def test_backfill_pending_transcripts_missing_file_is_noop(tmp_path: Path) -> None:
    assert backfill_pending_transcripts(tmp_path / "p.json", dry_run=False) == 0


def test_backfill_pending_transcripts_patches_entries(tmp_path: Path) -> None:
    path = tmp_path / "p.json"
    path.write_text(json.dumps([{"project_id": "p", "video_id": "v1"}]))

    patched = backfill_pending_transcripts(path, dry_run=False)

    assert patched == 1
    result = json.loads(path.read_text())
    assert result[0]["channel_name"] == ""
    assert path.with_suffix(".json.bak").exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
