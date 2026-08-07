"""Append-only record of successful uploads, for the weekly recap.

Distinct from ``poller_state.json`` / ``petition_queue.json``, which both *remove* an
entry once it's uploaded (they track "not yet done", not history). This is the only
durable record of what has ever landed.

Best-effort by design: a failed history write must never crash a subsystem (the poller
re-raises `PersistenceError` as fatal from its own tick) or block a user's upload
confirmation. `record_upload` logs and swallows instead of propagating — the same
"log loudly, don't propagate" shape as `notify()`. This deliberately diverges from
`queue.ack`/`queue.release`, which do propagate persistence failures, because losing an
ack/release would silently corrupt queue state, whereas losing a history entry only
narrows next week's recap.
"""

from dataclasses import asdict, dataclass
from pathlib import Path

from .logger import get_logger
from .persistence import CorruptDataError, PersistenceError, atomic_write_json, load_json

logger = get_logger(__name__)

HISTORY_SCHEMA_VERSION = 1
_FILE_NAME = "upload_history.json"


@dataclass(frozen=True, slots=True)
class UploadRecord:
    project_id: str
    file_name: str
    video_title: str
    channel_name: str
    uploaded_at: str  # ISO-8601 UTC
    # Lets the weekly recap link each title back to the video. Defaulted rather than
    # required, and deliberately without a HISTORY_SCHEMA_VERSION bump: load_history
    # does UploadRecord(**item), so records written before this field existed keep
    # loading and simply render as plain text.
    video_id: str | None = None


def record_upload(data_dir: Path, record: UploadRecord) -> None:
    """Append one record to upload_history.json. Read-modify-write under the same
    atomic_write_json pattern already used for poller_state.json / petition_queue.json
    — safe for the low, bursty write frequency of uploads (nothing here needs a real
    append-only log format).

    Failures are logged and swallowed, not propagated: the history is a visibility aid,
    not a correctness-critical record, and must never crash an upload path or a
    supervised task over a failed side write."""
    path = data_dir / _FILE_NAME
    try:
        records = load_history(data_dir)
        records.append(record)
        payload = {"version": HISTORY_SCHEMA_VERSION, "records": [asdict(r) for r in records]}
        atomic_write_json(path, payload)
    except PersistenceError:
        logger.exception("Failed to record upload history for %s; continuing", record.file_name)


def find_upload(data_dir: Path, project_id: str, video_id: str) -> UploadRecord | None:
    """The most recent recorded upload of `video_id` into `project_id`, if any.

    This is what lets duplicate detection key on the *video* rather than on a doc name
    (see `upload_service.TranscriptUploadService.find_existing`): the name a path builds
    depends on metadata that may have been unavailable when it built it, but the video
    id is the same everywhere. Records written before `video_id` existed are skipped —
    they carry None and can't identify anything.

    Same best-effort policy as `record_upload`: a missing or unreadable history means
    "nothing known", never a failed upload. Callers fall back to matching by name, which
    is exactly the behaviour that predates this function."""
    try:
        records = load_history(data_dir)
    except PersistenceError:
        logger.exception("Could not read upload history to check for %s; continuing", video_id)
        return None
    return next(
        (r for r in reversed(records) if r.project_id == project_id and r.video_id == video_id),
        None,
    )


def load_history(data_dir: Path) -> list[UploadRecord]:
    """Missing file: empty history (valid — no uploads yet). Corrupt file: fails closed
    via CorruptDataError, same policy as poller_state.json / channels.json."""
    path = data_dir / _FILE_NAME
    raw = load_json(path)
    if raw is None:
        return []
    valid_shape = isinstance(raw, dict) and raw.get("version") == HISTORY_SCHEMA_VERSION
    if not valid_shape or "records" not in raw:
        raise CorruptDataError(path, "unrecognized upload history schema")
    try:
        return [UploadRecord(**item) for item in raw["records"]]
    except (KeyError, TypeError) as e:
        raise CorruptDataError(path, f"malformed upload record: {e}") from e
