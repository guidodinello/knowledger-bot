import json
from pathlib import Path

import pytest

from knowledger.persistence import (
    CorruptDataError,
    PersistenceIOError,
    atomic_write_json,
    load_json,
)


def test_load_json_missing_file_returns_none(tmp_path: Path) -> None:
    assert load_json(tmp_path / "missing.json") is None


def test_load_json_corrupt_json_raises(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    path.write_text("{not json")
    with pytest.raises(CorruptDataError):
        load_json(path)


def test_load_json_bad_utf8_raises(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    path.write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(CorruptDataError):
        load_json(path)


def test_load_json_unreadable_file_raises_io_error(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"a": 1}))
    path.chmod(0o000)
    try:
        with pytest.raises(PersistenceIOError):
            load_json(path)
    finally:
        path.chmod(0o600)


def test_load_json_valid_file_returns_data(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"a": 1}))
    assert load_json(path) == {"a": 1}


def test_atomic_write_json_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    atomic_write_json(path, {"a": 1})
    assert load_json(path) == {"a": 1}
    assert not path.with_suffix(".tmp").exists()


def test_atomic_write_json_failure_leaves_destination_untouched(tmp_path: Path) -> None:
    path = tmp_path / "missing-dir" / "f.json"
    with pytest.raises(PersistenceIOError):
        atomic_write_json(path, {"a": 1})
    assert not path.exists()


def test_corrupt_file_is_never_overwritten_by_a_failed_load(tmp_path: Path) -> None:
    path = tmp_path / "f.json"
    path.write_text("not json")
    with pytest.raises(CorruptDataError):
        load_json(path)
    assert path.read_text() == "not json"  # untouched


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
