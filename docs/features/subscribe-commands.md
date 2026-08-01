# Feature: `/subscribed` and `/subscribe` Commands

**Status:** In review.
**Value:** Medium (the auto-upload watch list becomes visible and editable from
Telegram instead of only by SSH + editing JSON)
**Effort:** Medium
**Touches:** `knowledger/subscriptions.py` (new), `knowledger/bot.py`,
`knowledger/poller.py`, `knowledger/youtube.py`, `README.md`

## Problem

`channels.json` is the poller's watch list, and today the only way to see or change it
is on the host:

```bash
ssh vps 'cat /opt/knowledger/channels.json'   # what am I even watching?
ssh vps 'vim /opt/knowledger/channels.json'   # add a channel, then restart the bot
```

Two things make that worse than it looks:

1. **An entry needs a `channel_id`**, which is a `UCxxxx` string that appears nowhere in
   the YouTube UI. You either paste the `@handle` and trust the poller's backfill, or go
   digging in page source.
2. **The poller reads `channels.json` exactly once**, in `run_poller()` before its loop
   starts. Editing the file has no effect until the container restarts — and if the file
   is empty at startup the poller returns immediately and never runs at all.

Meanwhile the natural thing to have in hand when you decide "I want everything this
channel posts" is a link to *a video* from it, not the channel's id or even its handle.

## Design

### 1. Resolution — `knowledger/subscriptions.py` (new)

A small module that turns whatever the user pasted into the three fields a `Channel`
entry needs (`handle`, `name`, `channel_id`):

```python
def resolve_subscription(url: str, proxy: ProxyConfig | None = None) -> ResolvedChannel
```

- **A video link** (the documented path) goes through the existing
  `fetch_video_metadata()`. oEmbed already returns `author_name` — the channel name —
  and `author_url`, which is the channel's own URL; the only new thing is that
  `VideoMetadata` now carries `channel_url` so the handle can be read off it. The
  `@handle` then goes through the poller's existing `resolve_channel_id()` scrape.
- **A channel link or bare `@handle`** is accepted too, since it's what a user will
  reach for half the time. `youtube.com/channel/UC...` already contains the id and skips
  the scrape entirely; the display name comes from the Atom feed's author element
  (`fetch_feed`), which the poller already parses, falling back to the handle text if the
  feed can't be read.

Failures raise `SubscriptionError`, whose message is written to be shown in Telegram
verbatim ("Try again in a minute", not "NoneType has no attribute").

`add_subscription()` performs its load/check/append/save under the same filesystem lock
used by the poller's `channel_id` backfill. The backfill's final write also uses an
atomic replace-existing operation, so a concurrent append cannot be clobbered and a
channel file deleted in flight is not recreated as `[]`. It returns None if the channel
is already present, matching on **either** id or handle: a hand-written entry may have
no id yet, and a channel that changed its handle still matches by id.

### 2. Making the watch list live — `knowledger/poller.py`

Writing `channels.json` is pointless if the poller only reads it at startup, so the
startup sequence — load, backfill missing ids, baseline-seed anything new — is extracted
into `sync_channels()` and re-run at the top of every tick.

This is deliberately *the same* path a channel added by hand goes through, which means a
newly subscribed channel is baseline-seeded like any other: its back catalogue is marked
seen, never uploaded. Doing the seeding in the poller rather than in the command handler
is what buys that for free — `state.baseline_seeded` already tracks exactly this.

Three consequences worth stating:

- **An empty or missing watch list is no longer fatal.** `run_poller()` used to `return`
  when there were no channels; now it logs and keeps ticking, so the first `/subscribe`
  starts working within one poll interval instead of at the next restart.
- **An unreadable `channels.json` mid-run keeps the channels already loaded** instead of
  clearing them. Losing the file is a reason to keep watching what we have, not to
  silently stop watching everything. A file that legitimately parses to `[]` *does*
  clear the list — that's an edit, not a failure. Startup stays fail-closed, though: a
  corrupt watch list at boot still raises, because booting up watching nothing would
  hide a configuration error rather than degrade around a transient one.
- **A pending video whose channel was removed still uploads**, to the global default
  project. Previously `channel_project_name[video.channel_id]` would `KeyError`; with a
  reloading watch list, "the channel is gone but its video is mid-flight" stops being
  a theoretical state, and dropping the video would be silent data loss.

### 3. Commands — `knowledger/bot.py`

