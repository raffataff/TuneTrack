"""The live engine: the C++ process (engine/) that plays a session in real time and renders it offline.

`build_spec` turns a project into the flat description the engine loads: every time already in samples, every
device resolved to a file path. `Engine` runs the process and talks to it, one JSON command per line.

What the engine hosts itself: VST3 instruments and effects, the built-in synths and effects, faders, pans,
automation, sends, buses, sidechain keys, the master chain, and SFZ instruments through sfizz when it is built
with it. What it plays from a file: audio tracks, and SFZ instruments in a build without sfizz, rendered dry by
the offline worker (cached) and run through their effects live.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import threading
from pathlib import Path

import numpy as np
import soundfile as sf

from . import config, eqmap, synths
from .engine import Renderer, audio_track_input, resolve_device, sidechain_links, soloed_sources
from .model import Device, Project, Track, read_rev

TPB = 960  # the offline renderer hands notes over as a MIDI file at this resolution; keep the same grid


def engine_exe() -> Path | None:
    """The built engine, or None when it has not been built (see engine/README.md)."""
    for cand in (os.environ.get("TUNETRACK_ENGINE"), config.load_config().get("engine_exe"),
                 config.ROOT / "engine" / "build" / "tunetrack-engine_artefacts" / "Release" / "tunetrack-engine.exe"):
        if cand and Path(cand).exists():
            return Path(cand)
    return None


_RUNS: dict[str, dict | None] = {}  # build -> its ready line (None: Windows would not start it)

BLOCKED = ("Windows did not let the engine start: Smart App Control blocks a program it has not seen before "
           "unless it is signed (every new build of engine/ is such a program). Until this build is allowed, "
           "renders use the DawDreamer worker and LIVE playback is unavailable.")


def _ready_line(out) -> dict:
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    for line in (out or "").splitlines():
        if not line.startswith("@@ "):  # the engine's own lines; a plugin may print anything
            continue
        try:
            msg = json.loads(line[3:])
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get("ev") == "ready":
            return msg
    return {}


def _started() -> dict | None:
    """What the built engine said when it started ({"ev": "ready", "sfz": bool, ...}), or None when it is not built
    or Windows will not start it. Checked once per build per process: started without an audio device and with
    nothing on stdin, it exits at once."""
    exe = engine_exe()
    if exe is None:
        return None
    st = exe.stat()
    k = f"{exe}:{st.st_mtime_ns}:{st.st_size}"
    if k not in _RUNS:
        try:
            done = subprocess.run([str(exe), "--no-device"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, timeout=30,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            _RUNS[k] = _ready_line(done.stdout)
        except subprocess.TimeoutExpired as e:
            _RUNS[k] = _ready_line(e.stdout)  # it started; it just did not stop by itself
        except OSError:
            _RUNS[k] = None
    return _RUNS[k]


def engine_runs() -> bool:
    """Whether the built engine can be started on this machine."""
    return _started() is not None


def engine_plays_sfz() -> bool:
    """Whether SFZ instruments play inside the engine (sfizz, built in when engine/third_party/sfizz has its
    source) instead of arriving as stems from TuneTrack's own SFZ player. `sfz_player: "tunetrack"` in the library
    config keeps the old player."""
    if str(config.load_config().get("sfz_player", "auto")).lower() == "tunetrack":
        return False
    return engine_runs() and bool((_started() or {}).get("sfz"))


def synth_per_sample() -> bool:
    """Built-in synths compute one sample per call, as DawDreamer's Faust host did: a releasing voice is freed at
    the first near-silent sample, so long releases are cut short. Off (the owner's choice, 2026-11-16): releases ring
    out as each synth sets them, live and offline alike. `synth_per_sample: true` in the library config brings back
    the old sound."""
    return bool(config.load_config().get("synth_per_sample", False))


def engine_stamp() -> str:
    """What identifies the engine's sound in a render cache key: the build and the synth mode."""
    exe = engine_exe()
    st = exe.stat() if exe else None
    return f"engine:{st.st_mtime_ns if st else 0}:{st.st_size if st else 0}:{int(synth_per_sample())}"


# ---- the spec ----------------------------------------------------------------------------------------------

def _midi_clock(tm, sr: int):
    """beat -> sample, exactly where the offline renderer plays a MIDI event at that beat.

    Offline, notes travel as a MIDI file: beats snap to ticks, tempo is stored in whole microseconds per beat
    (84 bpm becomes 84.00002) and the time is cut down to a sample. Reproducing that keeps the two engines
    sample-aligned; a one-sample slip is enough to change which synth voice plays a note."""
    import mido
    marks = [(int(round(b * TPB)), (1.0 / TPB) * (mido.bpm2tempo(bpm) / 1000000.0)) for b, bpm in zip(tm.beats, tm.bpms)]

    def at(beat: float) -> int:
        tick = int(round(beat * TPB))
        sec, last, per_tick = 0.0, 0, 0.5 / TPB
        for t, per in marks:
            if t >= tick and t > 0:
                break
            sec += (t - last) * per_tick
            last, per_tick = t, per
        return int((sec + (tick - last) * per_tick) * sr)

    return at


def _events(p: Project, t: Track, tm, sr: int, channel: int) -> list[int]:
    """A track's notes, CCs and pitch bend as a flat [sample, status, data1, data2, ...] list, in play order.
    The same rules as engine._track_events (notes past a clip's end are hidden, times on the MIDI tick grid)."""
    ev = []
    at = _midi_clock(tm, sr)

    def tick(beat: float) -> int:
        return int(round(beat * TPB))

    for c in t.clips:
        if c.muted or c.kind != "midi":
            continue
        for n in c.notes:
            if n.start >= c.length - 1e-9:
                continue
            on, off = c.start + n.start, c.start + min(n.end, c.length + 8)
            if off <= on:
                continue
            ev.append((tick(on), 2, at(on), 0x90 | channel, int(n.pitch), max(1, min(127, int(n.vel)))))
            ev.append((tick(off), 1, at(off), 0x80 | channel, int(n.pitch), 0))
        for lane, pts in c.cc.items():
            for b, v in pts:
                if lane == "bend":
                    val = int(max(-8192, min(8191, v * 8191))) + 8192
                    ev.append((tick(c.start + b), 0, at(c.start + b), 0xE0 | channel, val & 0x7F, (val >> 7) & 0x7F))
                else:
                    ev.append((tick(c.start + b), 0, at(c.start + b), 0xB0 | channel, int(lane), int(max(0, min(127, v)))))
    ev.sort(key=lambda e: (e[0], e[1]))
    return [x for e in ev for x in e[2:]]


def _lane_at(pts: list, at: float):
    """A lane's value at a beat: straight lines between points, held before the first and after the last (a text
    value holds until the next point)."""
    if at <= pts[0][0]:
        return pts[0][1]
    for (b0, v0), (b1, v1) in zip(pts, pts[1:]):
        if b0 <= at <= b1:
            if isinstance(v0, str) or isinstance(v1, str) or b1 - b0 < 1e-9:
                return v1 if at >= b1 else v0
            return v0 + (v1 - v0) * (at - b0) / (b1 - b0)
    return pts[-1][1]


def _lanes(d: Device) -> dict[str, list]:
    return {k: sorted(v, key=lambda q: q[0]) for k, v in (d.automation or {}).items() if v}


def _auto(p: Project, d: Device, numeric: bool, label: str, warnings: list[str] | None, skip=()) -> dict:
    """A device's automation as the engine reads it: {setting: [[sample, value], ...]}. Built-in devices take
    numbers only; a plugin parameter takes 0..1 or the text it shows."""
    tm, sr = p.tempo_map(), p.sample_rate
    out = {}
    for k, pts in _lanes(d).items():
        if k in skip:
            continue
        if numeric and any(isinstance(v, str) for _b, v in pts):
            if warnings is not None:
                warnings.append(f"{label}: automation of {k} needs numbers, not text; ignored")
            continue
        out[k] = [[int(round(tm.sec(b) * sr)), float(v) if numeric else v] for b, v in pts]
    return out


def _delay_lane(p: Project, d: Device, params: dict) -> list | None:
    """The built-in delay's time in seconds over the song, when it changes: its time is in beats, so it follows the
    tempo map (a step at each tempo change) and any automation of `time`. None when it never changes."""
    tm, sr = p.tempo_map(), p.sample_rate
    lane = _lanes(d).get("time") or []
    if len(tm.beats) < 2 and not lane:
        return None

    def time_at(b: float) -> float:
        return float(_lane_at(lane, b)) if lane else float(params["time"])

    out = [[0, time_at(0.0) * 60.0 / tm.bpm_at(0.0)]]
    for b in sorted(set(tm.beats[1:]) | {float(x) for x, _ in lane}):
        if b <= 0:
            continue
        s, t = int(round(tm.sec(b) * sr)), time_at(b)
        if b in tm.beats:  # a step from the old tempo's time to the new one's
            out.append([s, t * 60.0 / tm.bpms[tm.beats.index(b) - 1]])
        out.append([s, t * 60.0 / tm.bpm_at(b)])
    return out


def _eq_auto(p: Project, d: Device, eng: str, label: str, warnings: list[str] | None) -> dict:
    """builtin:eq automation when it runs on SSL / Nova: the settings at each point, translated as its static
    settings are, become lanes of the plugin's own parameters (the text each shows), straight lines between."""
    lanes = _lanes(d)
    if not lanes:
        return {}
    tm, sr = p.tempo_map(), p.sample_rate
    static = eqmap.translate(eng, d.params)[0]

    def active(k: str, v: float) -> float:
        # a band at 0 dB or a filter at its off point is left out of the plugin's settings; along a lane it stays
        # in, a hair inside its range, so the plugin's band moves instead of switching
        if k.endswith("_gain") and abs(v) < eqmap.FLAT:
            return eqmap.FLAT
        if k == "hpf" and v <= eqmap.HPF_OFF:
            return eqmap.HPF_OFF + 0.01
        if k == "lpf" and v >= eqmap.LPF_OFF:
            return eqmap.LPF_OFF - 1.0
        return v

    rows = []
    for b in sorted({b for pts in lanes.values() for b, _ in pts}):
        settings = {**d.params, **{k: active(k, float(_lane_at(pts, b))) for k, pts in lanes.items()}}
        rows.append((int(round(tm.sec(b) * sr)), eqmap.translate(eng, settings)[0]))
    out = {}
    for key in sorted(set(static) | {k for _s, r in rows for k in r}):
        vals = [r.get(key) for _s, r in rows]
        if all(v == static.get(key) for v in vals):
            continue
        if None in vals and warnings is not None:
            warnings.append(f"{label}: on {eng}, '{key}' switches off and on along the automation; "
                            f"it follows only where it is on")
        out[key] = [[s, v] for (s, _r), v in zip(rows, vals) if v is not None]
    return out


def _fx_spec(d: Device, p: Project, label: str = "", warnings: list[str] | None = None) -> dict | None:
    """One insert as the engine loads it: a VST3, or a built-in effect by name with every setting spelled out.
    `auto` carries its automation (_auto)."""
    if d.bypass:
        return None
    eng = eqmap.engine_for(d, p) if d.type == "builtin" and d.name == "eq" else "builtin"
    if d.type == "builtin" and not (d.name == "eq" and eqmap.plugin_entry(eng)):
        if d.name not in synths.FX:
            raise KeyError(f"Unknown built-in effect {d.name!r}")
        params = {**synths.FX[d.name][1], **{k: float(v) for k, v in d.params.items()}}
        out = {"type": "faust", "name": d.name, "params": params}
        auto = _auto(p, d, True, label, warnings, skip=("time",) if d.name == "delay" else ())
        if d.name == "delay":
            params["delay_sec"] = params["time"] * 60.0 / p.tempo
            if (lane := _delay_lane(p, d, params)) is not None:
                auto["delay_sec"] = lane
        if auto:
            out["auto"] = auto
        return out
    spec = resolve_device(d, p, False)
    if spec is None:
        return None
    out = {k: spec.get(k) for k in ("type", "path", "shell", "state", "params", "name")}
    auto = _eq_auto(p, d, eng, label, warnings) if eng != "builtin" else _auto(p, d, False, label, warnings)
    if auto:
        out["auto"] = auto
    return out


def _stem(r: Renderer, t: Track, spec: dict) -> str:
    """The dry (no effects) audio of a track the engine cannot host (SFZ): rendered once by the offline worker,
    then cached."""
    dry = {**spec, "fx": []}
    r.render_track(t, dry)
    return str(r.cache / f"{r.key(dry)}.wav")


def _audio_stem(r: Renderer, p: Project, t: Track, spec: dict) -> str:
    # where each clip lands (and how a stretched one plays) follows the tempo map
    what = {"clips": spec["clips"], "n": r.n, "sr": r.sr, "tempo": [r.tm.beats, r.tm.bpms]}
    key = hashlib.sha1(json.dumps(what, sort_keys=True, default=str).encode()).hexdigest()[:20]
    out = r.cache / f"live-in-{key}.wav"
    if not out.exists():
        part = out.with_suffix(".part.wav")
        sf.write(str(part), audio_track_input(p, t, r.n).T, r.sr, subtype="FLOAT")
        part.replace(out)
    return str(out)


def spec_head(r: Renderer) -> dict:
    """The part of an engine spec that is the song's clock: sample rate, metre, tempo map, length."""
    sr, tm, p = r.sr, r.tm, r.p
    return {"sr": sr, "block": 512, "ts": list(p.time_sig), "end": r.n,
            "tempo": [[int(round(tm.sec(b) * sr)), float(b), float(bpm)] for b, bpm in zip(tm.beats, tm.bpms)]}


def build_spec(p: Project, progress=None, tracking: dict | None = None) -> tuple[dict, list[str]]:
    """(engine spec, warnings) for a project. Renders the stems the engine needs, so the first call can be slow.

    tracking: {track id: {"arm": bool, "monitor": bool}}, what is set up to record (livehost keeps it; it is not
    part of the project). An audio track with an input gets it on its strip, heard when monitoring; an armed MIDI
    track's instrument is loaded even with no notes yet and plays the MIDI keyboard."""
    r = Renderer(p, progress)
    sr, tm = r.sr, r.tm
    warnings: list[str] = list(eqmap.warnings(p))

    def smp(beat: float) -> int:
        return int(round(tm.sec(beat) * sr))

    buses = {t.id: t for t in p.tracks if t.kind == "bus"}
    sources = [t for t in p.tracks if t.kind != "bus"]
    playing = soloed_sources(p)

    # buses that can play: the offline mixer skips any bus caught in a routing loop
    live_buses: set[str] = set()
    for _ in range(len(buses) + 1):
        for bid in buses:
            if bid in live_buses:
                continue
            feeders = [x for x in buses.values() if x.id != bid and x.id not in live_buses and
                       (x.output == bid or any(s.get("to") == bid for s in x.sends))]
            if not feeders:
                live_buses.add(bid)
    if len(live_buses) < len(buses):
        warnings.append("bus routing has a cycle; some buses were skipped")

    has_audio: set[str] = set()   # tracks with a signal before the fader (what a sidechain key can listen to)
    key_ids = {kid for _o, _d, kid in sidechain_links(p)}
    tracks = []

    for t in sources:
        audible = (not t.mute) and t.id in playing
        entry = {"id": t.id, "name": t.name, "kind": t.kind, "source": None, "fx": [], "on": audible}
        if audible or t.id in key_ids:
            try:
                spec = r.track_spec(t)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"track '{t.name}': {e}")
                spec = None
            if spec is not None:
                try:
                    entry.update(strip_source(r, t, spec, warnings))
                    has_audio.add(t.id)
                except Exception as e:  # noqa: BLE001
                    warnings.append(f"track '{t.name}': {e}")
                    entry["source"], entry["fx"] = None, []
        tr = (tracking or {}).get(t.id)
        if tr and audible:
            try:
                _tracked(r, t, entry, tr, warnings)
                has_audio.add(t.id)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"track '{t.name}': {e}")
        tracks.append((t, entry))

    for b in buses.values():
        ok = b.id in live_buses
        entry = {"id": b.id, "name": b.name, "kind": "bus", "source": None, "on": ok and not b.mute,
                 "fx": fx_chain(p, b.fx, f"bus '{b.name}'", warnings) if ok and not b.mute else []}
        tracks.append((b, entry))

    out_tracks = []
    for t, entry in tracks:
        entry.update(db=float(t.volume_db), pan=float(t.pan),
                     vol_auto=[[smp(b), float(v)] for b, v in sorted(t.automation.get("volume_db", []))],
                     pan_auto=[[smp(b), float(v)] for b, v in sorted(t.automation.get("pan", []))])
        dead_bus = t.kind == "bus" and t.id not in live_buses
        entry["out"] = "" if dead_bus else (t.output if t.output in buses else "master")
        entry["sends"] = [] if dead_bus else [[s["to"], float(s.get("db", -12))] for s in t.sends if s.get("to") in buses]
        for d in entry["fx"]:
            if d.get("key") and d["key"] not in has_audio:
                name = next((x.name for x in p.tracks if x.id == d["key"]), d["key"])
                warnings.append(f"'{t.name}': sidechain key '{name}' has no audio; the effect hears silence")
                d["key"] = "silent"
        out_tracks.append(entry)

    spec = {
        **spec_head(r),
        "loop": [smp(p.loop["start"]), smp(p.loop["end"])] if p.loop else None,
        "tracks": out_tracks,
        "master": {"db": float(p.master.get("volume_db", 0)),
                   "fx": fx_chain(p, [d for d in p.master.get("fx", []) if isinstance(d, Device)], "master", warnings)},
    }
    return spec, warnings


