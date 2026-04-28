import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .logger import get_logger

logger = get_logger(__name__)

QUEUE_FILE = Path("petition_queue.json")


@dataclass(frozen=True)
class QueueEntry:
    project_id: str
    file_name: str
    transcript: str
    chat_id: int
    video_title: str
    queued_at: str  # ISO-8601, for display only


def _load(path: Path) -> list[QueueEntry]:
    try:
        return [QueueEntry(**item) for item in json.loads(path.read_text())]
    except FileNotFoundError:
        return []
    except Exception:
        logger.warning("Queue file %s is corrupt or unreadable; treating as empty", path)
        return []


def _save(entries: list[QueueEntry], path: Path) -> None:
    path.write_text(json.dumps([asdict(e) for e in entries], indent=2))


def enqueue(entry: QueueEntry, path: Path = QUEUE_FILE) -> bool:
    """Append entry to the queue. Returns False (without writing) if already queued."""
    entries = _load(path)
    if any(e.project_id == entry.project_id and e.file_name == entry.file_name for e in entries):
        return False
    _save(entries + [entry], path)
    return True


def drain_queue(path: Path = QUEUE_FILE) -> list[QueueEntry]:
    """Remove and return all queued entries atomically."""
    entries = _load(path)
    if entries:
        _save([], path)
    return entries
