# Follow-up: Persist poller/queue state across container restarts

**Status:** Implemented.
**Value:** Medium (prevents skipped videos on restart)
**Effort:** Low–Medium
**Touches:** `knowledger/config.py`, `knowledger/poller.py`, `knowledger/bot.py`, `Dockerfile`, `deploy.sh`, `.env.oracle`

## Problem

`poller_state.json` (the poller's `seen` set + `pending` list) and `petition_queue.json`
(the auth-failure upload queue) are written inside the container at `/app/`. On every
container recreate (`deploy.sh update` / `env` / `restart`) that state is lost:

- The poller re-runs its **first-run baseline seed**, marking all current feed videos as
  `seen` *without* enqueueing them. So a video detected before a restart but still inside
  its 24h wait window is both forgotten (dropped from `pending`) **and** re-marked seen —
  it will never be picked up. Net effect: **a restart during a video's 24h window skips
  that video.**
- Any transcript parked on `petition_queue.json` awaiting `/refresh` is also lost.

The bot restarts rarely, so the risk is low — but it's real, and the fix is cheap.

## Why NOT to mount the two files individually

Both `queue.py` and `poller.py` save **atomically**: write to a `.tmp` sibling, then
`os.replace(tmp, path)`. With a **single-file bind mount** (`-v host.json:/app/x.json`),
that rename replaces the container's directory entry and **detaches the bind mount** — the
host file goes stale and subsequent writes never reach it. This is a well-known Docker
trap. Do not mount the files directly.

## Fix: mount a data *directory*

Put both state files inside one directory and bind-mount the directory, so `.tmp` and the
final file live on the same filesystem and the atomic rename stays inside the mount.

### 1. `knowledger/config.py`

Add a `data_dir` field (default `.` so local behaviour is unchanged) read from `DATA_DIR`:

```python
# in Config
data_dir: Path = field(default_factory=lambda: Path("."))

# in load_config()
data_dir=Path(os.getenv("DATA_DIR", ".")),
```

### 2. `knowledger/poller.py`

Drop the module-level `POLLER_STATE_FILE = Path("poller_state.json")` and resolve the path
from config inside `run_poller` (which already receives `config`):

```python
state_path = config.data_dir / "poller_state.json"
first_run = not state_path.exists()
state = PollerState.load(state_path)
```

Ensure the directory exists before the first save (mount provides it in prod, but guard
for safety): `config.data_dir.mkdir(parents=True, exist_ok=True)` at the top of
`run_poller`.

### 3. `knowledger/bot.py`

In `build_application`, point the Queue at the data dir instead of the default:

```python
app.bot_data["queue"] = Queue(path=config.data_dir / "petition_queue.json")
```

`http_server.py` / `cmd_refresh` read the queue from `bot_data`, so no other change needed.

### 4. `Dockerfile`

Create the mount point so it exists even before a volume is attached:

```dockerfile
RUN mkdir -p /app/data
```

### 5. `deploy.sh` — `recreate()`

Create the host dir and add the volume mount alongside the existing `cookies.txt` mount:

```bash
$SSH "mkdir -p \$HOME/knowledger-bot/data"
# add to the docker run line:
#   -v \$HOME/knowledger-bot/data:/app/data
```

### 6. `.env.oracle`

```
DATA_DIR=/app/data
```

### 7. `.gitignore`

`poller_state.json` / `petition_queue.json` are already ignored by basename, so they stay
ignored under `data/`. Optionally add `/data/` to be explicit. Do **not** commit real
state.

## Verification

1. `uv run ruff check .` clean; local run still writes state to the repo root (DATA_DIR
   unset → `.`).
2. Deploy, let the poller detect a video (or hand-craft a `pending` entry in
   `~/knowledger-bot/data/poller_state.json`), then `deploy.sh restart`. Confirm the
   `pending` entry **survives** the restart (it's read back from the host file) instead of
   being wiped by the baseline seed.
3. Confirm `docker logs` shows the poller loading existing state rather than
   "Baseline-seeded N existing videos" on every restart.

## Sequencing note

When implementing: change the **code + Dockerfile first** (so the image creates `/app/data`
and reads `DATA_DIR`), then `deploy.sh update`, and only **after** the new image is live add
`DATA_DIR=/app/data` to `.env.oracle` + `deploy.sh env`. Setting `DATA_DIR` before the
mount/dir exists would break state writes.
