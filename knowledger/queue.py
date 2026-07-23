from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .persistence import CorruptDataError, atomic_write_json, load_json

SCHEMA_VERSION = 2

PENDING = "pending"
IN_FLIGHT = "in_flight"


class OldQueueSchemaError(CorruptDataError):
    """The file is the pre-durable-queue array format. No automatic migration is
    performed — see docs/features/persistent-upload-queue.md for manual conversion."""


@dataclass(frozen=True, slots=True)
class QueueEntry:
    project_id: str
    video_id: str
    file_name: str
    transcript: str
    chat_id: int
    video_title: str
    queued_at: str  # ISO-8601, for display only
    channel_name: str = ""  # for the weekly recap; blank for old entries predating it
    upload_attempts: int = 0  # consecutive non-auth retry failures, for the stuck-entry alert
    overwrite_doc_uuid: str | None = None  # set for an overwrite retry: delete this doc first
    id: str = ""  # assigned by Queue.enqueue(); empty only before first persistence
    state: str = PENDING  # "pending" | "in_flight" — durable claim state
    claimed_at: str | None = None  # ISO-8601 UTC, set while in_flight


@dataclass(slots=True)
class Queue:
    """Durable JSON-persisted upload queue with claim/ack/release semantics.

    A claimed (in_flight) entry is never removed from disk before terminal success
    (ack). A process restart finds any in_flight entry left over from before the crash
    and treats it as abandoned — claim()/recover_abandoned() flip it back to pending so
    it gets retried rather than silently lost."""

    path: Path = field(default_factory=lambda: Path("petition_queue.json"))

    def enqueue(self, entry: QueueEntry) -> QueueEntry | None:
        """Append entry to the queue, assigning it a stable id. Returns the persisted
        entry, or None (without writing) if project_id+video_id is already queued."""
        entries = self._load()
        if any(e.project_id == entry.project_id and e.video_id == entry.video_id for e in entries):
            return None
        persisted = replace(entry, id=str(uuid4()), state=PENDING, claimed_at=None)
        self._save(entries + [persisted])
        return persisted

    def peek(self) -> list[QueueEntry]:
        """Read all queued entries (pending and in_flight) without claiming them."""
        return self._load()

    def remove(self, project_id: str, video_id: str) -> None:
        """Drop any entry for this project/video pair, regardless of state. Used by the
        poller to clear a stale auth-fallback entry once its own direct upload lands."""
        entries = self._load()
        remaining = [
            e for e in entries if not (e.project_id == project_id and e.video_id == video_id)
        ]
        if len(remaining) != len(entries):
            self._save(remaining)

    def recover_abandoned(self) -> int:
        """Flip any in_flight entry back to pending. Nothing in this process can have a
        legitimate in_flight claim before it has run its own claim()/ack()/release()
        cycle, so any in_flight entry found is a claim abandoned by a prior process
        (crash or unclean shutdown). Safe to call anytime; intended for startup."""
        entries = self._load()
        recovered = [
            replace(e, state=PENDING, claimed_at=None) if e.state == IN_FLIGHT else e
            for e in entries
        ]
        count = sum(1 for e in entries if e.state == IN_FLIGHT)
        if count:
            self._save(recovered)
        return count

    def claim(self, exclude: frozenset[str] = frozenset()) -> QueueEntry | None:
        """Atomically pick one pending entry (not in `exclude`) and mark it in_flight,
        persisting the claim before returning it. Does NOT touch existing in_flight
        entries — those may be legitimately claimed elsewhere in this same process
        (e.g. claim_by_id()), so resetting them here on every call would race with
        whoever holds that claim. Recovering in_flight entries abandoned by a prior
        process's crash is recover_abandoned()'s job, called once at startup. Returns
        None if there is nothing eligible to claim."""
        entries = self._load()
        target = next((e for e in entries if e.state == PENDING and e.id not in exclude), None)
        if target is None:
            return None
        claimed = replace(target, state=IN_FLIGHT, claimed_at=datetime.now(UTC).isoformat())
        self._save([claimed if e.id == claimed.id else e for e in entries])
        return claimed

    def claim_by_id(self, entry_id: str) -> QueueEntry | None:
        """Claim a specific pending entry (e.g. one just enqueued) for immediate,
        synchronous processing. Returns None if it is missing or already claimed
        (e.g. a concurrent drain got to it first) — the caller should not retry."""
        entries = self._load()
        target = next((e for e in entries if e.id == entry_id and e.state == PENDING), None)
        if target is None:
            return None
        claimed = replace(target, state=IN_FLIGHT, claimed_at=datetime.now(UTC).isoformat())
        self._save([claimed if e.id == claimed.id else e for e in entries])
        return claimed

    def ack(self, entry_id: str) -> None:
        """Permanently remove an entry after confirmed terminal success (upload landed,
        or confirmed already-landed). Persistence failures propagate — an ack that
        can't be durably recorded must not be treated as having happened."""
        entries = self._load()
        remaining = [e for e in entries if e.id != entry_id]
        self._save(remaining)

    def release(self, entry_id: str, *, increment_attempts: bool = False) -> None:
        """Return a claimed entry to pending after a transient failure (or an auth
        failure, which does not count toward the attempt limit). Persistence failures
        propagate — a release that can't be durably recorded must not be swallowed,
        since that would silently erase the entry from disk."""
        entries = self._load()
        updated = []
        for e in entries:
            if e.id == entry_id:
                attempts = e.upload_attempts + 1 if increment_attempts else e.upload_attempts
                e = replace(e, state=PENDING, claimed_at=None, upload_attempts=attempts)
            updated.append(e)
        self._save(updated)

    def _load(self) -> list[QueueEntry]:
        raw = load_json(self.path)
        if raw is None:
            return []
        if isinstance(raw, list):
            raise OldQueueSchemaError(
                self.path,
                "pre-durable-queue array format detected; manual conversion required "
                "before this version can start (see docs/features/persistent-upload-queue.md)",
            )
        valid_shape = isinstance(raw, dict) and raw.get("version") == SCHEMA_VERSION
        if not valid_shape or "entries" not in raw:
            raise CorruptDataError(self.path, "unrecognized queue schema")
        try:
            return [QueueEntry(**item) for item in raw["entries"]]
        except (KeyError, TypeError) as e:
            raise CorruptDataError(self.path, f"malformed queue entry: {e}") from e

    def _save(self, entries: list[QueueEntry]) -> None:
        payload = {"version": SCHEMA_VERSION, "entries": [asdict(e) for e in entries]}
        atomic_write_json(self.path, payload)


def build_auth_fallback_entry(
    *,
    project_id: str,
    video_id: str,
    file_name: str,
    transcript: str,
    chat_id: int,
    video_title: str,
    channel_name: str = "",
    overwrite_doc_uuid: str | None = None,
) -> QueueEntry:
    """Build a QueueEntry for the auth-fallback path: upload hit AuthError but a
    transcript is already in hand, so it gets parked in petition_queue.json for
    /refresh to retry instead of being lost. Shared by the poller and the
    interactive-flow transcript retrier, which both hit this same fallback."""
    return QueueEntry(
        project_id=project_id,
        video_id=video_id,
        file_name=file_name,
        transcript=transcript,
        chat_id=chat_id,
        video_title=video_title,
        queued_at=datetime.now(UTC).isoformat(),
        channel_name=channel_name,
        overwrite_doc_uuid=overwrite_doc_uuid,
    )
