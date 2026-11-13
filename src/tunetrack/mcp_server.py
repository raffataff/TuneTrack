"""TuneTrack MCP server: exposes the composition/mixing/rendering API as tools for AI agents."""
from __future__ import annotations

import functools
import os
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import api, roles, studio, takes

INSTRUCTIONS = """TuneTrack is a multitrack music workstation for game soundtracks, driven by tools.

Workflow:
1. status() - see instruments: VST3 plugins, saved patches (plugin + sound), built-in synths, SFZ libraries.
2. project_new(name, tempo, key, time_sig, brief={genre, mood, use, references}) - plan form first (sections,
   bars, loop region, where the hook lands) and record it with markers(). Then put the harmony on the chord track:
   harmony("i VI III VII", section="A") per section (roman numerals in the key, or symbols "Am7 F/A G").
3. track_add(name, instrument=..., role=...) per part. instrument: "patch:<id>" (real VST3 sound - preferred for
   acoustic/orchestral), "sfz:<path>", "synth:<preset>" (built-in Faust synths; great for electronic/chiptune),
   "vst3:<plugin>" (only if it makes sound without a patch, e.g. MODO DRUM). Add buses (kind="bus") for shared
   reverb and route with sends.
4. Write music: write() with TTN notation for melodies/counterlines, chords() for voice-led harmony, pattern()
   for arps/basslines/ostinatos, drums() for grooves, cc() for dynamics swells (CC1/CC11 on orchestral patches).
   chords()/pattern() without a progression follow the chord track (section="A" or at/end); to reharmonise, edit
   the chord track and re-run them for that range. Positions are beats or "bar:beat" ("9:1"). Reuse material with
   arrange(op="copy") and vary it with notes_edit.
5. Review: score() (bar by bar: the chord on each beat and every part in TTN, lined up, plus harmony warnings:
   clashes, parallel 5ths/8ves, crossings, parts off the chord track), notes_read() (TTN per clip), analyze()
   (key/range/overlap/mud/harmony checks + piano_roll_png - view it).
6. render() - returns loudness, band balance, per-track levels and warnings (silent tracks, mud, clipping, loop
   seam). Fix what it flags, re-render (unchanged tracks are cached). Game loops: project_set(loop="1:1-17:1")
   and render(loop="auto") folds reverb tails into the loop start for a seamless loop + .loop.json sidecar.
   Deliver into the game's audio folder with render(out="<game>/Audio/Music/menu_theme.ogg").
7. listen() - your ears: measures the rendered audio per section (level, brightness, who leads), which parts are
   buried or fight for one band, and probe-renders each instrument to catch silent/out-of-range pitches, transposed
   patches, slow attacks and drum notes that don't sound like their names. Fix what it flags and listen again.

TTN notation: D4:q!mf F4 A4:h | (D3 A3 F4):w | r:h C5:e. B4:s ...  durations w h q e s t (dotted '.', triplet '3',
beats '1.5'), sticky; velocity !p..!ff or !0-127; ' staccato, > accent, ~ tie, | bar check, @beat reposition.
C4 = middle C (MIDI 60).

Quality bar: real phrases with contour and rhythmic identity, functional bass, voice-led harmony, contrast
between sections, dynamics (velocity + CC), instruments in their ranges, headroom. No generic filler loops.
Never call patch_capture unless the user is at the computer - it opens a plugin window for them.

The studio: a project can be worked on by a crew - seats such as producer, composer, arranger, rhythm, sound,
mix and review, each filled by an agent or by the user. studio_status() shows the board: who sits where, open
tasks, takes waiting, and what needs the user. Without a seat you act for the user and edit the session directly;
seat_join("composer") makes you that seat. Seats work in takes: take_new() cuts a private copy of the project
that all your tools then edit, take_diff() shows what you changed, take_submit(summary) hands it in, and the
Producer or the user accepts it into the session (take_accept). Tasks carry the work (task_add / task_update),
message_send is talkback between seats, and ask_human() puts a real choice or blocker on the user's Needs You
list while you carry on with whatever doesn't depend on it.
"""

SEATED = """You are the {title} of this studio session ({room}).
{purpose}

How a seat works:
1. inbox() - your tasks and messages. studio_status() - the whole board. project_info() and score() - the music.
2. take_new(task="<task id>") - cut your take. Every tool you call then edits your take, not the session.
   task_update(task, state="working").
3. Do the task. Stay inside it: its tracks, its bars, your department ({owns}). If the fix belongs to another
   seat, tell that seat (message_send) or the Producer instead of doing it yourself.
4. Check your own work the way a listener would: score() and analyze() for notes, render() and listen() for sound.
5. take_diff() - make sure the take holds what you meant and nothing else. take_submit(summary) - say what you
   made, where (tracks, bars), and what the listener should check. Then inbox() again.
A real choice only the user can make, or a blocker you cannot get past: ask_human(...), then carry on with the
rest. Anything you could not do or verify goes in the summary - never hand in a take described as finished when
part of it is not.

"""


