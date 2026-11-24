# Fixes from The Drive crew test (handover)

Written 2026-11-22 from a full review of the The Drive session (`projects/the-drive`, run 2026-11-18 and 2026-11-20).
Every finding below was reproduced on a scratch copy; you do not need to reread the session, its logs or the board.
Work from this file. Line numbers are as of the review and may have moved a few lines.

## Ground rules

- The working tree has a large uncommitted change set on `main` (24 modified files plus new ones: `signoff.py`,
  `needs.js`, `appearance.*`, tests). Ask the user to commit it, or branch, before you start. Never `git stash` to
  compare: it takes every uncommitted change with it.
- Never touch `projects/` (the user's songs) or `library/lessons/`, `library/state.json`. Part D (repairing the song)
  needs the user's yes first.
- To render or check against a real song, copy its `project.json` (+ `audio/`) into your scratchpad and set
  `TUNETRACK_HOME` and `TUNETRACK_PROJECTS` to scratch folders *before* importing `tunetrack` (copy
  `library/patches`, `plugins.json`, `config.json` into the scratch home). The tests already isolate themselves
  (`tests/conftest.py`).
- Tests: `uv run python -m pytest -q` (about 2 minutes, 569 pass at the start). Add a test for every fix; none of
  these bugs is caught by the current suite.
- Edit files with the Edit tool; Bash heredocs mangle backslash-n and quotes in patch scripts.
- Agent-facing text (tool docstrings, briefs, task messages) is paid for on every agent step: add the fewest words
  that change an agent's decision. No new small centred modals in the UI.
- Keep `docs/STUDIO_PLAN.md` current: add a short "The Drive fixes" entry when done.

## Part A: the mix that came out wrong (do first)

**A1. Pan scale.** Pan is -1..1 (`model.py`, `Track.pan`), but no tool says so and `api.track_update`
(`api.py:729`) and `api.track_add` (`api.py:682`) store any number. Mix set -30, 30, 15, -20, -10, which the engine
clamps to hard left or right (`engine._pan`, `engine.py:419`; same in `engine/src/Nodes.h` Fader). The Desk then
shows "L3000". Fix: in both functions, values with 1 < |pan| <= 100 are read as percent (divide by 100) and the
answer says so; anything else outside -1..1 raises. Same for pan points in `api.automation`. Say "pan -1 (left)
.. 1 (right)" in the `track_add`/`track_update` docstrings in `mcp_server.py` (lines 334, 345).
Test: `track_update(pan=-30)` stores -0.3; `pan=500` raises.

**A2. Built-in effect settings checked when set, not at render.** `fx_add`/`fx_update` (`api.py:894`, `:925`)
accept any params; `synths.fx_dsp` (`synths.py:246`, check at `:257`) raises only at render, where
`engine.py:909` turns it into a warning and drops the whole track. That is how the lead melody went missing from
tk8 (delay `time_ms`) and the take was still handed in and accepted. Fix: validate builtin params by name at
`fx_add`/`fx_update`/`track_update(instrument_params)` using the same defaults table `fx_dsp` uses (EQ already does
this through `eqmap.settings`). Add ranges per built-in effect and reject values outside them, with the unit in the
message. At least: reverb `decay`/`low_decay` 0.1-30 s (Mix set 6500 and 9000, meant ms: the tail never decays),
`predelay` 0-500 ms, `mix` 0-1; delay `time` 0.0625-8 beats (Mix set 375), `feedback` 0-0.95, `mix` 0-1. Put the
units in the `fx_add`/`fx_update` docstrings in a few words.
Test: `fx_update(params={"time_ms": 250})` raises naming `time`; reverb `decay=6500` raises.

**A3. A track that fails to render blocks hand-in and accept.** Today `render()` reports
`track '<name>': <error>` as a warning and the take goes through. Make `take_submit` (`takes.py:681`) refuse a take
whose last render of it dropped a track (or run the device check from A2 over the take's project: cheaper, no
render), and have `take_accept` (`takes.py:748`) return the same error instead of merging. Put the dropped track
first in `render()`'s warnings, prefixed so it can't be missed ("TRACK MISSING FROM THE MIX: ...").

**A4. Delivery check before export.** Add a check in `api.render` (`api.py:1889`) when `out` is given on the session
itself (beside `_takes_left_out`, `api.py:1871`), returned as `delivery` in the report: length against
`brief.length`; loop region set when the brief or task says loop; every track with notes present; left/right RMS
within 3 dB; tail below -60 dB in the last 0.5 s for a one-shot; loop seam result when looped. Warnings, not
errors. The Producer's brief (`crew/briefs.py:12`, "Finishing") says: don't call a cue finished while `delivery`
warns.

## Part B: the flow (tasks that dead-end, words that are lost)

**B1. `needs_you` without a ticket.** `studio.task_update` (`studio.py:574`) lets anyone set `needs_you`. The
Producer did it to the Arranger's task right after returning its take: no Needs You item, `_ready` (`studio.py:367`)
skips it, the floor never starts it, and the seat lamp says NEEDS YOU while the Needs You list is empty. Fix: only
the floor and `ask_human` (`studio.py:679`) set `needs_you`. `task_update(state="needs_you")` from an agent raises
and says to use `ask_human`. Also add a floor check in `tick()` (`floor.py:102`): an open task in `needs_you` with no
open attention item blocking it goes back to `queued` with an event.
Repro: make a session with an agent arranger, submit a take, Producer `take_reject`, Producer
`task_update(tid, state="needs_you")`; then `studio.ready_tasks` is empty, no open attention, `Floor._launchable()`
is False.

**B2. Talkback can't wake a seat that has an open task.** `web/studio.js:1091` adds the "Answer the user on
talkback" task only when `!s.open_tasks`. With a stuck task the message is never read. Fix: if the seat has an open
task that is not running (queued and not ready, or needs_you), still add the talkback task; if its task is running,
say in the toast that it reads the message on its next start.

**B3. The user's notes stop at the Producer.** A sign-off note ("I'm not sure this melody makes sense", "I prefer
A-Session") is stored on the take (`signed.note`, `signoff._sign`) and given to the Producer's audition
(`signoff.audition_task`, `signoff.py:179`), but `takes.take_reject` (`takes.py:815`, message at `:841`) sends the
seat only the Producer's reason. Fix: when the take has a `signed` entry for this hand-in, append "The user said:
<note>" (and their changes, if any) to the message the seat gets. The Producer's audition prompt already carries the
note; add one line to `PRODUCER` in `briefs.py`: a user's note on a take is a requirement; answer it in the accept
or return reason.

**B4. The user's brief never reaches the project.** `floor.kickoff` (`floor.py:729`) puts the user's words only in
task T1's detail. Fix: kickoff also stores them as `brief.request` on the project (via `api.project_set`, which
merges into the existing brief), and `briefs.task_prompt` (`briefs.py:242`) gives every seat's task message one line
"The user asked for: <request>" (first 300 characters). Review in particular judges "against the brief" and never
saw "about 2 minutes, loops seamlessly".

