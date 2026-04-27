# Bug Audit

Six bugs found across the codebase, ranging from a user-visible message failure to silent data quality issues.

| # | Severity | File | Summary |
|---|----------|------|---------|
| 1 | High | `knowledger/bot.py` | [Unescaped Markdown in bot messages](unescaped-markdown-injection.md) |
| 2 | Medium | `knowledger/youtube.py` | [Page-title exception discards oEmbed title](page-title-exception-propagation.md) |
| 3 | Medium | `knowledger/bot.py` | [Blocking I/O inside async handlers](blocking-io-in-async-handlers.md) |
| 4 | Low | `knowledger/logger.py` | [LOG_LEVEL from .env is silently ignored](log-level-ignored.md) |
| 5 | Low | `knowledger/bot.py` | [/refresh drops non-auth network errors](refresh-error-handling.md) |
| 6 | Low | `knowledger/invidious.py` | [VTT cue sequence numbers bleed into transcript](vtt-cue-numbers-in-transcript.md) |