def _tracked(r: Renderer, t: Track, entry: dict, tr: dict, warnings: list[str]) -> None:
    """Add what tracking needs to a strip: the interface input of an audio track, the keyboard on a MIDI track."""
    p = r.p
    if t.kind == "audio" and t.input and (tr.get("arm") or tr.get("monitor")):
        entry["input"] = {"ch": [int(c) - 1 for c in t.input[:2]], "monitor": bool(tr.get("monitor"))}
        if tr.get("arm") and entry["source"] is not None:
            entry["source"]["duck"] = True  # silent between the punch points while recording over it
        if entry["source"] is None:  # nothing recorded yet: the strip is its input and its inserts
            entry["fx"] = fx_chain(p, t.fx, f"track '{t.name}'", warnings)
    elif t.kind == "midi" and t.instrument is not None and tr.get("arm"):
        if entry["source"] is None:  # no notes yet: load the instrument anyway, to be played
            spec = {**r._base(), "fx": [s for s in (resolve_device(d, p, False) for d in t.fx) if s],
                    "instrument": resolve_device(t.instrument, p, True), "clips": []}
            entry.update(strip_source(r, t, spec, warnings))
        src = entry["source"]
        if src.get("type") == "stem":
            warnings.append(f"track '{t.name}': this instrument is not played live by the engine; the keyboard "
                            f"will not sound")
            return
        src["thru"] = True
        src["thru_from"] = int(tr.get("key") or 0)  # which keyboard (the engine's number for it; 0 = any)


