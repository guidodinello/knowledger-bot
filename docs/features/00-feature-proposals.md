# Feature Proposals

## Implemented

| Feature | Summary |
|---------|---------|
| [Chrome extension: automatic token updater](chrome-extension-spec.md) | WXT/TypeScript extension that POSTs the new `sessionKey` to the bot whenever the user logs into claude.ai |
| [Duplicate detection before upload](duplicate-detection.md) | Checks for an existing doc with the same filename before uploading; prompts Skip or Overwrite |
| [Persistent upload queue on token failure](persistent-upload-queue.md) | On `AuthError`, serializes the already-fetched transcript to a JSON queue; `/refresh` drains and retries it |
| [Auto-transcript on new upload](auto-transcript-on-upload.md) | In-process poller watches channel RSS feeds; 24h after a new video it fetches the transcript and uploads it to a designated project automatically |

## Proposed / Not yet implemented

| Feature | Summary |
|-------|---------|
| [Improver command](improver-command.md) | rough idea, no spec yet |
| [Instagram Reel support](instagram-reel-support.md) | Capture + transcribe Instagram Reels (not just YouTube). Feasibility: low — no public API for reel captions, scraping is fragile and against ToS. |
