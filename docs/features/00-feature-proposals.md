# Feature Proposals

**Status legend:** ⬜ Proposed (unclaimed) · ✅ Implemented

## Implemented

| Feature | Summary |
|---------|---------|
| [Chrome extension: automatic token updater](chrome-extension-spec.md) | WXT/TypeScript extension that POSTs the new `sessionKey` to the bot whenever the user logs into claude.ai |
| [Duplicate detection before upload](duplicate-detection.md) | Checks for an existing doc with the same filename before uploading; prompts Skip or Overwrite |
| [Persistent upload queue on token failure](persistent-upload-queue.md) | On `AuthError`, serializes the already-fetched transcript to a JSON queue; `/refresh` drains and retries it |
| [Auto-transcript on new upload](auto-transcript-on-upload.md) | In-process poller watches channel RSS feeds; 24h after a new video it fetches the transcript and uploads it to a designated project automatically |
| [Persist poller/queue state across restarts](persist-poller-state-volume.md) | Mounts a persistent data directory so `poller_state.json` and `petition_queue.json` survive container restarts instead of resetting |
| [Redesign `/inqueue` output for readability](inqueue-redesign.md) | Human section labels + emoji, counts in headers, absolute timestamps, per-section cap with a `+N more` trailer, and empty-section collapse. Ended up covering three sections (retry queue, poller pending, and blocked transcripts — the last one added after the spec was written), not the two in the original mockup. |
| [Instagram Reel support](instagram-reel-support.md) | Extends `scripts/transcribe.py` (offline, manual) to download Instagram Reels via yt-dlp cookies auth and transcribe with faster-whisper, with a configurable `--language` flag replacing the old Spanish hardcode |
| [Weekly upload recap](weekly-recap.md) | Sunday summary of what the poller auto-transcribed and uploaded that week, per project, backed by a new append-only `upload_history.json` |
| [Per-channel Claude project + baseline seeding](per-channel-project-and-baseline-seed.md) | Lets each watched YouTube channel target its own Claude project (investments vs. exercise, etc.) instead of one global project, and seeds new channels without flooding their back-catalogue |
| [`/version` command](version-command.md) | Reports the git SHA and commit date of the running build, baked in at image build time, so production's actual version is visible from Telegram |
| [Dedicated bot session](dedicated-bot-session.md) | Decouples the bot's auth from the browser's login state: adopts renewed `sessionKey` cookies off Claude responses, and stops `/update-token` from overwriting a token that still works |
| [Message hierarchy pass](message-hierarchy-pass.md) | Readability pass over the five most-read messages: a tappable commit SHA in `/version`, bulleted `/help`, blockquoted sections in `/inqueue`, the auto-save notification naming its project instead of echoing a uuid, and `/subscribed` grouped by project with linked channel names |
| [`/subscribed` and `/subscribe` commands](subscribe-commands.md) | Lists the watched channels and adds new ones from a link to any of their videos, with a project picker — and makes the poller re-read `channels.json` every tick so a new channel is watched without a restart |

## Proposed

None.
