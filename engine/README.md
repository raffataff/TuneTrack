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

## Matching the offline renderer

`tunetrack live <project> --null-test` renders the raw master with both engines. Deterministic material cancels
to about -95 dB. Two things never cancel, by nature: instruments that vary each time they play (round-robin
samples, free-running modulation), and built-in synth parts where the two engines free a voice at a different
sample. Those are compared by level, octave-band balance and loudness curve instead.

`--per-sample` makes the built-in synths compute one sample per call, as the offline renderer's Faust host does.
It costs CPU and frees a releasing voice at the first near-silent sample, which shortens long releases; without
it (whole blocks) releases ring out. `tunetrack live` and the null test use `--per-sample`, so what is heard
live is what the offline renderer exports.

## Licences

The engine links JUCE under its AGPLv3 option, so the engine's source is offered under AGPLv3 too.
`third_party/faust/` holds unmodified Faust architecture headers (LGPL with the Faust architecture exception),
and `gen/tt_faust_gen.h` is output of the Faust compiler.
