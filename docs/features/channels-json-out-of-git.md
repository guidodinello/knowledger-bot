# Chore: Move the Watch List Out of Git and Into `DATA_DIR`

**Status:** In review.
**Value:** Medium (stops every redeploy from wiping `/subscribe` edits and resolved
channel ids, and takes personal subscriptions out of a public repo)
**Effort:** Low
**Touches:** `knowledger/config.py`, `Dockerfile`, `.gitignore`,
`channels.example.json` (new), `deploy.sh`, `.env.example`, `.env.oracle`,
`.github/workflows/deploy.yml`, `README.md`

## Problem

`channels.json` is tracked in git and baked into the image:

```dockerfile
COPY channels.json .
```

But it is not configuration — it is runtime-mutable state. Two code paths write it:

- `_resolve_missing_ids()` (poller.py) backfills a null `channel_id` from the handle and
  persists the result.
- `add_subscription()` (subscriptions.py) appends whatever `/subscribe` resolved.

`/app` is an image layer, not a volume, so both kinds of write vanish on the next
`docker run`. Every deploy silently reverts the watch list to whatever is committed:
`/subscribe` a channel today, redeploy tomorrow, and the bot stops watching it without
saying anything. The `channel_id` backfill then re-scrapes YouTube for the same handles
on every restart.

Separately, the committed file is personal deployment data — seven private channel
subscriptions and two Claude project UUIDs — published in a public repo for no benefit.

The file is already in the same class as `poller_state.json`, `petition_queue.json`,
`session_token.json` and `upload_history.json`: mutable, personal, gitignored, and living
under `DATA_DIR`. It is the one member of that class that got left behind in git.

## Design

### 1. Derive the path from `DATA_DIR` — `knowledger/config.py`

`load_config()` already computes `data_dir` before it constructs `Config`, so the watch
list can simply hang off it:

```python
poller=PollerSettings(
    auto_transcript_project=os.getenv("AUTO_TRANSCRIPT_PROJECT") or None,
    channels_path=data_dir / "channels.json",
    poll_interval=poll_interval,
),
```

`PollerSettings.channels_path` stays a field with its `Path("channels.json")`
`default_factory` — that is how `test_bot_subscribe.py` and
`test_poller_per_channel_project.py` already inject a `tmp_path`, and none of them go
through `load_config()`.

`CHANNELS_PATH` is deleted rather than kept as an override. With the path derived from
`DATA_DIR` there is no deployment that needs a different value, and tests set the field
directly; a second way to specify the same thing is only a second thing to get wrong —
which is exactly what `.env.oracle` did (see Cutover).

Local development is unaffected: `DATA_DIR` unset means `data_dir == Path(".")`, so the
path resolves to `./channels.json`, byte-identical to today.

### 2. Stop shipping it in the image — `Dockerfile`, `.gitignore`

Delete the `COPY channels.json .` line, `git rm --cached channels.json`, and add
`channels.json` to `.gitignore` alongside the other runtime-state entries. The `/data/`
and `*.tmp` rules already cover the file's neighbours in its new home
(`channels.json.lock` and the `channels.tmp` that `atomic_write_json` creates).

Removing the `COPY` is mandatory, not cosmetic: `ci.yml` runs
`docker build --target test -t knowledger:test .` from a fresh checkout, where a
gitignored-and-untracked file does not exist. CI is the guard that proves this was done.

The `chown -R appuser:appuser /app` comment above it needs its rationale corrected — it
currently cites `channels.json` and its lock file as the reason the chown covers all of
`/app` rather than `DATA_DIR` alone. After this change `logs/knowledger_<date>.log` is
the only CWD-relative write left, and it is the whole reason on its own.

`.github/workflows/deploy.yml`'s script comment ("The image is self-contained
(channels.json is baked in), so the VPS needs no git checkout") states a fact that stops
being true. The conclusion still holds — the VPS still needs no checkout — but the
premise becomes "the image carries no deployment data at all", and `channels.json` joins
the list of persistent host files in the sentence that follows. No mount change is
needed: `data/` is already bind-mounted and chowned by this script.

### 3. Ship the schema, not the data — `channels.example.json`

```json
[
  { "handle": "@ExampleChannel", "name": "Example Channel", "channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx", "project": "your-project-uuid-or-name" },
  { "handle": "@AnotherChannel", "name": "Another Channel" }
]
```

Two entries, because the interesting part is the difference between them: `project` is
optional and its absence means "inherit `AUTO_TRANSCRIPT_PROJECT`" (`_channel_dicts()`
deletes a null `project` on write for exactly this reason), and `channel_id` is optional
because the poller resolves it from the handle on first sight.

### 4. Host-side plumbing — `deploy.sh`

A `channels` command mirroring `sync_cookies()`, with the target inside `data/`:

```bash
sync_channels() {
    rsync -e "ssh -i $SSH_KEY" channels.json "$HOST:$REMOTE_DIR/data/channels.json"
}
```

