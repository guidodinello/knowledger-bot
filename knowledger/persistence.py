"""Shared atomic JSON read/write with fail-closed error semantics.

A missing file is a valid empty/first-run state. Everything else — corrupt JSON, bad
UTF-8, or an unreadable/unwritable path — is a `PersistenceError` that callers must let
propagate. The alternative (silently treating a corrupt file as empty) risks a
subsequent write permanently discarding data that was only temporarily unreadable.
"""

import json
import os
from contextlib import suppress
from pathlib import Path
from typing import Any


class PersistenceError(Exception):
    """Base for all fail-closed persistence errors. Always carries the offending path."""

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"{path}: {detail}")
        self.path = path


class CorruptDataError(PersistenceError):
    """The file exists but its contents are not valid JSON/UTF-8, or fail schema checks."""


class PersistenceIOError(PersistenceError):
    """The file exists but could not be read or written (permissions, disk, etc.)."""


def load_json(path: Path) -> Any | None:
    """Read and parse `path` as JSON. Returns None if the file does not exist (valid
    empty state). Raises CorruptDataError on bad UTF-8/JSON, PersistenceIOError on any
    other OSError. Never modifies the file."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as e:
        raise CorruptDataError(path, f"not valid UTF-8: {e}") from e
    except OSError as e:
        raise PersistenceIOError(path, f"could not be read: {e}") from e

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise CorruptDataError(path, f"not valid JSON: {e}") from e


def atomic_write_json(path: Path, data: Any, *, mode: int | None = None) -> None:
    """Write `data` as JSON to `path` atomically (same-directory temp file + os.replace).
    On failure, the destination is left untouched and the temp file is cleaned up where
    possible; the failure always propagates as PersistenceIOError.

    `mode`, if given, sets the temp file's permissions at creation (e.g. 0o600 for a
    sensitive file like a credential) — the default `Path.write_text()` path creates
    the file with umask-default permissions, which os.replace() would then carry over
    to the destination."""
    tmp = path.with_suffix(".tmp")
    try:
        if mode is not None:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, indent=2))
        else:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        with suppress(OSError):
            tmp.unlink()
        raise PersistenceIOError(path, f"could not be written: {e}") from e
