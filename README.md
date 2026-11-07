# TuneTrack

An AI-controllable multitrack music workstation for game soundtracks. An agent (Claude Code, or anything that speaks MCP or
runs a shell) writes notes, chords, patterns and expression curves; TuneTrack plays them through real instruments (your VST3
plugins, SFZ sample libraries, built-in synths), mixes them through your VST3 effects, masters, and renders loop-ready files
with analysis the agent can act on. A browser editor gives you the usual arrangement / piano roll / mixer tools on the same
project, live.

```
"Use TuneTrack to make main-menu music for this game"
  -> agent reads the game, writes a brief, plans the form, composes, renders, reviews the report and piano roll,
     fixes issues, delivers Audio/Music/menu_theme.ogg + menu_theme.loop.json
```

## Setup

```bash
uv sync
```
```bash
uv run tunetrack setup
```
```bash
uv run tunetrack install-claude
```

`setup` creates `library/` and scans every VST3 in `C:\Program Files\Common Files\VST3` (each plugin is probed in its own
process; re-run `tunetrack scan` after installing plugins). `install-claude` copies the skill to `~/.claude/skills/tunetrack`
and registers the MCP server with Claude Code at user scope, so every project can use it. Restart Claude Code afterwards.

Then, in any project: *"use TuneTrack to generate some menu music for this project"*.

## Sound sources

| Source | Use | How the agent picks it |
|---|---|---|
| **Patches** | A VST3 plugin plus a saved sound (HALion program, BBC SO instrument, a Supermassive setting...) | `patch:<id>` |
| **SFZ** | Sample libraries played by TuneTrack's own sampler, no GUI needed | `sfz:<path>` |
| **Built-in synths** | 23 Faust synths: supersaw, pads, strings, brass, basses, Karplus-Strong plucks, FM keys/bells/mallets, chiptune channels, organ, GM drum machine | `synth:<name>` |
| **VST3 directly** | Instruments that make sound with default settings (e.g. MODO DRUM) | `vst3:<name>` |

**Capturing patches.** HALion Sonic, Groove Agent, BBC Symphony Orchestra, Spitfire LABS and most romplers load empty, so an
agent can't use them until you've saved some sounds:

```bash
uv run tunetrack capture "HALion Sonic" "Warm Strings" --tags strings,legato,warm --role strings
```

