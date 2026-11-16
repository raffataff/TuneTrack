# TuneTrack Studio: plan and handover

Read this first if you are picking up the studio work in a new session. It is self-contained: what the owner
wants, what was decided and why, what exists, what is verified, what to build next, and what only the owner can
unblock. Keep it current: when a stage lands or a decision changes, edit this file in the same change.

Last updated: 2026-11-16. Merged to `main`: `studio-core` (PR #4), `desk-takes` (PR #5, 47e445a: takes on the
Desk, benches, Needs You per situation, reports and scorecard, the usage cuts, the Mix console, per-seat craft
text, the take fixes below, and stage 3's first cut), a dependency bump (PR #6), `crew-usage` (PR #7, 826a36e:
the take-path fix, `listen()` for a bar range, lean MCP instructions, batched auditions, open tasks for the seats
that add tasks), `offline-engine` (PR #8, 880e673: stage 3.2, offline rendering through the engine), and
`engine-lanes` (PR #9: stage 3.3, strips on several cores, sample-rate conversion, the mono sidechain key, SFZ
through sfizz; and stage 3.4, automation of any device setting, tempo-synced effects, time-stretch of audio clips).

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
| **Audio engine: C++ on JUCE (probably Tracktion Engine), not Rust** | Hosting commercial VST3 plugins with their own windows (HALion, Groove Agent, Waves shells, SSL) is the hard part of a DAW. The current renderer (DawDreamer, pedalboard) is already JUCE underneath, so the owner's plugins are known to work there. Rust has no mature VST3 host. Built on bare JUCE 9 (2026-11-14, section 6, stage 3). |
| **One engine, two modes** | Live through the audio interface when the owner listens; faster than real time and cached when agents work alone. What agents measure must be what the owner hears. DawDreamer stays until the new engine null-tests against it. |
| **UI: web tech (canvas for meters/spectrum), wrapped as a desktop app later** | Best fit for a bespoke rack look, and plugin windows open as native windows from the engine process anyway. |
| **Frontend stays plain JS/CSS with no build step (decided 2026-11-13)** | The server serves `src/tunetrack/web/` as it is, so `pip install` is the whole install and there is no Node toolchain to keep working. Two classic scripts share globals: `app.js` (desk + header) and `studio.js` (rack). Revisit only if the live-engine UI (stage 3) outgrows it. |
| **The editor's server hosts the floor manager; one manager per session** | The UI has to start, pause and stop the crew, and a manager that stays up picks up new tasks and Needs You answers by itself. `floor.py` holds an OS lock on `<session>/studio/floor.lock` while it runs, so the editor and `tunetrack crew run` cannot both drive one session. |

Open decisions:

- **Licence.** The engine links JUCE under its AGPLv3 option, so the engine's source has to be offered under
  AGPLv3. The owner said (2026-11-14) the project will probably be open source; the repo has no LICENSE file
  yet. Simplest: AGPL-3.0 for the whole repo. Their call; nothing was added.
- Decided 2026-11-14: bare JUCE, not Tracktion Engine (section 6, stage 3).

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

On `main`.

### Code map

| File | What it is |
|---|---|
| `src/tunetrack/roles.py` | The nine seats; tool groups; `tools_for(seat)`. |
| `src/tunetrack/studio.py` | The board, in `<session>/studio/state.json` + `events.jsonl`: seats and occupants, tasks with dependencies, messages (talkback), attention items (Needs You), activity log. `me()` / `seat_join()` say which seat a process speaks for (`TUNETRACK_SEAT`, else the user). |
| `src/tunetrack/takes.py` | Takes in `<session>/takes/<id>/` (`project.json`, `base.json`, `take.json`); `merge()` (three-way), `changes()` (readable diff), `take_new / use / diff / sync / submit / accept / reject / discard`, `take_pending()`: every take whose work is not in the session, with where it stands and whether anyone is dealing with it, and `take_cleanup()`: deletes the folders of accepted and closed takes (the user only; lists unless told to delete). |
| `src/tunetrack/crew/detect.py` | Finds Claude Code, Codex and Kilo CLIs and their models; cached in `library/agents.json`. |
| `src/tunetrack/crew/runners.py` | Launches an agent CLI headless (`Launch` → `Run`), normalises its event stream. Runners: `claude`, `codex`, `kilo`, and `script` (a Python file standing in for a model, for tests). |
| `src/tunetrack/crew/briefs.py` | What a launched agent is told: seat instructions, the Producer playbook, the Mix stopping rule, the seat's craft text, and the task message. |
| `src/tunetrack/crew/craft.py` | How each seat does good work, written per seat (2026-11-14) instead of cut from `skill/SKILL.md`: the Composer gets melody, harmony and the notation; Mix gets levels, EQ engines, sidechain and loudness; and so on. A test keeps each text to the tools that seat has. Briefs went from 5,500-15,500 characters to 3,500-12,000 (Composer 14,700 to 6,100). |
| `src/tunetrack/crew/floor.py` | The floor manager: `Floor(session).run()` starts agents on ready tasks, logs their activity, routes handed-in takes, handles failure. `kickoff(brief)` gives the Producer its first task. Holds the one-manager lock (`FloorBusy`, `managed()`), skips seats on hold, `stop()` / `stop_seat()` requeue without counting the attempt, and `_recover()` requeues tasks a dead manager left "working". |
| `src/tunetrack/crew/host.py` | The crew hosted in a long-lived process (the editor's server): `crew_start(brief)`, `crew_stop()`, `crew_state()`, `seat_hold(seat)`. Runs `Floor.run(keep_alive=True)` in a thread per session. |
| `src/tunetrack/bench.py` | `seat_bench(seat)`: a read-only snapshot of a seat's work (its take in progress, else its handed-in take, else the session) with what the take changed marked: notes added and removed, each mix value next to what it was, instruments, form, last render. What the seat's first workstation tab draws. The Mix console's controls edit through the Desk's own calls, not through this module. |
| `src/tunetrack/crew/scorecard.py` | `crew_scorecard()` counts each seat and agent from the activity log (runs and how they ended, tool calls, minutes, takes handed in / accepted / first time / returned, asks). `crew_review()` files it as a report and tasks the Producer with the written review. `remember()` / `crew_history(seat)` keep it across sessions in `<library>/crew_history.json`; the seat picker shows it per model. |
| `src/tunetrack/mcp_server.py` | 20 new tools (70 total; `report_write / report_list / report_read` for every seat, `take_discard` for the Producer). `apply_seat()` strips a seated agent's server down to its seat's tools; seated agents also get seat instructions. |
| `src/tunetrack/server.py` | `/api/events` (server-sent events: project version + board activity, and with `?take=` the version of the take the Desk has open), board and crew functions callable from the editor (`STUDIO`), `board` and `crew` in `/api/state`, `studio_view()` for cheap redraws. A take on the Desk: `/api/state?take=tk3` and `/api/version?take=tk3`, a `take` field on `/api/call` that points the Desk's own calls at that take, and `take_cut()`. Stops hosted crews on exit. |
| `src/tunetrack/web/` | The studio UI. `index.html` (one header, two rooms), `style.css` (the rack look; all drawn in CSS, no fonts or images fetched), `studio.js` (Studio room: rack, seat workstations, Needs You, monitor/talkback, A/B audition), `app.js` (Desk room: arrangement / piano roll / mixer, and the take bar that says whether the Desk is editing the session or a take). |
| `src/tunetrack/cli.py` | `tunetrack crew detect / status / run [--brief] [--parallel] [--max-runs]`; `tunetrack takes clean [--project] [--days N] [--yes]`; `call` and `functions` now reach `studio` and `takes` too. |
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
- `app.js` `api(fn, args, take, rev)`: `take` defaults to what the Desk has open; a bench passes the take it shows
  (null: the session), so a bench edit lands in what the bench shows whatever the Desk is on.
- `/api/call` refuses (409) a Desk call on a take that is accepted or closed: a page a moment behind would have
  edited a folder nobody will hear.
- Edits that name a thing by its place in a list (`fx_update`, `fx_remove`, `track_move`: `BY_PLACE` in `app.js`)
  carry the revision the page read (`rev` in the call body, `api.expected_rev` on the server) and are refused if the
  project has moved on; the page reloads. Field edits (a fader, a pan, mute) do not: the last writer wins per field.
- `mcp_server._mix_pass`: a working `render()` (no `out`) by a launched Mix agent comes back with `mix_pass` first
  ("Listening pass 2 of 3...", then "hand in now"). Counted per process, which is one run. Nothing is refused.
- `mcp_server._trim_schema`: tools are listed without the "title" labels the SDK writes into every schema (all
  70 tools: 34,500 characters of schema down to 25,600). Arguments are checked against the function signature as
  before.
- `mcp_server._take_in_hand`: `take_new()` from a launched agent whose tools already edit its own open take for the
  same task (or with no task named) returns that take instead of cutting another.

Usage (2026-11-14; what a model reads is not written for people, see section 8):

- `mcp_server._wire`: every tool answers in JSON on one line. The SDK wrote dicts indented, one value per line: two
  fifths of every answer. The name in the module (`mcp_server.render`) still returns the dict; only what is
  registered with MCP is compacted.
- `studio.studio_status(lean=True)` is what a launched agent gets from `studio_status()`: filled seats as
  `{"composer": "agent", "mix": "user"}`, open tasks and takes without their texts, the last five finished tasks as
  one line each. No empty seats, model names, role catalogue, summaries or results (6,200 characters down to 630 on
  the Lonely Walk board). The user's own assistant and outside apps still get the full board.
- A launched agent's `status()` lists patches as `"id [tags]"` under their plugin and has no project summary
  (31,000 characters down to 9,000); its MCP instructions no longer repeat what its brief says about seats.
- **The Producer is started on an audition with the take in front of it.** `floor._launch` puts the take's diff,
  the seat's summary, any conflict and what was asked for in the task message (`briefs._audition`) and points the
  Producer's tools at the take (`TUNETRACK_PROJECT`). `take_accept` / `take_reject` / `take_discard` finish the
  audition task themselves (`_Board.close_auditions`: its result is "tk9 accepted" or "tk9 returned: reason") and
  put the caller's tools back on the session (`takes._leave`). So an audition is render, listen, score, accept:
  no `task_update`, `take_use`, `project_info`, `studio_status`, `take_diff`, report or message around it.
- A take the user accepts or returns before the Producer was started closes the Producer's audition task: no run.
- No bookkeeping calls: the task message no longer ends with "Start with task_update(working)" (the floor marks
  it), and a seat that works on the session ends its turn with the outcome, which the floor stores as the result.
- `takes._to_session`: "back to the session" for a launched agent is the session it was started for
  (`TUNETRACK_SESSION`), not whatever project the editor has open by now.
- `studio.messages_read`: the messages put in a task message are marked read when that run ends well. Before, a
  seat that never called `inbox()` was sent every old message again with each task (Mix had 21 waiting).
- `briefs.RESULT_SHOWN` (240): how much of an earlier task's result a task message quotes. The Producer's task
  message says what each filled seat is for; other seats get the names only.
- Briefs (`briefs.SEAT`, `PRODUCER`): nobody reads along; a hand-in summary or result is three lines, a message
  two; `message_send` only when the receiver must act on it; `report_write` only when the task asks for a report;
  no narration between tool calls; `listen(section_bars=1)` only to find a bar. The tool docstrings say the same.
- `scorecard.crew_review`: the review task carries the returned takes with their reasons and asks for 40 lines
  (keep or swap per seat, what cost most, the instruction that would have avoided it) without re-reading every
  report and take.
- `audition([...])` takes up to 12 instruments in one call (Sound made 66 single calls).
- `render()` reports `file_format` (format, sample rate, channels, bits), and `project_set`'s docstring says what
  `tail` is: the most a one-shot render runs past the end, cut sooner once the sound has died.
- `api.track_update(output=...)` refuses a route that leads back into the track itself.

Loose ends closed (2026-11-14, on `main`):

- `floor._left_behind`: an open take whose task is over and whose seat is not at work. Empty, it is closed (as
  before). With an agent's work in it, the floor hands it in for the seat once (`handed_in_by_floor` on the take),
  saying the task was set done or cancelled, so it reaches the Producer or Needs You. A person's take is left alone.
- `floor._undecided`: a Producer run that ends an audition without `take_accept` or `take_reject` is started once
  more with a message saying so; a second undecided run closes the audition task and puts the take on Needs You.
- Audition tickets on Needs You carry DISCARD as well as ACCEPT and RETURN (`attention_resolve` option `c`).
- `takes.take_cleanup` / `tunetrack takes clean`: open and handed-in takes are never touched; ids are not reused.

On `crew-usage` (2026-11-16, merged in PR #7):

- `api._path`: when this process's tools point at a take, `project=` naming that take's own session (its folder
  name, path or `project.json`) resolves to the take. `"session"` / `"."` still mean the session itself, and any
  other project is not redirected. Seen live: a Mix agent passed `project="shop-loop"` on all 44 calls; every edit
  went to the session, `take_sync` then emptied its take, it closed its own task (allowed: the take held nothing)
  and the floor put the take away. Nobody heard the mix.
- `listen(start=, end=, section=)`: sections, `bar_lufs` (with `bars` saying where it starts) and masking for that
  range only; probe renders still cover the tracks asked for. With `section_bars`, `energy_arc` is left out.
- `mcp_server.LAUNCHED`: a launched agent's MCP instructions are four lines (quality bar, positions, middle C).
  The general workflow they replaced (3,000 characters) named tools most seats do not have, and the notation is in
  the brief of each seat that writes notes.
- `floor._more_auditions`: auditions ready at the same moment (seats that worked in parallel hand in together) are
  heard in one Producer run, up to `AUDITIONS_PER_RUN` (5); the run's start event lists them under `also`. The
  first take's diff comes as before, the others follow in the same message, and the Producer moves between them
  with `take_use`. Each accept or return closes its own audition task; one left undecided goes the way of an
  undecided audition (`_settle_also`), and a stop requeues them all uncounted. The Lonely Walk ran five auditions
  back to back, each reading the brief, tools and session again.
- `briefs._open_tasks`: the task message of a seat with `task_add` (Producer, Review) ends with the open tasks, one
  line each (`- T3 mix queued after T2: <title>`, titles cut at 70), audition tasks left out.

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
  OPEN ON DESK lands on the matching Desk panel. ROLL, SOUNDS, PLAN and REPORT are read-only: editing is the Desk's
  job.
- **The CONSOLE works.** Drag a fader or the pan strip (the drag is relative, so a stray click moves nothing; Shift
  is fine steps), double-click for 0 dB or centre, arrow keys / PageUp / PageDown / Home on a focused fader or pan,
  click the dB readout to type a level, click a send to type its level, M and S, click an effect to bypass it, a
  master fader, UNDO and REDO. An edit goes to what the bench shows: the take (in progress or handed in) or, with SESSION picked, the session
  itself; one undo step per gesture. The strip then shows the dashed outline of where the control stood before the
  take. A tag says when the seat's agent is at work in the same take. Under each strip's slots is a key row: +FX
  (the Desk's device picker; also on the master strip), +SND (a menu of the buses the track does not send to yet),
  OUT (Master or a bus, the current one marked). An × beside an effect or a send removes it. Removing, and every
  send edit, carries the revision the bench read. Effect settings are still the Desk mixer's (OPEN ON DESK).
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

- `uv run python -m pytest -q` → 305 pass (135 existed before this work; since 2026-11-16 the whole suite renders
  through the engine when it is built, about 60 s). `tests/test_crew_host.py` covers the manager
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

- In a browser, 2026-11-13 late night (`output/_preview/console_check.py`: a copy of the short session, a person in
  the Mix seat, a take cut for it): every console control named above, with real pointer drags for the faders and
  pan; two controls moved in quick succession both land; a click without a drag changes nothing; the take's change
  count and the dashed outlines follow; UNDO and REDO; SESSION picked on the bench edits the session and the Desk
  follows; a Desk effect-bypass sent against an old revision is refused and the Desk reloads. No console errors
  apart from the refused call.
- Live, 2026-11-13 late night: **the Claude runner** (`output/_preview/claude_smoke.py`, scratch library, Composer on
  `claude-haiku-5-5`, low effort, one run): 15 s, 8 tool calls, none failed, $0.011, a two-bar melody written in a
  take and handed in, the audition ticket raised. Session id, text, tool calls, tool results and usage all parsed.
  Claude Code loads the studio's tools on demand (one `ToolSearch` call in the run). One trip: it called
  `take_new()` although its take was cut, worked in the new one and left the first open and empty; fixed
  (`_take_in_hand`).

- Live, 2026-11-13 night: the pass-check session (`output/_preview/pass-check/summary.json`): Mix made 2 renders in
  its run, inside its limit of three. On The Lonely Walk, Mix averaged 2.2 renders a run over five runs.
- Live, 2026-11-14 (the owner, on their own library): **The Lonely Walk**, a 48-bar orchestral cue on Codex models
  (Producer `gpt-6.1-sol` medium; Composer, Arranger, Mix, Review `gpt-6.1-terra` medium; Sound `gpt-6-luna` high):
  49 runs, 1,006 tool calls, 65 minutes of agent work, 15 of 15 takes accepted (13 first time), cue delivered. What
  it cost and what was done about it is in the usage table below. The Producer's review (R22) recommends the same
  crew again with Terra medium tried in Sound.
- In a browser, 2026-11-14 (`console_check.py`): on the console, +SND adds a send and the key goes dark when there
  is no bus left, × removes it, OUT routes to a bus and back, a bus is not offered itself, × removes an effect and
  UNDO restores it, +FX on the master opens the picker and adds the effect, the take's change count follows. No
  console errors.
- Live, 2026-11-14 (`cuts-check`, `kilo/kilo-auto/free`, the usage cuts, old craft slices; cut off when the app
  restarted, 5 runs): answers 650 characters per call (1,262 before), an audition 3 steps and 44k tokens (10 and
  202k), `studio_status` 630 characters. Steps per run not comparable: auto routing sent each run to another model.
  It showed the Mix agent editing the session past its take (fixed on `crew-usage`, section 4).
- Live, 2026-11-16 (`craft-check`, pinned to `kilo/nvidia/nemotron-3.5-super-vl:free`, the model of most of the
  13 Nov baseline; craft text, lean MCP instructions, the path fix): 10 runs, 1,047 s, $0, 2.65M input tokens, as
  the baseline. Like for like: the Composer's writing run 12 steps and 320k tokens (20 and 960k), an audition 3-5
  steps and 60-98k (10 and 202k), Review one run of 4 steps and 57k (two runs, 421k), planning 175k (277k in two
  runs). Mix 17 steps and 512k (14 and 436k), with 4 renders against its 3. No polling, no asks to the user, one
  report. The savings went into duplicate tasks (about 1.4M tokens): see the table below. In a browser the same
  day: an audition ticket's DISCARD closes the take without merging and the other ticket's ACCEPT still merges.

- Live, 2026-11-16 (`dup-check`, same model and brief, with the open-task list and batched auditions): 10 runs,
  1,264 s, 2.80M input tokens. No duplicate tasks: the Producer returned the first mix to Mix with a reason instead
  of adding a second Mix task, the one task it added (shorten the bass tie at the seam) was new work, and Review's
  two fix tasks covered problems no open task did. The cue went Composer, Mix (returned once), seam fix, Review
  within the 10 runs, every take accepted or returned and none left open. Per run the model varies a lot on the
  same task (the Composer took 12 steps in one session and 23 in the next).

Not verified:

- Stage 3.3 through a real audio device: lanes on the ASIO callback thread (dropouts, CPU) and a converted
  device have only been checked offline and through the `pull` op. Nobody has heard LIVE since lanes.
- Lanes with the owner's heavy plugins (HALion, BBC SO) rendering on worker threads: the suite's HALion probe test
  passes, the big fuzeball session was not re-measured with lanes (its files were missing that day, section 8).
- Batched auditions on a live session: no two takes were handed in at the same moment in `dup-check`.
- **The loop-seam figure drives extra work.** In both 2026-11-16 sessions an 8-bar loop's `edge_level_diff_db`
  (7-8 dB, bar 8 into bar 1) set off a seam fix by the Composer, a returned mix and a Review task. Nobody has
  listened to whether that jump is audible on a loop whose downbeat is simply louder than its last bar. Worth a
  person's ears before agents spend more runs on it.
- The fixes for takes that miss the session, the question cap and the put-away of empty takes have tests (some
  driven by stand-in agents) but have not been seen in a live session.
- A person stepping into a take while its agent is at work (allowed; edits are serialised by the project lock,
  and nothing warns that someone else is in it).
- The new floor behaviour (one out-of-usage item per model, a run stopped when its task gains a dependency) and
  have tests but have not been seen in a live session (the short session on free models never ran dry).
- On free Kilo models the Producer's and Review's runs hit the 480 s limit four times in the pass-check session
  (10 runs for a 7-run job). The model was slow that night; not looked into further.
- One run of the whole suite failed once in `test_a_crashing_agent_is_retried_then_becomes_a_blocker` while that
  live session and a preview server were running on the same machine; alone and on the next full run it passed.

- **Claude Code in a whole session.** One Composer run is verified (above); a Producer or Mix run on Claude, a
  resumed run and a run that hits a usage limit are not. Its tool-call event fixtures in the tests are still
  hand-written.
- The console with an agent at work in the same take (the tag is drawn from the crew state; no agent was running
  in the browser check), and on a touch screen.
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

What the short sessions of 2026-11-14 and 16 tripped over, and what was done (`crew-usage`):

| Trip | Outcome |
|---|---|
| A Mix agent passed `project="shop-loop"` (its session's name) on all 44 calls: every edit went to the session, its take ended empty and was put away, nobody auditioned the mix | Fixed in `api._path`: from a take, the take's own session means the take |
| Auditioning the Composer's take, the Producer added a second Mix task beside the planned one (it cannot see the board in an audition); Review then filed a seam fix and a masking fix the Producer had already handed out. 12 tasks for a job planned as 4, two Mix runs on the same bars, about half the session's tokens | The task message of a seat that can add tasks (Producer, Review) lists the open tasks; the Producer's playbook says to `task_update(detail=...)` the waiting task, Review names the task that covers a finding instead of adding one |
| Mix rendered 4 and 5 times against a limit of 3 (told, not refused) | Left; not-done item 7 below |
| The Composer's seam fix ran 481 s with 6 renders and was stopped by the time limit | Not looked into; it was one of the duplicate tasks |

What The Lonely Walk cost (2026-11-14), measured from its run logs, and what was done. The session read
**21.8 M input tokens** (20.0 M of them cached) and wrote 109 k, over 1,054 agent steps. Every step re-reads the
whole run so far, so a step saved is about 20 k tokens and a character saved in an early answer is paid back on
every later step.

| Where it went | Outcome |
|---|---|
| The Producer was woken 22 times (6.2 M tokens), 19 of them to audition one take. It was not polling between runs (the floor wakes it on a hand-in), but each wake-up opened with `task_update`, `take_use`, `project_info`, `studio_status`, `take_diff` and closed with a report, two messages, `take_use`, `task_update`: about ten steps around four of work | The audition arrives with the take in front of it and closes itself (above). Expect 5 to 6 steps instead of 15 |
| `studio_status` 27 calls at 9,300 characters: every seat including empty ones, model names, the role catalogue, full summaries and results | The short board, 630 characters |
| Answers indented one value per line: `listen` 62 calls at 10,700 characters (31,000 with a block per bar), `status` 10 at 32,000, `project_info` 57 at 5,400, `score` 65 at 4,000 | One line each (about 40% off); `status` cut to 9,000 for agents; the playbook says when a block per bar is worth it |
| 25 `report_write` calls (22 reports, the Producer filed one per audition) and 41 `report_read` | Reports only when a task asks; the audition's verdict is the accept or the reason on the return |
| 57 messages (22,000 characters), most of them "tk9 passes" to seats that needed to do nothing; unread ones were sent again with every task | Messages only when the receiver must act; read once |
| One `task_update(working)` to open and one `task_update(done)` to close every run: about 90 steps | Gone |
| The session review: 48 calls, 494 k tokens, 15,000 characters | 40 lines, from the counts and the returns |
| Sound: 7.1 M tokens in 10 runs, 66 `audition`, 37 `sfz_list`, 32 `sfz_info`, `status` 8 times | `audition` takes a list; the playbook has Sound told what the parts will demand. Its two empty investigation runs were a tool gap (below) |
| The Producer spent 53 calls and two questions to the user on delivery: `tail=6` did not add six seconds, and nothing said what sample rate the file has | `file_format` in the render report; `tail` explained |

Not done, in order of what they would save:

1. Done 2026-11-14 (partly): **seen live** on free Kilo models (section 5, the cuts-check session). Answers are
   half the size and an audition is 3 steps. Steps per run could not be compared: see the note there.
2. Done 2026-11-14: per-seat craft text (`crew/craft.py`).
3. The second half of the schema trim (optional parameters as `anyOf [type, null]`): needs one live run per runner.
4. Done 2026-11-16 (`crew-usage`): `listen()` for a bar range, and no `energy_arc` when sections are bars.
5. Done 2026-11-16 (`crew-usage`): one Producer run for several takes handed in together (up to five).
6. A cheaper model in the Producer seat for auditions only, the top one for planning and the final listen.
7. A Mix agent that renders past its limit is told to stop and is not refused (seen live: five renders against
   three). Refusing the fourth render is the next step if it keeps happening on paid models.

What the session's own review (R22) says the studio lacks, not built:

- A sampled instrument's release or envelope cannot be changed: `sfz_info` shows no such control, and a HALion
  "Amp Decay" set through `track_update(instrument_params=...)` read back unchanged. Two Sound runs ended empty.
- EQ cannot be automated for a section; Mix used volume automation instead. Built 2026-11-16 (stage 3.4):
  `automation(..., device=)` automates any effect or instrument setting, the EQ's included.
- An articulation per phrase (spiccato for bars 13-24 only) needs the bars moved to their own track by hand.

Known gaps and risks:

- Each answer to a Needs You item still restarts the seat that asked (one agent run). The cap and the "settled"
  line bound it; answers to several items from one seat are not batched into one run.
- A take the floor handed in for a seat and that was then returned, with its task still closed, stays open and
  `loose`: it is not handed in a second time, and nobody is asked again.
- The chord lane edits the chord track only. Notes already written do not follow a changed chord; `score()`
  shows what no longer fits. Chords typed as roman numerals are kept as typed.
- On the console, effect settings are still the Desk mixer's. The other benches are views.
- A person in the Mix seat who moves a fader with SESSION picked edits the session directly, as on the Desk; the
  bench does not steer them into a take.

- **Context cost per agent step was about 25k tokens** before the 2026-11-14 cuts (28k on Claude Code, measured in
  the smoke); not re-measured since. Mostly cached.
  Measured 2026-11-13 (characters / 4): what TuneTrack itself sends is 5k (Spotting) to 12k (Composer) of that:
  for the Composer 2.9k of tool descriptions, 4.5k of tool schemas after the label trim (6.1k before) and 3.5k of
  brief, 2.9k of which is the `skill/SKILL.md` slice (now 0.8k of craft text). The rest is the agent CLI's own
  prompt. Still to take: the
  SDK writes every optional parameter as `anyOf [type, null]` with `default: null`; collapsing that would halve the
  schemas again (to 16,200 characters for all tools) but a client that checks arguments against the listed schema
  would then refuse an explicit null, so it needs one live run per runner (Claude, Codex, Kilo) first.
- Takes are deleted only by `tunetrack takes clean --yes` (or `takes.take_cleanup`); nothing runs it on its own.
- Tool lists per seat are coarse (`track_update` covers both fader and instrument). The real guard is the
  out-of-department flag at `take_diff`.
- Kilo has no strict mode: MCP servers in the owner's own `kilo.jsonc` would also load (it has none today).
- A take a person hands in is routed like an agent's (audition task for a Producer agent, otherwise a Needs You
  ticket), with or without a task, but only while a floor manager is running, because routing lives in
  `floor._route_takes`. With the crew stopped it waits on the seat's TAKES tab, where ACCEPT works.
- The Desk sends the revision it read only with edits that name a thing by its place (`BY_PLACE`). Whole-list
  note saves are guarded by `expect_notes`; field edits are last-writer-wins by design; clip edits name a clip id.
- The hosted crew stops when the editor's server stops. Nothing restarts it; RUN does, and recovery requeues
  what was cut off.
- Talkback WAKE costs one agent run per message to an idle seat. It is a visible toggle, on by default.

---

## 6. Build order

Stages 1, 2, 2b (except the Claude runner) and 2c (first cut) are done. Each stage below says what done looks like.

### Stage 2b: prove the crew with real models

- Done 2026-11-13: one real small session end to end on free Kilo models, and the trips it showed fixed (section 5).
- Done 2026-11-13 late night: the live smoke for the `claude` runner (section 5). Its event parsing needed no fix.
- Done 2026-11-13 (`desk-takes`): the two open rows of the trips table (no pictures for launched agents, a
  stopping rule in the Mix brief).
- Done 2026-11-13: the second live session, and a long one run by the owner (section 5).
- Done 2026-11-13 night: a short session on the new briefs (section 5). Agents no longer ask the user to listen
  or poll while waiting, and reviews arrive as reports. Left: seeing the take and question fixes in a live session.
- Done 2026-11-14: the owner's first whole session on paid models (The Lonely Walk), measured, and the usage cuts
  that followed (section 5). Mix's pass count held (2 renders a run).
- Done 2026-11-14/16: two short live sessions on free models to see the cuts (section 5). They work where they do
  not depend on the model (answer size, the audition). Left: the "not done" list under the usage table.

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
     Done 2026-11-13 late night: the Mix console's controls work (faders, pan, M/S, effect bypass, typed
     levels, send levels, master fader, undo). Done 2026-11-14: adding and removing effects and sends, and
     routing. Left: effect settings on the console; editing on the other benches.
     The Composer's chord-track editor is on the Desk (the chord lane, done 2026-11-13 night).
  5. Done 2026-11-13 (`desk-takes`): Needs You answers per situation, full-length talkback, reports, scorecard and
     crew history. Left: the owner's view of how these read in use; a review that also weighs cost per accepted
     take once paid runs report usage; picking a crew from history automatically.
  6. Later niceties: workflow/audio cable view, movable modules (stage 6), push-to-talk.

### Stage 3: live engine (first cut built 2026-11-14, on `main` since PR #5)

Decided in the spike: **bare JUCE 9, no Tracktion Engine.** `project.json` stays the one model; the engine is a
player that loads a flat spec. Tracktion would have been a second model to keep in step, harder to null against
DawDreamer, and the built-in Faust devices need custom nodes either way. Revisit for stage 4 (recording, comping).

Built (`engine/`, `src/tunetrack/live.py`, `tests/test_live.py`, `engine/README.md` has the detail):

- `tunetrack-engine.exe`: one process hosting the session in a JUCE graph. VST3 instruments and effects (states,
  shell plugins, settings by number or shown text), the built-in synths and effects (Faust turned into C++ by
  `engine/tools/gen_faust.py`, settings live), faders, balance pan, volume/pan automation, sends, buses, sidechain
  keys, master chain, plugin latency lined up by JUCE. Audio clips play as dry stems the offline worker renders
  and caches; their inserts run live. SFZ instruments did too until stage 3.3 put sfizz in the engine (item 3).
- Real time through ASIO (the SSL 2) or Windows Audio, transport with seek and loop, meters per strip, plugin
  windows (`editor`), saving a plugin's state (`save_state`), offline render from the same graph.
- Hot swap: `Player.sync()` reloads when `project.json` has a new revision; devices that did not change keep
  playing (a note edit, a fader move or an insert setting never reloads a plugin).
- `tunetrack live <project>` plays; `--null-test` compares both engines; `--devices` lists outputs.

Measured 2026-11-14 on `fuzeball-galactic-anthem-v2` (30 tracks, 19 HALion/Groove Agent instances, SSL, Waves,
Valhalla): live at 28-43% CPU of one core, no dropouts, 512-sample ASIO buffer; offline 22 s for 71 s of music.
Against the offline renderer: same RMS, octave bands within 0.14 dB, loudness curve 0.99 alike. Deterministic
material cancels to -50..-200 dB per track (HALion, SFZ, audio, all ten built-in effects, the whole mixing path).
Round-robin samplers and built-in synth parts where a voice is freed at a different sample never cancel.

Found on the way, worth knowing:

- HALion Sonic's licence client crashes the process when a throwaway instance was made first, which is what
  listing a plugin file's contents does. The engine lists in a second short-lived process and caches the result
  (`plugin-cache/` beside the exe). `TT_TRACE=1` prints load steps, wiring and symbolised crash stacks.
- Valhalla Supermassive arrives with its buses off under JUCE 9; the engine switches main buses on.
- MIDI timing offline is the MIDI file's: ticks at 960, tempo in whole microseconds, cut to a sample.
  `live._midi_clock` reproduces it; one sample out changes which synth voice plays a note.
- The offline Faust host computes built-in synths one sample per call, which frees a releasing voice at the
  first near-silent sample: long releases are cut short (about 0.1 s instead of the set time). The engine matches
  this with `--per-sample`; without it releases ring out. Decided 2026-11-16: ring out (`synth_per_sample` off,
  stage 3 item 2).

Left, in order:

1. Done 2026-11-14: **UI transport on the engine.** A LIVE key beside the transport (remembered per browser).
   On: play, stop, seek and loop drive one engine hosted by the editor's server (`livehost.py`, `/api/live`
   streams position, meters, CPU and dropouts); the playhead follows the engine, the mixer strips and the master
   show live peaks, and VST3 devices get a Window button in the inspector. The engine follows what the Desk shows
   (the session or the open take) and reloads on every new revision. Off: the browser preview as before, and the
   engine shuts down so the audio interface is free (Cubase and the engine cannot share the ASIO driver).
   Checked in the browser on a scratch synth session: start, seek, loop wrap, an edit while playing, meters, off.
   Not checked by a person yet; opening a take while live was checked on the server side only. Live plays the raw
   mix (no loudness normalisation or limiter, the tag's tooltip says so). Left: live peaks on the console bench
   (`studio.js`), Window buttons for master inserts, an output device picker.
2. Done 2026-11-16 (`offline-engine`, PR #8): **offline rendering goes through the engine.** What agents
   measure (`render`, `listen`, `audition`, exports, stems, layers) is now rendered by the engine the owner hears.
   The Python mixer stays (faders, sends, buses, loudness, stems in numpy, as before); only the jobs moved:
   - Per-track audio before the fader comes out of the engine as **taps** (`render` op `taps`, written by the
     fader node, lined up for latency). Every uncached track renders in one batch: tracks keyed by each other's
     sidechains share an engine, the rest spread over `render_workers` engines side by side. Bus and master chains
     run in one engine kept for the render. SFZ stays with the worker; with inserts, its dry render is a stem.
   - **The ready probe**: before a render, each new plugin instrument is played one of its own notes, measured
     after the instrument, until it sounds and stops getting louder (rounds 250 ms apart, 15 s cap; `unready` names
     the rest). HALion with no settle wait at all: silent without the probe, every note right with it (751 ms).
   - Cache keys carry the engine build and synth mode. A track that comes out silent or never settled is used once
     and not cached; a crashed engine fails only its group and leaves nothing in the cache.
   - `engine.offline_backend()` / library config `offline_engine` (`auto`, `engine`, `dawdreamer`);
     `live.engine_runs()` falls back to DawDreamer when Windows will not start the build. The null tests still
     render their reference with DawDreamer.
   - `synth_per_sample` (library config) picks the built-in synth mode for live and offline alike. **Off by
     default: the owner chose ringing releases (2026-11-16).** Per-sample mode (DawDreamer's way) frees a
     releasing voice at its first near-silent sample, so a single string or oscillator stops dead at note-off:
     `synth:pluck` loses its 0.35 s release; `pad`, `bell` and `epiano` sound the same either way. Existing projects
     with plucked built-in parts now ring a little longer, and the built-in kit's crash rings past a short note
     instead of being choked (so `listen()` no longer warns about it). `true` brings the old sound back.
   Measured on a copy of `fuzeball-galactic-anthem-v2` (30 tracks, 19 HALion / Groove Agent): fresh 48.9 s
   (DawDreamer 59.7 s), one note edited 9.7 s (21.4 s), cached 1.8 s; no warnings, no instrument unready. Master
   level equal, octave bands within 0.11 dB, loudness curve 0.992 alike. Per track: deterministic parts cancel to
   -45..-145 dB; Groove Agent kits, HALion Taiko and the built-in `drums` and `saw_pluck` synths do not (equal level
   and bands; round robin, noise, voice allocation). The built-in `drums` synth on "Hybrid" has a loudness curve
   only 0.82 alike: the same through the old master path, so it predates this stage, but agents now measure the
   engine's version of it.
   Left: the probe could replace most of the fixed settle wait (2 s + 0.4 s per plugin with a state) for
   instruments, but parameter overrides still wait for it; one engine computes on one core (item 3).
3. Done 2026-11-16 (`engine-lanes`, PR #9), stage 3.3:
   - **Several cores** (`engine/src/Lanes.h`). Every source strip whose inserts listen to no other strip is a lane:
     a graph of its own, computed before each block by `LanePool` on worker threads (one per physical core but one,
     at most 8; `--threads`). The session's graph sees a lane as one device with the lane's latency, so alignment
     is JUCE's as before. Keyed strips, buses and the master stay in the session's graph. Renders are bit for bit
     the same on 1 and 4 threads. One engine, 16 built-in synth strips with three effects each, 39 s of music:
     5.3 s on one thread, 3.5 s on two, 1.7 s on four, 1.05 s on eight. Offline, `engine.engine_threads` shares the
     cores out between the `render_workers` engines (16 logical cores / 4 engines = 4 each); the bus and master
     engine gets one. Live playback uses the default.
   - **Sample-rate conversion.** The session runs at the project's rate. On load the device is asked for it (an
     ASIO interface switches); one that cannot keeps its own rate and is fed through `Resampler.h` (Kaiser-windowed
     sinc, cut-off under the lower Nyquist): level exact, flat to 18 kHz at 48 to 44.1 kHz, no fold-back (a 30 kHz
     tone from a 96 kHz project under -60 dB at 48 kHz), 1-2% of a core. JUCE's own windowed sinc was tried first:
     -0.09 dB at every frequency and no low-pass going down. The LIVE tag's tooltip says when playback is
     converted. The old "device runs at X Hz" problem is gone.
   - **Mono sidechain key**: a one-channel key input (TDR Nova's) hears (L + R) / 2, as the offline worker feeds it,
     not the left alone. The test fails with the old wiring.
   - **SFZ in the engine (sfizz 1.2.3)** instead of stems, live and offline (`engine/src/SfzNode.cpp`,
     `engine/cmake/Sfizz.cmake`). The owner OK'd the download; the source release is unpacked in
     `engine/third_party/sfizz/` (not in git; the plugin installer the owner ran there put only its uninstaller in
     it, the plugins went to Common Files). Played at TuneTrack's player's level: sfizz's -7.35 dB master volume is
     set to 0, and its own CC7 volume link starts at 127 where the file reads no CC7 (the General MIDI 100 was
     -4.15 dB on everything). Renders are bit for bit repeatable: two patches to sfizz on configure (a random
     generator per SFZ node; the offline wait covers loads still queued), and the node is told it runs offline
     (a node added to a JUCE graph is not; without it sfizz streamed in real time and a held note dropped out 0.17 s
     in, differently each render; there is a test). `sfz_player: "tunetrack"` keeps the old player. Measured
     against it: The Lonely Walk's six VSCO/piano tracks within 0.1 dB, master equal, render 6.3 s (8.1 s); VSCO
     violins and cello pizz, jRhodes and Salamander within 0.5 dB (Salamander's loudness curve 0.85 alike: sfizz
     plays its ARIA velocity/CC opcodes); Virtuosity drums +3.3 dB; Meatbass pizz, near silent in the old player
     (-71 dB: its envelope is CC-driven), now plays. Not checked: live playback through the audio interface
     (real-time streaming; the `pull` op runs offline). DawDreamer is still used for plugin scanning,
     `patch_capture`, preset import and `plugin_params`.
4. Done 2026-11-16 (`engine-lanes`, PR #9), stage 3.4: the README limits.
   - **Device automation.** `Device.automation` (`{setting: [[beat, value]]}`) on any effect or instrument, set with
     `automation(track, param, points, device=<effect index> | "instrument")` (`track="master"` for master effects).
     Built-in settings are checked on the edit (an unknown one gets the list); a VST3 parameter goes by name, index
     or shown text and is checked on load (a warning). An automated setting ignores its static value. In the engine
     (`engine/src/Automation.h`): a VST3 parameter is set at each block's start (512 samples offline), built-ins move
     every 32 samples of song time inside the node, so their renders do not depend on the block size. The value is
     read at the song time of the audio arriving at the device: a step after plugins with 0 to 184 samples of latency
     lands on the same sample. A cleared lane goes back to the setting, or to the value before automation (checked on
     a running engine). builtin:eq on SSL / Nova: the lanes are the translated plugin settings at each point, and a
     band at 0 dB or a filter at its off point stays in a hair inside its range, so a sweep from flat works. Lanes
     are in the render cache key, arrange insert/delete moves them, `project_info` and effect labels show
     `~setting`, the console's slot shows `~`. The DawDreamer fallback holds the setting and says so; SFZ
     instruments have none (cc). Measured: a gain ramp -40 to 0 dB within 0.3 dB at each point; Nova's mid band from
     flat to +12 dB on a 1 kHz tone; Nova's Output Gain by its shown text (`tests/test_automation.py`).
   - **Tempo-synced effects.** The engine's play head carried the tempo map since stage 3; now checked: Waves
     H-Delay synced to the host at 1/4 follows a change from 120 to 60 bpm (it glides to a new time, so its first
     echo after a change or a load is ~25 ms early). SSL X-Delay ignored the host tempo even at a steady 60 bpm, in
     DawDreamer too (the plugin, or a setting not found). The built-in delay's time is in beats: `live._delay_lane`
     steps `delay_sec` at each tempo change and follows `time` automation (before, it kept the first tempo's time).
     The DawDreamer worker gets the tempo map too (`set_bpm` per tick) for the plugins it hosts; Waves plugins run
     through pedalboard there and see no host tempo.
   - **Time-stretch** (built by an Opus subagent, reviewed): `Clip.bpm` (the tempo the audio was played at: the clip
     follows the tempo map, pitch kept) and `Clip.pitch` (semitones); `audio_import(bpm=, pitch=)`,
     `clip_edit(op="stretch")`; split moves the right half's offset in source seconds. Rubber Band through
     pedalboard's `time_stretch`, one constant-tempo segment at a time with 10 ms crossfades (its per-sample factor
     array lagged tempo steps by ~15 ms). Rubber Band's own rate is off by 0.05-0.3% (22-31 ms by the end of 30 s):
     it is measured once per ratio per process on a click train and resampled away, with a pitch nudge of up to
     0.03 semitones; beats land within 0.8-2 ms, pitch within 1-2 cents. A 38 s clip takes ~2.2 s, plus 3-5 s the
     first time a process meets a ratio; each stretched clip is cached (`cache/stretch-<key>.wav`). Formants are not
     kept (keeping them dropped a pure tone 9 dB). A new `bpm` keeps the clip's source audio, so its length in beats
     changes. `tests/test_stretch.py`.
   - Found on the way: the engine's audio stem key (`live-in-*`) left out the tempo map, so a tempo change before a
     clip could replay a stale stem. Fixed (audio stems are assembled again once).
   Not done: no UI to draw or edit automation (agents and the API only); VST3 automation is per block, not sample
   accurate; nobody has heard any of it.

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
| Claude as a crew agent | Done: the CLI is logged in (`crew detect` says `auth: ok`, and one run worked, 2026-11-13). |
| Stage 3 | Done 2026-11-14: Build Tools, CMake and JUCE 9 are installed. Done 2026-11-16: Smart App Control switched off (it blocked a fresh engine build, and with it LIVE). Left: choose the repo's licence (below). |
| Orchestral palette | `BBC Symphony Orchestra (64 Bit).vst3` is in the VST3 folder as of 2026-11-16 (not scanned or tried yet); Spitfire LABS is not. Then `tunetrack scan` and capture patches. |
| SFZ in the engine | Done 2026-11-16: sfizz's source is in `engine/third_party/sfizz/`. Left: hear an SFZ track on LIVE (real-time streaming is not checked). |
| Stage 5 | Install `ffmpeg` on PATH. |
| New tools in their own apps | Restart Claude so the MCP server reloads (it runs from this checkout). |

---

## 8. Working rules for this repo

- **Commands from an agent's shell**: `uv run tunetrack ...`, `.venv/Scripts/tunetrack.exe` and now `uv run pytest`
  are blocked there by Windows Application Control. Use `uv run python -m tunetrack.cli <command>` and
  `uv run python -m pytest -q`.
- **Untracking files deletes them on the next merge.** Stage 3.2's commit (58c95f5) took `projects/` and
  `library/crew_history.json` out of git with the files left on disk; merging it into `main`, where they were still
  tracked, made git delete them from the working folder (found 2026-11-16: 14 sessions' `project.json`, 8 audio
  clips, the crew history). Restored the same day with the owner's OK: from `58c95f5^`, and The Lonely Walk from
  its newest `history/` snapshot (rev 39; git had rev 31; its last save after that is lost). All 15 sessions load
  with their audio. Before a commit that untracks files, copy them somewhere outside the repo, and check they are
  still on disk after the merge.
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
- **What an agent reads is not written for people.** Tool answers, the board an agent sees, task messages and
  hand-in summaries are read by models and paid for on every later step: keep them to what the reader acts on. The
  owner does not want to be in the loop of everything; what people read is the UI (Needs You, the benches, REPORTS).
  Before adding a field to a tool answer or a line to a brief, ask which agent decision it changes.
- Subagents: brief them with the exact files they may touch, no commits/installs/config changes, a cap on live
  calls; then read their diff and re-run the tests yourself.
- Writing style in code and docs: plain, specific, no filler. Tool docstrings are prompts for other agents:
  say what done looks like.

## 9. Picking up: first ten minutes

```bash
git status --short
```
```bash
uv run python -m pytest -q
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
that miss the session, a run per Needs You answer), and late that night the Mix console's working controls, the
pass count told to Mix with each render, the Claude runner smoke, and the Desk's revision guard. On 2026-11-14 the
owner ran a whole orchestral session on paid Codex models (The Lonely Walk) and asked for less text and fewer
wasted runs: the usage cuts and the rest of the console are built (section 5), and so are per-seat craft text, the
take cleanup command, loose takes handed in by the floor and undecided auditions (all on `main` since PR #5). On
2026-11-16 two live sessions showed agents editing the session from a take by naming it, and duplicate tasks;
both fixed on `crew-usage` (PR #7) and the second seen gone live. Later that day stage 3.2 was built on
`offline-engine`: offline renders go through the engine (section 6, stage 3, item 2). The owner chose ringing synth
releases (`synth_per_sample` off). PR #8 merged it. Later still, stage 3.3 on `engine-lanes`: strips on several
cores, sample-rate conversion, the mono sidechain key, and SFZ through sfizz in the engine (section 6, stage 3,
item 3); then stage 3.4 on the same branch: automation of any device setting, tempo-synced effects, time-stretch
(item 4; 326 tests pass). PR #9 merged both. The same day the session files were found deleted from disk by the merge
(section 8) and restored. Next: the owner's ears on LIVE with lanes and an SFZ track, their ears on the loop-seam
figure (section 5, "Not verified"), a short live crew session on the engine backend, the schema trim (needs one
live run per runner), a whole session with Claude in a seat.

To measure a session's usage: each run's log is `<session>/studio/logs/<task>-<attempt>-<seat>.jsonl` (the agent
CLI's own event stream). For Codex, `turn.completed` carries `usage`, and each `item.completed` of type
`mcp_tool_call` carries the tool, its arguments and its answer. Count calls and answer characters per tool and
per seat, and input tokens per run: `uv run python output/_preview/usage.py <session folder>` (untracked, like
the other throwaways there; forty lines to rewrite if it is gone). For Kilo, `step_finish` carries `tokens` and the
model it was routed to, and `tool_use` the tool, input and output: `output/_preview/usage_kilo.py`.
**Pin a model when comparing sessions.** `kilo/kilo-auto/free` routes each run to a different free model, and step
counts per run follow the model far more than the brief (an 83-step Composer run on one, 20 on another).

To try it on a scratch session, add a temporary entry to `.claude/launch.json` (it is tracked: remove the entry
before any commit) that runs one of the throwaways in `output/_preview/` with `uv run python`; `output/` is not
tracked. `serve.py` serves `output/_preview/live` (the owner's long test session) on port 7399,
`brief_check.py [model] [folder]` reruns the short live session (free models, about 25 minutes),
`serve_check.py` serves a copy of its result with nobody to launch, `console_check.py` does the same with a person
in the Mix seat and a take cut (port 7397), and `claude_smoke.py` is the one-run Claude check.