Ownership takes care of itself: host `ubuntu` is uid 1001 (the uid Dockerfile pins
`appuser` to, precisely because `DATA_DIR` is bind-mounted out of that user's home), and
`recreate` chowns `data/` to `1001:1001` on every deploy anyway.

`inspect` gains a third `_print_remote_json` call for `data/channels.json`, so "is the
watch list even on the host?" is answerable without an SSH session. Its existing
(empty — no file) branch already covers the not-yet-seeded case.

`recreate` is deliberately left alone — no pre-flight check for a missing watch list. A
missing file is a legitimate first-run state, and the bot already reports it (§5).

### 5. Missing-file behaviour: unchanged, deliberately

No code needed. `load_channels()` treats a missing file as a valid empty state and logs
a warning; `run_poller()` logs `No channels configured (%s)`, poller idle until one is
added and starts the loop anyway; `sync_channels()` re-reads the file every tick, so the
poller starts working the moment one appears. `/subscribed` reports "Not watching any
channels yet."

This matters more after the change than before it, since a missing host file is now a
reachable state — and it is already handled correctly.

## Rejected alternatives

A single-file bind mount, mirroring `cookies.txt`
(`-v ~/knowledger-bot/channels.json:/app/channels.json`) — the original shape of this
idea, and it does not work. A bind-mounted file is a mount point, and `rename(2)` onto a
mount point returns `EBUSY`. Every write to this file goes through `atomic_write_json()`
(persistence.py), which writes a sibling temp file and then `os.replace()`s it over the
destination — so `/subscribe` and the `channel_id` backfill would both fail with
`PersistenceIOError` on a bind-mounted path. `cookies.txt` survives the pattern only
because it is mounted `:ro` and nothing ever rewrites it. The fix under this approach is
to mount a directory instead, which is `DATA_DIR`.

Keeping `CHANNELS_PATH` as an escape hatch — see §1. It has no caller once the
default is correct, and its one real-world use so far was to pin production to the wrong
path.

Leaving the file in git and mounting over it — the writes would work (the mount wins),
but the personal data stays published and the committed copy becomes a permanent lie
about what production watches.

## Cutover

Order matters; getting it wrong means the poller silently watches nothing.

1. Keep the current `channels.json` as your local untracked copy, and back it up outside
   the repo. It is the only copy of the resolved `channel_id`s and project mappings.
2. From the branch, `./deploy.sh channels` — seeds `~/knowledger-bot/data/channels.json`
   while the old image is still running. Harmless: that container reads
   `/app/channels.json` and ignores this file entirely.
3. `./deploy.sh env` — pushes `.env.oracle` without its `CHANNELS_PATH=channels.json`
   line. Also a no-op for the running image, since that value equals the old default.
   This step cannot be skipped and CI will never do it: left in place, the variable
   would pin the new image straight back to the ephemeral `/app/channels.json` and defeat
   the entire change, with no error to notice.
4. `./deploy.sh inspect` — confirm all seven entries are on the host.
5. Merge. CI builds and deploys the image that no longer contains the file; the
   already-mounted `data/` supplies it.

No back-catalogue risk: `poller_state.json` already lists these channel_ids in
`baseline_seeded`, so a correctly seeded file triggers no re-seed. And if the host file
is ever lost, the failure mode is an idle poller (not a flood), recoverable with
`./deploy.sh channels`.

## Out of scope

- Telegram alerting when the poller is enabled but watching nothing. The startup warning
  and `/subscribed` are enough; a lost host file is a deploy-time mistake, not a
  runtime condition worth paging about.
- `./deploy.sh channels --pull` to fetch the host copy back down. `inspect` already shows
  it, and the host copy is the authority — pulling it invites merge confusion.
- Off-host backup of `data/`. Real gap, unrelated to this change, and now slightly more
  costly since the watch list joins the data at risk.
- Moving `logs/` under `DATA_DIR`, which would let the Dockerfile chown narrow from
  `/app` to `/app/data`.
- Rotating the two Claude project UUIDs. They were public in git history and stay there;
  they are not credentials on their own. Purging them means rewriting history — its own
  task, with its own blast radius.

## Verification

1. `uv run ruff check .` and `shellcheck deploy.sh` clean.
2. Unit test (`tests/test_channels_path_config.py`, reusing the autouse
   `_isolated_env` fixture from `test_weekly_recap_config.py`): `DATA_DIR` unset →
   `channels_path == Path("channels.json")` (local behaviour preserved); `DATA_DIR` set
   to a tmp dir → path is inside it; `CHANNELS_PATH` set → ignored.
3. Clean-tree build: with no `channels.json` present (temp clone, or `git stash -u`),
   `docker build --target test .` succeeds. This is the regression that would otherwise
   break CI for every contributor and every fresh clone.
4. Empty-state run: start the image with no `data/channels.json`; logs show the
   "poller idle until one is added" warning, `/subscribed` replies "Not watching any
   channels yet", and the bot stays up.
5. Write-through: with the file present under a mounted `data/`, `/subscribe` a
   channel, then `docker rm -f` and recreate the container — `/subscribed` still lists
   it. This is the bug the whole change exists to fix, and the check that would have
   caught the bind-mount EBUSY failure.
6. Production: after the cutover, `/version` shows the new SHA, `/subscribed` lists
   all seven channels with the right projects, `./deploy.sh inspect` prints the host
   file, and the next `./deploy.sh update` leaves a newly subscribed channel in place.