**B5. One bad field kills the whole `project_set`.** `loop="24:1"` fails at `api.py:572` (no range separator) and
nothing else in the call is applied, including the brief. Fix: parse and validate every field before applying any;
a single position for `loop` is an error that says `"1:1-25:1"` (or accept `"1:1-24:4"` style, not one position).
Error messages name the field. Also tell the Producer where the loop end goes: the bar after the last bar.

**B6. Final listen whoever accepts last.** The Producer's final listen only happens inside an audition run
(`floor._last_work`, about `floor.py:359`). When the user accepts the last take from Needs You, nothing runs. Fix:
in `tick()`, when no task is open, the last accepted take was accepted by the user, and the Producer is an agent,
add one Producer task "Final listen" (once per accepted revision) that runs render + listen + the A4 delivery check
and either adds tasks or reports done.

**B7. Seats that can't do their task are returned for it.** The Arranger was returned twice for "loop region null"
but has no `project_set`. Fix: in the Producer's audition message (`briefs._audition`), list what is outside the
seat's tools ("Arranger cannot set the loop: project_set is yours"). Smallest version: one line in `PRODUCER`: before
returning a take for something, check the seat has the tool (each seat's tools are in `roles.py`).

## Part C: wasted runs and wrong numbers

**C1. Literal quotes in string arguments.** Two Mix runs sent `"\"Pads\""`, `"\"tk8\""`, `"\"builtin:eq\""`: 75 of
the session's 114 tool errors. Fix in `mcp_server.tool` (`mcp_server.py:117`): before calling, strip one pair of
matching surrounding quotes from every top-level string argument (and strings inside list arguments).
Test: `track_update(track='"Pads"', ...)` works.

**C2. `project="none"`, `"null"`, `session="none"`.** Agents pass these strings for "no value". Treat
`"none"`, `"null"`, `""` as None for optional string arguments in the same wrapper.

**C3. `end` in score/notes_read/listen excludes the bar named.** `score(start="1", end="24")` shows bars 1-23
(`api._span`, `api.py:609`). Agents and tasks write bars inclusively ("bars 1-24"). Fix: a bare bar number as `end`
(no `:beat`) means through the end of that bar; `"24:1"` stays exclusive. Update the docstrings in one phrase.

**C4. Positions that overflow the bar.** `markers(add=[{"at": "1:5"}])` in 6/8 is stored at 2:2 without a word
(`model.pos`, `model.py:308`). Fix: a beat larger than the bar's beats raises ("6/8 has 3 beats per bar: 1:1-1:3.5").

**C5. Range warnings that trigger busywork.** `analysis.py:144` flags "many notes in the extreme register of pad"
for a drone at F2-A2; that produced a Review task, a Sound task that "fixed" it by changing the track roles to
silence the check, and a wrong masking warning from the new roles. Fix: don't flag pads or drones below the
comfortable range (sustained low pads are normal), and phrase the extreme-register line as information, not a
problem. Tell Sound in `crew/craft.py` not to change a track's role to quiet a check.
The dark_pad octave-lower reading (`listening.py:573`) is true (sub=0.25 sounds an octave down; with sub=0 the
warning goes) and its wording is fine; leave it.

**C6. Run logs mix runs.** The log name is `<task>-<attempt>-<seat>.jsonl` (`floor.py:380`) opened in append mode
(`crew/runners.py:565`), and attempts are decremented on stops/blockers, so two runs (even two models) land in one
file. Fix: add a run counter or timestamp to the name so each run has its own file.

**C7. Scorecard numbers.** `scorecard.py:133` counts every Needs You item a seat raised as "asked you", including
sign-off stops (the session showed 8 asks; the real number was 0). Count only `ask_human` items (attention with no
`stage`). Count a run "ok" only if fewer than half of its tool calls failed; add a "tool errors" column.

**C8. Lessons from a session review.** `crew_review` (`scorecard.py:277`) has the Producer write lessons; in The
Drive they blamed seats for tool bugs. In its task text: a lesson is about the seat's craft, not about a tool error
or a missing tool (those go in the review, for the user). Don't change `library/lessons/` itself (Part D).

**C9. Other songs reachable by every seat.** `project_list` is in `roles.READ` (`roles.py:15`), so a launched seat
read every project's name, path and brief and sent them to a free model that trains on prompts. Remove
`project_list` from READ (it stays for the user and unseated clients).

**C10. A run that goes quiet.** One Mix run sent nothing for 25 minutes (cause unknown; the timeout is 30 min).
In `floor._watch` (`floor.py:465`) note the time of the last event; in `tick()`, a run silent for 10 minutes gets a
`run` event `what="quiet"` and its seat lamp says so. Don't kill it.

## Part D: The Drive itself (only with the user's yes)

Do this in the app or with the user watching; it changes their song. On the session (`projects/the-drive`):
pans to Pads -0.3, Counter 0.3, Shaker -0.1, Bell Melody about 0.15; Reverb bus `decay` 6.5, `low_decay` 9;
Bass Line is about 30 dB under the mix (fader -30 plus automation -33..-39): bring it up by ear; set the loop
(`"1:1-25:1"`); the brief asked for about 2 minutes, which is 48 bars at 72 bpm in 6/8 (24 bars = 60 s): growing
the form is a crew task, not a mix fix. Ask the user whether to clear the six lessons written on 2026-11-20 in
`library/lessons/*.md` (they blame seats for these bugs). Measured on a copy: with pans and reverb fixed, left/right
balance goes from 3.8 dB to 0.7 dB and the last second of the tail from -29 dB to -71 dB.

## Done when

- Every item in A-C has a test, the suite passes, and `docs/STUDIO_PLAN.md` has a short entry.
- Changes to tool docstrings and briefs are a line or less each.
- Nothing is committed unless the user asks. Report what was done per item (A1 ... C10), and what was skipped and why.
