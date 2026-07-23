"""One-time migration: backfill channel_name="" into existing petition_queue.json /
pending_transcripts.json entries written before the field was added.

QueueEntry.channel_name and PendingTranscript.channel_name are required fields (no
default) so that no upload can ever silently go unrecorded in the weekly recap. Any
entry missing the key would otherwise fail to load as CorruptDataError the moment the
new bot version starts. Run this once, against the same DATA_DIR the bot uses, BEFORE
starting the new version.

Idempotent (entries that already have channel_name are left untouched) and safe to run
against a DATA_DIR where one or both files don't exist yet (nothing to do). Each file
that needs a change is backed up to "<name>.bak" before being rewritten.

Usage:
    uv run python scripts/backfill_channel_name.py [--data-dir PATH] [--dry-run]

DATA_DIR defaults to "." (matching the bot's own DATA_DIR default).
"""

import argparse
import shutil
from pathlib import Path

from knowledger.persistence import CorruptDataError, atomic_write_json, load_json

QUEUE_FILE = "petition_queue.json"
PENDING_TRANSCRIPTS_FILE = "pending_transcripts.json"


def _backfill(path: Path, entries: list[dict], *, dry_run: bool) -> int:
    patched = [e for e in entries if "channel_name" not in e]
    if not patched:
        print(f"{path}: already up to date, nothing to do")
        return 0

    if dry_run:
        print(f"{path}: would patch {len(patched)} entr{'y' if len(patched) == 1 else 'ies'}")
        return len(patched)

    for entry in patched:
        entry["channel_name"] = ""

    shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    return len(patched)


def backfill_queue(path: Path, *, dry_run: bool) -> int:
    raw = load_json(path)
    if raw is None:
        print(f"{path}: not found, nothing to do")
        return 0
    if not isinstance(raw, dict) or "entries" not in raw:
        raise CorruptDataError(path, "unrecognized queue schema — fix manually before backfilling")

    patched = _backfill(path, raw["entries"], dry_run=dry_run)
    if patched and not dry_run:
        atomic_write_json(path, raw)
        print(f"{path}: patched {patched} entr{'y' if patched == 1 else 'ies'}")
    return patched


def backfill_pending_transcripts(path: Path, *, dry_run: bool) -> int:
    raw = load_json(path)
    if raw is None:
        print(f"{path}: not found, nothing to do")
        return 0
    if not isinstance(raw, list):
        raise CorruptDataError(path, "unrecognized pending-transcripts schema — fix manually")

    patched = _backfill(path, raw, dry_run=dry_run)
    if patched and not dry_run:
        atomic_write_json(path, raw)
        print(f"{path}: patched {patched} entr{'y' if patched == 1 else 'ies'}")
    return patched


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("."))
    parser.add_argument("--dry-run", action="store_true", help="report counts without writing")
    args = parser.parse_args()

    backfill_queue(args.data_dir / QUEUE_FILE, dry_run=args.dry_run)
    backfill_pending_transcripts(args.data_dir / PENDING_TRANSCRIPTS_FILE, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
