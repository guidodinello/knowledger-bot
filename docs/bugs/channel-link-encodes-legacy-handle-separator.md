# Bug: /subscribed Links a Legacy Channel Handle to a Dead URL

**Severity:** Low
**File:** `knowledger/bot.py` (`_channel_link`)

## Description

`_channel_link` prefers a channel's canonical `/channel/{id}` URL and falls back to its
handle when no `channel_id` has been backfilled:

```python
path = f"channel/{ch.channel_id}" if ch.channel_id else quote(ch.handle, safe="@")
```

`quote()`'s `safe` parameter *replaces* its `"/"` default rather than adding to it, so
the separator inside a handle gets percent-encoded:

```pycon
>>> quote("channel/UCabc123", safe="@")
'channel%2FUCabc123'
```

A handle is not always an `@name`. `extract_channel_handle` documents and returns the
legacy `channel/UC…`, `c/Name` and `user/Name` forms too, and `resolve_channel_id`
appends them to `https://www.youtube.com/` verbatim, so they reach `_channel_link`
intact. The result is `https://www.youtube.com/channel%2FUCabc123` — a 404.

## Impact

Reachable for any `channels.json` entry with no `channel_id`, which is precisely the
case this fallback exists to serve. Aggravated by the listing dropping the `(@handle)`
suffix: the broken link is now the only identity such a channel has in `/subscribed`.

## Fix

Keep `/` in the safe set:

```diff
-    path = f"channel/{ch.channel_id}" if ch.channel_id else quote(ch.handle, safe="@")
+    path = f"channel/{ch.channel_id}" if ch.channel_id else quote(ch.handle, safe="@/")
```

The fallback branch had no test reaching it with a legacy handle — every channel in
`tests/test_bot_subscribe.py` supplied a `channel_id`, so the branch never ran.
`test_subscribed_links_a_legacy_handle_without_encoding_its_separator` now covers all
three forms.
