# knowledger

Telegram bot that saves YouTube video transcripts to Claude project knowledge bases.

**Workflow:** send a YouTube URL → tap a project button → done.

## Setup

### 1. Prerequisites

- [uv](https://docs.astral.sh/uv/)
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram user ID from [@userinfobot](https://t.me/userinfobot)
- A `sessionKey` cookie from claude.ai (open DevTools → Application → Cookies while logged in)

### 2. Install dependencies

```bash
uv sync
```

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```
TELEGRAM_BOT_TOKEN=your-token-from-botfather
CLAUDE_SESSION_TOKEN=sk-ant-sid01-...
ALLOWED_USER_IDS=your-telegram-user-id
```

### 4. Run

```bash
uv run python main.py
```

Send the bot a YouTube URL. It will show your Claude projects as buttons — tap one to save the transcript.

## Commands

| Command | Description |
|---|---|
| `/start` | Welcome message |
| `/help` | Show help |
| `/refresh` | Reload Claude project list |

## Notes

- The Claude session token expires after a few weeks. When the bot reports an auth error, grab a fresh `sessionKey` cookie from claude.ai and update `.env`.
- The project list is cached at startup; use `/refresh` to pick up new projects without restarting.
- Videos without captions will report "no transcript available".
