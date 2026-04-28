# Bug: Page-Title Exception Discards oEmbed Title

**Severity:** Medium  
**File:** `knowledger/youtube.py:65`

## Description

`fetch_video_metadata` makes two sequential HTTP requests: one to the oEmbed endpoint (for `author_name` and a truncated `title`), and one to the watch page (for the full `og:title`). The full title is preferred because oEmbed truncates long titles.

```python
# youtube.py:65
title = _fetch_page_title(video_id) or data["title"]
```

The `or` short-circuit only handles the case where `_fetch_page_title` returns `None`. If `_fetch_page_title` raises an exception — for example, `raise_for_status()` on a non-200 response, or a network timeout — the exception propagates before `data["title"]` is ever evaluated. The already-completed oEmbed response is discarded and the entire metadata fetch fails.

## Impact

Any transient YouTube watch-page failure (rate limit, temporary block, network hiccup) causes the bot to report a metadata error, even though the oEmbed data is already in hand and is sufficient to continue.

## Fix

Catch exceptions from `_fetch_page_title` inside `fetch_video_metadata` and fall back to the oEmbed title:

```python
try:
    title = _fetch_page_title(video_id) or data["title"]
except Exception:
    logger.debug("Could not fetch full page title for %s, using oEmbed title", video_id)
    title = data["title"]
```
