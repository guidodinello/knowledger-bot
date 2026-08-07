# Bug Audit

**Status legend:** ⬜ Open (unclaimed) · 🔧 In review (PR open, link in this table) · ✅ Fixed (merged)

## Fixed

Twelve bugs found across the codebase, ranging from a user-visible message failure to silent data quality issues.

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
| 7 | Low | `knowledger/bot.py` | ✅ Fixed | [/inqueue filenames break Markdown rendering](inqueue-markdown-italics.md) |
| 10 | Medium | `knowledger/upload_service.py`, `knowledger/pending_transcripts.py` | ✅ Fixed | [The same video is uploaded twice under two different doc names](duplicate-upload-divergent-doc-names.md) |
| 11 | Low | `knowledger/bot.py` | ✅ Fixed | [/subscribed links a legacy channel handle to a dead URL](channel-link-encodes-legacy-handle-separator.md) |
| 12 | Low | `knowledger/telegram_format.py` | ✅ Fixed | [Message-length cap counts code points, Telegram counts UTF-16 units](message-cap-counts-code-points-not-utf16.md) |

## Open

None.