def _launched() -> bool:
    """True in a crew agent the floor manager started (it sets TUNETRACK_SEAT). Such an agent has the studio's
    tools and nothing else: it cannot open a file. An outside app that took a seat with seat_join() can."""
    return bool(os.environ.get("TUNETRACK_SEAT"))


def _instructions() -> str:
    seat = studio.me()
    if seat not in roles.ROLES:
        return INSTRUCTIONS
    r = roles.ROLES[seat]
    rest = INSTRUCTIONS.replace(" + piano_roll_png - view it", "") if _launched() else INSTRUCTIONS
    return SEATED.format(title=r.title, room=r.room.lower(), purpose=r.purpose,
                         owns=", ".join(sorted(r.owns)) or "you change nothing: you listen and report") + rest


mcp = MCPServer("tunetrack", instructions=_instructions(), version="0.1.0")
TOOLS: list[str] = []


def tool(fn):
    """Register a tool whose failures reach the model with their real message (the SDK hides other exceptions)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except Exception as e:  # noqa: BLE001 - every failure is reported to the agent
            raise ToolError(f"{type(e).__name__}: {e}") from e
    TOOLS.append(fn.__name__)
    return mcp.tool()(wrapper)


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


# ---- project -------------------------------------------------------------------------------------------------

@tool
def status() -> dict:
    """Library overview (VST3 instruments/effects, patches, built-in synths/effects, SFZ count) + current project."""
    out = api.library_status()
    try:
        out["current_project"] = api.project_info(clips=False)
    except Exception as e:  # noqa: BLE001
        out["current_project"] = str(e)
    return out


@tool
def project_new(name: str, tempo: float = 120, key: str = "C major", time_sig: str = "4/4",
                brief: dict | None = None, folder: str | None = None, overwrite: bool = False,
                eq_engine: str | None = None) -> dict:
    """Create a project and make it current. brief: free-form intent, e.g. {"genre": "orchestral fantasy",
    "mood": "hopeful, nostalgic", "use": "main menu loop", "game": "..."}. folder defaults to TuneTrack/projects.
    eq_engine: what builtin:eq inserts run on - "builtin", "ssl" (SSL Native Channel Strip 2) or "nova" (TDR Nova);
    default from the library config (builtin unless set)."""
    return api.project_new(name, folder, tempo, key, time_sig, brief, overwrite, eq_engine)


@tool
def project_open(path: str) -> dict:
    """Open a project folder (or name under TuneTrack/projects) and make it current."""
    return api.project_open(path)


@tool
def project_list() -> dict:
    """Current, recent and known projects."""
    return api.project_list()


@tool
def project_info(project: str | None = None, clips: bool = True) -> dict:
    """Project summary: tempo, key, loop, markers, master, tracks (instrument, mix, fx, clips)."""
    return api.project_info(project, clips)


@tool
def project_set(name: str | None = None, tempo: float | None = None, key: str | None = None,
                time_sig: str | None = None, brief: dict | None = None, loop: str | None = None,
                length: str | None = None, tail: float | None = None, target_lufs: float | None = None,
                ceiling_db: float | None = None, master_volume_db: float | None = None,
                tempo_changes: list | None = None, eq_engine: str | None = None, project: str | None = None) -> dict:
    """Change project settings. loop: "1:1-17:1" (bar:beat range) or "off". length: end position "33:1",
    a length "32 bars", or "auto". target_lufs: loudness the render is normalised to (default -16; -14..-18 typical
    for game music). ceiling_db: true-peak-ish limiter ceiling. tempo_changes: [["17:1", 90], ...].
    eq_engine: what every builtin:eq insert runs on, same settings - "builtin", "ssl" (SSL Native Channel Strip 2:
    console colour) or "nova" (TDR Nova: clean, wide ranges). Inserts added as "eq:ssl"/"eq:nova" keep theirs."""
    return api.project_set(project, name, tempo, key, time_sig, brief, loop, length, tail, target_lufs, ceiling_db,
                           master_volume_db, tempo_changes, eq_engine)


@tool
def markers(add: list[dict] | None = None, remove: list[str] | None = None, clear: bool = False,
            project: str | None = None) -> dict:
    """Section markers. add: [{"at": "1:1", "name": "Intro"}, {"at": "9:1", "name": "A"}]."""
    return api.markers(project, add, remove, clear)


@tool
def harmony(progression: str | None = None, at: Any = None, end: Any = None, section: str | None = None,
            chord_length: Any = None, key: str | None = None, clear: bool = False, project: str | None = None) -> dict:
    """The chord track: one harmonic timeline for the song. chords() and pattern() follow it when called without a
    progression, and score() checks every part against it. Set it before writing parts.
    Set: progression = roman numerals in the key ("i VI III VII", "ii7 V7 Imaj7", "V/V") or symbols ("Am7 F/A G"),
    one chord per bar (chord_length) or ":beats" per chord ("i:2 iv:2 V7:4"); "%" repeats the previous chord,
    "N.C." leaves a gap. It replaces the range starting at `at` (or the marker `section`); with `end` or `section` it
    repeats to fill that range. key: a different key for this range (a modulation); roman numerals otherwise follow
    the project key. clear=True removes the range (or everything). No progression and no clear: read it."""
    return api.harmony(progression, at, end, section, chord_length, key, clear, project)


@tool
def undo(project: str | None = None) -> dict:
    """Undo the last edit."""
    return api.undo(project)


@tool
def redo(project: str | None = None) -> dict:
    """Redo the last undone edit."""
    return api.redo(project)


# ---- tracks & mixing -----------------------------------------------------------------------------------------

@tool
def track_add(name: str, instrument: Any = None, kind: str = "midi", volume_db: float = 0.0, pan: float = 0.0,
              role: str | None = None, layer: str | None = None, output: str = "master", fx: list | None = None,
              project: str | None = None) -> dict:
    """Add a track. kind: midi | audio | bus. instrument (midi): "patch:<id>", "synth:<preset>", "sfz:<path>",
    "vst3:<plugin>" or {"type","name","patch","params"}. role: instrument family used for range checks
    (violin, cello, horn, bass, pad, drums ...). layer: adaptive-music layer name for render(layers=True).
    fx: ["builtin:eq", "eq:ssl", "vst3:TDR Nova", "patch:<fx patch id>"]. output: "master" or a bus track."""
    return api.track_add(name, instrument, kind, project, volume_db, pan, role, layer, output, None, fx)


@tool
def track_update(track: str, name: str | None = None, instrument: Any = None, instrument_params: dict | None = None,
                 volume_db: float | None = None, pan: float | None = None, mute: bool | None = None,
                 solo: bool | None = None, output: str | None = None, sends: list[dict] | None = None,
                 role: str | None = None, layer: str | None = None, project: str | None = None) -> dict:
    """Change a track. sends: [{"to": "Reverb", "db": -10}] (replaces sends). instrument_params merge into the
    instrument (built-in synth params like {"cutoff": 900, "release": 1.2}, or VST3 params by name)."""
    return api.track_update(track, project, name, instrument, volume_db, pan, mute, solo, output, sends, role,
                            layer, None, instrument_params)


@tool
def track_remove(track: str, project: str | None = None) -> dict:
    """Delete a track."""
    return api.track_remove(track, project)


@tool
def track_duplicate(track: str, new_name: str | None = None, with_clips: bool = True, project: str | None = None) -> dict:
    """Duplicate a track (e.g. to layer/double a part with another instrument)."""
    return api.track_duplicate(track, new_name, with_clips, project)


@tool
def fx_add(track: str, device: Any, index: int | None = None, sidechain: str | None = None,
           project: str | None = None) -> dict:
    """Insert an effect on a track, bus, or "master". device: "builtin:<eq|compressor|reverb|delay|chorus|
    saturator|width|filter|bitcrush|gain>", "vst3:<plugin>", "patch:<id>" or {"type","name","params"}.
    EQ: builtin:eq runs on the project's eq_engine; "eq:ssl", "eq:nova" or "eq:builtin" (or {"type": "eq",
    "engine": "ssl", "params": {...}}) choose for this insert. Same params on every engine (hpf, low_freq/low_gain,
    mid_freq/mid_gain/mid_q, high_freq/high_gain, lpf); values a plugin can't reach are clamped and listed in "notes".
    sidechain: a MIDI/audio track whose own sound (pre-fader, ignoring its mute/solo) drives the effect's detector,
    e.g. a compressor on the bass keyed by "Kick" for ducking/pumping. Works with builtin:compressor and VST3s with a
    sidechain input (SSL Bus Compressor 2, Channel Strip 2, G3, LMC+); their external sidechain switch is turned on
    for you (see "notes"). A muted ghost-kick track makes a clean key."""
    return api.fx_add(track, device, project, index, sidechain)


@tool
def fx_update(track: str, index: int, params: dict | None = None, bypass: bool | None = None,
              replace: Any = None, sidechain: str | None = None, project: str | None = None) -> dict:
    """Change an effect's params (built-in: names from synths_list; VST3: param name -> 0..1 or display text like
    "-6 dB"), bypass it, replace the device, or set its sidechain key (a track name; "" disconnects it and turns
    the plugin's external sidechain switch off)."""
    return api.fx_update(track, index, project, params, bypass, replace, sidechain)


@tool
def fx_remove(track: str, index: int, project: str | None = None) -> dict:
    """Remove an effect by index."""
    return api.fx_remove(track, index, project)


@tool
def automation(track: str, param: str = "volume_db", points: list | None = None, clear: bool = False,
               project: str | None = None) -> dict:
    """Track volume_db or pan automation: points [["1:1", -20], ["5:1", -6]] (linear ramps). clear=True resets."""
    return api.automation(track, param, points, clear, project)


# ---- notes -----------------------------------------------------------------------------------------------------

@tool
def write(track: str, notes: str, at: Any = 0, mode: str = "replace", vel: int | None = None, transpose: int = 0,
          clip_name: str | None = None, project: str | None = None) -> dict:
    """Write notes in TTN notation starting at `at` ("bar:beat" or beats). mode replace clears notes starting in
    the written span; merge layers on top (add a second voice). Clips are created/extended automatically.
    Bar lines '|' are checked against the time signature and mismatches are reported as warnings."""
    return api.write(track, notes, at, project, mode, vel, transpose, clip_name)


@tool
def add_notes(track: str, notes: list, mode: str = "merge", project: str | None = None) -> dict:
    """Add notes as [[pitch ("C4" or 60), position ("5:1" or beats), duration_beats, velocity], ...]."""
    return api.add_notes(track, notes, project, mode)


@tool
def chords(track: str, progression: str | None = None, at: Any = None, end: Any = None, section: str | None = None,
           chord_length: Any = None, rhythm: str | None = None, voices: int = 4, low: str = "C3", high: str = "G5",
           vel: int = 80, gate: float = 1.0, bass: bool = False, bass_octave: int = 2, key: str | None = None,
           mode: str = "replace", project: str | None = None) -> dict:
    """Voice-led chords. Without a progression they follow the chord track (harmony) over the marker `section` or
    [at, end) (end defaults to the end of the chord track). progression: roman numerals in the key
    ("i VI III VII", "ii7 V7 Imaj7", "V/V", "bVI") or symbols ("Dm Bb F C", "Cmaj7/E"); each lasts chord_length
    (default 1 bar), override per chord with ":beats" ("i:2 iv:2 V:4"); with end/section it repeats to fill them.
    rhythm repeats within each chord: "h h", "q. e q q", "e r:e e e q h", "q>" accents.
    voices/low/high bound the voicing; bass=True adds a root in bass_octave."""
    return api.chords(track, progression, at, project, key, chord_length, rhythm, voices, low, high, vel, gate,
                      bass, bass_octave, mode, end, section)


@tool
def pattern(track: str, pattern: str, progression: str | None = None, at: Any = None, end: Any = None,
            section: str | None = None, rate: float = 0.5, octave: int = 4, vel: int = 85, gate: float = 0.9,
            chord_length: Any = None, restart: bool = True, key: str | None = None, mode: str = "replace",
            project: str | None = None) -> dict:
    """Arpeggios/basslines/ostinatos that follow the chords: the chord track over `section` or [at, end) when no
    progression is given (like chords()), else the progression. pattern = chord degrees, one per `rate` beats:
    "1 3 5 8 5 3" arp, "1 . 1 5- 8 . 5- ." bass, "1 - - 5 8 -" ('-' holds, '.' rests), "5+"/"3-" octave
    shifts, "b7"/"#4" altered degrees above the root, "1>" accent. octave = octave of the chord root (C4 = middle C; bass ~2)."""
    return api.pattern(track, progression, pattern, at, project, key, chord_length, rate, octave, vel, gate,
                       restart, mode, end, section)


@tool
def drums(track: str, grid: dict, at: Any = 0, bars: int = 1, steps_per_beat: int = 4, swing: float = 0.0,
          vel: int = 100, drum_map: dict | None = None, mode: str = "replace", project: str | None = None) -> dict:
    """Step-sequenced drums: {"kick": "x.......x.x.....", "snare": "....x.......x...", "hat": "x.x.x.x.x.x.x.xX"}.
    x hit, X accent, o ghost, . rest; "|" separates alternate bars that cycle ("x...x...|x...x.x."). Names are
    GM (kick snare clap rim hat pedal_hat open_hat tom_low tom_mid tom_high crash ride ride_bell shaker
    tambourine cowbell ...) or MIDI numbers; drum_map overrides note numbers for non-GM kits."""
    return api.drums(track, grid, at, bars, project, steps_per_beat, swing, vel, drum_map, mode)


@tool
def cc(track: str, controller: Any, start: Any, end: Any = None, from_value: float | None = None,
       to_value: float | None = None, points: list | None = None, shape: str = "linear",
       resolution: float = 0.25, mode: str = "replace", project: str | None = None) -> dict:
    """MIDI CC / pitch-bend curves: controller 1 (mod wheel = dynamics on most orchestral libraries), 11
    (expression), 64 (sustain), "bend" (-1..1). Ramp from_value->to_value between start and end with shape
    linear|exp|log|swell, or explicit points [["1:1", 40], ["3:1", 100]]."""
    return api.cc(track, controller, start, end, from_value, to_value, points, shape, resolution, project, mode)


@tool
def notes_read(track: str | None = None, start: Any = None, end: Any = None, format: str = "ttn",
               exact: bool = False, project: str | None = None) -> dict:
    """Read notes back as TTN per clip (format="ttn") or as [name, bar:beat, dur, vel] lists (format="list").
    TTN snaps humanized timing to a 1/24-beat grid and velocity to steps of 4 unless exact=True."""
    return api.notes_read(track, start, end, project, format, exact)


@tool
def notes_edit(track: str, op: str, start: Any = None, end: Any = None, low: Any = None, high: Any = None,
               semitones: int | None = None, steps: int | None = None, grid: float | None = None,
               strength: float | None = None, swing: float | None = None, ends: bool | None = None,
               timing_ms: float | None = None, velocity: int | None = None, seed: int | None = None,
               set: int | None = None, scale: float | None = None, add: int | None = None,
               ramp_from: int | None = None, ramp_to: int | None = None, overlap: float | None = None,
               dur: float | None = None, factor: float | None = None, beats: float | None = None,
               axis: Any = None, interval: int | None = None, project: str | None = None) -> dict:
    """Edit notes in [start, end) and pitch [low, high] on a track. op:
    transpose(semitones | steps=diatonic) quantize(grid, strength, swing, ends) humanize(timing_ms, velocity, seed)
    velocity(set | scale | add | ramp_from+ramp_to) legato(overlap) length(dur | scale) stretch(factor)
    shift(beats) reverse invert(axis) double(interval: 12 octave, -12, 7...) delete fit_scale dedupe"""
    args = _clean(dict(semitones=semitones, steps=steps, grid=grid, strength=strength, swing=swing, ends=ends,
                       timing_ms=timing_ms, velocity=velocity, seed=seed, set=set, scale=scale, add=add,
                       ramp_from=ramp_from, ramp_to=ramp_to, overlap=overlap, dur=dur, factor=factor, beats=beats,
                       axis=axis, interval=interval))
    return api.notes_edit(track, op, start, end, low, high, project, **args)


@tool
def clip_edit(clip: str, op: str, to: Any = None, track: str | None = None, times: int = 1, length: Any = None,
              at: Any = None, name: str | None = None, project: str | None = None) -> dict:
    """Clip ops: move(to[, track]) copy(to[, track]) duplicate(times) resize(length) split(at) delete
    rename(name) mute unmute."""
    return api.clip_edit(clip, op, project, to, track, times, length, at, name)


@tool
def arrange(op: str, start: Any, end: Any, to: Any = None, tracks: list[str] | None = None,
            mode: str = "replace", project: str | None = None) -> dict:
    """Section editing across tracks: copy(start,end,to) move(start,end,to) insert(start,end) opens a gap,
    delete(start,end) removes and closes the gap, clear(start,end). copy mode="replace" (default) clears the
    destination's notes first, "merge" layers onto them. Automation moves with insert/delete; markers, tempo
    changes, loop region, song length and the chord track do too unless `tracks` limits it to some tracks (copy
    takes the chord track along in replace mode)."""
    return api.arrange(op, start, end, to, tracks, project, mode)


@tool
def audio_import(track: str, file: str, at: Any = 0, gain_db: float = 0.0, fade_in: float = 0.0,
                 fade_out: float = 0.0, project: str | None = None) -> dict:
    """Place an audio file (wav/flac/ogg/aiff) on an audio track; it's copied into the project."""
    return api.audio_import(track, file, at, project, True, gain_db, fade_in, fade_out)


@tool
def midi_import(file: str, at: Any = 0, instrument: Any = None, project: str | None = None) -> dict:
    """Import a .mid file as new tracks (one per MIDI track)."""
    return api.midi_import(file, at, project, instrument)


@tool
def midi_export(path: str | None = None, project: str | None = None) -> dict:
    """Export a multi-track .mid (tempo, markers, one track per part) for finishing in a DAW."""
    return api.midi_export(path, project)


@tool
def theory(scale: str | None = None, chord: str | None = None, progression: str | None = None,
           key: str | None = None, range: str | None = None, chords_in_key: str | None = None,
           sevenths: bool = False) -> dict:
    """Theory lookups: scale notes ("D dorian"), chord tones ("F#m7b5"), a progression's chords in a key,
    diatonic chords of a key, an instrument's playable/comfortable range ("cello")."""
    return api.theory_info(scale, chord, progression, key, range, chords_in_key, sevenths)


# ---- library -----------------------------------------------------------------------------------------------------

@tool
def plugins_list(kind: str | None = None, query: str | None = None) -> dict:
    """Installed VST3 plugins (kind: instrument | effect)."""
    return api.plugins_list(kind, query)


@tool
def plugins_scan(force: bool = False, retry_failed: bool = False) -> dict:
    """Scan VST3 folders for new plugins (each is probed in a separate process)."""
    return api.plugins_scan(force, retry_failed)


@tool
def plugin_params(device: str, query: str | None = None, limit: int = 120) -> dict:
    """List a VST3 plugin's (or "patch:<id>"'s) parameters with current value and display text."""
    return api.plugin_params(device, query, limit)


@tool
def patches_list(query: str | None = None, kind: str | None = None, plugin: str | None = None) -> dict:
    """Saved patches (a plugin + a stored sound) searchable by words in id/name/tags/description."""
    return api.patches_list(query, kind, plugin)


@tool
def patch_capture(plugin: str, name: str, tags: list[str] | None = None, description: str = "",
                  base_patch: str | None = None, role: str | None = None, dynamics_cc: int | None = None,
                  drum_map: dict | None = None, patch_id: str | None = None) -> dict:
    """ONLY when the user is at the computer: opens the plugin's window so they can load/tweak a sound; when they
    close it the state is saved as a patch and auditioned. dynamics_cc: 1 or 11 if the sound's loudness follows
    that CC. drum_map: {"kick": 36, ...} for non-GM kits."""
    return api.patch_capture(plugin, name, tags, description, base_patch, role, dynamics_cc, drum_map, patch_id)


@tool
def patch_import(plugin: str, preset_file: str, name: str, tags: list[str] | None = None, description: str = "",
                 role: str | None = None) -> dict:
    """Create a patch headlessly from a .vstpreset/.fxp file for that plugin."""
    return api.patch_import(plugin, preset_file, name, tags, description, role)


@tool
def patch_import_folder(plugin: str, folder: str, tags: list[str] | None = None, role: str | None = None) -> dict:
    """Create patches headlessly from every .vstpreset/.fxp in a folder (subfolders included; each subfolder name
    becomes a tag). Returns the imported patch ids and any per-file errors."""
    return api.patch_import_folder(plugin, folder, tags, role)


@tool
def patch_update(patch_id: str, name: str | None = None, tags: list[str] | None = None,
                 description: str | None = None, role: str | None = None, dynamics_cc: int | None = None,
                 drum_map: dict | None = None, range: list | None = None, channel: int | None = None) -> dict:
    """Edit a patch's metadata. channel (1-16): MIDI channel the track's notes go out on, for patches that are one
    slot of a multitimbral plugin state (HALion slot N listens on channel N by default)."""
    return api.patch_update(patch_id, name, tags, description, role, dynamics_cc, drum_map, range, channel)


@tool
def synths_list() -> dict:
    """Built-in synth presets and effects with their parameters and defaults."""
    return api.synths_list()


@tool
def sfz_list(query: str | None = None) -> dict:
    """SFZ instruments found in the sample folders (search by words in the path)."""
    return api.sfz_list(query)


@tool
def sfz_info(path: str) -> dict:
    """An SFZ instrument's playable range, keyswitches and the CCs it responds to."""
    return api.sfz_info(path)


@tool
def audition(instrument: str, phrase: str = "auto") -> dict:
    """Render a short test phrase through an instrument to check it makes sound and how bright it is.
    phrase: auto | chords | melody | bass | drums | any TTN text."""
    return api.audition(instrument, phrase)


# ---- render & review -------------------------------------------------------------------------------------------

@tool
def render(out: str | None = None, format: str = "wav", bits: int = 24, loop: str = "auto", normalize: bool = True,
           stems: bool = False, layers: bool = False, midi: bool = False, project: str | None = None) -> dict:
    """Render and analyse. out: folder or file path (extension sets format: .wav .ogg .flac .mp3); default
    <project>/renders. loop: auto (use project loop) | off | loop (loop body only) | intro+loop (with loop points).
    stems: per-track files; layers: one file per track layer (adaptive music); midi: also export .mid.
    Report: lufs, true peak, band balance %, per-track levels, warnings to act on."""
    return api.render(project, out, format, bits, loop, normalize, stems, layers, midi)


@tool
def score(start: Any = None, end: Any = None, section: str | None = None, tracks: list[str] | None = None,
          check: bool = True, project: str | None = None) -> dict:
    """Read the music as a score, bar by bar: the chord on each beat (from the chord track; "Dm*" = inferred from
    the notes where it has none) and every part's notes in TTN, columns lined up by beat so you can see what
    sounds together. Harmony warnings ("!" lines and `warnings`) give bar:beat and a fix: lead/bass notes that clash
    with the chord on strong beats (not passing/neighbour notes that move on by step), parallel 5ths/8ves between
    bass and lead, voice crossings, parts that don't follow the chord track. Range: start/end or a marker section
    (default the whole song, up to 32 bars). tracks: only these parts (drums are left out unless named)."""
    return api.score(start, end, section, tracks, check, project)


@tool
def analyze(images: bool = True, start: Any = None, end: Any = None, project: str | None = None) -> dict:
    """Note checks (out-of-key, out-of-range, overlaps, low-register mud) and harmony_warnings (as in score()).
    images=True also writes a piano-roll PNG and a spectrogram PNG of the last render and returns their paths:
    open them to see the arrangement. A crew agent launched by the studio cannot open files, so it gets the checks
    only - score() is its view of the notes and listen() of the sound."""
    return api.analyze(project, images and not _launched(), start, end)


@tool
def listen(reference: str | None = None, probe: bool = True, section_bars: int | None = None,
           tracks: list[str] | None = None, project: str | None = None) -> dict:
    """Your ears: measures the rendered audio instead of the notes. Run it after render() and act on its warnings.
    sections: loudness (LUFS), brightness, stereo width, notes per bar and each track's level per marker section
    (or every section_bars bars), bar_lufs (one value per bar), energy_arc; flags neighbouring sections that barely
    differ and big level jumps at the loop point.
    masking: each track's main band and how far it sits above (+) or under (-) the rest of the mix there, what covers
    it, and pairs of tracks fighting for one band (kick vs bass, pad vs lead) with the bars.
    verify: bars where a track has notes but no sound, and a probe render of every pitch each track uses (tracks:
    limit it to these): pitches that are silent or much quieter (out of range), slow attacks, instruments sounding
    transposed, and what each drum note really sounds like (kick/snare/hat/cymbal/click) vs its name.
    reference: path to a recording to compare loudness, loudness range, crest, band balance, brightness, width."""
    return api.listen(project, reference, probe, section_bars, tracks)


@tool
def play(file: str | None = None, project: str | None = None) -> dict:
    """Play a file (default: the last render) in the user's audio player."""
    return api.play(file, project)


@tool
def ui_open(project: str | None = None) -> dict:
    """Open the TuneTrack editor (arrangement, piano roll, mixer) in the browser for the user."""
    from .server import ensure_running
    url = ensure_running()
    if project:
        api.project_open(project)
    import webbrowser
    webbrowser.open(url)
    return {"url": url}


# ---- studio: seats, tasks, talkback, Needs You, takes -------------------------------------------------------------

@tool
def studio_status(session: str | None = None) -> dict:
    """The studio board: seats and who is in them, open tasks, takes waiting for acceptance, the user's Needs You
    list, and which seat you are. Start here in a session with a crew."""
    out = studio.studio_status(session)
    if studio.me() in (roles.YOU, "producer"):
        out["departments"] = [f"{r['id']}: {r['purpose']}" for r in roles.catalog()]
    return out


@tool
def seat_join(seat: str | None = None) -> dict:
    """Take a seat in the session ("composer", "mix", ...), or stand up again (None): you then act for the user.
    A seated client works in takes and gets that seat's tasks and messages from inbox()."""
    return studio.seat_join(seat)


@tool
def seat_assign(seat: str, kind: str | None = None, runner: str | None = None, model: str | None = None,
                effort: str | None = None, label: str | None = None, mode: str | None = None,
                notes: str | None = None, session: str | None = None) -> dict:
    """Put someone in a seat or change how it works. kind: "agent" (with runner = claude | codex | kilo and a
    model), "human" (the user takes it), "external" (an outside app sits in), "none" (empty it).
    mode: "propose" (work in takes) or "direct". notes: standing instructions for whoever sits there."""
    return studio.seat_assign(seat, kind, runner, model, effort, label, mode, notes, session)


@tool
def task_add(seat: str, title: str, detail: str = "", depends: list[str] | None = None, scope: dict | None = None,
             session: str | None = None) -> dict:
    """Give a seat a task. detail: enough to work from alone - what to make, which tracks and bars, what it must
    fit with, what done looks like. depends: task ids that must be done first. scope: {"tracks": [...],
    "bars": "9-16"}. Seats: producer, spotting, composer, arranger, rhythm, sound, tracking, mix, review."""
    return studio.task_add(seat, title, detail, depends, scope, session)


@tool
def task_update(task: str, state: str | None = None, result: str | None = None, title: str | None = None,
                detail: str | None = None, depends: list[str] | None = None, session: str | None = None) -> dict:
    """Move a task along: state = queued | working | needs_you | done | failed | cancelled (take_submit sets
    "submitted" for you). result: one or two sentences on what was done, or why it failed and what you tried."""
    return studio.task_update(task, state, result, None, title, detail, depends, session)


@tool
def inbox(session: str | None = None) -> dict:
    """What is waiting for your seat: unread messages (including the user's answers to your questions) and your
    open tasks. Call it when you start and after each step you finish."""
    return studio.inbox(None, True, session)


@tool
def message_send(to: str, text: str, task: str | None = None, session: str | None = None) -> dict:
    """Talkback: say something to a seat, to "all", or to "you" (the user). task: the task it is about. Messages
    inform and ask; work that someone must do goes on the board with task_add."""
    return studio.message_send(to, text, task, session)


@tool
def report_write(title: str, body: str, kind: str = "report", report: str | None = None,
                 session: str | None = None) -> dict:
    """File a document for the user to read in the studio's REPORTS: a session review, a mix or listening report,
    findings too long for talkback (which is for short messages). body: markdown, complete in itself. kind:
    "report", "review" or "note". report: the id of an earlier one to replace instead of filing a new one."""
    return studio.report_write(title, body, kind, report, session)


@tool
def report_list(session: str | None = None) -> dict:
    """The reports filed in this session (id, title, kind, who filed it)."""
    return studio.report_list(session)


@tool
def report_read(report: str, session: str | None = None) -> dict:
    """One report in full, by id."""
    return studio.report_read(report, session)


@tool
def ask_human(title: str, detail: str = "", kind: str = "decision", options: list[Any] | None = None,
              task: str | None = None, blocks: list[str] | None = None, session: str | None = None) -> dict:
    """Put something on the user's Needs You list. kind: "decision" (choose between options), "input" (they must
    provide or pick something), "blocker" (you cannot continue). options: labels, or {"label", "take": "<take
    id>", "audio": "<file>", "detail"} so each can be auditioned. task: the task it is about (it waits);
    blocks: other tasks that must wait too. The answer arrives in inbox(). Keep working on what doesn't depend on
    it. For real choices and blockers only - not progress reports, and not "please listen for me": listen() and
    render() are how you hear. When taste has to decide between versions, pass their takes as options so the user
    gets a play key for each."""
    return studio.ask_human(title, detail, kind, options, task, blocks, session)


@tool
def needs_you(resolve: str | None = None, option: str | None = None, text: str = "",
              session: str | None = None) -> dict:
    """The user's Needs You list. With resolve=<item id>, answer it for the user: option = the chosen option's id
    or label, text = anything they added. Only answer with what the user actually said."""
    if resolve:
        return studio.attention_resolve(resolve, option, text, session)
    return studio.attention_list("open", session)


@tool
def take_new(name: str | None = None, task: str | None = None, session: str | None = None) -> dict:
    """Cut a take: a private copy of the session's project. Your tools then edit the take, so nobody's work changes
    under you and yours isn't heard until it is accepted. task: the task the take is for."""
    return takes.take_new(name, task, None, True, session)


@tool
def take_use(take: str | None = None, session: str | None = None) -> dict:
    """Point your tools at an existing take (to continue or audition it), or back at the session (None)."""
    return takes.take_use(take, session)


@tool
def take_diff(take: str | None = None, session: str | None = None) -> dict:
    """What a take changed (default: the one you are working in), anything outside its seat's department, and - if
    the session has moved on since - the conflicts accepting it would hit."""
    return takes.take_diff(take, session)


@tool
def take_sync(take: str | None = None, on_conflict: str = "fail", session: str | None = None) -> dict:
    """Bring what the session has accepted since your take was cut into your take. on_conflict: "fail" (list the
    conflicts, change nothing), "mine" or "session"."""
    return takes.take_sync(take, on_conflict, session)


@tool
def take_submit(summary: str, take: str | None = None, session: str | None = None) -> dict:
    """Hand in your take. summary: what you made, where (tracks, bars), what the listener should check, and
    anything you could not do or verify."""
    return takes.take_submit(summary, take, session)


@tool
def take_accept(take: str, on_conflict: str = "fail", session: str | None = None) -> dict:
    """Merge a take into the session (one undo step). Listen first: take_use(take), render(), listen().
    on_conflict: "fail" (default: list the conflicts, change nothing), "take" or "session" wins each conflict."""
    return takes.take_accept(take, on_conflict, session)


@tool
def take_reject(take: str, reason: str, session: str | None = None) -> dict:
    """Send a take back to its seat. reason: what has to change, specific enough to act on - bars, tracks, what
    you heard. Its task goes back in the queue and the take stays open for the seat to continue."""
    return takes.take_reject(take, reason, session)


@tool
def take_discard(take: str, session: str | None = None) -> dict:
    """Close a take without merging it: its work is left out of the session for good (the files stay on disk).
    For a take that is not wanted - superseded, cut by mistake, or left over when its task was closed. A take
    that needs changes is returned with take_reject instead."""
    return takes.take_discard(take, session)


def apply_seat() -> list[str]:
    """Leave a seated agent only its seat's tools (roles.py); the user and unseated clients keep them all."""
    allowed = roles.tools_for(studio.me())
    removed = [] if allowed is None else [n for n in TOOLS if n not in allowed]
    for n in removed:
        mcp.remove_tool(n)
    return removed


def main() -> None:
    apply_seat()
    mcp.run()


if __name__ == "__main__":
    main()
