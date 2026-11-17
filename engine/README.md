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
The first build compiles JUCE (and sfizz) and takes a few minutes; later builds take seconds.

**SFZ through sfizz (optional).** Unpack sfizz's source release, the tarball with its dependencies in
`external/` (`sfizz-1.2.3.tar.gz` from github.com/sfztools/sfizz/releases, 19 MB; not a git checkout without its
submodules, and not the plugin installer), so that `engine/third_party/sfizz/sfizz-1.2.3/CMakeLists.txt` exists
(`-DTT_SFIZZ_DIR=<path>` for another place), then configure and build as above. The folder is not in git.
`cmake/Sfizz.cmake` builds sfizz as a static library into the engine and patches two of its files on configure
(below). Without it the engine builds as before and SFZ tracks arrive as stems.

## Use

```
tunetrack live <project>             play through the audio interface; edits made elsewhere are heard as they land
tunetrack live --devices             list audio outputs
tunetrack live <project> --null-test render with both engines and report the difference
```

Directly:

```
tunetrack-engine [--no-device] [--device <name part>] [--inputs] [--per-sample] [--threads <n>]
tunetrack-engine --render <spec.json> <out.wav> [--per-sample] [--threads <n>]
tunetrack-engine --devices
```

`--threads` is how many threads compute strips side by side, the calling one included (default: one per physical
core but one, at most 8).

## How it fits together

- **Spec.** `live.build_spec(project)` flattens a project into JSON: every time in samples, every device a file
  path. The engine knows nothing about beats, clips, patches or notation.
- **Graph.** `Session` (src/Session.cpp) builds a JUCE `AudioProcessorGraph`: per track a MIDI source or stem,
  the instrument, the inserts and a fader; buses and the master the same way; sends and sidechain keys as extra
  connections. JUCE lines up plugin latency. Loading a new spec keeps every device whose identity did not
  change, so a note edit or a fader move never reloads a plugin.
- **Lanes: several cores** (src/Lanes.h). A source strip whose inserts listen to no other strip gets a graph of
  its own (source, instrument, inserts, up to the fader). Before each block, `LanePool` computes every lane on the
  worker threads and the calling thread; the strip's `LaneNode` hands the result to the session's graph, which
  sees it as one device with the lane's latency. Strips with a sidechain-keyed insert, buses and the master stay in
  the session's graph. Lanes depend only on the transport, so a render is bit for bit the same on any number of
  threads. A strip that gains or loses a keyed insert moves between the two, which reloads its devices.
- **Automation** (src/Automation.h). A device's `auto` in the spec is `{setting: [[sample, value], ...]}`, straight
  lines between points. A VST3 parameter takes 0..1 or the text it shows (turned into 0..1 once per distinct text on
  load); it is set at the start of every block, by whichever thread computes its graph (a lane's worker, or the audio
  thread). Built-in effects and synths follow their lanes inside the node, every 32 samples of song time, so a render does
  not depend on the block size. Either way the value is read at the song time of the audio arriving at the device (the
  block's position less the latency before it, as the fader does). A new lane reloads nothing. A setting that stops
  being automated goes back to its static value, or to the value it had before automation if it has none; each plan
  carries every such return, so a plan the audio thread never picked up loses nothing. The built-in delay's `delay_sec`
  lane is written by `live._delay_lane`: its time is in beats, so it steps at each tempo change.
- **Tempo.** Every plugin's play head is the transport: tempo map, metre, bar position, play state and loop, as of the
  block's start, so tempo-synced plugins follow tempo changes. (SSL X-Delay ignored the host tempo even at a steady
  tempo in a test, under DawDreamer too; Waves H-Delay follows it, gliding to each new time.)
