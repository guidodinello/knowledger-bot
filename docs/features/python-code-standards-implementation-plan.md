Implementation Plan — Python Code Standards Audit

harness: kiro-cli
model: gpt-5.6-sol

Problem statement

Address every finding in the 2026-07-15 audit, prioritizing durable upload reliability and clear failure behavior. The implementation should avoid
speculative framework-level rewrites, may introduce breaking persisted-state schemas without automatic migration, target Python 3.14.5, and finish with a
pull request.

Requirements and decisions

- Existing invalid or inaccessible queue, poller, channel, or persisted-token state must fail closed without replacing the source file.
- Missing state files remain valid first-run/empty-state conditions.
- Token updates will use persist-before-activate semantics. If persistence fails, the old in-memory token remains active and /update-token returns an
error.
- Queue processing must survive cancellation and process crashes, and concurrent drain requests must not upload an entry twice.
- Interactive overwrite must be recorded durably before deleting the existing document.
- Telegram, poller, and CLI workflows will share a focused TranscriptUploadService.
- Essential subsystem failures terminate the application visibly; bounded HTTP background jobs are retained and their exceptions explicitly observed.
- Python 3.14.5 is the project baseline.
- The new queue schema can reject the old JSON-array format with a clear operator-facing error; migration is not automatic.
- Execution will use a feature branch, include the currently untracked audit report, and open a PR against main.

Research findings

- Python 3.14 asyncio.TaskGroup cancels sibling tasks when one fails and propagates failures as an ExceptionGroup, matching the desired
essential-subsystem behavior.
- youtube-transcript-api distinguishes blocked/HTTP failures from TranscriptsDisabled; the wrapper currently collapses both into None.
- The transcript library creates an internal requests.Session when none is supplied. Supplying and context-managing a session gives deterministic
cleanup.
- HTMLParser normalizes tag and attribute names and does not depend on attribute order, making it a suitable replacement for the exact-order og:title
regex.
- The repository is on main tracking origin/main; only the audit report is currently untracked.
- .python-version and Docker already select 3.14.5, while requires-python, Ruff, project guidance, and the current virtual environment still reflect
Python 3.13.

Proposed architecture

flowchart TD
    Telegram[Telegram adapter]
    Poller[TranscriptPoller]
    CLI[CLI adapter]
    HTTP[Token HTTP adapter]

    Upload[TranscriptUploadService]
    Processor[Singleton QueueProcessor]
    Queue[Durable upload queue]
    Transcript[Transcript fetcher]
    Claude[ClaudeClient]
    Token[TokenUpdateService]
    JSON[Strict atomic JSON persistence]

    Telegram --> Upload
    Poller --> Upload
    CLI --> Upload

    Upload --> Transcript
    Upload --> Processor
    Processor --> Queue
    Processor --> Claude
    Queue --> JSON

    HTTP --> Token
    Token --> Claude
    Token --> Processor

    Main[main_async TaskGroup] --> Telegram
    Main --> Poller
    Main --> HTTP

Main design points

1. Strict persistence: a small atomic JSON module will support queue and poller state using same-directory temporary files, flush/fsync, and os.replace.
Schema validation remains in each owning module. Token persistence remains specialized to enforce 0600 permissions.
2. Durable queue records: queue records gain stable operation IDs and pending/in_flight states. Processing uses claim, ack, and release; startup recovers
abandoned claims. A persisted claim is never removed before terminal success.
3. Single processor: one QueueProcessor instance and asyncio.Lock serialize in-process processing from /refresh, token updates, and immediate overwrite
execution. Multi-process shared-file operation remains explicitly unsupported.
4. Shared upload use case: TranscriptUploadService owns transcript retrieval, duplicate detection, normal upload, durable overwrite submission,
authentication deferral, and explicit results such as Uploaded, AlreadyExists, DeferredForAuth, RetryPending, and NoTranscript.
5. Adapter-specific presentation: Telegram, poller, and CLI translate service results into their own messages or retry policy. Optional persisted
completion-notification metadata remains separate from the upload operation itself.
6. Nested settings: top-level configuration will contain focused immutable settings for Telegram, Claude/transcripts, token server, poller, storage, and
logging.
7. Explicit I/O APIs: Claude organization and project access become explicit methods with internal caches rather than network-performing properties.
8. Structured lifecycle: TaskGroup supervises Telegram polling, HTTP serving, and the poller. A controlled shutdown sentinel handles signals without
suppressing real child failures.

