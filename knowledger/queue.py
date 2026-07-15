import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class QueueEntry:
    project_id: str
    video_id: str
    file_name: str
    transcript: str
    chat_id: int
    video_title: str
    queued_at: str  # ISO-8601, for display only
    upload_attempts: int = 0  # consecutive non-auth retry failures, for the stuck-entry alert
    overwrite_doc_uuid: str | None = None  # set for an overwrite retry: delete this doc first


@dataclass(slots=True)
class Queue:
    path: Path = field(default_factory=lambda: Path("petition_queue.json"))

    def enqueue(self, entry: QueueEntry) -> bool:
        """Append entry to the queue. Returns False (without writing) if already queued."""
        entries = self._load()
        if any(e.project_id == entry.project_id and e.video_id == entry.video_id for e in entries):
            return False
        self._save(entries + [entry])
        return True

    def peek(self) -> list[QueueEntry]:
        """Read all queued entries without clearing them. Missing/corrupt file is empty."""
        return self._load()

    def remove(self, project_id: str, video_id: str) -> None:
        """Drop any entry for this project/video pair. No-op if none is queued."""
        entries = self._load()
        remaining = [
            e for e in entries if not (e.project_id == project_id and e.video_id == video_id)
        ]
        if len(remaining) != len(entries):
            self._save(remaining)

    def _load(self) -> list[QueueEntry]:
        try:
            return [
                QueueEntry(**item) for item in json.loads(self.path.read_text(encoding="utf-8"))
            ]
        except FileNotFoundError:
            return []
        except Exception:
            logger.warning(
                "Queue file %s is corrupt or unreadable; treating as empty",
                self.path,
                exc_info=True,
            )
            return []

    def _save(self, entries: list[QueueEntry]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(e) for e in entries], indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
