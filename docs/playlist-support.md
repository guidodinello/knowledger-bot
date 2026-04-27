# Feature: YouTube Playlist Support

**Value:** High  
**Effort:** Medium  
**Touches:** `knowledger/youtube.py`, `knowledger/bot.py`

## Problem

Users frequently want to archive entire YouTube playlists — lecture series, podcast episodes, conference talk collections. Currently each video URL must be sent to the bot individually. A 20-video playlist requires 20 separate interactions (send URL → select project → wait), making bulk archival tedious enough that users either skip it or abandon it partway through.

## Proposed Solution

Detect playlist URLs, extract all video IDs from the playlist page, and process them sequentially against the selected Claude project — with a confirmation step before starting and a summary on completion.

A playlist URL is any YouTube URL that contains a `list=` query parameter, e.g.:
- `https://www.youtube.com/playlist?list=PLxxxxxx`
- `https://www.youtube.com/watch?v=xxxxxx&list=PLxxxxxx`

In the second form, both a single video and a playlist are present. The bot should detect the `list=` parameter and ask the user which they want: just the video, or the full playlist.

## Implementation

### 1. Add playlist functions to `youtube.py`

`extract_playlist_id(url: str) -> str | None`  
Parse the `list` query parameter from the URL. Return it if present, `None` otherwise.

`fetch_playlist_video_ids(playlist_id: str) -> list[str]`  
Fetch `https://www.youtube.com/playlist?list={playlist_id}` with `curl-cffi` (browser impersonation already used elsewhere). Parse video IDs from the `ytInitialData` JSON embedded in the page, or from the `href` attributes of `/watch?v=` links in the rendered HTML. Return the ordered list of video IDs. No YouTube Data API key is required.

### 2. Update `handle_youtube_url` in `bot.py`

After extracting the video ID, also check for a playlist ID:

- **Video only (no `list=`):** existing flow unchanged.
- **Playlist URL (no `v=`):** reply with "Playlist detected — fetching video count…", resolve the video count, then show the project keyboard with the message "Playlist: N videos — pick a project."
- **Video + playlist (`v=` and `list=`):** ask the user which they want via two inline buttons ("This video only" / "Full playlist of N videos") before showing the project keyboard.

Store `playlist_id` (if applicable) in `user_data` alongside `video_{msg_id}`.

### 3. Update `handle_project_selection` in `bot.py`

When `user_data` contains a `playlist_id`, run the batch flow instead of the single-video flow:

```
for video_id in video_ids:
    await bot.edit_message_text(f"Processing {i}/{n}: {title}…")
    transcript = await asyncio.to_thread(fetch_transcript, video_id)
    if transcript is None:
        skipped.append(video_id)
        continue
    file_name = f"Youtube - {channel} - {title}"
    await asyncio.to_thread(client.upload_content, project_id, transcript, file_name)
    saved.append(file_name)
```

Send a final summary message: "Saved 17/20 videos. 3 had no captions."

Reuse all existing `fetch_transcript`, `upload_content`, `sanitize_filename`, and `fetch_video_metadata` logic — no duplication.

## Design Constraints

- **No partial state corruption:** if the user aborts mid-playlist (session restart, bot crash), already-uploaded documents remain in the knowledge base and are not orphaned — they are valid complete transcripts.
- **Respect rate limits:** a short `asyncio.sleep(0.5)` between uploads is sufficient for a personal bot; no exponential backoff needed.
- **Whitelist applies per-project selection**, not per-video. The playlist as a whole goes to one project.

## Why This Produces the Most Value

For content-heavy users (researchers, students archiving lecture series), this collapses N interactions into one. It reuses every piece of existing infrastructure and adds no new dependencies. The marginal complexity (playlist page scraping) is isolated to two new functions in `youtube.py`.