Task breakdown

Task 1: Establish the Python 3.14.5 development and validation baseline

Objective: Make all project metadata and tooling consistently target Python 3.14.5 before behavior changes begin.

Implementation guidance:

- Create a feature branch from main.
- Set requires-python and Ruff’s target to Python 3.14.
- Add a project-specific Python 3.14.5 override to the local project guidance rather than changing the global guideline used by unrelated repositories.
- Update the pinned Ruff pre-commit revision if the existing revision cannot recognize py314.
- Regenerate uv.lock under Python 3.14.5 and verify the existing Docker base remains aligned.
- Record the existing 16-test baseline before adding regression coverage.

Tests:

- Run Ruff check and format check under 3.14.5.
- Run the existing test suite unchanged.
- Verify uv run python --version reports 3.14.5.

Demo: A clean Python 3.14.5 environment can sync the project and run the original tests and lint checks successfully.

Task 2: Introduce strict, fail-closed persistence behavior

Objective: Prevent corrupt or inaccessible persisted data from silently becoming empty state or being overwritten.

Implementation guidance:

- Add a focused atomic JSON persistence module for queue and poller/channel state.
- Distinguish missing files from invalid UTF-8, invalid JSON, invalid schema, permission errors, and other I/O failures.
- Preserve invalid files in place and raise a path-specific persistence/schema error.
- Convert queue, poller-state, and channel loading to explicit schema validation.
- Make persisted-token loading fail closed when an existing file is invalid or inaccessible instead of falling back to the environment token.
- Create DATA_DIR once during startup/configuration and surface directory creation failures immediately.
- Keep token writes separate because their permission requirements differ.
- Document that old queue schemas require manual backup/removal or conversion.

Tests:

- Missing files produce their documented empty/first-run behavior.
- Invalid JSON, invalid UTF-8, malformed fields, and simulated PermissionError propagate.
- Existing source files remain byte-for-byte unchanged after failed reads.
- A corrupt poller state cannot trigger baseline seeding or a state save.
- A corrupt token file cannot silently reactivate the environment token.
- Atomic-write failure leaves the previous destination intact and removes temporary artifacts where possible.

Demo: Starting with a corrupt queue or poller file stops safely with a clear path-specific error while preserving the file.

Task 3: Make transcript retrieval outcomes explicit and resource-safe

Objective: Ensure caption absence and transport failures follow different policies and all HTTP sessions close deterministically.

Implementation guidance:

- Replace str | None with explicit transcript exceptions or result variants.
- Map authoritative caption absence to TranscriptUnavailable.
- Map blocking, connection, retry, and HTTP retrieval failures to TranscriptTransportError.
- Let unexpected library/programming errors propagate.
- Always create an explicit requests.Session, apply cookies when configured, pass it to YouTubeTranscriptApi, and close it with a context manager.
- Update Telegram and CLI messages to distinguish no captions from retrieval failure.
- Update poller policy so only transcript absence can age into the 72-hour give-up path; transport failures remain retryable and observable.
- Replace the exact-order og:title regex with a small HTMLParser implementation while retaining the existing upload-date extraction.

Tests:

- Caption-disabled, blocked, connection, HTTP, and successful transcript cases map correctly.
- Poller transport failure never becomes “no captions” after 72 hours.
- Caption absence follows the existing retry/give-up timing policy.
- Sessions close after success and every exception path.
- og:title parsing handles reordered attributes, extra attributes, case variation, and escaped entities.

