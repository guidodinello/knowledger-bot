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

    def drain(self) -> list[QueueEntry]:
        """Read and clear all queued entries. Missing or corrupt file is treated as empty."""
        entries = self._load()
        if entries:
            self._save([])
        return entries

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
