# TuneTrack Studio: plan and handover

Read this first if you are picking up the studio work in a new session. It is self-contained: what the owner
wants, what was decided and why, what exists, what is verified, what to build next, and what only the owner can
unblock. Keep it current: when a stage lands or a decision changes, edit this file in the same change.

Last updated: 2026-11-13. Branch: `studio-core` (off `main` at 6885d55). **Nothing on this branch is committed
yet**; the owner has not asked for a commit.

---

## 1. What the owner wants

TuneTrack today is an AI-driven music workstation for game soundtracks: a Python library, 66 MCP tools, a CLI, a
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

All of this is on `studio-core`, uncommitted.

### Code map

| File | What it is |
|---|---|
| `src/tunetrack/roles.py` | The nine seats; tool groups; `tools_for(seat)`. |
| `src/tunetrack/studio.py` | The board, in `<session>/studio/state.json` + `events.jsonl`: seats and occupants, tasks with dependencies, messages (talkback), attention items (Needs You), activity log. `me()` / `seat_join()` say which seat a process speaks for (`TUNETRACK_SEAT`, else the user). |
| `src/tunetrack/takes.py` | Takes in `<session>/takes/<id>/` (`project.json`, `base.json`, `take.json`); `merge()` (three-way), `changes()` (readable diff), `take_new / use / diff / sync / submit / accept / reject / discard`. |
| `src/tunetrack/crew/detect.py` | Finds Claude Code, Codex and Kilo CLIs and their models; cached in `library/agents.json`. |
| `src/tunetrack/crew/runners.py` | Launches an agent CLI headless (`Launch` → `Run`), normalises its event stream. Runners: `claude`, `codex`, `kilo`, and `script` (a Python file standing in for a model, for tests). |
| `src/tunetrack/crew/briefs.py` | What a launched agent is told: seat instructions, the Producer playbook, the sections of `skill/SKILL.md` that seat needs, and the task message. |
| `src/tunetrack/crew/floor.py` | The floor manager: `Floor(session).run()` starts agents on ready tasks, logs their activity, routes handed-in takes, handles failure. `kickoff(brief)` gives the Producer its first task. Holds the one-manager lock (`FloorBusy`, `managed()`), skips seats on hold, `stop()` / `stop_seat()` requeue without counting the attempt, and `_recover()` requeues tasks a dead manager left "working". |
| `src/tunetrack/crew/host.py` | The crew hosted in a long-lived process (the editor's server): `crew_start(brief)`, `crew_stop()`, `crew_state()`, `seat_hold(seat)`. Runs `Floor.run(keep_alive=True)` in a thread per session. |
| `src/tunetrack/mcp_server.py` | 16 new tools (66 total). `apply_seat()` strips a seated agent's server down to its seat's tools; seated agents also get seat instructions. |
| `src/tunetrack/server.py` | `/api/events` (server-sent events: project version + board activity), board and crew functions callable from the editor (`STUDIO`), `board` and `crew` in `/api/state`, `studio_view()` for cheap redraws. Stops hosted crews on exit. |
| `src/tunetrack/web/` | The studio UI. `index.html` (one header, two rooms), `style.css` (the rack look; all drawn in CSS, no fonts or images fetched), `studio.js` (Studio room: rack, seat workstations, Needs You, monitor/talkback, A/B audition), `app.js` (Desk room: the earlier arrangement / piano roll / mixer editor, recoloured). |
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
6. Failures: an agent that never used a tool (not logged in, wrong model) becomes a blocker immediately; a crash
   is retried with the reason, then becomes a blocker after `max_attempts`; a run past `run_timeout` is stopped;
   after `max_launches` runs the crew stops and asks.

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
- **Needs You**: one ticket per item with its options; audition tickets carry A/B keys, ACCEPT and RETURN.
- **Monitor · talkback**: the activity and message feed, a target key per seat plus ALL, and WAKE: when the
  target's agent is idle, the message is also handed to it as a task so it answers now (one agent run).
- **Desk room**: the arrangement, piano roll, mixer, track inspector, render report and brief, on the session.

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

- `uv run pytest -q` → 219 pass (135 existed before this work). `tests/test_crew_host.py` covers the manager
  lock, hold, stop, recovery, the host, `board()`, `take_audition()` and the server's new calls.
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

Not verified:

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
| Agents cannot see the PNGs `analyze()` returns (piano roll, spectrogram) | Open: return them as MCP image content for seated agents, or drop the paths from their view. |
| Mix spent about 10 minutes and 14 renders chasing band percentages | Open: the Mix brief needs a stopping rule ("two passes, then hand in with what is left"). |

Known gaps and risks:

- **Context cost per agent step is about 25k tokens** (a composer has 45 tools plus its brief). Mostly cached,
  but trimming tool descriptions per seat is worth doing before long sessions on paid models.
- Takes are never deleted; no cleanup command yet.
- Tool lists per seat are coarse (`track_update` covers both fader and instrument). The real guard is the
  out-of-department flag at `take_diff`.
- Kilo has no strict mode: MCP servers in the owner's own `kilo.jsonc` would also load (it has none today).
- A take cannot be opened on the Desk: it can be auditioned, diffed, accepted and returned, but the arrangement
  and piano roll always show the session. So when the owner sits in a seat, their edits on the Desk go to the
  session directly (MARK DONE closes the task); cutting and handing in a take by hand needs the MCP tools.
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
- Left: the two open rows of the trips table (images for agents, a stopping rule in the Mix brief), then a
  second live session to confirm the run count drops (7 runs where 5 were needed).

### Stage 2c: the rack UI

- Done 2026-11-13 (first cut, see "The studio UI, as built"): the rack, seat workstations, Needs You, talkback,
  the crew hosted in the server, the look, honest playback labelling. The owner can run a session, watch every
  seat, answer Needs You and audition takes without the CLI.
- Left, in order of value:
  1. **The owner's first look.** Nobody but the builder has seen it. Expect a round of changes to the look.
  2. **Open a take on the Desk** (`/api/state?project=<take>`, `project=` on the desk's calls, cut and hand in
     from the UI), so a person in a seat works like any other seat.
  3. A real-model session driven from RUN, on cheap models, start to finish.
  4. Per-seat workstation depth: the Mix seat's bench should be the mixer, the Composer's the piano roll.
  5. Later niceties: workflow/audio cable view, movable modules (stage 6), push-to-talk.

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
| Keeping the work | Say when to commit `studio-core`, and whether to push / open a PR. |

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
`tests/test_crew_floor.py` for how a session is driven. For the UI, read `web/studio.js` top to bottom (about 480
lines) and run it against a scratch library: set `TUNETRACK_HOME` and `TUNETRACK_PROJECTS` to temporary folders
before starting the server, and seat a `script` runner so nothing is spent.

Next: ask the owner to look at the UI (stage 2c, item 1). Then what is left of stage 2b if Claude is logged in,
otherwise stage 2c item 2 (open a take on the Desk).
