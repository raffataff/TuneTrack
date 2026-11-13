# TuneTrack Studio: plan and handover

Read this first if you are picking up the studio work in a new session. It is self-contained: what the owner
wants, what was decided and why, what exists, what is verified, what to build next, and what only the owner can
unblock. Keep it current: when a stage lands or a decision changes, edit this file in the same change.

Last updated: 2026-11-13 (evening). `studio-core` (stages 1 to 2c first cut) is merged to `main` (PR #4, 8c1ad6b).
The work after it is on branch `desk-takes`, off `main` at 8c1ad6b: takes on the Desk, the Mix stopping rule, no
pictures for launched agents, then (same day, after two live sessions) seat benches, Needs You answers that fit
the situation, full-length talkback, reports and the crew scorecard, and the fixes those sessions showed; then (night) a
third live session on the new briefs, takes that can no longer miss the session, a cap on repeat questions, and the
chord track on the Desk.
**Nothing on `desk-takes` is committed yet**; the owner has not asked for a commit.

---

## 1. What the owner wants

TuneTrack today is an AI-driven music workstation for game soundtracks: a Python library, 70 MCP tools, a CLI, a
skill, and a small browser editor over an offline render engine. The owner wants it to become their main music
tool, for games, video and their own music: an **agentic studio**.

Their requirements, in their terms:

- **A crew of agents by role** (producer, mixer, ...). They choose which AI app and model fills each role, and
  can sit in any role themselves, do the work by hand, and hand it back like any other agent.
- **A real DAW**: play back in real time, full fader desk, meters, spectrum analysers, everything a human
  producer expects. When nobody is listening, agents should run however is fastest, not in real time.
- **Record real audio**: plug in a guitar or microphone (they have an SSL 2 interface with ASIO drivers).
- **Studio-shaped UI**: a rack/HUD they can rearrange, an overview of what every seat is doing, click into a
  seat to see its work. The monitor/talkback section is the chat: press and talk to the producer or a seat.
- **"Needs You"**: one clear list of what needs their attention: things to audition, choose, or unblock.
- **Auto-detect their AI apps** (Claude Desktop, Claude Code, Codex, Kilo Code in VS Code with its free and
  cheap models) and let any of them fill seats.
- **Easy plugins and instruments**: add new ones without fuss, hear them while choosing, and let agents choose
  instruments too. They want Spitfire LABS and BBC Symphony Orchestra set up (not installed yet).
- **Any genre done properly**: 8-bit, lo-fi, energetic, orchestral that sounds composed; humanised or quantised
  as the genre needs.
- **Score to picture and games**: work from video, screenshots, or the game itself; build cues around hit points.
- **MCP and skills throughout**, modular, every part able to talk to every other, usable from Claude Desktop or
  Codex as well as from its own app.
- **Look**: sleek, dark, digital with a vintage-studio feel; classy and distinctive. Explicitly not generic
  "AI slop" UI. Smooth and intuitive, but deep.

How they want the work run: the lead agent decides what to build and in what order. Routine parts may be handed
to **Opus 5.5 at medium effort** subagents, with tight briefs and a review of their output; the hard parts
(design, merge logic, audio engine, musical quality) stay with the top model. Commits and pushes still need the
owner's say-so.

---

## 2. Decisions made (and why)

| Decision | Why |
|---|---|
| **Extend TuneTrack, don't restart** | The model, harmony, notation, `listen()`, tools and skill are the musical brain and already work. Two things were missing: a crew layer and a real-time engine. |
| **A seat is a standing job; its occupant is replaceable** | The same seat can hold a launched agent, an outside app that joined over MCP, or the owner. All three use the same tools and hand work in the same way, which is what makes "step in and hand back" work. |
| **State lives in files under cross-process locks** | TuneTrack was already built this way (`project.json` + lock + revision). The board and takes follow it, so the MCP server, CLI, editor and every agent process can all work at once without a central daemon. |
| **Seats work in takes; accepting is a three-way merge by department** | Several agents can work at once without hearing each other's half-finished work, and nothing overwrites accepted music. See section 4. |
| **The Producer is the musical coordinator; the floor manager is plumbing** | There is no separate "orchestrator" agent. Scheduling, retries, routing and the Needs You list are code, so they work even when an agent is offline. |
| **Launched agents get only their seat's studio tools** | No shell, no file access. Everything they do goes through MCP and is visible on the board. |
| **Audio engine: C++ on JUCE (probably Tracktion Engine), not Rust** | Hosting commercial VST3 plugins with their own windows (HALion, Groove Agent, Waves shells, SSL) is the hard part of a DAW. The current renderer (DawDreamer, pedalboard) is already JUCE underneath, so the owner's plugins are known to work there. Rust has no mature VST3 host. Not yet tested: needs a spike (section 6, stage 3). |
| **One engine, two modes** | Live through the audio interface when the owner listens; faster than real time and cached when agents work alone. What agents measure must be what the owner hears. DawDreamer stays until the new engine null-tests against it. |
| **UI: web tech (canvas for meters/spectrum), wrapped as a desktop app later** | Best fit for a bespoke rack look, and plugin windows open as native windows from the engine process anyway. |
| **Frontend stays plain JS/CSS with no build step (decided 2026-11-13)** | The server serves `src/tunetrack/web/` as it is, so `pip install` is the whole install and there is no Node toolchain to keep working. Two classic scripts share globals: `app.js` (desk + header) and `studio.js` (rack). Revisit only if the live-engine UI (stage 3) outgrows it. |
| **The editor's server hosts the floor manager; one manager per session** | The UI has to start, pause and stop the crew, and a manager that stays up picks up new tasks and Needs You answers by itself. `floor.py` holds an OS lock on `<session>/studio/floor.lock` while it runs, so the editor and `tunetrack crew run` cannot both drive one session. |

Open decisions:

- **Licence.** JUCE and Tracktion Engine are GPL/AGPL or paid. Fine for this public repo; matters only if the
  owner ever sells it closed-source. They have not decided. Check current terms before depending on either.
- **Tracktion Engine vs a bare JUCE graph.** Decide in the engine spike.

---

## 3. The departments

Defined in `src/tunetrack/roles.py` (purpose, what each may change, tool list, suggested model tier).

| Seat | Owns | Notes |
|---|---|---|
| producer | form, brief, tracks | Plans, hands out tasks, auditions and accepts takes. Works on the session directly. Top-tier model. |
| spotting | form, brief | Picture/game: cue sheet, hit points, tempo map. Optional. |
| composer | notes, form, tracks | Themes, harmony, structure. Top-tier model. |
| arranger | notes, tracks | Who plays what, voicings, articulations, dynamics. Merges into composer on small jobs. |
| rhythm | notes, tracks | Drums, percussion, bass, feel. |
| sound | sound, tracks | Chooses and shapes instruments. |
| tracking | notes, tracks | Recorded audio. Optional; human by default. |
| mix | mix, master | Balance, EQ, dynamics, space, automation, master chain. |
| review | nothing | Independent ears; reports and files fix tasks. Best on a different model from the composer. |

Mastering/delivery, scheduling and Needs You belong to the studio, not to a seat.

---

## 4. What exists now

On `main`, except what sections 5 and 6 mark `desk-takes`, which is uncommitted on that branch.

### Code map

| File | What it is |
|---|---|
| `src/tunetrack/roles.py` | The nine seats; tool groups; `tools_for(seat)`. |
| `src/tunetrack/studio.py` | The board, in `<session>/studio/state.json` + `events.jsonl`: seats and occupants, tasks with dependencies, messages (talkback), attention items (Needs You), activity log. `me()` / `seat_join()` say which seat a process speaks for (`TUNETRACK_SEAT`, else the user). |
| `src/tunetrack/takes.py` | Takes in `<session>/takes/<id>/` (`project.json`, `base.json`, `take.json`); `merge()` (three-way), `changes()` (readable diff), `take_new / use / diff / sync / submit / accept / reject / discard`, and `take_pending()`: every take whose work is not in the session, with where it stands and whether anyone is dealing with it. |
| `src/tunetrack/crew/detect.py` | Finds Claude Code, Codex and Kilo CLIs and their models; cached in `library/agents.json`. |
| `src/tunetrack/crew/runners.py` | Launches an agent CLI headless (`Launch` → `Run`), normalises its event stream. Runners: `claude`, `codex`, `kilo`, and `script` (a Python file standing in for a model, for tests). |
| `src/tunetrack/crew/briefs.py` | What a launched agent is told: seat instructions, the Producer playbook, the sections of `skill/SKILL.md` that seat needs, and the task message. |
| `src/tunetrack/crew/floor.py` | The floor manager: `Floor(session).run()` starts agents on ready tasks, logs their activity, routes handed-in takes, handles failure. `kickoff(brief)` gives the Producer its first task. Holds the one-manager lock (`FloorBusy`, `managed()`), skips seats on hold, `stop()` / `stop_seat()` requeue without counting the attempt, and `_recover()` requeues tasks a dead manager left "working". |
| `src/tunetrack/crew/host.py` | The crew hosted in a long-lived process (the editor's server): `crew_start(brief)`, `crew_stop()`, `crew_state()`, `seat_hold(seat)`. Runs `Floor.run(keep_alive=True)` in a thread per session. |
| `src/tunetrack/bench.py` | `seat_bench(seat)`: a read-only snapshot of a seat's work (its take in progress, else its handed-in take, else the session) with what the take changed marked: notes added and removed, each mix value next to what it was, instruments, form, last render. What the seat's first workstation tab draws. |
| `src/tunetrack/crew/scorecard.py` | `crew_scorecard()` counts each seat and agent from the activity log (runs and how they ended, tool calls, minutes, takes handed in / accepted / first time / returned, asks). `crew_review()` files it as a report and tasks the Producer with the written review. `remember()` / `crew_history(seat)` keep it across sessions in `<library>/crew_history.json`; the seat picker shows it per model. |
| `src/tunetrack/mcp_server.py` | 20 new tools (70 total; `report_write / report_list / report_read` for every seat, `take_discard` for the Producer). `apply_seat()` strips a seated agent's server down to its seat's tools; seated agents also get seat instructions. |
| `src/tunetrack/server.py` | `/api/events` (server-sent events: project version + board activity, and with `?take=` the version of the take the Desk has open), board and crew functions callable from the editor (`STUDIO`), `board` and `crew` in `/api/state`, `studio_view()` for cheap redraws. A take on the Desk: `/api/state?take=tk3` and `/api/version?take=tk3`, a `take` field on `/api/call` that points the Desk's own calls at that take, and `take_cut()`. Stops hosted crews on exit. |
| `src/tunetrack/web/` | The studio UI. `index.html` (one header, two rooms), `style.css` (the rack look; all drawn in CSS, no fonts or images fetched), `studio.js` (Studio room: rack, seat workstations, Needs You, monitor/talkback, A/B audition), `app.js` (Desk room: arrangement / piano roll / mixer, and the take bar that says whether the Desk is editing the session or a take). |
| `src/tunetrack/cli.py` | `tunetrack crew detect / status / run [--brief] [--parallel] [--max-runs]`; `call` and `functions` now reach `studio` and `takes` too. |
| `src/tunetrack/model.py`, `engine.py`, `api.py` | Small changes, below. |

Changes to existing code that are easy to miss:

- `model.session_root()` / `Project.root`: a take reads its session's `audio/` and shares its `cache/`.
- `engine._cache_entry()`: each cache file is rendered by one process at a time, written under a temporary name
  and renamed when complete. Needed once several agents render at once.
- `api.set_default_project()` and `TUNETRACK_PROJECT`: a per-process default project, so an agent's tools edit
  its take without passing `project=` and without moving the editor's open project. Opening a project clears it.
- `api._path()`: a seated process may name a take of its session as `project="tk2"` or `"takes/tk2"`.
- `Project.track()` accepts `"t1 Pad"`, `"t1:Pad"`, `"t1 (Pad)"` (how listings and task texts write tracks).
- `api.render(out=...)`: a relative `out` is inside the session folder, not the calling process's folder.
- `studio.board()`: everything the UI draws in one read. `studio.seat_pause()`: a seat on hold.
- `takes.take_audition(take)`: render a take (or the session) to hear it; the UI's A/B uses it.
- An audition request on Needs You closes when its take is accepted, returned or discarded by anyone, with or
  without a floor manager running (`_Board.close_auditions`).
- The Desk names a take by id only (`tk3`, checked against a pattern and the open session's `takes/` folder), and
  the server passes `project=<take folder>` on each Desk call. It never calls `api.set_default_project`: that is
  one value per process and the server runs calls on several threads. So `take_cut()` cuts with `use=False`.
- A Desk call that carries a take also carries the session path the page believes is open; if the open project
  has changed, the call is refused (409) instead of editing the take with the same id in another session.
- `/api/state` returns `version` (what the Desk shows; `tk3@...` for a take) and `session_version`. The A/B
  audition and the stream compare against `session_version`, the Desk's own redraw against `version`.
- `mcp_server._launched()`: true in an agent the floor manager started (`TUNETRACK_SEAT` in its environment).
  `analyze()` then skips the PNGs and their paths; an outside app that took a seat with `seat_join` still gets them.
- Events carry text in full (`studio.EVENT_TEXT`, 20,000 characters). They were cut at 240 (messages) and 400
  (what an agent says), and the monitor draws from events, so long messages could not be read.
- Reports: `studio.report_write(title, body, kind)` keeps markdown in `<session>/studio/reports/<id>.md` with an
  index on the board (`board()["reports"]`). Talkback is for a few sentences; anything longer is a report.
- A returned take keeps its `reason` in `take.json`, so a person's task card and the Desk's take bar show why.
- Needs You items the floor manager raises carry `why` (limit, quota, login, attempts, start) and options whose
  `do` says what the answer means: `seat` (choose who sits there), `edit` (rewrite the task), `wait` (start again
  later: the task gets `not_before`), `cancel`, `stop`. `studio.attention_resolve` carries out wait and cancel,
  the floor reads the run-limit answer, the UI does seat and edit and then resolves. Options an agent passes to
  `ask_human` never carry `do`.
- A model that is out of usage (`floor._QUOTA` matches the error) is one item for every seat on it: seats on
  that model are not started, their tasks wait on the item, and it closes by itself when those seats have another
  model.
- A working task that gains an unfinished dependency is stopped and requeued, uncounted (`floor.tick`).
- `api._path("none")` and `takes.take_use("none" / "session")` mean what agents meant by them.
- A take reaches someone's ears whatever state its task is in. `floor._route_takes` goes by the take (the board's
  `routed` map: take id -> the hand-in that was routed), so a take handed in after its task was closed, or cut for
  no task, still becomes an audition (a Producer task carrying `audition: <take>`, or a Needs You ticket).
  `take_submit` moves a task that was already `done` back to `submitted`: its work is not in the session yet.
- A seat cannot set its own task `done` while that task's take holds work: with changes in an open take
  `task_update` refuses and says to hand in; with the take handed in it changes nothing and says the task is
  finished on acceptance. The Producer and the user still can.
- `floor._put_away_empty`: an open take that changed nothing, whose task is over and whose seat is not running, is
  closed (`rejected`, with a reason). A take with work in it is never closed by the floor.
- `api.render(out=...)` of the session adds `not_in_this_render` and a warning when takes hold work that the
  export does not contain. A working render (no `out`) and a render of a take do not.
- `studio.ASKS_PER_TASK` (2): a seat's third question to the user on one task is refused, quoting the answers it
  already has; `kind="blocker"` is not counted. A task released by an answer starts with zero attempts, and the
  restarted agent's prompt says the answer is settled.
- `take_discard` refuses a take that is already accepted or closed, and says what is left of its task.
- `floor._quota_lock`: two seats on one model that run dry in the same moment raise one item, not two.
- The Desk can call `harmony` (`server.CALLABLE`); the chord lane sends positions in beats.

### How a session flows

1. `seat_assign` puts agents (or the owner) in seats. `kickoff(brief)` adds the Producer's planning task.
2. `Floor.run()` loops: for each ready task whose seat holds an agent, cut a take (unless the seat works
   directly), launch the agent with its seat env and tools, mark the task working.
3. The agent works in its take and calls `take_submit`. If it stops without submitting but changed something,
   the floor manager submits for it and says so in the summary.
4. A submitted take goes to the Producer agent as an "Audition take" task, or to the owner's Needs You list
   when no Producer agent is seated. Answering that item with Accept or Return carries out the decision.
5. `take_accept` merges into the session (one undo step). `take_reject` sends the reason to the seat, requeues
   the task, and the same take is continued.
6. Failures: an agent that never used a tool (not logged in, wrong model) becomes a blocker immediately; one whose
   model is out of usage becomes a single item for all seats on that model; a crash is retried with the reason,
   then becomes a blocker after `max_attempts`; a run past `run_timeout` is stopped; after `max_launches` runs the
   crew stops and asks how much further to go.

### The studio UI, as built

`tunetrack ui` (or `uv run python -m tunetrack.cli serve`) serves it at http://127.0.0.1:7373/.

- **Header**: transport (render-then-play, with a tag that says whether the audio is the current revision:
  `RENDER = REV 12` or `RENDER REV 9 · NOW 12`), the CREW cluster (RUN/STOP, BRIEF, a readout of agents at work,
  runs used / limit and cost), the NEEDS YOU lamp with its count, and the STUDIO / DESK room switch.
- **Studio room**: a rack with one unit per seat (lamp and state, a VFD with occupant, current task and last
  activity, a run clock, an activity meter fed by tool calls, SEAT / HOLD / OPEN). OPEN pulls out the seat's
  workstation: TASKS (add, cancel, requeue, mark done), TAKES (what changed, changes outside the seat's
  department flagged, conflicts, A/B audition, accept / return with a reason / discard), LOG, SETUP (occupant,
  takes or direct, standing notes). SEAT opens the picker fed by `crew.detect` (runner sign-in state, model
  tier and cost).
- **A seat's bench** is the first tab of its workstation and what it opens on (unless a take is waiting for
  your ears or you have a task of your own there). It is named for the seat: ROLL (Composer, Arranger, Rhythm,
  Tracking: every part on one piano roll over markers and the chord track, notes the take added lit, removed ones
  outlined, a chip per part to hide it), CONSOLE (Mix: a strip per track with fader, pan, mute/solo, inserts and
  sends, the last render's level, and a dashed outline where each moved fader was; master strip with band balance
  and LUFS), SOUNDS (Sound: one slot per instrument, what it was, AUDITION), PLAN (Producer, Spotting: form,
  chords, the arrangement, every task and what it waits on), REPORT (Review: the last render as measured). It
  follows the seat's take live while an agent works, switches between the seat's takes and the session, and
  OPEN ON DESK lands on the matching Desk panel. Read-only: editing is the Desk's job.
- **Needs You**: one ticket per item with the answers that fit it. Audition tickets carry A/B keys, ACCEPT and
  RETURN. A seat's question gets keys to hear what it is about (A/B for a take it names, else PLAY SESSION) and
  short replies (SOUNDS RIGHT / NEEDS WORK… / YOU DECIDE, or GO AHEAD / YOU DECIDE / SKIP IT) next to the note
  box. The floor's tickets offer what the situation allows: carry on for 12 or 40 more runs or stop; try again now,
  in 15 minutes, or put another model in every seat that ran dry (one picker for all of them); rewrite the task,
  reseat, or cancel.
- **Monitor · talkback**: the activity and message feed, a target key per seat plus ALL, and WAKE: when the
  target's agent is idle, the message is also handed to it as a task so it answers now (one agent run). Long
  lines fold to a few rows; a click opens the whole text in a reader.
- **REPORTS** (key on the monitor): what the crew has filed, each opened in a reader, the scorecard for this
  session, and ASK FOR A REVIEW.
- **Desk room**: the arrangement, piano roll, mixer, track inspector, render report and brief. A bar across the
  top says what the Desk is editing: `SESSION` (green lamp, `LIVE · REV n`) or a take (cyan lamp and a cyan edge
  round the whole Desk, a readout of seat, task and number of changes, tags for HANDED IN, RETURNED n× and
  SESSION MOVED ON). Under the ruler is the **chord lane**: the chord track drawn as blocks (an amber edge marks a
  chord in another key). Double-click a bar to write its chord (symbols or roman numerals; several in one slot share
  it; Tab sets it and moves to the next slot; empty clears it; an unreadable chord keeps the box open and says
  why), drag a chord's right edge to lengthen or shorten it, Write... for a whole progression over a section, the
  loop, or from the playhead (with a key for a modulation). The selector switches between the session and its open takes. Keys: CUT A TAKE… (seat, task,
  name), CHANGES (the take's diff), SYNC (bring in what the session accepted since; asks who wins on a
  collision), HAND IN… (shows the diff, flags changes outside the seat's department, takes a summary), BACK TO
  SESSION. In a take, every edit, undo/redo, playback and export is the take's; the transport tag says so
  (`TK3 · RENDER = REV 9`). The Desk goes back to the session by itself when its take is accepted or discarded.
- **Getting to a take from the Studio room**: OPEN ON DESK on every take in a seat's TAKES tab, DESK on an
  audition ticket in Needs You, and on a seat you sit in that works in takes, WORK IN A TAKE on each open task
  (CONTINUE TKn ON THE DESK when the task already has an open take, for example one that came back).

The UI follows `/api/events`; the 1 s version poll in `app.js` only runs while that stream is down.

### The merge, in one paragraph

`merge(base, src, dst)` returns `dst + (src - base)`. Units: project fields (form, brief, master), markers and
chord track (merged per bar range), and per track: existence, name/role, instrument (sound), each mix field, and
clips. Two sides changing the same MIDI clip are merged note by note using absolute time when they touched
different bars; the same bars is a conflict. Ids created on both sides are renumbered on the incoming side and
routing follows. Conflicts are reported and nothing is applied unless the caller chooses a winner. A change
outside the take's seat's department is flagged in `take_diff`, not blocked.

### Environment variables for a seated process

`TUNETRACK_SEAT`, `TUNETRACK_SESSION` (session folder), `TUNETRACK_PROJECT` (take folder, or the session),
`TUNETRACK_HOME` (library), `TUNETRACK_PROJECTS`.

---

## 5. What is verified, and what is not

Verified:

- `uv run pytest -q` → 258 pass (135 existed before this work). `tests/test_crew_host.py` covers the manager
  lock, hold, stop, recovery, the host, `board()`, `take_audition()`, the server's calls, and a take open on the
  Desk (edits, undo and playback land in the take; ids only; a stale page is refused; the stream follows it).
- `uv run python tests/fuzz_merge.py 600` → `bad 0`. It builds random edits with the real API and checks four
  properties of the merge. **Run it after any change to `takes.py`.** It found two real flaws during development.
- Live, on a throwaway project: a free Kilo model (`kilo/kilo-auto/free`) and a Codex model
  (`gpt-5.6-luna`, low effort) each sat in the Composer seat, wrote a melody in a take using only studio tools,
  and handed it in (about 22 s each).

- Live, 2026-11-13 (stage 2b): **a whole session from one brief with no manual steps.** Producer + Composer + Mix,
  all on `kilo/kilo-auto/free`, an 8-bar menu loop: the Producer set the contract and handed out two tasks, the
  Composer wrote three parts in a take, the Producer auditioned and accepted it, Mix balanced it and added a
  reverb bus in a take, the Producer accepted that. 7 agent runs, 769 s, $0, about 3.6 M input tokens (mostly
  cached). What the agents tripped over is listed below; the fixable ones are fixed.
- Live, after the fix: a free Kilo model with Kilo's own tools denied still reaches the studio tools (4 calls,
  none failed).
- The studio UI in a browser, against a copy of that session with a scripted stand-in Composer: add a task,
  RUN, watch the unit work, the take arrives on Needs You, A/B audition (both sides render and play), RETURN
  with a reason (the same take comes back reworked), ACCEPT (merged, ticket closes, desk reloads), talkback
  with WAKE, HOLD (run stops, task requeued uncounted), STOP, the seat picker, the Desk room. No console errors.

- Takes on the Desk in a browser, 2026-11-13 (`desk-takes`), against a scratch library with the owner in the
  Composer seat: WORK IN A TAKE cuts a take and opens the Desk in it; notes, undo/redo and playback go to the take
  and the session file stays as it was; a change to the session lights SESSION MOVED ON and SYNC brings it in; an
  edit to the open take by another process shows up without a reload; HAND IN puts it on the seat's TAKES tab;
  RETURN then CONTINUE TK2 ON THE DESK reopens the same take; accepting a take that is open on the Desk puts the
  Desk back on the session. No console errors.

- Live, 2026-11-13 afternoon: **a session driven from RUN in the browser** on a scratch library, free Kilo models
  in Producer and Mix, a person in Composer. BRIEF and RUN started it; the Producer planned (13 tool calls, 95 s);
  the person's take, cut with WORK IN A TAKE and handed in on the Desk, reached the Producer agent as an audition
  task and was accepted; a second take was returned with a reason, continued on the Desk and handed in again;
  HOLD stopped a real agent mid-run and requeued its task uncounted.
- Live, the same afternoon: **the owner ran that session on for about four hours** with seven seats filled, first
  on free Kilo models and then on Codex `gpt-6.1-terra`: 47 agent runs, 827 tool calls, 12 takes (10 accepted), a
  24-bar loop exported. What it showed is in the second trips table below.
- In a browser, against a copy of that session: every bench view draws from real data, the scorecard matches the
  log, a 1,700-character talkback message opens in full, the grouped out-of-usage ticket opens one picker for both
  seats, and a quick reply resolves a listening request. No console errors. 246 tests pass.

- Live, 2026-11-13 night: **a short session on the new briefs** (`output/_preview/brief_check.py`, scratch
  library, Producer + Composer + Mix + Review on `kilo/kilo-auto/free`, an 8-bar shop loop): 7 runs, 1,576 s, $0,
  both takes accepted first time, nothing left on Needs You. No seat asked the user to listen (0 `ask_human`).
  Polling is gone: at most 3 `studio_status` + `inbox` calls in a run (it was 20 each). Review filed its
  findings with `report_write` and the Producer's session review arrived as a report. Two things it showed: Mix
  still made six render/listen passes where its brief says three at most (140 s in all, so cheap, but the rule is
  not being followed to the letter), and the free model sent `report_write` with no arguments four times before
  getting it right (its own mistake; it corrected itself on the validation error).
- In a browser, against a copy of that session: the chord lane draws under the ruler and lines up with the lanes;
  double-click, Tab to the next slot, an unreadable chord, two chords in one slot, dragging an edge out and back,
  a progression over the loop, and a chord written in a take (the session's chord track stays, the take's diff says
  `form chord track`). 258 tests pass; `fuzz_merge.py 600` → `bad 0`.

Not verified:

- The fixes for takes that miss the session, the question cap and the put-away of empty takes have tests (some
  driven by stand-in agents) but have not been seen in a live session.
- A person stepping into a take while its agent is at work (allowed; edits are serialised by the project lock,
  and nothing warns that someone else is in it).
- The new floor behaviour (one out-of-usage item per model, a run stopped when its task gains a dependency) and
  have tests but have not been seen in a live session (the short session on free models never ran dry).
- The Mix stopping rule holds the time down (140 s) but not the pass count (six, for a limit of three).

- **Claude Code as a crew runner.** Flags were checked against `--help` only; the standalone CLI is not logged
  in. Its tool-call event fixtures in the tests are hand-written.
- **A real-model session driven from the UI.** The live session ran from a script on `floor.Floor`; the UI was
  driven with a stand-in agent. Both use the same floor manager, but nobody has pressed RUN on paid models yet.
- Kilo: `--variant` on free models.
- The UI on a narrow window (it reflows below 1180 px; only glanced at), on a long session (hundreds of
  events), and after the event stream drops and reconnects.

What real agents tripped over (stage 2b), and what was done:

| Trip | Outcome |
|---|---|
| Kilo's own subagent tool `task` was called when the model meant `task_update` (every run), and its `read` tool was tried on `analyze()`'s PNGs, which Kilo refused and which ended two runs early (each cost a re-run) | Fixed: the Kilo config now starts with `"*": "deny"`, so only the studio's MCP tools exist for the agent. |
| Tracks written as `"t1 Pad"` (the Producer's task text named them that way) | Fixed in `Project.track()`. |
| `project="takes/tk2"` resolved against the agent's working folder | Fixed in `api._path()`. |
| `render(out="renders/menu_loop_mix")` landed in the folder the MCP server ran in (the repo root) | Fixed: relative `out` is inside the session. |
| First calls with scrambled argument names (`{"Pad": "A", "3": "C3"}`) | The free model's own mistake; it corrected itself on the validation error. Left alone. |
| Agents cannot see the PNGs `analyze()` returns (piano roll, spectrogram) | Fixed (`desk-takes`): a launched agent gets the checks without the pictures or their paths, and is told `score()` and `listen()` are its view. Sending the pictures as MCP image content was not done: nobody has checked which of the models in use can read images, and one that cannot may end its run on them. |
| Mix spent about 10 minutes and 14 renders chasing band percentages | Fixed (`desk-takes`), not yet seen live: the Mix brief has a stopping rule (`briefs.MIX`: fix the two or three things a listener notices, two passes, three at most, hand in with what is left). |

What the two sessions of 2026-11-13 afternoon tripped over, and what was done (all on `desk-takes`):

| Trip | Outcome |
|---|---|
| The Producer added tracks nobody was asked to write (Pad and Bass had no task) | Fixed in the playbook: every track needs a task that writes its part; Sound cannot write notes, so a colour track needs a note-writing seat too. Seen again later (an Air Bells track sat empty for 140 minutes). |
| The Producer accepted a take, which freed the mix task, and only then added the part that had to come first | Fixed twice: the playbook says to add the task and the dependency before accepting, and the floor stops a run whose task gains an unfinished dependency. |
| A free model ran out of usage and every seat on it raised its own "not logged in?" blocker (10 runs lost, 6 tickets) | Fixed: one item per model, seats on it are not started, answers are try again / in 15 minutes / another model for all of them. |
| The run limit (12 at a time) stopped the crew three times; with the user away that cost two hours | The ticket now offers 12 or 40 more runs or stop. The default limit is 40; 12 was typed into the brief box. |
| Agents asked the user to listen for them four times ("no audible playback") and Review left its task waiting three times | Brief and `ask_human` now say render() and listen() are their ears and that asking is for taste between takes. Tickets from seats carry play keys and short replies. Seen live that night: no seat asked. |
| The Producer polled `studio_status` and `inbox` 20 times each in one run while the Composer worked | Brief: end your turn when you are waiting; you are started again when there is something to do. Seen live that night: 3 at most. |
| A returned take showed a person no reason outside the log | The reason stays on the take and shows on the task card and the Desk. |
| Talkback and what agents said were cut at 240 and 400 characters; the Producer's closing crew review could not be read | Events carry full text; long lines fold and open in a reader; old sessions read full messages from the board. |
| The Producer was asked for a review of each agent and had nowhere to put it | Reports (`report_write`) and the scorecard. |
| `take_use("none")`, `project="none"`, handing in an empty take when nothing needed changing | The first two are accepted; the third's error says to finish the task with `task_update` instead. |
| Rhythm set its task done and then handed in its take (the Pulse). The task was no longer "submitted", so nobody was asked to audition it and the final export went out without the part | Fixed four ways: a seat cannot close a task whose take holds work, a late hand-in puts the task back to submitted, routing goes by the take, and an export lists the takes it does not contain. The Producer's finishing step says to clear them and it has `take_discard`. |
| A Mix task that needed no change left an empty take open for the rest of the session | The floor closes an empty take once its task is over. |
| The Producer asked, was answered, re-checked everything and asked again: three runs on one accepted take. Review's waits for answers were counted as "tried 3 times without finishing" | Two questions per task, then only blockers; an answer resets the task's attempts; the restarted run is told the answer is settled. |
| Found by a flaky test: two seats on one model that ran dry at the same moment each raised their own ticket | One lock around look-then-raise. |

Known gaps and risks:

- Each answer to a Needs You item still restarts the seat that asked (one agent run). The cap and the "settled"
  line bound it; answers to several items from one seat are not batched into one run.
- A take left open with work in it after its task was cancelled or closed by the Producer is flagged
  (`take_pending` marks it `loose`, an export warns) but nobody is asked about it.
- The chord lane edits the chord track only. Notes already written do not follow a changed chord; `score()`
  shows what no longer fits. Chords typed as roman numerals are kept as typed.
- The bench is a view. A person in the Mix seat still edits on the Desk's mixer.

- **Context cost per agent step is about 25k tokens** (a composer has 45 tools plus its brief). Mostly cached,
  but trimming tool descriptions per seat is worth doing before long sessions on paid models.
- Takes are never deleted from disk (closed ones stay for reference); no cleanup command yet.
- Tool lists per seat are coarse (`track_update` covers both fader and instrument). The real guard is the
  out-of-department flag at `take_diff`.
- Kilo has no strict mode: MCP servers in the owner's own `kilo.jsonc` would also load (it has none today).
- A take a person hands in is routed like an agent's (audition task for a Producer agent, otherwise a Needs You
  ticket), with or without a task, but only while a floor manager is running, because routing lives in
  `floor._route_takes`. With the crew stopped it waits on the seat's TAKES tab, where ACCEPT works.
- The Desk does not send the revision it loaded with its edits (`expected_rev` exists server-side; `app.js` never
  sets `rev`). Whole-list note saves are guarded by `expect_notes`; other edits are last-writer-wins.
- The hosted crew stops when the editor's server stops. Nothing restarts it; RUN does, and recovery requeues
  what was cut off.
- Talkback WAKE costs one agent run per message to an idle seat. It is a visible toggle, on by default.
- Per-seat craft guidance is just slices of `skill/SKILL.md`; only the Producer has a written playbook.

---

## 6. Build order

Stages 1, 2, 2b (except the Claude runner) and 2c (first cut) are done. Each stage below says what done looks like.

### Stage 2b: prove the crew with real models

- Done 2026-11-13: one real small session end to end on free Kilo models, and the trips it showed fixed (section 5).
- Left: once Claude is logged in, run the live smoke for the `claude` runner and fix its event parsing if needed.
- Done 2026-11-13 (`desk-takes`): the two open rows of the trips table (no pictures for launched agents, a
  stopping rule in the Mix brief).
- Done 2026-11-13: the second live session, and a long one run by the owner (section 5).
- Done 2026-11-13 night: a short session on the new briefs (section 5). Agents no longer ask the user to listen
  or poll while waiting, and reviews arrive as reports. Left: Mix's pass count, and seeing the take and question
  fixes in a live session.

### Stage 2c: the rack UI

- Done 2026-11-13 (first cut, see "The studio UI, as built"): the rack, seat workstations, Needs You, talkback,
  the crew hosted in the server, the look, honest playback labelling. The owner can run a session, watch every
  seat, answer Needs You and audition takes without the CLI.
- Left, in order of value:
  1. **The owner's first look.** Nobody but the builder has seen it. Expect a round of changes to the look.
  2. Done 2026-11-13 (`desk-takes`): **a take open on the Desk**, cut and handed in from the UI, so a person in a
     seat works like any other seat (see "The studio UI, as built").
  3. Done 2026-11-13 (`desk-takes`): a real-model session driven from RUN with a person in a seat.
  4. Done 2026-11-13 (`desk-takes`, first cut): per-seat benches (ROLL, CONSOLE, SOUNDS, PLAN, REPORT), read-only.
     Left: editing in place on the bench (faders on the console). The Composer's chord-track editor is on the Desk
     (the chord lane, done 2026-11-13 night).
  5. Done 2026-11-13 (`desk-takes`): Needs You answers per situation, full-length talkback, reports, scorecard and
     crew history. Left: the owner's view of how these read in use; a review that also weighs cost per accepted
     take once paid runs report usage; picking a crew from history automatically.
  6. Later niceties: workflow/audio cable view, movable modules (stage 6), push-to-talk.

### Stage 3: live engine (the long pole; spike can run alongside 2c)

- Blocked until a C++ toolchain is installed (section 7).
- Spike, about a week: load HALion Sonic, one SSL plugin and one Waves plugin; play an existing `project.json`
  through the SSL 2 over ASIO; open a plugin window; null-test a render against today's DawDreamer render.
  Decide Tracktion Engine vs bare JUCE.
- Then: engine as its own process controlled by the Python side; real-time transport, faders, meters, spectrum;
  agents' edits hot-swap into the playing graph; offline mode replaces DawDreamer once it null-tests.
- Also closes existing limits in the README: plugin parameter automation, tempo-synced effects, time-stretch.

### Stage 4: tracking

ASIO input, record-arm, monitoring, takes and comping on audio tracks, MIDI keyboard input.

### Stage 5: picture and game

Video track locked to the transport; the Spotting seat reads frames and cuts (needs `ffmpeg`) and writes
markers and a tempo map so hits land on beats; screenshots and recorded gameplay first, an agent playing the
game later.

### Stage 6: polish

Desktop packaging, movable rack modules, voice talkback, watched plugin folders with auto-scan and a gear
browser that auditions live, genre style sheets as per-seat skills.

---

## 7. Only the owner can do these

| Needed for | What |
|---|---|
| Claude as a crew agent | Run `claude auth login` once in a terminal. |
| Stage 3 | Install Visual Studio Build Tools (C++ workload) and CMake. Multi-gigabyte system install; ask first. |
| Orchestral palette | Install Spitfire LABS / BBC SO Discover (needs their Spitfire sign-in), then `tunetrack scan` and capture patches. |
| Stage 5 | Install `ffmpeg` on PATH. |
| New tools in their own apps | Restart Claude so the MCP server reloads (it runs from this checkout). |
| Keeping the work | Say when to commit `desk-takes`, and whether to push / open a PR. |

---

## 8. Working rules for this repo

- **Commands from an agent's shell**: `uv run tunetrack ...` and `.venv/Scripts/tunetrack.exe` are blocked there
  by Windows Application Control. Use `uv run python -m tunetrack.cli <command>` and `uv run pytest -q`.
- **Don't commit or push unless the owner asks.** The repo is public (`raffataff/TuneTrack`): no secrets, no
  licensed sample content, and never the owner's email (use the GitHub noreply address already in git config).
- **Don't disturb the owner's open project.** `library/state.json` holds what their editor shows; `project_new`
  and `project_open` change it. For experiments use a folder outside `projects/`, pass `project=` or
  `set_default_project`, and restore the current project afterwards.
- **Live model calls cost the owner money.** Use the `script` runner for tests, free Kilo models or low-effort
  cheap models for live checks, and keep them tiny.
- **Never read credential files** (`~/.codex/auth.json`, Kilo/Claude auth stores) or change the owner's app
  configs (`~/.claude`, `~/.codex`, `~/.config/kilo`).
- **`patch_capture` opens a plugin window on the owner's desktop**: only when they are at the computer.
- Subagents: brief them with the exact files they may touch, no commits/installs/config changes, a cap on live
  calls; then read their diff and re-run the tests yourself.
- Writing style in code and docs: plain, specific, no filler. Tool docstrings are prompts for other agents:
  say what done looks like.

## 9. Picking up: first ten minutes

```bash
git status --short
```
```bash
uv run pytest -q
```
```bash
uv run python tests/fuzz_merge.py 200
```
```bash
uv run python -m tunetrack.cli crew detect
```

Then read `roles.py`, `studio.py`, `takes.py` and `crew/floor.py` in that order (about 1,700 lines), and
`tests/test_crew_floor.py` for how a session is driven. For the UI, read `web/studio.js` top to bottom (about 820
lines) and the "takes on the desk" part of `web/app.js`, and run it against a scratch library: set `TUNETRACK_HOME` and `TUNETRACK_PROJECTS` to temporary folders
before starting the server, and seat a `script` runner so nothing is spent.

The owner has now used the UI for a full session (2026-11-13) and asked for better Needs You handling, readable
talkback, and agent reviews; all three are built on `desk-takes` and waiting for their reaction. Since then the
short live session on the new briefs is done, and so are three of the known gaps (chord track on the Desk, takes
that miss the session, a run per Needs You answer). Next: editing in place on the benches (faders on the Mix
console first), tightening Mix's pass count, then the rest of "Known gaps" in section 5 (context cost per agent
step, the Desk sending the revision it loaded, a take cleanup command). The Claude runner smoke and the stage 3
spike wait on section 7.

To try it on the owner's test session: `.claude/launch.json` has a temporary entry `tunetrack-live-check` that
serves `output/_preview/live` (their session, in a scratch library) on port 7399. Remove that entry before any
commit; `output/` is not tracked. `output/_preview/brief_check.py` reruns the short live session (free models,
about 25 minutes) and `output/_preview/serve_check.py` serves a copy of its result with nobody to launch.
