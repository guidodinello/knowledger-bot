# Feature: `/version` Command

**Status:** Proposed.
**Value:** Low–Medium (answers "is the fix actually deployed?" without SSHing to the
VPS)
**Effort:** Low
**Touches:** `knowledger/config.py`, `knowledger/bot.py`, `Dockerfile`,
`.github/workflows/deploy.yml`, `deploy.sh`

## Problem

There is no way to tell which commit is running in production short of SSHing to the
VPS and running `git log` there. `pyproject.toml` has a `version = "0.1.0"` field, but
it's static and never read at runtime — there's no `__version__` symbol anywhere in
the package.

The obvious first idea — have the bot shell out to `git rev-parse HEAD` at request
time — doesn't work, because the running container has no `.git` directory to query.
`Dockerfile` never does `COPY . .`; it copies only specific paths:

```dockerfile
COPY pyproject.toml uv.lock README.md ./
COPY knowledger/ knowledger/
COPY main.py .
COPY channels.json .
```

`.git` isn't among them, and there's no `.dockerignore` needed because it was simply
never copied in the first place. So the version has to be captured *before* the image
loses access to git — at build time — and carried into the running container as
data, not recomputed from a repo that isn't there.

## Design

### 1. Version metadata baked at build time — `Dockerfile`

Add build args, promoted to `ENV` so they're visible to the running process, placed
*after* `RUN uv sync` so a doc- or CI-only commit (no changes under `knowledger/`)
still gets a full layer-cache hit up to that point:

```dockerfile
RUN uv sync --frozen --no-dev && mkdir -p /app/data
ENV DATA_DIR=/app/data

ARG GIT_SHA=
ARG GIT_COMMIT_DATE=
ENV GIT_SHA=${GIT_SHA}
ENV GIT_COMMIT_DATE=${GIT_COMMIT_DATE}
```

Both default to empty, which the app reports as `unknown` (see Design §4).

**Rejected alternative:** pass `-e GIT_SHA=...` on the `docker run` line instead of
baking it into the image. The CI deploy step already has `needs.build.outputs.short_sha`
in scope there too, so this is genuinely available. But a runtime env var can drift
from the image it's describing, or be set to anything by whoever runs the container —
it doesn't actually prove what code is inside. The version is a property of the
*image*, so it belongs baked into the image, not supplied at `docker run` time.

### 2. CI supplies the values — `.github/workflows/deploy.yml`

The existing `Compute short SHA` step already emits `short_sha`:

```yaml
      - name: Compute short SHA
        id: sha
        run: echo "short_sha=$(git rev-parse --short HEAD)" >> "$GITHUB_OUTPUT"
```

Extend the same step to also emit the commit date, and pass both into the build step's
`build-args:`:

```yaml
      - name: Compute short SHA
        id: sha
        run: |
          echo "short_sha=$(git rev-parse --short HEAD)" >> "$GITHUB_OUTPUT"
          echo "commit_date=$(git log -1 --format=%cI)" >> "$GITHUB_OUTPUT"

      - name: Build and push image
        uses: docker/build-push-action@53b7df96c91f9c12dcc8a07bcb9ccacbed38856a  # v7.3.0
        with:
          build-args: |
            GIT_SHA=${{ steps.sha.outputs.short_sha }}
            GIT_COMMIT_DATE=${{ steps.sha.outputs.commit_date }}
          ...
```

`%cI` is strict ISO 8601 (`2026-07-24T21:36:16-03:00`) — display code can truncate it
later; `%cs` (date-only) can't be widened back if we ever want the time. Worth noting
that `actions/checkout@v7`'s default shallow clone is sufficient for `git log -1` —
nobody needs to add `fetch-depth: 0` for this.

### 3. The second build path — `deploy.sh`

`deploy.sh update` runs its own build on the VPS, after a real `git fetch`/`reset`, so
`.git` *is* present there at build time:

```bash
$SSH "cd $REMOTE_DIR && git fetch origin && git reset --hard origin/main && docker build ..."
```

That `docker build` call must pass the same two `--build-arg`s, computed the same way:

