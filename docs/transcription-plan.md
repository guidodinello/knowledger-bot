# Transcription Plan: faster-whisper + yt-dlp fallback

**Context**: Some YouTube videos don't have captions. The thesis project (`~/Desktop/fac/2024/proyecto-grado/tesis`) already has `yt-dlp` + `faster-whisper`. We process locally (not VPS) and upload via `claude-client`.

## Pipeline

```
YouTube URL
    │
    ▼
1. yt-dlp ─── download best audio ───→ audio.wav
    │
    ▼
2. faster-whisper medium ─── transcribe (es, no timestamps, no prompt) ───→ plain text
    │
    ▼
3. claude-client docs upload <project_id> transcript.txt ───→ Claude project KB
```

## Script: `scripts/transcribe.py`

Lives in the knowledger project so it's available for future use.

```python
# Uses yt-dlp to download audio + faster-whisper to transcribe
# Output: plain text file (no timestamps, no segments, no initial_prompt)

yt-dlp → audio file → WhisperModel("medium", compute_type="int8")
                     → segments → join text → write .txt
```

## Fallback strategy

Some YouTube videos (members-only, restricted) don't expose formats with the default web client:

1. **Default**: standard yt-dlp with best audio extraction
2. **Fallback**: `--extractor-args "youtube:player_client=android" -f "18"` — uses the Android client which often exposes formats the web client hides

## Upload

```bash
claude-client docs upload <uuid> out.txt --name "Title"
```

## Dependencies

`faster-whisper` and `yt-dlp` are in the thesis project's venv (`~/Desktop/fac/2024/proyecto-grado/tesis/backend/.venv`). Run with:

```bash
/home/guido/Desktop/fac/2024/proyecto-grado/tesis/backend/.venv/bin/python scripts/transcribe.py <url>
```