`/subscribed` reads the watch list and prints it, resolving project uuids to project
names through the Claude client's cached list. That resolution is display-only and
degrades to printing the raw uuid on any failure: a dead token is no reason to refuse
to answer "what am I watching?".

`/subscribe <link>` resolves the channel, rejects one already on the list, and then
offers the **same inline project picker as the upload flow** — `_build_keyboard()` grew a
`prefix` parameter so its callbacks route to a different handler (`sub:`), keeping the
whitelist filtering and the `More...` row. On top of it sits one extra row, **Default
(<project>)**, which writes no `project` key at all so the channel inherits
`AUTO_TRANSCRIPT_PROJECT` — the common case, and the reason `save_channels()` already
drops null projects.

Nothing is written until a project is picked, and the resolved channel lives in
`user_data` between the two steps, exactly like the pending video in the upload flow
(so a tap on a picker from before a restart answers "Session expired", not a traceback).

Both replies use HTML parse mode for consistent emphasis. Every arbitrary channel/project
name is passed through `telegram_format.esc()` (and video mentions through `subject()`),
so YouTube-provided strings cannot become markup — see
`docs/bugs/unescaped-markdown-injection.md` for the bug class this centralization avoids.

The `_projects_for_picker()` helper that `/subscribe` uses for its picker is the same
live-list-then-cached-list-then-give-up logic `handle_youtube_url` already had inline;
it moved into a helper rather than being duplicated.

## Mockup

```
/subscribed

📺 Watching 3 channel(s)

• José Luis Cava (@JoseLuisCavatv)
  → Investments (default)
• Dr. La Rosa (@DRLAROSA)
  → Exercise
• Neurona Financiera (@NeuronaFinancieraOK)
  → Investments (default)

Checked every 1h; transcripts upload 24h after a video is published.
```

```
/subscribe https://www.youtube.com/watch?v=dQw4w9WgXcQ

Looking up the channel...
Found Rick Astley (@RickAstleyYT).

Where should its transcripts go?
[ Default (Investments) ]
[ Investments ]
[ Exercise ]
[ More... ]

→ tap → ✅ Watching Rick Astley (@RickAstleyYT).
        New videos go to Investments, picked up within 1h and uploaded 24h after
        publication.
```

## Out of scope

- **`/unsubscribe`.** Removing a channel is rare, and it raises a question adding one
  doesn't: what happens to that channel's already-pending videos. The reload work here
  is what makes it a small follow-up (the poller now handles a channel disappearing
  mid-flight), so it can be added when it's actually wanted.
- **Editing an existing subscription's project.** Same reasoning; today it's a one-line
  edit of `channels.json`, and the poller now picks that edit up without a restart.
- **BotFather-side command setup.** Commands are registered programmatically at startup:
  `build_application()` installs the `_register_commands` `post_init` hook, which calls
  `set_my_commands` from the same command list used by `/help`; no manual BotFather edit
  is part of this feature.
- **Immediate first poll after subscribing.** A new channel is picked up within one
  `POLL_INTERVAL_SECONDS`, and the first upload waits 24h after publication anyway —
  triggering a tick on subscribe would buy nothing real.

## Verification

1. `uv run ruff check .`, `uv run pyright`, `uv run pytest` clean.
2. **Unit tests**
   - `tests/test_subscriptions.py`: URL-form parsing (video, `@handle`, `/channel/UC…`,
     legacy `/c/` and `/user/`, non-YouTube), resolution from each entry point, the
     unresolvable-channel error, and `add_subscription`'s append / no-null-project /
     duplicate-by-id / duplicate-by-handle behaviour.
   - `tests/test_bot_subscribe.py`: `/subscribed` rendering (empty, names resolved,
     default marker, corrupt file, unusable project list), `/subscribe` (usage, bad
     link, picker callbacks and `Default` row, already-watched), and the picker callback
     (writes the entry, inherits the default, appends, session expiry, `More...`).
   - `tests/test_poller_per_channel_project.py` (Part C): a channel added to
     `channels.json` mid-run is picked up and baseline-seeded on the next tick, a corrupt
     file keeps the loaded channels (while a corrupt file at startup still raises), and a
     pending video from a removed channel still uploads.
3. **Live:** `/subscribe` a channel with a video link, confirm the entry appears in
   `channels.json` on the host, `/subscribed` lists it, and the next poller tick logs
   the baseline seed rather than a burst of pending videos.
