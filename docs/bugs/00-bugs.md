# Bug Audit

**Status legend:** ⬜ Open (unclaimed) · 🔧 In review (PR open, link in this table) · ✅ Fixed (merged)

## Fixed

Nine bugs found across the codebase, ranging from a user-visible message failure to silent data quality issues.

| # | Severity | File | Status | Summary |
|---|----------|------|--------|---------|
| 1 | High | `knowledger/bot.py` | ✅ Fixed | [Unescaped Markdown in bot messages](unescaped-markdown-injection.md) |
| 2 | Medium | `knowledger/youtube.py` | ✅ Fixed | [Page-title exception discards oEmbed title](page-title-exception-propagation.md) |
| 3 | Medium | `knowledger/bot.py` | ✅ Fixed | [Blocking I/O inside async handlers](blocking-io-in-async-handlers.md) |
| 4 | Low | `knowledger/logger.py` | ✅ Fixed | [LOG_LEVEL from .env is silently ignored](log-level-ignored.md) |
| 5 | Low | `knowledger/bot.py` | ✅ Fixed | [/refresh drops non-auth network errors](refresh-error-handling.md) |
| 6 | Low | `knowledger/invidious.py` | ✅ Fixed | [VTT cue sequence numbers bleed into transcript](vtt-cue-numbers-in-transcript.md) |
| 8 | Medium | `knowledger/bot.py`, `knowledger/claude_client.py` | ✅ Fixed | [Auth errors before project selection skip the retry queue entirely](petition-queue-missing-project-context.md) |
| 9 | Medium | `knowledger/bot.py` | ✅ Fixed | [Blocked transcript fetches are silently dropped, not retried](transcript-blocked-not-queued.md) |

## Open

| # | Severity | File | Status | Summary |
|---|----------|------|--------|---------|
| 7 | Low | `knowledger/bot.py` | 🔧 In review (PR link pending) | [/inqueue filenames break Markdown rendering](inqueue-markdown-italics.md) |
