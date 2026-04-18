# YouTube Knowledge Capture Workflow

## Overview

This is a manual workflow for saving valuable information from YouTube videos into a Claude project's knowledge base.

## Steps

1. **Discover a video** — While browsing YouTube (videos or Shorts), find something interesting or containing valuable information worth keeping.

2. **Copy the video URL** — Copy the link to the YouTube video.

3. **Get the transcript** — Go to [https://youtubetotranscript.com/](https://youtubetotranscript.com/), paste the video URL, and retrieve the full transcript.

4. **Copy the transcript** — Select and copy the transcript text from the page.

5. **Save to Claude project knowledge** — Paste the transcript into a Claude project knowledge file, naming it following this convention:

   ```
   Youtube - {channel_name} - {video_title}
   ```

   Example:
   ```
   Youtube - On-Chain Mind - Bitcoin's Bottom Is Near — Here's How to Spot It
   ```

## Pain Points (for automation)

- The process is entirely manual and requires switching between multiple tools (YouTube, the transcript website, and Claude).
- Copying and pasting the transcript is tedious, especially for long videos.
- Naming the file consistently requires remembering and applying the naming convention each time.

## Desired Automated Solution

The goal is to reduce the workflow to the bare minimum: provide a URL, and have the rest handled automatically.

### Ideal flow

1. Go to some interface (e.g. a simple webpage).
2. Paste the YouTube video or Short URL.
3. Optionally select the target Claude project.
4. The system processes it asynchronously through the pipeline:
   - Fetches the transcript (via the YouTube transcript API or a scraping approach).
   - Derives the channel name and video title to construct the filename following the naming convention.
   - Uploads the transcript as a knowledge file to the selected Claude project.
5. The user can later open the Claude project and have a conversation with that content in context.

### MVP / best ROI approach

Selecting the Claude project may be complex to implement (requires OAuth and Claude API project management). The biggest pain points are navigating to the transcript site, copying the transcript, and manually naming and uploading the file. A high-value MVP could skip the Claude API integration entirely and instead:

- Accept a YouTube URL as input.
- Fetch the transcript automatically.
- Generate a ready-to-upload file named according to the convention (`Youtube - {channel_name} - {video_title}t`).
- Present it for download or copy — so the only remaining manual step is uploading it to the Claude project.

This eliminates most of the friction with minimal implementation complexity.

## Implementation Notes

### Transcript Fetching

Use the [`youtube-transcript-api`](https://github.com/jdepoix/youtube-transcript-api) Python library. It fetches YouTube's existing caption tracks (auto-generated or manual) directly — no scraping, no API key, no browser needed. It supports multiple languages; prefer the original language track and fall back to whatever is available.

For videos with no captions available, notify the user in Telegram and skip — no speech-to-text fallback needed.

Note: `youtubetotranscript.com` and similar services are just wrappers around the same caption tracks, so there is no advantage to using them.

### Claude Project Upload

There is an existing working implementation at `weekly-highlights/clients/claude_uploader.py`. It uses the **unofficial Claude web API** (reverse-engineered endpoints at `claude.ai/api`), authenticating via a `sessionKey` cookie. It supports uploading, listing, and deleting project knowledge files. The payload is simply `{ file_name, content }` as plain text — no binary upload needed.

It also exposes the organization and project listing endpoints, which can be used to present the user with a project picker.

An **official Anthropic SDK** for project knowledge file management may now exist or be in progress — this should be investigated before building on the unofficial approach, since the unofficial API can break without notice.

### Frontend / Trigger Interface

**Telegram bot** — Personal bot created via `@BotFather` (no approval process). The interaction flow is:

1. User sends a YouTube URL to the bot.
2. Bot fetches the video title and channel name from YouTube metadata.
3. Bot replies with inline buttons — one per Claude project — for the user to select the destination.
4. Bot fetches the transcript, constructs the filename, and uploads it to the selected project.
5. Bot confirms success or notifies if no transcript was available.

Project list is fetched from the Claude API at startup or on demand using the same unofficial API.

### Hosting

Fly.io free tier — sufficient for a personal always-on bot with no spin-down behavior. No cost for this scale.
