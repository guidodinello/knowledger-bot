# Chrome Extension: Automatic Token Updater

## Implementation prompt

Build a Manifest V3 Chrome extension that automatically updates the knowledger bot's Claude session token whenever the user logs into their Claude account on desktop. The extension requires zero user interaction after installation — logging into claude.ai is the only trigger.

---

## Problem

The knowledger bot authenticates with Claude using a `sessionKey` session cookie. This cookie is invalidated on logout. When the user logs out of their personal Claude account on desktop (e.g. to switch to a work account) and later logs back in, the bot's stored token is stale and all uploads fail until manually updated.

---

## Solution

A background service worker listens to `chrome.cookies.onChanged`. When a new `sessionKey` is set on `https://claude.ai`, it POSTs the token to the bot's `/update-token` HTTP endpoint. The bot handles account filtering server-side via `PERSONAL_ORG_ID` — the extension sends blindly for every login.

**Full user flow after installation:**

1. Log back into personal Claude account on desktop Chrome
2. Extension detects the new `sessionKey` cookie
3. Extension POSTs token to bot endpoint in the background
4. Bot is fixed — no manual steps required

**When logging into the work account:**

1. Extension detects the new `sessionKey`
2. Extension POSTs it to the bot
3. Bot calls the Claude API, sees the org doesn't match `PERSONAL_ORG_ID`, returns `403`
4. Extension logs the rejection silently — bot token is unchanged

---

## File structure

```
knowledger-token-updater/
├── manifest.json
└── background.js
```

Two files total. No popup, no options page, no icons required.

---

## `manifest.json`

```json
{
  "manifest_version": 3,
  "name": "Knowledger Token Updater",
  "version": "1.0",
  "description": "Automatically updates the knowledger bot token when you log into claude.ai.",
  "permissions": ["cookies"],
  "host_permissions": ["https://claude.ai/*"],
  "background": {
    "service_worker": "background.js"
  }
}
```

**Permissions:**

- `"cookies"` — required to listen to `chrome.cookies.onChanged` and read HttpOnly cookies
- `"https://claude.ai/*"` — host permission scoping cookie access to claude.ai only

No `"notifications"` permission is needed unless you want feedback toasts (optional).

---

## `background.js`

```js
const BOT_ENDPOINT = "http://your-server:8080/update-token";
const TOKEN_UPDATE_SECRET = "your-secret-here";

chrome.cookies.onChanged.addListener(({ cookie, removed }) => {
  if (
    !removed &&
    cookie.name === "sessionKey" &&
    cookie.domain.includes("claude.ai")
  ) {
    fetch(BOT_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: cookie.value, secret: TOKEN_UPDATE_SECRET }),
    })
      .then((res) => res.json())
      .then((data) => {
        if (data.status === "ok") {
          console.log("[knowledger] Token updated successfully.");
        } else {
          // 403 "wrong account" is expected on work account login — not an error
          console.log("[knowledger] Token update skipped:", data.error);
        }
      })
      .catch((err) => console.error("[knowledger] Failed to reach bot endpoint:", err));
  }
});
```

`BOT_ENDPOINT` and `TOKEN_UPDATE_SECRET` are hardcoded — this is a self-use extension, not a published one, so an options page adds unnecessary complexity.

---

## Installation (no publishing required)

1. Create a folder `knowledger-token-updater/` with the two files above
2. Open `chrome://extensions` in Chrome
3. Enable **Developer mode** (toggle in the top-right)
4. Click **Load unpacked** and select the folder
5. Done — the extension is active immediately

**Updating config:** edit `background.js`, then click the reload icon on the extension card in `chrome://extensions`.

---

## Compatibility

| Browser | Supported | Notes |
|---|---|---|
| Chrome (desktop) | Yes | Primary target |
| Edge (desktop) | Yes | Same Chromium extension API |
| Brave (desktop) | Yes | Same Chromium extension API |
| Firefox (desktop) | Mostly | Manifest V3 support is complete as of Firefox 109+ |
| Kiwi Browser (Android) | Yes | Supports loading unpacked extensions from a `.zip` — enables mobile use |
| Chrome (Android) | No | Chrome on Android does not support extensions |

---

## Server-side prerequisite

The bot must be running with `TOKEN_SERVER_PORT` set. See [README](../../README.md#token-management) for configuration. The `PERSONAL_ORG_ID` variable is what makes the work-account rejection work — set it to your personal Claude org UUID.
