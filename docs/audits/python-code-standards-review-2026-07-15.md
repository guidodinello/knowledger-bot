# Python Code Standards Review — 2026-07-15

## Scope

This review covers the Python codebase against the standards in
`/home/guido/.claude/guidelines/python.md`, with particular attention to:

- Standard-library usage versus custom implementations
- Separation of concerns
- API and abstraction design
- Parameter and boolean-flag smells
- Error handling, persistence, and asynchronous execution
- Test coverage

The review was read-only; no production code was changed.

## Executive summary

The codebase is readable and generally follows the Python standards. It uses modern typing,
slotted dataclasses, `pathlib`, deferred logging, and sensible module boundaries. Most blocking
network calls are correctly moved off the asyncio event loop.

The main weakness is reliability around durable uploads rather than code style. `bot.py` and
`poller.py` contain duplicated orchestration, and several failure paths can lose queued work,
produce duplicate uploads, or leave the process running with a failed subsystem.

## High-priority findings

### 1. Interactive overwrite can delete the old document and lose the replacement

**Location:** `knowledger/bot.py:529-565`

The overwrite handler deletes the existing document before uploading its replacement. If deletion
succeeds and the upload raises anything other than `AuthError`, the handler reports
`"Overwrite failed"` but does not restore the old document or queue the replacement.

**Recommendation:** Treat overwrite as a durable operation. Record or enqueue the replacement
before deletion, then let a single idempotent processor perform delete and upload. At minimum,
queue the replacement after any upload failure that follows a successful deletion.

### 2. The persistent queue removes work before it is complete

**Locations:**

- `knowledger/bot.py:196-214`
- `knowledger/bot.py:144-150`
- `tests/test_bot_queue.py:194-222`

`drain_queue()` reads the entries and removes each one before listing documents, deleting, or
uploading. A process crash or task cancellation after removal permanently loses that entry.
`_requeue()` also catches and suppresses persistence failures.

The current test suite explicitly demonstrates this behavior: one entry is expected to disappear
when its requeue write fails.

**Recommendation:** Replace `peek/remove/re-enqueue` with durable `claim/ack/release` semantics,
or leave entries queued until a terminal success while tracking an `in_flight` state.

### 3. Overlapping drains can upload the same entry twice

**Locations:**

- `knowledger/bot.py:196-287`
- `knowledger/http_server.py:67-73`

Both `/refresh` and the HTTP token-update task can invoke `drain_queue()`. Two drains can snapshot
the same entries; the second removal becomes a no-op, but both invocations continue and can upload
the same document.

**Recommendation:** Introduce a single queue processor. An `asyncio.Lock` provides an immediate
single-flight fix, while durable claiming is the stronger long-term design.

### 4. Corrupt or unreadable persistence silently becomes empty state

**Locations:**

- `knowledger/queue.py:49-67`
- `knowledger/poller.py:75-97`
- `knowledger/config.py:14-26`

Queue and poller-state loading catch broad exceptions and return empty state. A subsequent write can
overwrite an existing but temporarily unreadable file. A corrupt existing poller state can also
skip first-run baseline seeding and classify the current feed as new work.

**Recommendation:** Distinguish missing, corrupt, and inaccessible files:

- Missing file: valid empty state
- Invalid JSON or schema: quarantine and alert or fail closed
- Permission and other unexpected `OSError` values: propagate

Catch expected exceptions such as `json.JSONDecodeError`, `UnicodeDecodeError`, `KeyError`, and
`TypeError` rather than `Exception`.

### 5. The token endpoint reports success when persistence fails

**Locations:**

- `knowledger/claude_client.py:94-113`
- `knowledger/http_server.py:67-75`
- `knowledger/poller.py:400`

`ClaudeClient._persist_token()` logs and suppresses `OSError`. The HTTP endpoint therefore returns
`{"status": "ok"}` even if the fresh token will be lost at restart. `DATA_DIR` is created only by
the optional poller, so deployments without a poller may lack the persistence directory.

**Recommendation:** Create `data_dir` once during application startup and propagate persistence
failure to the HTTP boundary. Explicitly decide whether a token should become active in memory when
durable storage fails.

### 6. Long-lived task failures are swallowed

**Locations:**

- `main.py:27-59`
- `knowledger/http_server.py:17-30`

`main.py` gathers long-lived tasks with `return_exceptions=True` and ignores the returned errors.
Telegram polling, the HTTP server, or the poller can fail while the remaining process continues in
a partially functional state. HTTP background tasks are also discarded without inspecting their
exceptions.

**Recommendation:** Use `asyncio.TaskGroup` or explicitly inspect and propagate task exceptions. A
failed essential subsystem should cancel its siblings and terminate visibly.

### 7. Transient transcript failures are classified as no captions

**Locations:**

- `knowledger/transcript.py:44-49`
- `knowledger/poller.py:269-273`

`fetch_transcript()` returns `None` for both authoritative no-transcript responses and
connection/proxy/blocking failures. After 72 hours, the poller treats either result as permanent,
discards the video, and reports that it has no captions.

**Recommendation:** Return explicit outcomes or raise distinct exceptions, such as
`TranscriptUnavailable`, `TranscriptNotReady`, and `TranscriptTransportError`. Only authoritative
absence should trigger the permanent give-up policy.

## Separation of concerns

The initial module split is good, but an application/use-case layer is missing. The same “store
transcript” workflow—fetch transcript, build a name, list documents, detect duplicates, upload or
overwrite, queue on authentication failure, retry, and notify—is implemented independently in:

