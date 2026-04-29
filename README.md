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
| `/update_token <sessionKey>` | Hot-swap the Claude session token without restarting |

## Token management

The Claude session token is a `sessionKey` cookie from claude.ai. It gets invalidated when you log out (e.g. to switch accounts). There are two ways to update it without restarting the bot:

**Via Telegram** — send `/update_token <sessionKey>` from any device. The message is deleted immediately after processing. Any queued uploads are retried automatically.

**Via HTTP endpoint** — set `TOKEN_SERVER_PORT` to enable a `POST /update-token` endpoint. Intended for automated flows like the [Chrome extension](docs/features/chrome-extension-spec.md), which updates the token silently whenever you log back into your personal Claude account on desktop.

```bash
curl -X POST http://localhost:8080/update-token \
  -H 'Content-Type: application/json' \
  -d '{"token": "sk-ant-sid01-...", "secret": "your-secret"}'
```

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Token from @BotFather |
| `CLAUDE_SESSION_TOKEN` | Yes | Initial `sessionKey` cookie from claude.ai |
| `ALLOWED_USER_IDS` | Yes | Comma-separated Telegram user IDs |
| `PROJECT_WHITELIST` | No | Comma-separated project names to show (shows all if unset) |
| `TOKEN_SERVER_PORT` | No | Port for the HTTP token update endpoint (disabled if unset) |
| `TOKEN_UPDATE_SECRET` | No | Shared secret to protect the HTTP endpoint |
| `PERSONAL_ORG_ID` | No | Claude org UUID; if set, the HTTP endpoint rejects tokens from other accounts |

To find your `PERSONAL_ORG_ID`: open claude.ai while logged in, check the network requests in DevTools — it appears in URLs like `/api/organizations/<uuid>/projects`.

## Notes

- The project list is cached at startup; use `/refresh` to pick up new projects without restarting.
- Videos without captions will report "no transcript available".
- Failed uploads (e.g. due to an expired token) are queued and retried automatically after a successful `/update_token` or `/refresh`.
