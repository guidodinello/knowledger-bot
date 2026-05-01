# Feature Proposals

## Implemented

| Feature | Summary |
|---------|---------|
| [Chrome extension: automatic token updater](chrome-extension-spec.md) | WXT/TypeScript extension that POSTs the new `sessionKey` to the bot whenever the user logs into claude.ai |
| [Duplicate detection before upload](duplicate-detection.md) | Checks for an existing doc with the same filename before uploading; prompts Skip or Overwrite |
| [Persistent upload queue on token failure](persistent-upload-queue.md) | On `AuthError`, serializes the already-fetched transcript to a JSON queue; `/refresh` drains and retries it |

## Proposed / Not yet implemented

| Feature | Summary |
|-------|---------|
| [Improver command](improver-command.md) | rough idea, no spec yet |
