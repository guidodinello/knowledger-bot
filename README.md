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
| `/inqueue` | Show queued/pending uploads |
| `/subscribed` | List the channels watched for auto-upload |
| `/subscribe <link>` | Watch a channel — send a link to any of its videos (or its `@handle`), then pick a project |
| `/refresh` | Reload Claude project list |
| `/version` | Show the running build (commit SHA and date) |

## Token management

The Claude session token is a `sessionKey` cookie from claude.ai. Logging out of the browser session it came from invalidates it (e.g. when switching accounts). Update it without restarting via the HTTP endpoint: set `TOKEN_SERVER_PORT` and `TOKEN_UPDATE_SECRET` to enable a `POST /update-token` endpoint. Intended for automated flows like the [Chrome extension](docs/features/chrome-extension-spec.md), which updates the token silently whenever you log back into your personal Claude account on desktop.

```bash
curl -X POST http://localhost:8080/update-token \
  -H 'Content-Type: application/json' \
  -d '{"token": "sk-ant-sid01-...", "secret": "your-secret"}'
```

Any queued uploads are retried automatically after a successful token update.

**A posted token is only adopted when the bot's current one has stopped working.** The endpoint probes the live token first and answers `{"outcome": "ignored", "reason": "current token still valid"}` if it's still good, leaving it untouched; a token that is taken up answers `{"outcome": "adopted"}`. Add `"force": true` to replace a working token deliberately. If the current token can't be verified at all (Claude unreachable), the endpoint fails closed with `503` rather than risk replacing a token that may be fine.

That check exists so the bot can hold a session of its own instead of borrowing the browser's — see [dedicated bot session](docs/features/dedicated-bot-session.md). Log into claude.ai once in an incognito window, give the bot that `sessionKey`, and close the window without logging out: your day-to-day account switching stops affecting the bot, and the extension becomes a fallback that only fires when the bot's token is genuinely dead. The bot also picks up renewed `sessionKey` cookies from Claude's responses, so a dedicated session refreshes itself instead of expiring on a fixed deadline.

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Token from @BotFather |
| `CLAUDE_SESSION_TOKEN` | Yes | Initial `sessionKey` cookie from claude.ai |
| `ALLOWED_USER_IDS` | Yes | Comma-separated Telegram user IDs |
| `PROJECT_WHITELIST` | No | Comma-separated project names to show (shows all if unset) |
| `TOKEN_SERVER_PORT` | No | Port for the HTTP token update endpoint (disabled if unset) |
| `TOKEN_UPDATE_SECRET` | If `TOKEN_SERVER_PORT` is set | Shared secret to protect the HTTP endpoint |
| `CORS_ALLOWED_ORIGIN` | No | Allowed CORS origin for the HTTP endpoint (e.g. `chrome-extension://<id>`); defaults to `*` |
| `PERSONAL_ORG_ID` | No | Claude org UUID; if set, the HTTP endpoint rejects tokens from other accounts (prevents work-account tokens from being accepted) |

To find your `PERSONAL_ORG_ID`: while logged into your personal claude.ai account, visit `https://claude.ai/api/organizations` — grab the `uuid` from the entry with `"claude_pro"` in its `capabilities`.

## Notes

- The project list is cached at startup; use `/refresh` to pick up new projects without restarting.
- The auto-upload watch list (`channels.json`, or `CHANNELS_PATH`) is re-read on every poll, so `/subscribe` — or an edit by hand — takes effect within one `POLL_INTERVAL_SECONDS` without a restart. A newly watched channel is baseline-seeded: its existing videos are marked as seen, so only what it posts from then on gets uploaded.
- Videos without captions will report "no transcript available". A blocked/temporarily-failed transcript request is reported separately and retried — it is never treated as "no captions available".
- Failed uploads (e.g. due to an expired token) are queued and retried automatically after a successful token update.

### Upgrading to the durable queue (breaking change)

The upload queue's on-disk format (`petition_queue.json` under `DATA_DIR`) changed from a
plain JSON array to a versioned object with per-entry claim state, to make queue draining
crash-safe and immune to double-uploads. **There is no automatic migration.** If the file
exists in the old array format, the new version will fail to start (a clear, path-specific
error) rather than silently discarding it.

Before deploying this version, drain the queue to empty on the version currently running —
repeat `/refresh` from Telegram (or wait for a token-update-triggered drain) until it
reports nothing left to upload, then confirm on the host:

```bash
cat "$DATA_DIR/petition_queue.json"   # safe to deploy once this is missing or an empty array
```

If the file doesn't exist, or is already `[]`, there is nothing to convert and you can
deploy normally.
