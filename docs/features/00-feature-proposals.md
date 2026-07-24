# Feature Proposals

**Status legend:** ⬜ Proposed (unclaimed) · 🔧 In review (PR open, link in this table) · ✅ Implemented (merged)

## Implemented

| Feature | Summary |
|---------|---------|
| [Chrome extension: automatic token updater](chrome-extension-spec.md) | WXT/TypeScript extension that POSTs the new `sessionKey` to the bot whenever the user logs into claude.ai |
| [Duplicate detection before upload](duplicate-detection.md) | Checks for an existing doc with the same filename before uploading; prompts Skip or Overwrite |
| [Persistent upload queue on token failure](persistent-upload-queue.md) | On `AuthError`, serializes the already-fetched transcript to a JSON queue; `/refresh` drains and retries it |
| [Auto-transcript on new upload](auto-transcript-on-upload.md) | In-process poller watches channel RSS feeds; 24h after a new video it fetches the transcript and uploads it to a designated project automatically |
| [Persist poller/queue state across restarts](persist-poller-state-volume.md) | Mounts a persistent data directory so `poller_state.json` and `petition_queue.json` survive container restarts instead of resetting |
| [Redesign `/inqueue` output for readability](inqueue-redesign.md) | Human section labels + emoji, counts in headers, absolute timestamps, per-section cap with a `+N more` trailer, and empty-section collapse. Ended up covering three sections (retry queue, poller pending, and blocked transcripts — the last one added after the spec was written), not the two in the original mockup. |

## Proposed / Not yet implemented

| Feature | Status | Summary |
|---------|--------|---------|
| [Instagram Reel support](instagram-reel-support.md) | ⬜ Proposed | Extend `scripts/transcribe.py` (offline, manual) to download Instagram Reels via yt-dlp cookies auth and transcribe with faster-whisper. Feasibility: low-effort but manual-only — no caption API, no bot/poller integration. |
| [Per-channel Claude project + baseline seeding](per-channel-project-and-baseline-seed.md) | 🔧 In review — [PR TBD](TBD) | Lets each watched YouTube channel target its own Claude project (investments vs. exercise, etc.) instead of one global project, and seeds new channels without flooding their back-catalogue |
| [Weekly upload recap](weekly-recap.md) | ⬜ Proposed | Sunday summary of what the poller auto-transcribed and uploaded that week, per project |
