# Application Security Audit

**System:** knowledger — Telegram bot + HTTP token-update server  
**Date:** 2026-05-01  
**Auditor:** Claude Code (claude-sonnet-4-6)  
**Scope:** Full static analysis — authentication, authorization, injection sinks, secrets management, HTTP API surface, CORS configuration, external dependency trust, Telegram bot input handling

---

## Overall Assessment

Knowledger is a small, single-user bot with a well-defined, narrow attack surface. The Telegram side is correctly locked behind a user-ID allowlist and a `_require_auth` decorator applied to every handler. The primary risk concentration is the HTTP token-update endpoint (`POST /update-token`): it can be deployed completely unauthenticated if `TOKEN_UPDATE_SECRET` is not set, it binds to all interfaces without TLS, and its CORS policy permits any origin to call it. A secondary concern is the transient exposure of the Claude session token in Telegram chat history during `/update_token` invocations. No SQL injection, command injection, or path traversal vectors exist.

---

## Attack Surface Map

### Entry Points

| Route / Handler | Method | Auth Required | Notes |
|---|---|---|---|
| `POST /update-token` | HTTP | Optional — only if `TOKEN_UPDATE_SECRET` set | Binds `0.0.0.0:8080` |
| `/start`, `/help`, `/refresh` | Telegram | YES — user-ID allowlist | Via `_require_auth` |
| `/update_token <token>` | Telegram | YES — user-ID allowlist | Token in message body |
| YouTube URL message | Telegram | YES — user-ID allowlist | Pattern-matched text |
| `(skip\|overwrite):*` callback | Telegram | YES — user-ID allowlist | Inline keyboard |
| `<project_id>:<msg_id>` callback | Telegram | YES — user-ID allowlist | Inline keyboard |

### User Input → Dangerous Sinks

| Input Source | Sink Type | Location | Sanitized |
|---|---|---|---|
| `body["token"]` (HTTP) | Claude API auth (session cookie) | `http_server.py:45` `claude_client.py:update_token` | PARTIAL — type-checked + stripped; no format validation |
| `body["secret"]` (HTTP) | Auth comparison | `http_server.py:25` | YES — but not constant-time |
| `context.args[0]` (`/update_token`) | Claude API auth | `bot.py:391` | PARTIAL — stripped; org validation optional |
| `update.message.text` (YouTube URL) | External HTTP (oEmbed, transcript APIs) | `youtube.py:extract_video_id` | YES — parsed via `urlparse` + `parse_qs` |
| `query.data` (callback) | Project ID / action routing | `bot.py:handle_project_selection` | YES — split + existence check |
| `track["url"]` (Invidious API response) | External HTTP request construction | `invidious.py:71` | NO — concatenated directly |

---

## Findings

### HIGH

**H-1: HTTP endpoint fully unauthenticated when TOKEN_UPDATE_SECRET is unset**

- Location: `knowledger/http_server.py:25`, `http_server.py:70`, `http_server.py:82`
- Finding: The auth check is `if secret is not None and body.get("secret") != secret`. When `TOKEN_UPDATE_SECRET` is not configured, `secret` is `None` and the entire check is skipped — the endpoint accepts any POST body without authentication. The server also binds to `0.0.0.0`, exposing the port on all interfaces. The CORS middleware is not applied either (`middlewares = [_cors_middleware] if secret is not None else []`), making the inconsistency hard to notice.
- Risk: Any attacker with network access to port 8080 can replace the live Claude session token with an invalid or attacker-controlled value, causing service disruption or, if the attacker has a valid Claude.ai session, routing all future bot activity to their account. The `PERSONAL_ORG_ID` guard only fires if that variable is also set; with neither variable set, there is no protection at all.
- Recommendation: Make `TOKEN_UPDATE_SECRET` a **required** configuration value — fail at startup if it is absent. Alternatively, enforce auth unconditionally: remove the `if secret is not None` guard and always reject requests where the secret doesn't match. The current pattern makes a dangerous no-auth state the default.

```python
# In build_aiohttp_app, raise at startup:
if secret is None:
    raise ValueError("TOKEN_UPDATE_SECRET must be set to run the HTTP server")
```

---

### MEDIUM

**M-1: Wildcard CORS permits any origin to call the token endpoint**

- Location: `knowledger/http_server.py:55`, `http_server.py:61`
- Finding: Both preflight (`OPTIONS`) and actual responses set `Access-Control-Allow-Origin: *`. Any webpage running in a user's browser can issue a cross-origin `POST /update-token` request. If the `TOKEN_UPDATE_SECRET` is ever leaked (e.g., via browser history, error messages, shared configuration), a malicious site can silently rotate the Claude session token.
- Risk: Combined with a secret leak, CSRF-style token replacement from any origin the user visits.
- Recommendation: Restrict `ACAO` to the specific browser extension origin once that is known (e.g., `chrome-extension://<id>`). If the extension ID is not known at deploy time, at minimum document the wildcard as an accepted risk and ensure the secret is treated with the same sensitivity as an API key.