def fx_chain(p: Project, devices: list[Device], label: str, warnings: list[str]) -> list[dict]:
    """An insert chain as the engine loads it. A sidechain key is left as the key track's id ("key"); the caller
    checks that the engine will have that strip, else sets it to "silent"."""
    out = []
    for i, d in enumerate(devices):
        try:
            spec = _fx_spec(d, p, f"{label} fx {i}", warnings)
        except Exception as e:  # noqa: BLE001 - one broken insert shouldn't stop the session
            warnings.append(f"{label} fx {i}: {e}")
            continue
        if spec is None:
            continue
        if d.sidechain:
            if spec["type"] == "vst3" or d.name in synths.SIDECHAIN_FX:
                spec["key"] = d.sidechain
            else:
                warnings.append(f"{label}: {d.label()} has no sidechain input; key ignored")
        out.append(spec)
    return out


def strip_source(r: Renderer, t: Track, spec: dict, warnings: list[str]) -> dict:
    """{"source", "fx"} of a source track's strip, from its render spec (Renderer.track_spec). Audio clips arrive as
    stems (assembled here and cached), and so do SFZ instruments when the engine does not play them itself
    (Renderer.sfz_in_engine); everything else is hosted."""
    p, tm, sr = r.p, r.tm, r.sr
    inst = spec.get("instrument")
    if t.kind == "audio":
        source = {"type": "stem", "file": _audio_stem(r, p, t, spec)}
    elif inst["type"] == "sfz" and r.sfz_in_engine:
        from .sfz import reads_cc
        # full_cc7: sfizz's own CC7 volume link starts at 127, not 100 (SfzNode.cpp), where the file has none
        source = {"type": "sfz", "path": inst["path"], "full_cc7": not reads_cc(inst["path"], 7),
                  "events": _events(p, t, tm, sr, 0)}
    elif inst["type"] == "sfz":
        source = {"type": "stem", "file": _stem(r, t, spec)}
    elif inst["type"] == "synth":
        tpl, _desc, defaults = synths.SYNTHS[t.instrument.name]
        source = {"type": "synth", "template": tpl, "voices": 32,
                  "params": {**defaults, **{k: float(v) for k, v in t.instrument.params.items()}},
                  "events": _events(p, t, tm, sr, 0)}
    else:
        source = {**{k: inst.get(k) for k in ("type", "path", "shell", "state", "params", "name")},
                  "events": _events(p, t, tm, sr, int(inst.get("channel", 1)) - 1),
                  "channel": int(inst.get("channel", 1)) - 1}
    if t.kind != "audio" and t.instrument is not None and t.instrument.automation:
        where = f"track '{t.name}' instrument"
        if inst["type"] == "sfz":
            warnings.append(f"{where}: an SFZ instrument has no settings to automate (use cc); automation ignored")
        elif auto := _auto(p, t.instrument, inst["type"] == "synth", where, warnings):
            source["auto"] = auto
    return {"source": source, "fx": fx_chain(p, t.fx, f"track '{t.name}'", warnings)}