Demo: A blocked YouTube request reports a temporary retrieval failure and remains retryable rather than being discarded as a captionless video.

Task 4: Implement durable queue claiming and singleton processing

Objective: Eliminate remove-before-work loss and duplicate uploads from overlapping drains.

Implementation guidance:

- Introduce versioned queue records with stable IDs and pending/in_flight states.
- Implement atomic claim, ack, release, attempt-count updates, and abandoned-claim recovery.
- Keep claimed records persisted until upload or idempotent duplicate confirmation succeeds.
- Release records after authentication or transient request failure without remove/re-enqueue.
- Ensure persistence failures propagate instead of being logged and suppressed.
- Move queue-drain coordination into one QueueProcessor instance guarded by asyncio.Lock.
- Wire both /refresh and HTTP token-update drains through that same instance.
- Preserve the existing overwrite idempotency checks for “delete already landed” and “upload landed but confirmation was lost.”

Tests:

- Cancellation after claim leaves recoverable durable work.
- Simulated restart recovers an in_flight record.
- Crash points before listing, after deletion, after upload, and before acknowledgement do not lose work.
- Two concurrent drain calls perform one upload.
- Authentication and request failures leave the entry pending with the correct attempt count.
- A release/write failure does not erase the record and is not swallowed.
- Successful and already-existing operations are acknowledged exactly once.

Demo: Two simultaneous drains process one queued upload once, while a simulated crash can be followed by successful recovery.

Task 5: Add TranscriptUploadService and make overwrite durable

Objective: Establish one tested upload use-case layer shared by Telegram, poller, queue retries, and CLI.

Implementation guidance:

- Define immutable request and explicit result types.
- Move transcript retrieval, duplicate checking, upload policy, authentication deferral, overwrite execution, and retry classification into the service.
- Record every overwrite operation before deleting the old document.
- Execute an immediate overwrite through the queue processor; acknowledge only after replacement upload or idempotent confirmation.
- Leave a failed replacement pending and report that it was retained for retry.
- Keep Telegram-specific text and poller notifications outside the service.
- Wire normal Telegram selection, Telegram overwrite, poller uploads, queue drains, and CLI uploads to the service.
- Remove superseded duplicate orchestration from bot.py, poller.py, and cli.py.

Tests:

- Normal upload, existing document, auth deferral, transient failure, and success outcomes.
- Upload failure after successful overwrite deletion leaves a durable pending replacement.
- Retry after deletion uploads without attempting a second delete.
- Retry after lost upload confirmation does not duplicate the replacement.
- Telegram, poller, and CLI adapters translate each service result correctly.
- Queued completion notification metadata survives restart without contaminating the core upload request.

Demo: An overwrite whose upload fails after deletion reports a deferred retry; a later drain installs the replacement without data loss or duplication.

Task 6: Correct Claude client and token-update contracts

Objective: Make network activity explicit and ensure HTTP success means the token is durable.

Implementation guidance:

- Replace _org_id and projects cached properties with explicit get_org_id() and list_projects() methods backed by private caches.
- Retain explicit cache invalidation when a token changes.
- Change upload_content() to return None and stop parsing a response body callers discard.
- Make token persistence propagate errors and clean temporary files.
- Persist the new token with owner-only permissions before changing the cookie or invalidating caches.
- Return an appropriate server error from /update-token when persistence fails; do not schedule a queue drain in that case.
- Extract token validation/update orchestration into a focused service so the HTTP adapter only validates HTTP input and translates outcomes.
- Retain HTTP background drain tasks and inspect/log their exceptions explicitly; await remaining jobs during cleanup.

Tests:

- Failed persistence leaves the old cookie and caches active.
- Successful persistence changes the token and invalidates caches.
- Token files retain 0600 permissions, including replacement of existing files.
- /update-token reports failure when persistence fails and success only after durable storage.
- No drain starts following a failed token update.
- Background task exceptions are observed and logged.
- Explicit client methods cache and invalidate as intended.
- Upload succeeds without requiring a JSON response body.