```bash
docker build \
  --build-arg GIT_SHA="$(git rev-parse --short HEAD)" \
  --build-arg GIT_COMMIT_DATE="$(git log -1 --format=%cI)" \
  -t knowledger .
```

Skipping this means `/version` silently reports `unknown` for anything deployed
through `deploy.sh` instead of CI — easy to miss since the command would still "work."

### 4. Config field + handler — `knowledger/config.py`, `knowledger/bot.py`

A frozen settings dataclass on `Config`, same shape as the other `*Settings` types:

```python
@dataclass(frozen=True, slots=True)
class VersionSettings:
    commit_sha: str | None = None
    commit_date: str | None = None
```

Populated in `load_config()` from the two env vars with the same
`os.getenv(...) or None` idiom already used for `TOKEN_UPDATE_SECRET`:

```python
version=VersionSettings(
    commit_sha=os.getenv("GIT_SHA") or None,
    commit_date=os.getenv("GIT_COMMIT_DATE") or None,
),
```

This is a `Config` field rather than a module-level constant in `bot.py` on purpose:
the existing command tests (`tests/test_bot_inqueue.py`) build a real `Config` and
inject it through `FakeContext.bot_data`, so a config field is trivially overridable
per test, where an import-time constant would need monkeypatching. Same precedent as
`DATA_DIR` — an image `ENV` that's a property of the deployment, not a secret.

Then the handler, mirroring `cmd_start`'s shape:

```python
@_require_auth
async def cmd_version(update: Update, context: CustomContext, user: User) -> None:
    if update.message is None:
        return
    version = context.bot_data["config"].version
    sha = version.commit_sha or "unknown"
    date = version.commit_date or "unknown"
    await update.message.reply_text(f"Running {sha}, committed {date}.")
```

No `parse_mode` — plain text sidesteps the Markdown-escaping class of bug tracked in
`docs/bugs/inqueue-markdown-italics.md` entirely. A raw SHA/ISO-date pair is safe
either way, but there's no reason to opt into Markdown parsing for it.

Register it alongside the other commands in `build_application` (bot.py:754):

```python
app.add_handler(CommandHandler("version", cmd_version))
```

And add it to the help string in `cmd_start` (bot.py:127-132, reused verbatim by
`cmd_help`) — the only discoverability this command gets, and easy to forget:

```python
"Commands: /inqueue — show queue contents, /refresh — reload project list, "
"/version — show running build, /help — show this message",
```

## Mockup

Normal case:

```
Running a1b2c3d, committed 2026-07-24T21:36:16-03:00.
```

Local/unbaked run:

```
Running unknown, committed unknown.
```

## Out of scope

- Local-dev fallback that shells out to `git` when `.git` happens to exist — YAGNI;
  a local run reporting `unknown` is fine, the developer already knows what they
  checked out.
- Wiring up `set_my_commands` / BotFather automation — the repo has no `post_init`
  hook at all today; adding one is a separate feature. `/version` needs the same
  manual BotFather registration every existing command already requires.
- Surfacing the version anywhere else (a startup log line, an `/inqueue` footer) —
  one entry point is enough to answer "what's running."

## Verification

1. `uv run ruff check .` clean.
2. **Unit test** (`tests/test_bot_version.py`, following the fake-`Update`/`Context` +
   `asyncio.run` pattern from `test_bot_inqueue.py`): a `Config` with both
   `VersionSettings` fields set renders both values; a `Config` with neither set
   renders `unknown` for both and doesn't crash.
3. **Local build:** `docker build --build-arg GIT_SHA=abc1234 --build-arg
   GIT_COMMIT_DATE=2026-07-24T00:00:00Z -t knowledger .`, run it, confirm `/version`
   echoes those exact values back.
4. **CI path:** after merge and a real deploy, send `/version` in Telegram and confirm
   the SHA matches `git rev-parse --short HEAD` on `main`.
5. **`deploy.sh update` path:** run it and confirm `/version` reports a real SHA
   rather than `unknown` — this is the path most likely to be missed since it's a
   second, independent build site.
