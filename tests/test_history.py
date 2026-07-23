import json
from pathlib import Path

import pytest

from knowledger.history import UploadRecord, load_history, record_upload
from knowledger.persistence import CorruptDataError


def test_load_history_missing_file_is_empty(tmp_path: Path) -> None:
    assert load_history(tmp_path) == []


def test_record_upload_round_trips(tmp_path: Path) -> None:
    record = UploadRecord(
        project_id="p1",
        file_name="f1",
        video_title="T1",
        channel_name="C1",
        uploaded_at="2026-07-17T18:00:00+00:00",
    )
    record_upload(tmp_path, record)
    assert load_history(tmp_path) == [record]


def test_record_upload_appends(tmp_path: Path) -> None:
    first = UploadRecord("p1", "f1", "T1", "C1", "2026-07-17T18:00:00+00:00")
    second = UploadRecord("p2", "f2", "T2", "C2", "2026-07-18T18:00:00+00:00")
    record_upload(tmp_path, first)
    record_upload(tmp_path, second)
    assert load_history(tmp_path) == [first, second]


def test_load_history_rejects_corrupt_schema(tmp_path: Path) -> None:
    path = tmp_path / "upload_history.json"
    path.write_text(json.dumps({"version": 1, "records": [{"project_id": "p"}]}))
    with pytest.raises(CorruptDataError):
        load_history(tmp_path)


def test_load_history_rejects_unknown_version(tmp_path: Path) -> None:
    path = tmp_path / "upload_history.json"
    path.write_text(json.dumps({"version": 99, "records": []}))
    with pytest.raises(CorruptDataError):
        load_history(tmp_path)


def test_record_upload_swallows_write_failure(tmp_path: Path) -> None:
    # data_dir itself doesn't exist and can't be created as a file path component —
    # atomic_write_json will hit a missing-directory OSError, which record_upload must
    # log and swallow rather than propagate (best-effort: a bad history write must
    # never crash an upload path).
    bad_dir = tmp_path / "missing" / "nested"
    record = UploadRecord("p1", "f1", "T1", "C1", "2026-07-17T18:00:00+00:00")
    record_upload(bad_dir, record)  # must not raise
    assert not bad_dir.exists()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
