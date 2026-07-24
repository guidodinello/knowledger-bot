# Feature: Per-Channel Claude Project + Per-Channel Baseline Seeding

**Status:** In review — [PR #50](https://github.com/guidodinello/knowledger-bot/pull/50)
**Value:** Medium (unblocks using the poller across unrelated topics — investments vs.
exercise vs. whatever's next — instead of one project for every watched channel)
**Effort:** Medium
**Touches:** `knowledger/poller.py`, `knowledger/config.py`, `channels.json`

## Problem

### Part A — one project for every channel

`PollerSettings.auto_transcript_project` (config.py:76) is a single global value, and
`Channel` (poller.py:56-60) has no project field at all. Every channel in
`channels.json` uploads to the same Claude project. This works fine while all
watched channels are about the same topic (the current `channels.json` is all
investments/finance), but breaks the moment you want to add a channel about a
different topic — e.g. "Dr. La Rosa" for exercise — since there's no way to say
"this channel's transcripts go to the Exercise project, not Investments."

`_tick()` (poller.py:400-402) resolves the target project **once per tick**, globally,
and reuses that single `project_id` for every pending video regardless of which
channel it came from:

```python
project_id = await asyncio.to_thread(_resolve_project, self._client, self._project_name)
...
result = await self._process_video(project_id, video, now)
```

### Part B — baseline seeding is global-first-run, not per-channel

`run_poller` (poller.py:489-493) computes a single `first_run` flag from whether
`poller_state.json` exists at all, and runs `_baseline_seed` (which marks every
channel's *current* videos as `seen` **without** enqueueing them, so the poller only
ever processes videos published after it starts) only on that one global first run:

```python
first_run = not state_path.exists()
state = PollerState.load(state_path)
if first_run:
    await asyncio.to_thread(_baseline_seed, channels, state, config.transcript.proxy)
    state.save()
```

This means baseline-seeding only ever covers the channels present in `channels.json`
**the first time the poller ever ran**. Add a channel later (exactly the scenario this
feature is for — adding Dr. La Rosa alongside existing channels) and it never gets
baseline-seeded: `_tick()`'s normal detection loop treats every one of its existing
videos as "new," enqueues all of them, and floods that channel's entire back-catalogue
into whatever project it's pointed at.

Both parts are bundled into one doc because they're the same use case hitting two
different gaps: the day you add a new channel with its own project, you'd hit both
"no way to route it to the right project" and "it dumps its whole history" at once.

## Design

### Part A — `channels.json` schema + per-channel resolution

Add an optional `project` field per channel entry — name or uuid, same format
`AUTO_TRANSCRIPT_PROJECT` already accepts. Omitted means "use the global default,"
so existing `channels.json` files need no migration:

```json
[
  { "handle": "@JoseLuisCavatv", "name": "José Luis Cava", "channel_id": "UCvCCLJkQpRg0NdT3zNcI08A" },
  { "handle": "@DrLaRosaHandle", "name": "Dr. La Rosa", "channel_id": "UCxxxxxxxxxxxxxxxxxxxxxx", "project": "Exercise" }
]
```

`AUTO_TRANSCRIPT_PROJECT` stays the required on/off switch for the poller subsystem
(unchanged startup gate in `run_poller`) — it's now the *default* project rather than
the *only* project, so no new "channel with no project anywhere" validation is needed
at startup.

`Channel` gains one field:

```python
@dataclass(slots=True)
class Channel:
    handle: str
    name: str
    channel_id: str | None = None
    project: str | None = None
```

`TranscriptPoller._tick()` resolves per distinct project name once per tick (not once
globally, not once per video) and looks up each video's project via its channel:

```python
async def _resolve_project_ids(self, project_names: set[str]) -> dict[str, str | None]:
    """One _resolve_project (and thus one Claude API round trip, cached at the
    client level) per distinct configured project name, not per channel or per video."""
    return {
        name: await asyncio.to_thread(_resolve_project, self._client, name)
        for name in project_names
    }

# in _tick(), replacing the single global resolve:
channel_project_name = {
    ch.channel_id: ch.project or self._project_name
    for ch in self._channels
    if ch.channel_id
}
project_ids = await self._resolve_project_ids(set(channel_project_name.values()))

for i, video in enumerate(original_pending):
    project_name = channel_project_name.get(video.channel_id, self._project_name)
    project_id = project_ids.get(project_name)
    if project_id is None:
        settled.append(video)  # misconfigured project name — leave pending, don't drop
        continue
    ...
```

`AuthError` handling stays at the batch level (raised by the first `list_projects()`
call inside `_resolve_project`, same as today) so a token failure still only notifies
once per tick, not once per channel.

A channel whose `project` name doesn't resolve (typo, wrong org) logs the existing
`_resolve_project` error and its videos simply stay pending — same "leave it, don't
drop it" pattern already used for auth errors and transient transport errors elsewhere
in this module.

### Part B — baseline seeding tracked per-channel in `PollerState`

Replace the single `first_run` bool with a persisted set of already-seeded channel
ids on `PollerState`:

```python
@dataclass(slots=True)
class PollerState:
    path: Path
    seen: set[str] = field(default_factory=set)
    pending: list[PendingVideo] = field(default_factory=list)
    baseline_seeded: set[str] = field(default_factory=set)  # channel_ids already baseline-seeded
    auth_error_notified: bool = False
```

`load()` defaults a missing `baseline_seeded` key to an empty set (same tolerant
pattern already used — no special migration needed, see Verification below for why
that's safe). `save()` persists it the same way as `seen`.

In `run_poller`, replace the one-shot `first_run` check with "seed whichever channels
haven't been seeded yet," every run (not just the very first):

```python
state = PollerState.load(state_path)
unseeded = [ch for ch in channels if ch.channel_id and ch.channel_id not in state.baseline_seeded]
if unseeded:
    await asyncio.to_thread(_baseline_seed, unseeded, state, config.transcript.proxy)
    for ch in unseeded:
        state.baseline_seeded.add(ch.channel_id)
    state.save()
```

`_baseline_seed` itself doesn't need to change — it already only touches `state.seen`
for the channels it's given; it's simply now given "the channels not yet seeded"
instead of "every channel, but only on the very first run ever."

## Verification

1. `uv run ruff check .` clean.
2. **Per-channel project:** configure two channels with different `project` values
   (or one with `project` set and one relying on the global default), let both publish
   a detectable video (or hand-craft `pending` entries), run a tick, and confirm each
   uploads to its own configured project.
3. **Misconfigured project:** set a channel's `project` to a name that doesn't exist
   in the account; confirm its videos stay in `pending` (not dropped) and the existing
   `_resolve_project` error is logged, while other channels' videos still process
   normally in the same tick.
4. **New-channel baseline seeding:** with an existing `poller_state.json` (simulating
   a channel added after first run), add a new channel to `channels.json`, restart,
   and confirm only the *new* channel's current videos get marked `seen` (not
   enqueued) — its back-catalogue does not flood into `pending`. Existing channels'
   `seen`/`pending` state is untouched.
5. **Backward-compat migration is a no-op, not a special case:** load an old-format
   `poller_state.json` (no `baseline_seeded` key) with the *same* channels it was
   already tracking. Confirm the resulting re-run of baseline-seed for all of them is
   harmless — it only re-adds already-`seen` video ids to the `seen` set (a no-op) and
   never touches `pending`, so no re-flood risk despite every existing channel
   technically getting "re-seeded" once after upgrading.