**M-2: Session token sent as plaintext Telegram message**

- Location: `knowledger/knowledger/bot.py:391–409`
- Finding: `/update_token <sessionKey>` accepts the raw session token as a command argument. The bot attempts to delete the message immediately after validation (`update.message.delete()`), but deletion can fail silently — in that case only a warning is sent. Telegram stores messages server-side; even a briefly visible message may be logged by Telegram, third-party clients, or a compromised device.
- Risk: If message deletion fails (network error, permissions, Telegram API throttle), the `sessionKey` cookie value is permanently visible in chat history.
- Recommendation: Consider a two-step flow where the user DMs the token to the bot privately (already a one-on-one chat), and the bot immediately confirms deletion. Additionally, add an explicit log line at `INFO` level whenever deletion fails so operators know to act. For higher assurance, the HTTP endpoint (H-1 after fixing) is a better transport for token updates because it never touches Telegram's servers.

**M-3: Non-constant-time secret comparison**

- Location: `knowledger/knowledger/http_server.py:25`
- Finding: `body.get("secret") != secret` uses Python's built-in string equality, which short-circuits on the first differing byte. Over a local network with low jitter this is theoretically exploitable via timing attack to infer the secret one character at a time.
- Risk: In practice, TCP jitter renders this infeasible against a 64-byte hex secret over the public internet. Against a co-located attacker (same datacenter, same host) the risk rises to LOW–MEDIUM.
- Recommendation: Use `hmac.compare_digest(body.get("secret", ""), secret)` for a one-line fix.

```python
import hmac
if secret is not None and not hmac.compare_digest(body.get("secret", ""), secret):
```

**M-4: Invidious track URL concatenated without validation**

- Location: `knowledger/knowledger/invidious.py:71`
- Finding: `f"{instance}{track['url']}"` concatenates the Invidious API-provided caption track URL directly with the instance base. If a compromised Invidious instance returns a `track["url"]` beginning with an absolute URL scheme (`https://...`), the result is malformed but still sent as an HTTP request. More practically, a `//evil.com/path` value would produce `https://inv.nadeko.net//evil.com/path` — not a redirect, but unexpected.
- Risk: A malicious Invidious instance (included dynamically from `api.invidious.io`) could serve crafted URLs that cause SSRF-like requests to attacker-controlled endpoints. No credentials are sent in these requests, but transcript fetches for specific video IDs could be leaked to third parties.
- Recommendation: Validate that `track["url"]` starts with `/` (relative path) before concatenating. Reject or log and skip any entry where the URL is absolute.

```python
url = track.get("url", "")
if not url.startswith("/"):
    logger.warning("Unexpected absolute track URL from %s: %s", instance, url)
    continue
```

---

### LOW

**L-1: HTTP server binds to all interfaces without TLS**

- Location: `knowledger/knowledger/http_server.py:82`
- Finding: `TCPSite(runner, "0.0.0.0", port)` listens on all interfaces. Both the `TOKEN_UPDATE_SECRET` and the new `sessionKey` value travel in plaintext HTTP.
- Risk: On a network with a passive eavesdropper (e.g., shared hosting, cloud internal network without encryption), both the secret and the Claude session token are readable in transit.
- Recommendation: If the endpoint is consumed only by a browser extension, add a note in deployment docs that TLS termination (Railway HTTPS, nginx proxy, or Cloudflare Tunnel) must sit in front. Alternatively, bind to `127.0.0.1` if only local access is needed and expose via a tunnel.

**L-2: No rate limiting on the HTTP token endpoint**

- Location: `knowledger/knowledger/http_server.py:12`
- Finding: aiohttp imposes no per-IP or global rate limit. There is no `client_max_size` configured either (aiohttp default: 1 MB).
- Risk: An attacker can flood the endpoint to exhaust server resources, or repeatedly trigger `get_org_id_for_token` (which makes outbound Claude.ai API calls), potentially causing IP bans from Claude.ai or elevated API costs. No exploit path to data exfiltration.
- Recommendation: Add a simple in-process token bucket, or rely on a reverse proxy (Railway or nginx) for rate limiting. Set `client_max_size=1024` explicitly to prevent 1 MB JSON bodies.

**L-3: `cmd_help` bypasses `_require_auth`**

- Location: `knowledger/knowledger/bot.py:96`
- Finding: `cmd_help` is implemented as `await cmd_start(update, context)`, and `cmd_start` has `@_require_auth`. However, `cmd_help` itself has no decorator — it calls through to an authed function, so the guard runs, but the pattern is fragile. If `cmd_start`'s implementation ever changes, `cmd_help` silently loses its auth.
- Risk: Currently no bypass. Latent fragility.
- Recommendation: Apply `@_require_auth` directly to `cmd_help`.