- **Sidechain keys.** A key input of one channel (TDR Nova's) hears (L + R) / 2 of a stereo key, as the offline
  worker feeds it, through a `MonoNode`.
- **Sample rate.** The session always runs at the project's rate. On load the device is asked for that rate; one
  that cannot run at it (Windows Audio in shared mode) keeps its own and is fed through `Resampler.h`, a
  Kaiser-windowed sinc whose cut-off follows the lower rate (flat to 18 kHz at 48 to 44.1 kHz, a 30 kHz tone in a
  96 kHz project under -60 dB on a 48 kHz device, 1-2% of a core). `deviceInfo` says `converted`; the `pull` op
  plays the device path into a file without a device (tests).
- **Built-in synths and effects.** The offline renderer compiles their Faust code at render time. The engine
  cannot, so `tools/gen_faust.py` turns each one into a C++ class with its settings as live controls
  (`gen/tt_faust_gen.h`). Run the script again after changing `tunetrack/synths.py`, then rebuild.
- **SFZ instruments** (src/SfzNode.cpp) play through sfizz when the engine is built with it; its ready line says
  `"sfz": true` (`live.engine_plays_sfz()`; `sfz_player: "tunetrack"` in the library config keeps TuneTrack's
  own player). Played at the level TuneTrack's player plays them: sfizz's -7.35 dB master volume is set to 0, and
  where a file reads no CC7 itself, sfizz's own CC7-to-volume link starts at 127 instead of the General MIDI 100
  (`full_cc7` in the spec, from `sfz.reads_cc`). Offline, sfizz waits for each block's samples (freewheeling).
  Two patches to sfizz make renders repeatable: its random generator is one per thread, and each SFZ node swaps
  its own in around every call (src/SfizzRandom.cpp), so a track's round robins depend only on its own notes,
  whatever thread computes it and whatever renders beside it; and the offline wait also covers loads still
  queued for sfizz's dispatcher thread. A reset (every render) puts voices, controllers and the generator back.
- **What it does not host.** Audio clips, and SFZ instruments in a build without sfizz, arrive as audio files
  ("stems") that the offline worker renders dry and caches; their inserts still run live. Time-stretched and transposed
  clips are stretched there too (Rubber Band through pedalboard, `engine._stretched_clip`, cached per clip).
- **Tracking** (src/Recorder.h). `device` with `"inputs": true` (or `--inputs`) opens the interface's inputs;
  `inputs` lists them and opens every MIDI keyboard. A strip with `"input": {"ch": [left, right?], "monitor": bool}`
  gets the input before its inserts, heard only while monitoring. `record` (`{"audio": {strip id: {"file", "ch"}},
  "from": sample, "count_until": sample}`) plays and writes each file as the input arrives, plus the keyboard's
  events; `record_stop` answers with `marks` (flat [samples into the recording, song position], one at the start
  and one per loop wrap), `midi` (flat [samples into the recording, status, d1, d2]), `samples`, `dropped` and
  `comp` (`graph`, `out`, `in`: the latencies the Python side moves the result earlier by). A source with
  `"thru": true` is played by the keyboard on `"channel"`. `click` switches the metronome (after the master, never
  offline). Positions before sample 0 are allowed (a count-in). `record` also takes `"punch": [in, out?]` (a
  stem source with `"duck": true`, an armed audio track, is silent between them while recording) and `"stop_at"`
  (the transport stops there by itself; `recording` stays true in the ticks until `record_stop`).
  Keyboards are numbered in the order they were opened (`inputs` lists them in that order; 0 is what the `midi`
  op sends): `midi_src` in the answer says which each event came from, and a source's `"thru_from": n` lets only
  keyboard n (and 0) play it. `devices` lists outputs and `inputs`; `device` takes `"input"` (the device to
  record from) and `"pick": true` (choose the output as at start-up).
  A device that is not at the project's rate: its input is not heard through the engine, and is recorded as it
  arrives at the device's rate (`file_sr`; marks, events and `samples` are then in device samples, song positions
  still in the project's), the keyboard plays at the start of the next session block. `pull` takes `input` (a
  file as the device's input), `midi` ([[device sample, status, d1, d2 [, keyboard]]]) and `hold` (leave the
  transport alone), and `record` without a device takes `device_sr`, which is how the tests record.
- **Protocol.** One JSON object per line on stdin (`{"op": "load", "path": ...}`, `play`, `stop`, `seek`, `loop`,
  `render`, `editor`, `save_state`, `devices`, `device`, `inputs`, `record`, `record_stop`, `click`, `midi`,
  `status`, `ticks`, `quit`), each answered with a line
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
  share an engine, the rest are spread over `render_workers` engines running side by side, each computing its
  strips on its share of the cores (`engine.engine_threads`; `engine_threads` in the library config overrides). Each strip is switched
  off and tapped; the taps become the cache files. Cache keys carry the engine build and synth mode, so engine and
  DawDreamer renders never mix and a rebuild renders afresh.
- **Bus and master chains** run in one engine kept for the render: the bus input as a stem, the chain, a tap.
- **SFZ** renders in the engine through sfizz when it is built in (`Renderer.sfz_in_engine`), with a cache key
  of its own. Otherwise it stays with the worker (TuneTrack's own SFZ player), and with inserts its dry render is
  a stem in the engine.
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
and `gen/tt_faust_gen.h` is output of the Faust compiler. sfizz (when built in) is BSD-2-Clause, with its bundled
dependencies under their own permissive licences (abseil Apache-2.0, dr_libs, WavPack, kissfft and others in its
`external/`); `cmake/Sfizz.cmake` patches two of its files in place on configure.