# ---- the process ---------------------------------------------------------------------------------------------

class EngineError(RuntimeError):
    pass


class Engine:
    """A running engine process. Not thread-safe per call id; one caller at a time is the intended use."""

    def __init__(self, device: str | None = None, no_device: bool = False, per_sample: bool = False,
                 on_event=None, threads: int | None = None, inputs: bool = False):
        """threads: how many threads compute the session's strips side by side (None: one per physical core but
        one, at most 8). Offline renders that run several engines at once share the cores out between them."""
        exe = engine_exe()
        if exe is None:
            raise EngineError("the live engine is not built: see engine/README.md")
        args = [str(exe)]
        if no_device:
            args.append("--no-device")
        elif device:
            args += ["--device", device]
        if inputs and not no_device:
            args.append("--inputs")
        if per_sample:
            args.append("--per-sample")
        if threads:
            args += ["--threads", str(int(threads))]
        self.on_event = on_event
        self.tick: dict = {}
        self.ready: dict = {}
        self.crash: dict = {}
        self.late_warnings: list[str] = []  # what plugins said once they had settled (sent as events)
        self._replies: dict[str, queue.Queue] = {}
        self._n = 0
        self._lock = threading.Lock()
        self._ready = threading.Event()
        try:
            self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                         bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as e:
            if getattr(e, "winerror", None) == 4551:  # "An Application Control policy has blocked this file"
                raise EngineError(BLOCKED) from None
            raise EngineError(f"the live engine could not start: {e}") from None
        threading.Thread(target=self._read, daemon=True).start()
        if not self._ready.wait(60):
            self.close()
            raise EngineError("the live engine did not start")

    def _read(self) -> None:
        for line in self.proc.stdout:
            if not line.startswith("@@ "):
                continue  # a plugin printing to our stdout
            try:
                msg = json.loads(line[3:])
            except ValueError:
                continue
            if "re" in msg:
                q = self._replies.get(str(msg["re"]))
                if q is not None:
                    q.put(msg)
            elif msg.get("ev") == "ready":
                self.ready = msg
                self._ready.set()
            elif msg.get("ev") == "tick":
                self.tick = msg
            elif msg.get("ev") == "crash":
                self.crash = msg
            elif msg.get("ev") == "warnings":
                self.late_warnings.extend(str(w) for w in msg.get("warnings", []))
            if "ev" in msg and self.on_event:
                self.on_event(msg)
        # the process is gone: whoever waits for an answer gets one now instead of at their timeout
        where = f" (crashed in {self.crash.get('at')})" if self.crash else ""
        for q in list(self._replies.values()):
            q.put({"error": f"the engine stopped{where}"})

    def alive(self) -> bool:
        return self.proc.poll() is None

    def call(self, op: str, timeout: float = 30.0, **args) -> dict:
        if not self.alive():
            raise EngineError("the live engine is not running")
        with self._lock:
            self._n += 1
            cid = str(self._n)
            q: queue.Queue = queue.Queue()
            self._replies[cid] = q
            self.proc.stdin.write(json.dumps({"op": op, "id": cid, **args}) + "\n")
            self.proc.stdin.flush()
        try:
            msg = q.get(timeout=timeout)
        except queue.Empty:
            raise EngineError(f"the live engine did not answer '{op}' within {timeout:.0f}s") from None
        finally:
            self._replies.pop(cid, None)
        if msg.get("error"):
            raise EngineError(str(msg["error"]))
        return msg

    def load(self, p: Project, progress=None, tracking: dict | None = None) -> dict:
        """Send the project's current state. Devices that did not change keep playing; returns the engine's
        answer with the spec's own warnings added."""
        spec, warnings = build_spec(p, progress, tracking)
        res = self.load_spec(spec, p.root / "cache" / "live-spec.json")
        res["warnings"] = warnings + list(res.get("warnings", []))
        return res

    def load_spec(self, spec: dict, path: Path) -> dict:
        """Load a spec already built (written to `path` on the way)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(spec), encoding="utf-8")
        res = self.call("load", timeout=600, path=str(path))
        self.sr = spec["sr"]
        if res.get("device"):
            self.ready.update(res["device"])  # the device may have changed rate to match the project
        # strips that have notes: a render in which one of them is silent started too early. Switched-off strips
        # count only when their audio is tapped (Engine.render's `taps`).
        self.expect = {t["id"]: (t["name"], t["on"]) for t in spec["tracks"] if (t["source"] or {}).get("events")}
        return res

    def play(self, from_sample: int | None = None, loop: bool | None = None) -> dict:
        args = {}
        if from_sample is not None:
            args["from"] = int(from_sample)
        if loop is not None:
            args["loop"] = bool(loop)
        return self.call("play", **args)

    def stop(self) -> dict:
        return self.call("stop")

    def render(self, out: str | Path | None, length: int | None = None, retries: int = 2,
               taps: dict[str, str] | None = None) -> dict:
        """Render offline: the master to `out` (None: no master file) and, for each strip in `taps`, its audio
        before the fader to a file of its own (32-bit float WAV, lined up with the song, `length` samples).

        The engine plays each new plugin instrument a test note until it sounds settled before it renders (the
        answer's `unready` names those that never did). As a backstop, a render that leaves a strip with notes
        silent is given more time and done again; strips still silent after that are named in `silent`."""
        import time
        args: dict = {"out": str(out) if out else ""}
        if length is not None:
            args["length"] = int(length)
        if taps:
            args["taps"] = {k: str(v) for k, v in taps.items()}
        expect = getattr(self, "expect", {})
        for attempt in range(retries + 1):
            res = self.call("render", timeout=3600, **args)
            if taps:
                silent = [tid for tid in expect if tid in taps and float((res.get("taps") or {}).get(tid, 1.0)) < 1e-7]
            else:
                silent = [tid for tid, (_name, on) in expect.items()
                          if on and max((res.get("peaks") or {}).get(tid, [1.0])[:2]) < 1e-7]
            if not silent or attempt == retries:
                break
            time.sleep(4.0)
        if silent:
            res["silent"] = [expect[tid][0] for tid in silent]
            res["silent_ids"] = silent
        return res

    def close(self) -> None:
        if self.alive():
            try:
                self.proc.stdin.write('{"op":"quit"}\n')
                self.proc.stdin.flush()
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                self.proc.kill()


class Player:
    """One engine kept in step with a project folder: `sync()` reloads when project.json has a new revision, so
    whatever edits it (an agent's tools, the editor, a take being accepted) is heard without stopping."""

    def __init__(self, path: str | Path, device: str | None = None, on_event=None, per_sample: bool | None = None,
                 no_device: bool = False):
        self.path = Path(path)
        # per_sample: built-in synths behave as in the offline render (what agents measure and what gets exported)
        self.engine = Engine(device=device, no_device=no_device, on_event=on_event,
                             per_sample=synth_per_sample() if per_sample is None else per_sample)
        self.rev = -1
        self.project: Project | None = None
        self.loaded: dict = {}
        self.tracking: dict = {}  # {track id: {"arm", "monitor"}}: see build_spec
        self.sync(force=True)

    def sync(self, force: bool = False) -> dict | None:
        """Reload if the project changed. Returns the engine's answer when it did, else None."""
        rev = read_rev(self.path)
        if not force and rev == self.rev:
            return None
        self.project = Project.load(self.path)
        self.rev = rev
        self.loaded = self.engine.load(self.project, tracking=self.tracking)
        return self.loaded

    def track(self, tracking: dict) -> dict | None:
        """Set what is armed and monitored; reloads (no device is reloaded for it) when that changed."""
        tracking = {k: dict(v) for k, v in tracking.items() if v.get("arm") or v.get("monitor")}
        if tracking == self.tracking:
            return None
        self.tracking = tracking
        return self.sync(force=True)

    def point(self, path: str | Path) -> dict:
        """Play another project folder (a take of the session, say) on the same engine."""
        self.path = Path(path)
        return self.sync(force=True)

    def sample(self, beat: float) -> int:
        p = self.project
        return int(round(p.tempo_map().sec(beat) * p.sample_rate))

    def play(self, beat: float | None = None, loop: bool | None = None) -> dict:
        return self.engine.play(None if beat is None else self.sample(beat), loop)

    def stop(self) -> dict:
        return self.engine.stop()

    def close(self) -> None:
        self.engine.close()


# ---- checking the engine against the offline renderer ---------------------------------------------------------

def compare(ref: np.ndarray, got: np.ndarray, sr: int) -> dict:
    """How far `got` is from `ref` (both [2, n]): the level of the difference against the level of the reference
    (-inf dB = identical), and the shift in samples that lines them up best."""
    n = min(ref.shape[1], got.shape[1])
    a, b = ref[:, :n].astype(np.float64), got[:, :n].astype(np.float64)

    def db(x: float) -> float:
        return round(20 * np.log10(max(x, 1e-12)), 1)

    ref_rms = float(np.sqrt(np.mean(a ** 2)))
    out = {"ref_rms_db": db(ref_rms), "got_rms_db": db(float(np.sqrt(np.mean(b ** 2)))),
           "ref_peak_db": db(float(np.abs(a).max(initial=0))), "got_peak_db": db(float(np.abs(b).max(initial=0)))}
    if ref_rms < 1e-9:
        out["residual_db"] = db(float(np.sqrt(np.mean(b ** 2))))
        return out
    out["residual_db"] = round(db(float(np.sqrt(np.mean((a - b) ** 2)))) - db(ref_rms), 1)
    out.update(_likeness(a, b, sr))
    # best alignment within +-64 samples, on a mono mix of the loudest 4 seconds
    m, g = a.mean(axis=0), b.mean(axis=0)
    w = min(n, 4 * sr)
    start = int(np.argmax(np.convolve(np.abs(m[: n - w + 1: 1024]), np.ones(max(1, w // 1024)), "valid"))) * 1024 \
        if n > w else 0
    seg_m = m[start:start + w]
    best = (0, -1.0)
    for lag in range(-64, 65):
        lo, hi = max(0, start + lag), min(n, start + lag + w)
        seg_g = g[lo:hi]
        k = min(len(seg_m), len(seg_g))
        if k < 16:
            continue
        c = float(np.dot(seg_m[:k], seg_g[:k]))
        if c > best[1]:
            best = (lag, c)
    out["best_lag"] = best[0]
    if best[0]:
        s = best[0]
        bb = np.roll(b, -s, axis=1)
        out["residual_db_aligned"] = round(db(float(np.sqrt(np.mean((a - bb) ** 2)))) - db(ref_rms), 1)
    return out


def _likeness(a: np.ndarray, b: np.ndarray, sr: int) -> dict:
    """For sources that never repeat sample for sample (round-robin samples, free-running oscillators): the
    largest gap between the two long-term octave-band levels, and how alike the loudness curves are (1 = same)."""
    ma, mb = a.mean(axis=0), b.mean(axis=0)
    n = len(ma)
    win = 1 << 14
    if n < win * 2:
        return {}
    k = (n // win) * win
    sa = (np.abs(np.fft.rfft(ma[:k].reshape(-1, win) * np.hanning(win), axis=1)) ** 2).mean(axis=0)
    sb = (np.abs(np.fft.rfft(mb[:k].reshape(-1, win) * np.hanning(win), axis=1)) ** 2).mean(axis=0)
    freqs = np.fft.rfftfreq(win, 1 / sr)
    gaps = []
    total = sa.sum()
    for lo in (31.25 * 2 ** i for i in range(9)):
        band = (freqs >= lo) & (freqs < lo * 2)
        ea, eb = sa[band].sum(), sb[band].sum()
        if ea > total * 1e-4:  # ignore bands with next to nothing in them
            gaps.append(abs(10 * np.log10(max(eb, 1e-30) / ea)))
    hop = sr // 20
    m = (n // hop) * hop
    ea = np.sqrt((ma[:m].reshape(-1, hop) ** 2).mean(axis=1))
    eb = np.sqrt((mb[:m].reshape(-1, hop) ** 2).mean(axis=1))
    corr = float(np.corrcoef(ea, eb)[0, 1]) if ea.std() > 0 and eb.std() > 0 else 1.0
    return {"band_gap_db": round(float(max(gaps, default=0.0)), 2), "envelope_corr": round(corr, 4)}


def null_tracks(p: Project, per_sample: bool = True, only: list[str] | None = None, progress=None) -> dict:
    """The same check one track at a time, before the fader (instrument + inserts only), to find which device
    the two engines disagree on. Returns {track name: compare()}."""
    import tempfile
    r = Renderer(p, progress, backend="dawdreamer")
    r.run()
    spec, _w = build_spec(p, progress)
    by_id = {t["id"]: t for t in spec["tracks"]}
    tmp = Path(tempfile.mkdtemp(prefix="tunetrack_null_"))
    out: dict = {}
    eng = Engine(no_device=True, per_sample=per_sample)
    try:
        for t in p.tracks:
            if t.id not in r.pre or (only and t.name not in only and t.id not in only):
                continue
            solo = {**by_id[t.id], "db": 0.0, "pan": 0.0, "vol_auto": [], "pan_auto": [], "out": "master",
                    "sends": [], "on": True}
            keys = [{**by_id[d["key"]], "on": False, "out": "master", "sends": []} for d in solo["fx"]
                    if d.get("key") in by_id]
            one = {**spec, "tracks": [solo] + keys, "master": {"db": 0.0, "fx": []}}
            path = tmp / "spec.json"
            path.write_text(json.dumps(one), encoding="utf-8")
            eng.call("load", timeout=600, path=str(path))
            wav = tmp / f"{t.id}.wav"
            eng.render(wav, r.n)
            got, _ = sf.read(str(wav), dtype="float32", always_2d=True)
            src = by_id[t.id]["source"] or {}
            out[t.name] = {"device": src.get("name") or src.get("template") or src.get("type"),
                           "fx": [d.get("name") for d in solo["fx"]], **compare(r.pre[t.id], got.T, r.sr)}
    finally:
        eng.close()
    return out


def null_test(p: Project, per_sample: bool = True, keep: str | Path | None = None, progress=None) -> dict:
    """Render the project's raw master (before loudness normalisation and the limiter) with both engines and
    report the difference."""
    import tempfile
    r = Renderer(p, progress, backend="dawdreamer")
    report = r.run()
    eng = Engine(no_device=True, per_sample=per_sample)
    try:
        loaded = eng.load(p, progress)
        out = Path(keep) if keep else Path(tempfile.mkdtemp(prefix="tunetrack_null_")) / "engine.wav"
        info = eng.render(out, r.n)
    finally:
        eng.close()
    got, _ = sf.read(str(out), dtype="float32", always_2d=True)
    res = compare(r.master, got.T, r.sr)
    # strip by strip, after the fader: where the two engines' peaks are more than 1 dB apart
    apart = {}
    names = {t.id: t.name for t in p.tracks}
    for tid, lr in (info.get("peaks") or {}).items():
        if len(lr) > 2:
            res.setdefault("bad_samples", {})[names.get(tid, tid)] = lr[2]
        if tid in r.post:
            a, b = float(np.abs(r.post[tid]).max(initial=0)), float(max(lr[:2]))
            da, dbb = 20 * np.log10(max(a, 1e-9)), 20 * np.log10(max(b, 1e-9))
            if abs(da - dbb) > 1.0 and max(da, dbb) > -80:
                apart[names.get(tid, tid)] = [round(da, 1), round(dbb, 1)]
    res["strips_apart_db"] = apart  # name: [offline peak, engine peak]
    res.update(engine_seconds=round(float(info.get("seconds", 0)), 1), offline_seconds=report.get("render_seconds"),
               latency=info.get("latency"), engine_file=str(out),
               warnings=loaded.get("warnings", []) + [f"'{n}' rendered silent in the live engine" for n in info.get("silent", [])])
    return res
