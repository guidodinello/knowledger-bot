# Feature: Message Hierarchy Pass

**Status:** Implemented.
**Value:** Low–Medium (quality-of-life across the five messages read most often)
**Effort:** Low
**Touches:** `knowledger/telegram_format.py`, `knowledger/bot.py`, `knowledger/poller.py`

## Problem

Five separate readability complaints from using the bot day to day (screenshots in
the originating conversation), all of them about *presentation* rather than behaviour:

1. `/version` prints a bare SHA. The repo is public — the SHA should be tappable.
2. `/help` runs its commands together; entries wrap at phone width and a wrapped
   second line is indistinguishable from the next command.
3. `/inqueue` gives a section header and its entries near-equal weight, so the block
   reads as a flat list rather than a header with contents.
4. "Auto-saved to `0199b13b-24ad-…`" — a raw project uuid where its name belongs.
5. `/subscribed` repeats `→ Investments` under all but one of seven channels, using
   two lines per channel to say one thing.

(4) is a defect rather than a preference: `AUTO_TRANSCRIPT_PROJECT` and a channel's
`project` in `channels.json` both accept a name *or* a uuid, and `_process_video` was
handed that configured string and printed it verbatim. `_resolve_project` already had
the matching project — with its `name` — and discarded everything but the uuid.

## Design decisions

- **`/version` links the commit; no tags or releases.** Every merge to `main` deploys,
  and every merge is a squashed PR, so a commit link already lands on a page carrying
  the PR title, body and full diff — release notes without a release process. Releases
  only add value if they are *coarser* than deploys, and the moment they are, `/version`
  can no longer name what is running (`v0.4.0+3`, then a commit link anyway). The
  command exists to answer "is my fix deployed?" (see
  [version-command.md](version-command.md)); a semver number is strictly worse at that
  than a SHA. Conventional commits are already in use, so a changelog remains available
  later via release-please without `/version` changing at all.
- **The repo URL is hardcoded, not a build arg.** A third `--build-arg` would have to
  be passed by both build paths (CI and `deploy.sh`), and the one that forgot would
  silently ship a broken link — the same trap `version-command.md` flags for
  `GIT_SHA`. The repo a bot is built from is not a per-deployment fact.
- **Command descriptions stay verb phrases.** `_COMMANDS` feeds both `/help` and
  Telegram's native command menu via `set_my_commands`; a bare noun phrase reads as a
  label rather than an action in the menu. Only `/refresh` was shortened.
- **`/inqueue` hierarchy is spent on `<blockquote>`, because size is not available.**
  HTML parse mode offers bold, italic, underline, strike, code, spoiler and blockquote
  — no font sizes. A header therefore cannot be made *bigger* than its entries, only
  structurally superior to them, and bold alone loses to a blue link one line below it.
  Blockquote indents the entries behind a vertical rule, which subordinates them.
  Caps on the section label and italics on the detail line are the nearest available
  equivalents of "bigger" and "smaller"; caps stay on the label alone, since a shouted
  "1 WAITING TO UPLOAD" reads as an alarm.
- **Bullets are kept inside the quote.** The blockquote separates a section from its
  entries; the bullet separates an entry from its own detail line. Different jobs.
- **`📊 Queue status` is dropped when there is only one section.** Above a lone
  `⏳ POLLER — 1 waiting to upload` it is a second title for one thing.
- **`/subscribed` groups on the *resolved* label, not the raw setting.** One channel
  routed by uuid and another by name to the same project must land under one heading,
  not two that happen to name the same place.
- **Channel names became links, which is what lets the listing drop `(@handle)`.** The
  handle doubled every line's width to carry an identity the link already provides.
  Largest group first (the main destination leads), unrouted channels last (they are
  the entries needing attention, and last is where a reader stops).

## Truncation hazard this introduced

`cap_message` truncates by dropping whole trailing lines, which was safe only while
every tag opened and closed on the same line — a `blockquote` spanning a section
breaks that, and Telegram rejects the *entire* message for one unbalanced tag. That is
precisely the "nothing arrives at all" failure the line-oriented truncation exists to
avoid (see [unescaped-markdown-injection.md](../bugs/unescaped-markdown-injection.md)).
`cap_message` now closes whatever is still open at the cut, and drops further lines
when the closing tags do not themselves fit in the budget.

## Mockup

```
Running 4118842, committed 2026-08-02 20:45 (3 min ago).
        ^^^^^^^ links to /commit/4118842
```

```
Send me a YouTube URL and I'll save its transcript to a Claude project.

• /subscribe <link> — watch a channel for new uploads
• /subscribed — list watched channels
...
```

```
⏳ POLLER — 1 waiting to upload          ← bold, label in caps
▏ • El PELIGRO de BAJAR RÁPIDO de PESO — DR LA ROSA
▏   seen 2026-08-01 17:08                ← italic

133 videos seen since the poller started.
```

```
✅ Auto-saved to Investments
El PELIGRO de BAJAR RÁPIDO de PESO — DR LA ROSA
```

```
📺 Watching 7 channels

Investments (default) — 6
• Crypto Currently        ← each name links to its channel
• On Chain Mind
...

Exercise — 1
• Dr. La Rosa

Checked every 1h; transcripts upload 24h after a video is published.
```

## Out of scope

- Blockquoting `/subscribed` for symmetry with `/inqueue`. Its entries are single
  lines with no detail line to separate, so the rule would be decoration without a
  job.
- Relative timestamps in `/inqueue`. Unchanged, and for the reason already recorded
  in `fmt_ts`: a message that sits unread goes stale.
- Making `/version` report anything for an unstamped local build — still the plain
  "wasn't stamped" sentence.

## Verification

1. `uv run ruff check .`, `ruff format --check`, and `pyright` clean.
2. Unit tests: section headers and balanced blockquotes in `/inqueue`
   (`test_bot_inqueue.py`); grouping by resolved label, group ordering, unrouted
   channels last, and linked names in `/subscribed` (`test_bot_subscribe.py`);
   `blockquote()` spanning a block and `cap_message` closing a quote it cut open
   (`test_telegram_format.py`); the auto-saved notification naming its project rather
   than echoing a uuid (`test_poller_per_channel_project.py`).
3. Manual: send each of the five commands in a real Telegram client and confirm no
   `BadRequest: Can't parse entities`, no link-preview card under `/version` or
   `/subscribed`, and that the `/version` SHA opens the right commit on GitHub.
