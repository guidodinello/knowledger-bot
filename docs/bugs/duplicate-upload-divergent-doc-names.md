# Bug: The Same Video Is Uploaded Twice Under Two Different Doc Names

**Severity:** Medium
**Files:** `knowledger/upload_service.py`, `knowledger/pending_transcripts.py`, `knowledger/youtube.py`

## Description

Every upload path guards against duplicates by comparing the doc name it is about to
write against the names already in the project (`TranscriptUploadService.upload`).
That works only as long as every path computes the same name for the same video, and
it doesn't:

- The interactive flow (and the retry list it feeds) names a video from
  `fetch_video_metadata`, whose upload date comes from the **watch page**
  (`"uploadDate"`). When that request fails, `upload_date` is `None` and
  `build_doc_name` yields the dateless `Youtube - {channel} - {title}`.
- The subscriptions poller names the same video from the **channel feed**'s
  `published` field, which is always present, so it always yields
  `Youtube - {channel} - {title} - {date}`.

The two failure modes are correlated, which is what turns a latent inconsistency into
a recurring bug: the watch page fails precisely when YouTube is blocking this bot, and
that is the same block that sends the transcript into `pending_transcripts.json` for
retry. So the retry path is disproportionately likely to be holding a dateless name.
Worse, the name is frozen into the pending entry at request time and reused verbatim
when the retry finally succeeds — long after the block has lifted and the date became
available again.

Observed sequence (2026-08-04):

1. 23:01 — the user sends a link. The watch page is blocked, so the entry is queued as
   `Youtube - On-Chain Mind - Bitcoin's Top Buyers Are Finally Capitulating`.
2. 23:27 — the retry succeeds and uploads under that dateless name.
3. 08:32 next day — the poller reaches the same video, builds
   `... - Capitulating - 2026-08-04`, finds no doc by that name, and uploads a second
   copy of the same transcript.

## Impact

Duplicate documents in the Claude project. They are invisible until the project's doc
list is inspected by hand, they consume project capacity, and they degrade retrieval —
Claude sees the same transcript twice and reads it as twice as relevant. This is the
exact failure `docs/features/duplicate-detection.md` set out to prevent.

## Fix

Two changes, one for each half of the problem.

**Duplicate detection keys on the video, not on the name.** `record_upload` already
stores `video_id` alongside `file_name` for every successful upload. `find_upload`
looks that record up, and `TranscriptUploadService.find_existing` falls back to it
when the name doesn't match: if this video was already uploaded to this project under
some other name, and a doc with that name is still there, that doc is the duplicate.

Matching by id rather than by "same name ignoring the date" is deliberate. Channels
that publish under a recurring title (a daily livestream — `En directo "DólarYen"`)
produce doc names that differ *only* in the date; those are genuinely different videos
and collapsing them would silently drop one. Without a history record (a project
predating the history file, a doc uploaded outside the bot) detection degrades to name
matching, which is the behaviour that predates this fix — never to a failed upload.

**The retry re-resolves the name it was queued with.** `_resolve_doc_name` re-fetches
the video's metadata when the queued name has no date suffix, so the upload lands under
the canonical `... - {date}` name both paths agree on — a retry runs after the block has
lifted, so the date it couldn't get at request time is available now. Best effort: if
the fetch fails again the queued name is kept, and the video-id check above still
prevents a second copy.
