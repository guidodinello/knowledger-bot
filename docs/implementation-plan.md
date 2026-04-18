# Knowledger — Implementation Plan

## Context

Manual workflow for saving YouTube video transcripts into Claude project knowledge bases involves too many steps: copy URL, visit transcript site, copy transcript, name file, upload to Claude. This bot reduces it to: send a YouTube URL to Telegram, tap a project button, done.

## Architecture

Telegram bot (long-polling) with three subsystems:
1. **YouTube** — extract video ID from URL, fetch metadata via oEmbed, fetch transcript via `youtube-transcript-api`
2. **Claude client** — list projects and upload content via unofficial web API (adapted from `weekly-highlights/clients/claude_uploader.py`)
3. **Telegram bot** — handles user interaction, project picker with inline buttons

## Project Structure

```
knowledger/
├── pyproject.toml              # modify — add dependencies
├── main.py                     # modify — bot entry point
├── .env.example                # create — document env vars
├── .gitignore                  # modify — add .env
├── Dockerfile                  # create — Fly.io deployment
├── fly.toml                    # create — Fly.io config
└── knowledger/
    ├── __init__.py
    ├── config.py               # env var loading/validation
    ├── youtube.py              # URL parsing + oEmbed metadata
    ├── transcript.py           # youtube-transcript-api wrapper
    ├── claude_client.py        # Claude web API (list projects, upload)
    └── bot.py                  # Telegram bot handlers + conversation flow
```

## Dependencies

```toml
dependencies = [
    "python-telegram-bot>=21.0",
    "youtube-transcript-api>=1.0.0",
    "curl-cffi>=0.7.0",
    "python-dotenv>=1.0.0",
]
```

- `python-telegram-bot` v21+ — async Telegram bot framework
- `youtube-transcript-api` — fetches YouTube caption tracks directly, no API key needed
- `curl-cffi` — HTTP client with browser impersonation (required for Claude's Cloudflare-protected API)
- `python-dotenv` — loads `.env` for local dev

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | From @BotFather |
| `CLAUDE_SESSION_TOKEN` | Yes | `sessionKey` cookie from claude.ai |
| `ALLOWED_USER_IDS` | Yes | Comma-separated Telegram user IDs (access control) |

## Implementation Steps

### Step 1: Project skeleton and config
- Create `knowledger/` package with `__init__.py`
- Create `knowledger/config.py` — load and validate env vars from `.env`
- Update `pyproject.toml` with dependencies
- Add `.env` to `.gitignore`, create `.env.example`
- Run `uv sync`

### Step 2: YouTube metadata (`knowledger/youtube.py`)
- `extract_video_id(url) -> str | None` — parse video ID from youtube.com/watch, youtu.be, youtube.com/shorts URLs using `urllib.parse`
- `fetch_video_metadata(url) -> VideoMetadata` — call YouTube oEmbed endpoint (`https://www.youtube.com/oembed?url=...&format=json`) via `curl_cffi`, return dataclass with `title`, `channel_name`, `video_id`
- `sanitize_filename(text) -> str` — strip characters illegal in filenames from title/channel

### Step 3: Transcript fetching (`knowledger/transcript.py`)
- `fetch_transcript(video_id) -> str | None` — use `YouTubeTranscriptApi.fetch(video_id)`, join snippet `.text` fields with newlines. Return `None` on `TranscriptsDisabled`/`NoTranscriptFound`.

### Step 4: Claude client (`knowledger/claude_client.py`)
Adapt from `/home/guido/projects/weekly-highlights/clients/claude_uploader.py` with these changes:
- Constructor takes only `session_token` (no project_id) — project is selected per-interaction
- Keep `_get_organization_id()`, `_get_headers()` as-is
- Add `list_projects() -> list[dict]` — `GET /api/organizations/{org_id}/projects` (verify endpoint during implementation)
- Add `upload_content(project_id, content, file_name) -> dict` — same as existing `upload_file` but takes content string directly instead of file path
- On 401/403, raise a clear error so the bot can notify the user about token expiration

### Step 5: Telegram bot (`knowledger/bot.py`)
**Conversation flow:**
1. User sends YouTube URL → bot validates, fetches metadata via oEmbed
2. Bot replies with video info + inline keyboard (one button per Claude project)
3. User taps project button → bot fetches transcript, constructs filename (`Youtube - {channel} - {title}.txt`), uploads to selected project
4. Bot confirms success or reports error (no captions, upload failure, etc.)

**Implementation details:**
- `python-telegram-bot` v21+ with `Application.builder().token().build()`
- `MessageHandler` with regex filter for YouTube URLs
- `CallbackQueryHandler` for project selection buttons
- Store pending video info in `context.user_data` keyed by message ID
- Callback data: `{project_uuid}` (look up video info from user_data)
- Cache project list at startup; `/refresh` command to re-fetch
- Access control: check `update.effective_user.id in ALLOWED_USER_IDS` early in each handler
- Long-polling via `application.run_polling()`

**Commands:** `/start` (welcome), `/refresh` (re-fetch projects), `/help`

### Step 6: Entry point (`main.py`)
- Validate config, create bot, run polling

### Step 7: Fly.io deployment
- `Dockerfile` — Python 3.13-slim + uv, install deps, run bot
- `fly.toml` — worker process (no HTTP service), primary region `iad`
- Secrets set via `fly secrets set`

## Key Design Decisions

- **oEmbed for metadata** instead of yt-dlp — zero extra dependencies (reuses `curl_cffi`), returns title + channel name, covers public videos
- **No ConversationHandler** — the flow is simple enough with user_data + callback queries
- **Project list cached at startup** — avoids hitting Claude API per message; `/refresh` to update
- **Long-polling** over webhooks — simpler for personal bot, no TLS/domain setup needed

## Risks and Mitigations

- **Claude session token expires** (~weeks) → bot sends clear Telegram notification on 401/403; update via `fly secrets set`
- **Projects list endpoint unverified** → if `GET /api/organizations/{org_id}/projects` doesn't work, inspect claude.ai network tab to find correct endpoint
- **youtube-transcript-api breakage** → actively maintained, usually fixed within days of YouTube changes

## Verification

1. **Local testing:** Set env vars in `.env`, run `uv run python main.py`, send a real YouTube URL to the bot in Telegram
2. **Verify each step:** metadata extraction shows correct title/channel, transcript is non-empty, project list appears as buttons, upload succeeds
3. **Edge cases:** video with no captions (expect graceful "no transcript" message), YouTube Shorts URL, invalid URL
4. **Deploy:** `fly launch`, `fly secrets set ...`, send a URL to the deployed bot
