# Feature: Instagram Reel support

**Status:** Implemented — [PR #32](https://github.com/guidodinello/knowledger-bot/pull/32).
**Value:** Low–Medium (occasional manual use, not a bot workflow)
**Effort:** Low
**Touches:** `scripts/transcribe.py`, `scripts/transcribe.sh` (docs only)

## Problem

Unlike YouTube, Instagram has no public transcript/caption API — there is no
`youtube-transcript-api` equivalent, and Reels typically expose no subtitle tracks at all.
Confirmed directly: `yt-dlp --list-subs` on a public-looking Reel returns *"Instagram sent
an empty media response … use --cookies"* even with no auth attempted. So the only way to
get a Reel's text is: download the audio, then transcribe it with ASR — there is nothing
to fetch, only something to generate.

The repo already solves exactly this shape of problem for YouTube videos without captions:
`scripts/transcribe.py` downloads audio via `yt-dlp` and transcribes it locally with
`faster-whisper`, run manually against the GPU box's dedicated `.venv-transcribe`. It's
currently YouTube-only in two small, non-structural ways.

## Proposed Solution

Extend the existing offline script rather than build new infrastructure:

- **No bot integration, no poller.** This stays a manual, run-it-yourself script — same as
  today. Two reasons: (1) `faster-whisper` needs a GPU to be fast, which the free-tier
  Oracle VPS running the bot doesn't have; (2) Instagram has no public RSS/Atom feed for
  accounts, so the poller's polling model doesn't transfer. Both would require materially
  new infrastructure (a hosted ASR API, a different watch mechanism) for a feature used
  occasionally — not justified yet.
- **`yt-dlp` is already source-agnostic.** The download step (`download_audio`) needs no
  Instagram-specific logic beyond authentication — an Instagram URL flows through the same
  `-x --audio-format wav` extraction as YouTube. The only two YouTube-specific things in
  the script are the android player fallback (irrelevant to Instagram) and language.
- **Cookies, not a new fallback strategy per se.** The yt-dlp error message itself says
  `use --cookies` — Instagram Reels sit behind a login-wall for programmatic access even
  when publicly viewable in a browser. Add a cookies-based download strategy, the same
  shape as the bot's `YOUTUBE_COOKIES_PATH` (Netscape/Mozilla cookie file).
- **Fix the language hardcode while we're in there.** `transcribe()` hardcodes
  `language="es"` — a leftover from the single Spanish video the script was originally
  built for (#25), not a deliberate constraint. Reels can be any language, and the current
  hardcode would silently mistranscribe non-Spanish audio today, YouTube or Instagram.
  Fix: make it a flag, default to Whisper's own language auto-detection.

Key decisions (from `instagram-transcript-generation-without-API-access-chat.md`):

- Third-party "reel transcript" sites use exactly this pattern — scrape the public video
  file (no official Graph API involved) and run their own ASR — confirming there's no
  shortcut being missed here; this is the standard approach.

## Implementation

### 1. Instagram cookies download strategy (`scripts/transcribe.py:73`, `download_audio`)

`download_audio` tries a list of `(label, build_args)` strategies in order; today that's
`_default_args` then `_android_fallback_args` (YouTube's android-client workaround,
irrelevant here). Add a third, cookie-authenticated strategy, only attempted when the user
supplies a cookies file:

```python
def _cookies_args(url: str, output_dir: Path, cookies_path: Path) -> list[str]:
    return [
        "yt-dlp",
        "--cookies", str(cookies_path),
        "-x", "--audio-format", "wav", "--audio-quality", "0",
        "-o", str(output_dir / "%(id)s.%(ext)s"),
        "--print", "after_move:filepath",
        "--no-simulate",
        url,
    ]
```

`download_audio` gains a `cookies_path: Path | None = None` parameter; when set, the
cookies strategy is tried (in place of, not in addition to, the android fallback — that
fallback is YouTube-only and would just waste a request against Instagram). The existing
YouTube strategies are unchanged when `cookies_path` is `None`, so YouTube usage is
unaffected.

### 2. Configurable language (`scripts/transcribe.py:103`, `transcribe`)

```python
def transcribe(
    audio_path: Path,
    model_size: str = "medium",
    device: str | None = None,
    language: str | None = None,  # None => Whisper auto-detects
) -> str:
    ...
    segments, _info = model.transcribe(
        str(audio_path),
        language=language,
        initial_prompt=None,
        word_timestamps=False,
        vad_filter=True,
    )
```

`language=None` is a supported `faster-whisper` value that triggers auto-detection from
the first 30s of audio — no new dependency, just removing the hardcoded default.

### 3. CLI (`scripts/transcribe.py`, `main`)

```
scripts/transcribe.sh <url> [-o out.txt] [--model medium] [--device cuda|cpu]
                       [--cookies cookies.txt] [--language es]
```

- `--cookies PATH` — Netscape-format cookie file; forwarded to `download_audio`. Required
  for Instagram URLs (yt-dlp will fail with the same "empty media response" error without
  it); optional/unused for YouTube.
- `--language CODE` — forwarded to `transcribe()`. Omit to auto-detect (new default);
  `es`/`en`/etc. to force it, same as before for known-language content.

### 4. Docstring / setup notes (`scripts/transcribe.py:1`)

Update the module docstring to state it handles YouTube *and* Instagram Reel URLs, and add
a short note on exporting Instagram cookies (e.g. a "Get cookies.txt" browser extension,
logged into the account that can view the target Reel) — mirroring how `YOUTUBE_COOKIES_PATH`
is documented for the bot.

## Edge Cases

| Scenario | Behaviour |
|----------|-----------|
| Instagram URL, no `--cookies` | yt-dlp fails with its existing "empty media response … use --cookies" error; script raises `RuntimeError` (no silent empty output) |
| Instagram URL, expired/invalid cookies | Same failure mode as above — surfaced, not swallowed |
| YouTube URL, `--cookies` omitted | Unchanged — default/android strategies as today |
| Non-Spanish audio (YouTube or Instagram), `--language` omitted | Auto-detected per-file instead of forced to Spanish (fixes a pre-existing mistranscription risk) |
| Private/restricted Reel not visible to the cookie account | yt-dlp fails at the download step, same as an unavailable YouTube video |

## Setup

1. Export Instagram session cookies to a Netscape-format file (e.g. via a browser
   extension), logged into an account that can view the target Reel.
2. `scripts/transcribe.sh <reel-url> --cookies ig-cookies.txt -o out.txt`
3. Manually upload the result: `claude-client docs upload <project> out.txt` (or via the
   Claude web UI) — no bot/API integration exists for this path. Recommended file naming
   convention for consistency with the bot's uploads: `Instagram - {account} - {caption/
   title} - {date}` (parallel to `Youtube - {channel} - {title} - {date}` from
   `build_doc_name` in `knowledger/youtube.py`) — pull `account`/`title`/`date` from
   `yt-dlp --print` metadata and name the output file accordingly before uploading.

## Out of scope

- Telegram bot integration (send-a-link UX) — would require a hosted ASR API since
  `faster-whisper` isn't practical on the free-tier VPS.
- Auto-polling Instagram accounts — no public feed equivalent to YouTube's Atom feeds.
- Programmatic doc upload from this script — stays a manual `claude-client` step.
