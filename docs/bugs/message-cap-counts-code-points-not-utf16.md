# Bug: The Message-Length Cap Counts Python Characters, Telegram Counts UTF-16 Units

**Severity:** Low
**File:** `knowledger/telegram_format.py` (`cap_message`, `cap_plain_message`)

## Description

`TELEGRAM_MAX_MESSAGE_LENGTH = 4096` was compared against `len(text)`, but the Bot API's
limit is measured in UTF-16 code units. Every astral-plane character is one Python
character and two UTF-16 units:

```pycon
>>> len("📊"), len("📊".encode("utf-16-le")) // 2
(1, 2)
```

So `len()` undercounts any message carrying emoji. `/inqueue` emits `📊 🔁 ⏳ 📥` as
section headers plus one `🛑` per stuck entry, all astral, and the weekly recap and
queue alerts carry their own.

## Impact

A message can pass the local check at ≤ 4096 and still be rejected by the API as too
long. That is the exact failure `cap_message` was written to prevent, and the send sites
that rely on it (`notify()` in particular) swallow the failure as a log line — so the
message simply never arrives.

The undercount is small — a handful of emoji per message, not per line — so it only
bites in the narrow band just under the cap. Which is also why it went unnoticed: every
existing test used ASCII, where `len()` and the real count agree.

## Fix

Measure in UTF-16 units throughout:

```python
def tg_len(text: str) -> int:
    return len(text) + sum(1 for char in text if ord(char) > 0xFFFF)
```

used for the early return, the budget arithmetic, the per-line loop, the closing-tag
check, and `_escape_prefix`'s per-character accounting.

`cap_plain_message`'s `text[:budget]` slice needed the same correction — a budget in
UTF-16 units can't index a Python string — so it now goes through `_tg_prefix`, which
walks characters and charges each its real cost. Slicing by code point is what keeps a
surrogate pair from being split: a lone surrogate is not encodable as UTF-8 and would be
rejected before its length ever mattered.

`truncate()` deliberately still counts code points. `TITLE_LIMIT` is about how wide a
title looks when it wraps, not about an API limit, and `cap_message` measures the
assembled message correctly regardless.
