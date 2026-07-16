# Bug: VTT Cue Sequence Numbers Bleed into Transcript

**Severity:** Low
**File:** `knowledger/invidious.py:22-35`

## Description

The WebVTT format allows optional cue identifiers between the blank line separator and the timestamp. These are typically integers (`1`, `2`, `3`, …) or short strings. A minimal VTT segment looks like:

```
1
00:00:01.000 --> 00:00:04.000
Hello world
```

`_parse_vtt` correctly skips blank lines, the `WEBVTT` header, `Kind:`, `Language:`, `NOTE`, and timestamp lines. It does not skip cue identifiers, so the bare integers `1`, `2`, `3`, … pass through as content lines and appear verbatim in the final transcript uploaded to Claude.

## Impact

The transcript gains spurious numeric lines scattered throughout the text. This is cosmetic but degrades transcript readability and may confuse Claude when it indexes the document.

## Fix

Inside the `_parse_vtt` loop, skip lines that consist entirely of digits (cue identifiers):

```python
if stripped.isdigit():
    continue
```

Add this check after the existing header-prefix check and before the timestamp check.