The plugin's own window opens. Pick a program and tweak it, then close the window. The state is saved to `library/patches/`, auditioned, and
from then on any agent can use `patch:halion-sonic-warm-strings`. In the editor, the same flow is on the Track tab ("Pick
sound in plugin window..."). Tag patches well (`strings`, `brass`, `pad`, `drums`, `orchestral`, `synthwave`...), because agents search by tag.
For CC-driven libraries (BBC SO, LABS, most orchestral), add `--dynamics-cc 1` so agents write mod-wheel curves.

Effects work the same way: `vst3:TDR Nova`, `vst3:ValhallaSupermassive`, SSL, Waves (shell plugins are hosted through
pedalboard) or 10 built-ins (`eq`, `compressor`, `reverb`, `delay`, `chorus`, `saturator`, `width`, `filter`, `bitcrush`,
`gain`). Agents set VST3 parameters by name and display text: `{"Mix": "100 %", "LowCut": "200 Hz"}`.

**Sidechain.** `fx_add("Bass", "vst3:SSL Native Bus Compressor 2", sidechain="Kick")` keys the compressor from the Kick track's
own sound (before its fader, and even while it's muted, so a silent ghost-kick track works). It works with `builtin:compressor`
and with VST3s that have a sidechain input (SSL Bus Compressor 2, Channel Strip 2, G3 MultiBusComp, LMC+). TuneTrack turns on
the plugin's external sidechain switch ("External S/C" and similar) for you. Waves shells hosted through pedalboard can't be
keyed. Keyed tracks render after their keys, and a change to the key re-renders the tracks it ducks. The editor's Track tab has
a Sidechain picker on each effect.

### Free instruments worth installing

- **BBC Symphony Orchestra Discover** and **Spitfire LABS** (free, Spitfire account + app). Install them, run `tunetrack scan`, then capture
  a patch per instrument/articulation you want agents to use.
- **Surge XT** (free, open source, surge-synthesizer.github.io): a deep synth for electronic music. Capture patches from its preset browser.
- **Dexed** (free FM synth): DX7 sounds.
- Free **SFZ** libraries: drop them into `library/instruments/` and they're found automatically (`sfz_list`).
  - VSCO 2 Community Edition (CC0 chamber orchestra, github.com/sgossner/VSCO-2-CE)
  - Virtual Playing Orchestra (virtualplaying.com), Sonatina Symphonic Orchestra
  - Salamander Grand Piano and the libraries at sfzinstruments.github.io
  - Versilian Community Sample Library (CC0 percussion, keys and folk instruments)

## The editor

```bash
uv run tunetrack ui
```

- **Arrangement**: drag clips to move (Alt-drag copies), drag the right edge to resize, double-click empty space for a new clip,
  right-click for rename/duplicate/split/mute/delete. Click the ruler to move the playhead, shift-drag it (or drag its top
  strip) to set the loop region, right-click it for markers. Ctrl+wheel zooms.
- **Piano roll**: click to add, drag to move, drag the edge to resize, double-click to delete, shift/ctrl-drag to box-select,
  arrows to nudge (shift = octave), Ctrl+D duplicates, Delete removes, velocity lane below.
- **Mixer**: faders, pan, mute/solo, insert FX (click a name to bypass), sends to buses, master chain.
- **Track**: instrument picker, built-in synth/FX parameters, VST3 parameter browser, patch capture, audition.
- **Render report**: loudness, true peak, band balance, per-track levels, warnings, output files.
- **Space** renders the changed tracks and plays (unchanged tracks come from cache). The Loop button loops the loop region.
- Ctrl+Z / Ctrl+Y undo/redo (shared with the agent's edits).

Everything goes through the same API the agent uses, and the editor polls the project file, so you can watch an agent work and
take over at any point.

## Rendering and delivery

`render(out="C:/Game/Audio/Music/menu.ogg")` (or Export... in the editor):

- **Loop region set**: tails after the loop end are folded into the loop start, so the file loops seamlessly. With an intro before the loop,
  you get an intro+loop file whose loop points are in a `smpl` chunk (WAV) and a `.loop.json` sidecar (samples and seconds:
  Godot `loop_offset`, Unity scheduling, FMOD/Wwise markers).
- Loudness normalised to the project's `target_lufs` (default -16), brick-wall limited to `ceiling_db`.
- `stems=True`: per-track files. `layers=True`: one file per track `layer` for vertical adaptive music. `midi=True`: a type-1
  MIDI file to finish the arrangement in Cubase.
- Formats: wav (16/24/32-bit), ogg, flac, mp3.

## Listening (the agent's ears)

An agent can't hear, so `listen()` (`tunetrack call listen '{}'`) measures the rendered audio and reports it as text it can act
on. The full report is also saved to `renders/listen.json`.

- **Sections**: loudness, peak, brightness, stereo width, notes per bar, band balance and each track's level per marker section
  (or every `section_bars` bars), plus one loudness value per bar. Warns when neighbouring sections barely differ, or when the
  loop wraps across a big level jump.
- **Masking**: each track's main octave band, how far it sits above or below everything else there, and what covers it. Also
  pairs of tracks that fight for one band (kick vs bass, pad vs lead), with the bars where they do.
- **Verify**: bars that have notes but no sound, and a probe render of every pitch each track uses through its instrument. This
  catches pitches that make no sound or are much quieter (outside the range), patches that sound transposed, slow attacks on
  short notes, and what each drum note really sounds like (kick/snare/hat/cymbal/click, with its level at equal velocity), so a
  wrong drum map or a choked crash shows up. Probe renders are cached like track renders.
- **Reference**: `reference="song.wav"` compares loudness, loudness range, crest, band balance, brightness and width with a track
  you want to match.

The drum and pitch readings are heuristics (spectral features, harmonic matching). They're tuned on the built-in synths,
Groove Agent SE, MODO DRUM and HALion, and they only warn on clear mismatches. Your ears still decide.

## Harmony: the chord track and the score

A project holds one harmonic timeline, the chord track (`chord_track` in `project.json`, covered by undo). Set it per
section, then let the parts follow it:

```python
harmony("i VI III VII", section="Intro")         # roman numerals follow the project key; symbols too: "Am7 F/A G"
harmony("i VI III VII i VI iv V", section="A")   # one chord per bar, or "i:2 iv:2 V7:4"; repeats to fill the section
chords("Pad", section="A", voices=4)             # no progression: follows the chord track
pattern("Bass", pattern="1 - - 1 . 1 5- .", section="A", octave=2)
```

To reharmonise, change the chord track for a range (`harmony("III iv:2 V:2", at="10:1", end="12:1")`) and re-run the
generators there. An explicit `progression` works as before and ignores the chord track. `arrange` moves the chord
track with insert/delete/move and copies it in replace mode. `%` repeats a chord, `N.C.` leaves a gap, `key=` gives a
range its own key.

`score()` (`tunetrack call score '{"section": "A"}'`) is the text view an agent reads alongside `analyze()` and
`listen()`:

```
bar 11
  chord   Gm(iv)            -       A(V)    -
  Melody  G5:q.             F5:e    E5:q    D5
  Harp    G3:e D4           G4 Bb4  D5 Bb4  G4 D4
  Pad     (G3 Bb3 D4 G4):w
  Bass    G2:q.             G2:e    r G2    D2
  ! 11:3-12:1 Pad doesn't follow the chord track (plays Gm where it says A(V)): re-run chords()/pattern() for
    11:3-12:1 without a progression so it follows the chord track, or change the chord track there
```

- **Chords**: the chord on each beat, from the chord track, or inferred from all pitched parts where it has none (`Dm*`).
- **Parts**: each part's notes per bar in TTN, highest part first, columns lined up by beat. Humanized timing and gated
  lengths read as written and `%` repeats the bar before, so a 32-bar, 6-track cue comes to about 2-4k tokens. Drums
  are left out unless named in `tracks`.
- **Harmony warnings**, each with bar:beat and a fix (also in `analyze()` as `harmony_warnings`): lead or bass notes that
  clash with the chord on a strong beat, unless they move on by step; parallel fifths/octaves between bass and lead;
  voice crossings by role; parts that don't follow the chord track. Like `listen()` it only flags clear problems:
  colour tones (9ths, 11ths, 13ths, maj7), passing and neighbour notes, pedal points, slash chords like D/E and long
  octave doublings pass.

## For agents

50 MCP tools (`tunetrack mcp`), the same operations as `tunetrack call <fn> '<json>'` (list them with `tunetrack functions`).
The skill (`skill/SKILL.md`) is the composition playbook: understanding the project, forms per cue type, melody/harmony/bass/
arrangement craft, genre palettes, mixing targets, the review loop and delivery per engine.

TTN, the note notation agents write in:

```
D4:q!mf F4 A4:h | (D3 A3 F4):w | r:h C5:e. B4:s A4:q' G4:q> | E4:h~ E4:q r:q
```

`examples/ember_hollow_menu.py` builds a complete intro+loop menu theme with nothing but API calls, its harmony set once
on the chord track.

## Layout

```
src/tunetrack/
  model.py      project/track/clip/note data, tempo map, undo snapshots
  notation.py   TTN parser/formatter         theory.py  keys, modes, chords, roman numerals, voice leading, ranges
  generators.py chords/arps/basslines/drums  edit.py    transpose, quantize, humanize, legato, ...
  synths.py     built-in Faust synths + FX   sfz.py     native SFZ sampler
  plugins.py    VST3 registry + patch library
  worker.py     plugin host subprocess (DawDreamer; pedalboard for shell plugins)
  engine.py     cached parallel track renders, mixing, buses, master, loudness, limiter, loops, stems, MIDI export
  analysis.py   loudness/spectrum/loop-seam/note checks, piano-roll and spectrogram images
  harmony.py    chord track, chord inference, score() text view, harmony check (clashes, parallels, crossings)
  listening.py  listen(): per-section measurements, masking, probe renders that verify each instrument, reference compare
  api.py        every operation (shared by MCP, CLI and editor)
  mcp_server.py MCP tools    server.py + web/  editor    cli.py  command line
library/        plugins.json, patches/, instruments/ (SFZ), config.json
projects/<name>/project.json, audio/, renders/, cache/, history/
```

## Limits

- Playback is render-then-play (offline, cached per track), not a real-time audio engine. Edits to one track re-render only
  that track.
- Built-in synths ignore CC and pitch bend (VST3 and SFZ instruments respond to both). Plugin parameter automation isn't
  exposed yet; track volume/pan automation is.
- VST3 effects see the project's starting tempo for tempo-synced features.
- Audio clips are placed and faded, but not time-stretched.