---

### INFO

**I-1: Positive control — `.env` files correctly excluded from git**  
`.gitignore` excludes `.env*` patterns; only `.env.example` is tracked. `git ls-files` confirms no secrets are in version history.

**I-2: Positive control — Telegram auth is strict allowlist, not role-based**  
`ALLOWED_USER_IDS` must be explicitly populated. No "any authenticated Telegram user" path exists. `_require_auth` is applied as a decorator to every sensitive command handler.

**I-3: Positive control — org_id double-validation on HTTP endpoint**  
When `PERSONAL_ORG_ID` is set, the endpoint calls `get_org_id_for_token(new_token)` and rejects tokens that don't belong to the expected account. This prevents an attacker who submits a valid-but-foreign token from hijacking the client.

**I-4: Positive control — token deletion on Telegram update**  
The bot attempts `update.message.delete()` and warns the user if deletion fails. This reduces the window during which the token is visible in chat history.

**I-5: Runtime assessment required — network exposure of port 8080**  
Whether the HTTP endpoint is reachable from the public internet depends on the deployment firewall (Oracle Cloud security list, Railway private networking). Static analysis cannot verify this. Confirm that port 8080 is not publicly accessible unless required by the browser extension use case.

---

## Prioritized Remediation

### Address immediately

| # | Finding | Effort |
|---|---|---|
| H-1 | Require TOKEN_UPDATE_SECRET at startup; remove no-auth fallback | Tiny |
| M-3 | Replace `!=` with `hmac.compare_digest` | Tiny |

### Address within 30 days

| # | Finding | Effort |
|---|---|---|
| M-4 | Validate Invidious track URLs are relative paths before fetching | Tiny |
| L-3 | Add `@_require_auth` decorator directly to `cmd_help` | Tiny |
| M-1 | Restrict CORS origin to the specific extension origin | Small |
| L-2 | Add rate limiting + explicit `client_max_size` to aiohttp | Small |

### Address within 90 days

| # | Finding | Effort |
|---|---|---|
| M-2 | Move token update to a non-Telegram channel (HTTP endpoint after H-1 fixed) or add ephemeral prompt flow | Medium |
| L-1 | Document TLS termination requirement; enforce TLS or bind to 127.0.0.1 | Small |

---

## Remediation Status

**Closed:** 2026-05-01

| Finding | Severity | Status | Notes |
|---|---|---|---|
| H-1 | HIGH | Fixed | `TOKEN_UPDATE_SECRET` now required at startup when `TOKEN_SERVER_PORT` is set; `build_aiohttp_app` takes `secret: str`; unconditional auth check |
| M-1 | MEDIUM | Fixed | `CORS_ALLOWED_ORIGIN` env var controls the allowed origin; defaults to `*` with a documented upgrade path to `chrome-extension://<id>` |
| M-2 | MEDIUM | Fixed | `/update_token` Telegram command removed entirely; HTTP endpoint is now the only token update path |
| M-3 | MEDIUM | Fixed | Replaced `!=` with `hmac.compare_digest` |
| M-4 | MEDIUM | Fixed | Invidious track URL validated to start with `/` before concatenation; unexpected URLs are logged and skipped |
| L-1 | LOW | Accepted | Railway terminates TLS on the public `*.railway.app` domain — no code change needed for current deployment |
| L-2 | LOW | Accepted | HTTP endpoint is auth-gated by a required secret; single-user deployment makes resource exhaustion a negligible risk |
| L-3 | LOW | Fixed | `@_require_auth` applied directly to `cmd_help` |

---

## Sources

- [OWASP Authentication Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html)
- [OWASP CORS Security](https://owasp.org/www-community/attacks/CORS_OriginHeaderScrutiny)
- [OWASP Timing Attack](https://owasp.org/www-community/attacks/Timing_attack) — Python `hmac.compare_digest` docs: https://docs.python.org/3/library/hmac.html#hmac.compare_digest
- [OWASP SSRF](https://owasp.org/www-community/attacks/Server_Side_Request_Forgery)
- [aiohttp Security — client_max_size](https://docs.aiohttp.org/en/stable/web_advanced.html#data-size-limits)

---

## Key Files Reference

| File | Security-Relevant Purpose |
|---|---|
| `knowledger/http_server.py` | Only HTTP entry point; auth check, CORS, token update handler |
| `knowledger/bot.py` | Telegram command handlers; `_require_auth` decorator; `/update_token` flow |
| `knowledger/claude_client.py` | Stores session cookie; `update_token()` mutates live auth state |
| `knowledger/config.py` | Loads all secrets from environment; startup validation |
| `knowledger/invidious.py` | Fetches from external Invidious instances; URL construction |
| `.env` | Live secrets (not in git; do not commit) |
| `.env.example` | Public template; only this file is tracked |
