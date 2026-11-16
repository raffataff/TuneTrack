# tunetrack-engine

TuneTrack's live audio engine: one C++ process that hosts a whole session and either plays it through the
audio interface in real time or renders it faster than real time. The Python side (`src/tunetrack/live.py`)
starts it, sends it the session and drives the transport.

## Build (Windows)

Needs Visual Studio Build Tools with the C++ workload, CMake 3.22+ and a JUCE 9 source checkout.

```
cmake -S engine -B engine/build -A x64
cmake --build engine/build --config Release --parallel
```

JUCE is looked for in `C:/Program Files (x86)/JUCE`; pass `-DJUCE_SOURCE_DIR=<path>` (or set `JUCE_DIR`) if it
lives elsewhere. The result is `engine/build/tunetrack-engine_artefacts/Release/tunetrack-engine.exe`, which is
where `live.engine_exe()` looks (override with `TUNETRACK_ENGINE` or `engine_exe` in the library config).
The first build compiles JUCE and takes a few minutes; later builds take seconds.

## Use

```
tunetrack live <project>             play through the audio interface; edits made elsewhere are heard as they land
tunetrack live --devices             list audio outputs
tunetrack live <project> --null-test render with both engines and report the difference
```

Directly:

```
tunetrack-engine [--no-device] [--device <name part>] [--per-sample]
tunetrack-engine --render <spec.json> <out.wav> [--per-sample]
tunetrack-engine --devices
```

## How it fits together

- **Spec.** `live.build_spec(project)` flattens a project into JSON: every time in samples, every device a file
  path. The engine knows nothing about beats, clips, patches or notation.
- **Graph.** `Session` (src/Session.cpp) builds a JUCE `AudioProcessorGraph`: per track a MIDI source or stem,
  the instrument, the inserts and a fader; buses and the master the same way; sends and sidechain keys as extra
  connections. JUCE lines up plugin latency. Loading a new spec keeps every device whose identity did not
  change, so a note edit or a fader move never reloads a plugin.
- **Built-in synths and effects.** The offline renderer compiles their Faust code at render time. The engine
  cannot, so `tools/gen_faust.py` turns each one into a C++ class with its settings as live controls
  (`gen/tt_faust_gen.h`). Run the script again after changing `tunetrack/synths.py`, then rebuild.
- **What it does not host.** SFZ instruments and audio clips arrive as audio files ("stems") that the offline
  worker renders dry and caches; their inserts still run live.
- **Protocol.** One JSON object per line on stdin (`{"op": "load", "path": ...}`, `play`, `stop`, `seek`, `loop`,
  `render`, `editor`, `save_state`, `devices`, `device`, `status`, `ticks`, `quit`), each answered with a line
  starting `@@ ` that repeats the command's `id` as `re`. `ticks` turns on position and meter events.
- **Offline renders.** `render` writes the master to `out` (leave it empty for none) and, for each strip named in
  `taps` (`{strip id: wav path}`), the strip's audio before its fader: 32-bit float, from song position 0, lined
  up for plugin latency, `length` samples. This is how TuneTrack's offline renderer uses the engine
  (`engine.Renderer`, see below). The answer carries each tap's peak (`taps`), each strip's post-fader peak
  (`peaks`), `probe_ms` and `unready`.
- **The ready probe.** A plugin instrument cannot say when it has finished loading its samples (HALion loads them
  from message-thread callbacks). Before a render, every new plugin instrument with notes is played one of its own
  notes (the median pitch, at that note's velocity), measured after the instrument and before its inserts, with
  the message thread free between rounds (`TT_PROBE_GAP_MS`, 250). It is ready once it sounds and is no louder than
  on the round before; after `TT_PROBE_MS` (15000) the render goes ahead and `unready` names the rest ("made no
  sound" or "still getting louder"). This runs after the fixed settle wait (2 s + 0.4 s per plugin given a state,
  `TT_SETTLE_MS` / `TT_SETTLE_EACH_MS`), which parameter overrides still need. `"probe": false` skips it.

## Offline rendering

TuneTrack renders offline through the engine whenever it is built and Windows lets it start
(`engine.offline_backend()`; `offline_engine` in the library config: `auto`, `engine` or `dawdreamer`). The Python
mixer stays: faders, sends, buses, loudness and stems are numpy, as before, and only the jobs move.

- **Tracks.** Every track missing from the cache renders in one batch: tracks that key each other's sidechains
  share an engine, the rest are spread over `render_workers` engines running side by side. Each strip is switched
  off and tapped; the taps become the cache files. Cache keys carry the engine build and synth mode, so engine and
  DawDreamer renders never mix and a rebuild renders afresh.
- **Bus and master chains** run in one engine kept for the render: the bus input as a stem, the chain, a tap.
- **SFZ** stays with the worker (TuneTrack's own SFZ player); with inserts, its dry render is a stem in the engine.
- A track that comes out silent though it has notes, or whose instrument never settled, is used for that render
  and not kept, so the next render tries again. A track whose engine crashed fails alone with its group; nothing
  half-written reaches the cache.
- `live.engine_runs()` checks the build can start (Smart App Control blocked one fresh build, 2026-11-16); when it
  cannot, renders use DawDreamer and LIVE explains why.

## Matching the offline renderer

`tunetrack live <project> --null-test` renders the raw master with both engines. Deterministic material cancels
to about -95 dB. Two things never cancel, by nature: instruments that vary each time they play (round-robin
samples, free-running modulation), and built-in synth parts where the two engines free a voice at a different
sample. Those are compared by level, octave-band balance and loudness curve instead.

`--per-sample` makes the built-in synths compute one sample per call, as DawDreamer's Faust host does. It costs
CPU and frees a releasing voice at its first near-silent sample, which for a single oscillator or string is the
first zero crossing after note-off: the plucked synths stop dead at note-off (the pad and FM synths checked sound
the same either way). Without it (whole blocks) releases ring out as each synth sets them. Live playback and
offline renders follow `synth_per_sample` in the library config: off by default since 2026-11-16 (the owner's
choice); true brings back the DawDreamer sound. The null tests always use it, since their reference is DawDreamer.

## Licences

The engine links JUCE under its AGPLv3 option, so the engine's source is offered under AGPLv3 too.
`third_party/faust/` holds unmodified Faust architecture headers (LGPL with the Faust architecture exception),
and `gen/tt_faust_gen.h` is output of the Faust compiler.