- Normal Telegram selection: `knowledger/bot.py:342-475`
- Telegram overwrite: `knowledger/bot.py:478-577`
- Queue draining: `knowledger/bot.py:153-287`
- Poller processing: `knowledger/poller.py:231-313`
- One-shot CLI: `cli.py:55-129`

This repetition is established enough to justify an abstraction under the guideline to extract
shared logic after three occurrences.

A focused `TranscriptUploadService` could expose operations such as:

```python
async def store(request: StoreTranscriptRequest) -> StoreResult: ...
async def overwrite(request: OverwriteTranscriptRequest) -> StoreResult: ...
```

Results should represent domain outcomes explicitly, for example `Uploaded`, `AlreadyExists`,
`DeferredForAuth`, and `NoTranscript`. Telegram, the poller, and the CLI would then translate those
outcomes into their own messages.

This would let `bot.py` become primarily a Telegram adapter instead of combining UI,
authorization, upload policy, retry handling, and persistence coordination in one 600-line module.

## API and abstraction design

There is not a codebase-wide parameter or flag problem, but several APIs stand out:

- `build_aiohttp_app()` has seven parameters, including `Config` plus values already present in
  `Config`: `knowledger/http_server.py:96-116`.
- `_process_video()` and `_tick()` each have seven parameters:
  `knowledger/poller.py:255-264` and `knowledger/poller.py:316-325`.
- `Config` contains logging, Telegram, HTTP security, poller, storage, proxy, and YouTube settings in
  one broad object: `knowledger/config.py:41-56`.
- `_build_keyboard(..., whitelist, show_all=False)` uses a boolean behavior flag and permits
  redundant combinations: `knowledger/bot.py:49-66`.

Recommended direction:

1. Split configuration into validated `TelegramSettings`, `TokenServerSettings`, `PollerSettings`,
   and `StorageSettings`.
2. Use a cohesive poller context or service for stable dependencies instead of repeatedly passing
   seven arguments.
3. Have callers compute visible projects, or provide separate filtered/all keyboard operations
   instead of `show_all`.
4. Replace network-performing cached properties with explicit methods:
   - `ClaudeClient._org_id`: `knowledger/claude_client.py:57-70`
   - `ClaudeClient.projects`: `knowledger/claude_client.py:80-89`
5. Change `ClaudeClient.upload_content() -> dict` to `-> None` unless the response is genuinely part
   of its contract. Every current caller discards the result.
6. Represent the authentication decorator’s narrowed user invariant in its API. Static diagnostics
   currently report `update.effective_user.id` as potentially accessing `None` at
   `knowledger/bot.py:304`.

Properties should not conceal blocking HTTP or cache invalidation behavior. Explicit
`get_org_id()` and `list_projects()` methods can retain internal caching while making I/O visible to
callers.

## Standard-library usage

The project generally makes good use of the standard library rather than reinventing it:

- Strong use of `pathlib`, `dataclasses`, `json`, `hmac.compare_digest`, `tempfile`, `subprocess`,
  logging handlers, `urllib.parse`, and `os.replace`
- Correct use of `functools.wraps`
- Consistent `slots=True` on production dataclasses, with immutability where appropriate
- Legitimate CLI `print()` usage for primary user output
- Deferred `%` formatting in logger calls
- Atomic destination replacement for durable files

Targeted improvements:

- Use `asyncio.TaskGroup` for structured concurrency.
- Manage the `requests.Session` created in `transcript.py` with a context manager so it has a
  deterministic lifetime.
- Consider a small atomic JSON persistence helper after defining the required corruption and
  permission semantics. Queue and poller state repeat temp-write plus `os.replace`; token storage
  has different permission requirements and should remain specialized.
- The regular expression for the `og:title` meta tag at `knowledger/youtube.py:56` is brittle.
  `html.parser.HTMLParser` would tolerate reordered or additional attributes. This is low priority.

## Configuration and tooling

The checked-in `.python-version` requests Python 3.14.5, while the supplied coding guideline and
Ruff configuration target Python 3.13.

Validation results during this review:

- `.venv/bin/python --version`: Python 3.13.3
- `.venv/bin/ruff check .`: all checks passed
- `.venv/bin/pytest tests/ -q`: 16 passed
- `uv run ruff check .`: failed because Python 3.14.5 was unavailable

Align `.python-version` with Python 3.13, or intentionally provision Python 3.14.5 and update the
project standard consistently.

## Testing priorities

The existing tests provide useful coverage of queue retries and token persistence. The highest-value
additions are:

1. Upload failure after a successful interactive overwrite deletion
2. Cancellation or crash after queue removal but before upload
3. Two simultaneous queue drains
4. Corrupt and permission-denied queue and poller-state files
5. Token persistence failure reflected by `/update-token`
6. Child-task failure propagation in `main_async()`
7. Distinguishing no captions from transcript transport failure

## Positive practices to preserve

- The package already has useful boundaries for YouTube metadata, transcript retrieval, Claude
  transport, configuration, logging, and notification.
- Retry idempotency for queued overwrites is thoughtfully handled and covered by tests.
- Background tasks created by the HTTP server are retained against premature garbage collection,
  even though their exceptions still need to be observed.
- Durable writes use temp files plus `os.replace`, reducing the risk of partially written
  destination files.
- Token files are created with owner-only permissions.
- Blocking external network operations are generally dispatched with `asyncio.to_thread` from
  asynchronous handlers.