Demo: A simulated disk-full token update returns an error, preserves the active token, and performs no queue drain.

Task 7: Refine configuration, poller, Telegram, and HTTP APIs

Objective: Remove the audited broad parameter lists, behavior flags, and typing ambiguities without introducing a framework.

Implementation guidance:

- Split Config into nested immutable TelegramSettings, ClaudeSettings, TranscriptSettings, TokenServerSettings, PollerSettings, StorageSettings, and
existing logging settings.
- Validate conditional requirements while constructing settings—for example, token-server secret with an enabled port.
- Replace the seven-argument poller functions with a cohesive TranscriptPoller object holding stable dependencies and mutable poller state.
- Allow persistence and unexpected poller errors to escape; continue handling known per-feed network failures locally.
- Reduce build_aiohttp_app() to focused token-server settings and service dependencies.
- Remove _build_keyboard(..., show_all=False); callers provide the exact project list and explicitly add the “More” row when needed.
- Change the authorization decorator contract to pass a statically non-optional authenticated User to protected handlers.
- Update all construction sites, tests, README examples, and .env.example.

Tests:

- Environment variables map to the correct nested settings and invalid combinations fail early.
- Poller baseline, detection, upload, retry, and persistence behavior remains intact through the object API.
- Fatal poller persistence failures propagate.
- Filtered and full project keyboards contain the expected buttons without flag combinations.
- Authorized handlers receive a non-optional user; unauthorized updates never invoke them.
- HTTP app construction uses settings as the single source of truth.

Demo: The application starts from the same environment variables, while handlers, poller methods, and HTTP construction expose substantially smaller
APIs.

Task 8: Supervise essential application tasks with structured concurrency

Objective: Prevent the process from silently continuing after Telegram, HTTP, or poller failure.

Implementation guidance:

- Replace the task list and gather(return_exceptions=True) with asyncio.TaskGroup.
- Treat Telegram polling, enabled HTTP serving, and enabled poller execution as essential sibling tasks.
- Implement signal shutdown using a private controlled-shutdown sentinel or equivalent mechanism that cancels siblings without hiding genuine failures.
- Preserve CancelledError propagation through subsystem cleanup.
- Ensure Telegram and aiohttp cleanup executes once during both signals and child failures.

Tests:

- Failure of each essential subsystem cancels its siblings and propagates visibly.
- SIGTERM/SIGINT-style shutdown cancels all children cleanly without being reported as an application defect.
- Cleanup runs on normal shutdown, cancellation, and child failure.
- No task result is discarded.

Demo: A simulated HTTP or poller crash terminates the complete application instead of leaving Telegram running in a partially functional process.

Task 9: Complete documentation, regression validation, and PR delivery

Objective: Verify the integrated system, document operational changes, and deliver the work as a reviewable PR.

Implementation guidance:

- Update README, .env.example, relevant feature documents, and queue recovery notes.
- Document Python 3.14.5 setup and the one-time manual handling required for an old queue schema.
- Include docs/audits/python-code-standards-review-2026-07-15.md in the branch.
- Review changed modules for obsolete helpers, broad exception catches, dead code, duplicated workflow logic, and stale comments.
- Run targeted tests after each prior task and the full suite at the end.
- Stage specific files, create a normal commit without bypassing hooks, push the feature branch with -u, and open a PR against main.

Tests and validation:

- uv run ruff check .
- uv run ruff format --check .
- uv run pytest tests/ -v
- Configured static type checking where available, especially the authenticated-user invariant.
- Minimal startup/configuration smoke test under Python 3.14.5.
- Docker build or an equivalent container validation if the environment supports it.
- Confirm the working tree contains only intentional PR changes.

Demo: The PR shows all checks passing and includes a concise mapping from every audit finding to its implementation and regression tests.
