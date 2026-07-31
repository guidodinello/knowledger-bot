# Feature: Dedicated Bot Session

**Status:** In review.
**Value:** High (removes the bot's dependence on the browser's login state, which is the
root cause the token updater exists to work around)
**Effort:** Low
**Touches:** `knowledger/claude_client.py`, `knowledger/http_server.py`

## Problem

The bot borrows the *browser's* session. `CLAUDE_SESSION_TOKEN` is a `sessionKey` cookie
copied out of a logged-in claude.ai tab, so the bot's auth is only alive as long as that
browser session is. Logging out — to switch to the work account, say — kills the bot too.

Everything built around this so far treats the symptom rather than the cause:

- [the Chrome extension](chrome-extension-spec.md) re-couples the two by pushing a fresh
  cookie to `/update-token` on every login
- [the persistent upload queue](persistent-upload-queue.md) absorbs the failures in
  between

Both are good, and neither stops the bot's auth from dying every time the browser's does.

## Solution

Give the bot its own session, independent of any browser the user logs out of:

1. Log into claude.ai once in an incognito window (or a throwaway browser profile).
2. Copy that session's `sessionKey` into the bot.
3. Close the window without logging out — the local cookie is discarded, the server-side
   session stays valid, and there is no longer a logged-in tab that *can* revoke it.

This rests on one assumption: claude.ai allows concurrent sessions per account and
revokes only the session that logs out. Multiple simultaneous logins on separate machines
have been confirmed to work; the logout-independence half is what the verification below
pins down.

Nothing about that requires code. Two things in the codebase have to change to make the
resulting session actually last, though.

### 1. Adopt renewed session cookies

`ClaudeClient` built its `Cookie` header from a stored string and never looked at
responses, so a `Set-Cookie` carrying a renewed `sessionKey` was discarded. claude.ai
renews on a sliding expiry, so the bot was riding its original cookie until that cookie
hard-expired.

That was invisible while the extension replaced the token on every login. For a dedicated
session that nothing else refreshes, it's the difference between a session that renews
itself indefinitely and one that dies on a fixed deadline.

Every claude.ai call now goes through one `ClaudeClient._request()` choke point, which
adopts a renewed cookie when a response carries one. Adoption deliberately does *not* go
through `update_token()`: a renewal is the same session on the same account, so the org
and project caches stay valid and only the cookie changes. It persists before activating
(same rule as `update_token()`) and never raises — a failure to adopt leaves the current
cookie in place, and the next renewal gets another chance.

### 2. Stop `/update-token` from overwriting a working token

With a dedicated session, the extension becomes actively harmful: every login on any
machine POSTs a browser cookie that overwrites the bot's good long-lived token with one
that dies on the next logout. That silently restores the exact coupling this feature
removes.

So `/update-token` now probes the bot's *current* token first and only adopts the posted
one if the current one has stopped working. The extension degrades into a fallback that
fires only when the bot is genuinely broken — which is what you'd want it to be anyway.

The probe is three-valued, and the distinction matters: `UNKNOWN` (Claude unreachable,
5xx, network error) is not `INVALID`. Treating "couldn't tell" as "dead" would mean a
transient Claude outage during any login is enough to replace a perfectly good token.
`UNKNOWN` therefore fails closed and returns `503`, which is self-healing — the next login
POSTs again.

The probe bypasses the org-id cache on purpose. `get_org_id()` answers from memory after
its first success, so using it would report a revoked token as fine forever.

It also short-circuits *before* `PERSONAL_ORG_ID` validation, so a token that has already
been declined is never forwarded to Claude just to identify which account it belongs to.

`{"force": true}` skips the liveness probe and adopts the posted token regardless — the
manual escape hatch for deliberate rotation, without which a working-but-unwanted token
could never be replaced by hand. It does not skip the shared-secret or org checks.

## Endpoint contract

`/update-token` is a cross-repo contract with
[knowledger-token-updater](https://github.com/guidodinello/knowledger-token-updater),
pinned by `tests/test_http_server_contract.py`.

| Case | Status | Body |
|---|---|---|
| Token adopted | 200 | `{"outcome": "adopted"}` |
| Current token still works, posted one ignored | 200 | `{"outcome": "ignored", "reason": "current token still valid"}` |
| Couldn't verify the current token | 503 | `{"error": "could not verify current token"}` |
| Wrong/missing secret | 403 | `{"error": "forbidden"}` |
| Posted token invalid | 401 | `{"error": "token is invalid"}` |
| Posted token from the wrong account | 403 | `{"error": "token belongs to wrong account"}` |

Success responses report a single `outcome` rather than a generic `{"status": "ok"}` plus a
separate `updated` flag. Whether the posted token was actually taken up is the one thing a
caller needs to know, so it shouldn't have to correlate two fields to learn it.

That is a breaking change for the extension, which branched on `data.status === "ok"`.
Keeping `status` alongside `outcome` purely to avoid the break was considered and rejected:
both repos are ours, so the endpoint gets the shape it should have and the extension is
updated in lockstep rather than the API carrying a compatibility shim indefinitely.

## Verification

Steps 1–2 are what actually decide whether the dedicated session is viable. The rest can
be checked at leisure.

1. **Concurrent sessions on one account** — log in via incognito while a normal session is
   open; confirm both work. (Already known to work across machines.)
2. **Logout independence** — set the incognito `sessionKey` as the bot's token, then log
   out of the normal session. The bot should keep uploading. If it doesn't, claude.ai
   revokes globally, this whole approach collapses, and the extension stays the mechanism.
3. **The extension no longer clobbers** — with the bot on a live dedicated token, log into
   claude.ai normally. The extension POSTs; the bot logs `Ignored token update — the
   current token still works` and keeps its own token.
4. **Renewal actually happens** — watch for `Adopted a renewed Claude session cookie` in
   the logs over the following days. Its *absence* is also a result: it means claude.ai
   doesn't renew `sessionKey` on these endpoints, so the dedicated session has a fixed
   lifetime and will eventually need a manual refresh (which the extension will handle
   automatically, since by then the current token is invalid).
5. **Fallback still works** — revoke the dedicated session, then log into claude.ai. The
   extension's POST should be adopted (`updated: true`) and the queue should drain.

## Alternative considered: log in from the bot

Rejected. A `/login <email>` + `/code <123456>` conversation in Telegram — bot POSTs
`send_magic_link`, user relays the emailed code — would let the bot mint its own session
with no browser involved.

Three problems, in order of severity:

- **Cloudflare.** The client already needs `impersonate="chrome110"` to pass TLS
  fingerprinting on ordinary data endpoints. Auth endpoints are guarded harder, and the
  request would come from the VPS's datacenter IP with no cookie history. Likely to fail
  immediately rather than drift.
- **Worse UX.** The extension is triggered by the login event and needs zero interaction.
  `/login` is triggered by the user noticing a failure, then costs four manual steps.
- **Credential handling.** A magic-link code is full account access, and it would sit in
  Telegram's cloud message history on both sides.

The dedicated session gets the same benefit — auth decoupled from the browser — for none
of that, because the login happens in a real browser exactly once.
