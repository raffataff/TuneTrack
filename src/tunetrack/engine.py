"""Rendering: per-track renders (cached, parallel, isolated in worker processes), mixing (volume/pan automation,
sends, buses), master chain, loudness normalisation, limiting, seamless loops, stems and layer exports."""
from __future__ import annotations

import hashlib
import json
import math
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import soundfile as sf

from . import analysis, config, eqmap, plugins
from .model import Device, Project, Track, file_lock
from .synths import fx_dsp, synth_dsp


# ---- device resolution ------------------------------------------------------------------------------------

def resolve_device(dev: Device, p: Project, instrument: bool) -> dict | None:
    """Device -> fully resolved spec the worker can load (paths, states, compiled DSP text)."""
    if dev.bypass:
        return None
    if dev.type == "synth":
        return {"type": "synth", "dsp": synth_dsp(dev.name, dev.params), "voices": 32}
    if dev.type == "builtin" and dev.name == "eq":
        eng = eqmap.engine_for(dev, p)
        entry = eqmap.plugin_entry(eng)
        if entry:  # the same settings on SSL Channel Strip 2 / TDR Nova (eqmap.warnings reports a missing plugin)
            return {"type": "vst3", "path": entry["path"], "host": entry["host"], "shell": entry.get("shell"),
                    "state": None, "params": eqmap.translate(eng, dev.params)[0], "name": entry["name"]}
    if dev.type == "builtin":
        spec = {"type": "faust", "dsp": fx_dsp(dev.name, dev.params, p.tempo)}
        if dev.sidechain and not instrument:
            spec["dsp_sc"] = fx_dsp(dev.name, dev.params, p.tempo, sidechain=True)
            spec["sidechain"] = dev.sidechain  # a track id; Renderer swaps it for the key track's render
        return spec
    if dev.type == "sfz":
        path = Path(dev.name)
        if not path.is_absolute():
            for base in [p.root] + [Path(d) for d in config.load_config().get("sample_dirs", [])]:
                if base and (Path(base) / path).exists():
                    path = Path(base) / path
                    break
        if not path.exists():
            raise FileNotFoundError(f"SFZ file not found: {dev.name}")
        return {"type": "sfz", "path": str(path.resolve())}
    if dev.type == "vst3":
        state = None
        name = dev.name
        extra = {}
        if dev.patch:
            meta = plugins.get_patch(dev.patch)
            name = meta["plugin"]
            sfile = plugins.state_file(meta)
            state = str(sfile) if sfile and sfile.exists() else None
            if meta.get("channel"):  # a slot of a multitimbral plugin (HALion: slot N listens on channel N)
                extra["channel"] = int(meta["channel"])
        entry = plugins.resolve(name)
        if instrument and entry["kind"] != "instrument":
            raise ValueError(f"{entry['name']} is an effect, not an instrument")
        if dev.sidechain and not instrument:
            extra["sidechain"] = dev.sidechain  # a track id; Renderer swaps it for the key track's render
        return {"type": "vst3", "path": entry["path"], "host": entry["host"], "shell": entry.get("shell"),
                "state": state, "params": dev.params, "name": entry["name"], **extra}
    raise ValueError(f"Unknown device type {dev.type!r}")


def offline_backend() -> str:
    """Who renders offline: "engine" (the live engine in engine/, so agents measure what the owner hears) or
    "dawdreamer" (the original worker). The library config's `offline_engine` picks; "auto" (the default) is the
    engine when it is built."""
    want = str(config.load_config().get("offline_engine", "auto")).lower()
    if want == "dawdreamer":
        return "dawdreamer"
    from .live import BLOCKED, engine_exe, engine_runs
    if engine_exe() is None:
        if want == "engine":
            raise RuntimeError("offline_engine is 'engine' but the engine is not built (see engine/README.md)")
        return "dawdreamer"
    if not engine_runs():
        if want == "engine":
            raise RuntimeError(BLOCKED)
        return "dawdreamer"
    return "engine"


def _worker_only(spec: dict) -> bool:
    """A job the worker renders whoever renders the rest: an SFZ instrument with no inserts (TuneTrack's own SFZ
    player; the engine plays the result as a stem)."""
    return (spec.get("instrument") or {}).get("type") == "sfz" and not spec.get("fx")


def _fingerprint(obj) -> str:
    def files(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("state", "path", "audio_in") and isinstance(v, str) and Path(v).exists():
                    st = Path(v).stat()
                    yield f"{v}:{st.st_mtime_ns}:{st.st_size}"
                else:
                    yield from files(v)
        elif isinstance(o, list):
            for v in o:
                yield from files(v)
    blob = json.dumps(obj, sort_keys=True, default=str) + "|".join(files(obj)) + config.ENGINE_VERSION
    return hashlib.sha1(blob.encode()).hexdigest()[:20]


# ---- MIDI ------------------------------------------------------------------------------------------------

def _track_events(p: Project, t: Track, tpb: int, channel: int = 0) -> tuple[list, int]:
    """(tick, order, message) events for a track's notes, CCs and pitch bend; notes past a clip's end are hidden."""
    import mido
    events = []
    count = 0
    for c in t.clips:
        if c.muted or c.kind != "midi":
            continue
        for n in c.notes:
            if n.start >= c.length - 1e-9:
                continue
            on = c.start + n.start
            off = c.start + min(n.end, c.length + 8)
            if off <= on:
                continue
            events.append((int(round(on * tpb)), 2, mido.Message("note_on", note=int(n.pitch), velocity=int(n.vel), channel=channel)))
            events.append((int(round(off * tpb)), 1, mido.Message("note_off", note=int(n.pitch), velocity=0, channel=channel)))
            count += 1
        for lane, pts in c.cc.items():
            for b, v in pts:
                tick = int(round((c.start + b) * tpb))
                if lane == "bend":
                    msg = mido.Message("pitchwheel", pitch=int(max(-8192, min(8191, v * 8191))), channel=channel)
                else:
                    msg = mido.Message("control_change", control=int(lane), value=int(max(0, min(127, v))), channel=channel)
                events.append((tick, 0, msg))
    return events, count


def _tempo_events(p: Project, tpb: int) -> list:
    import mido
    tm = p.tempo_map()
    return [(int(round(b * tpb)), 0, mido.MetaMessage("set_tempo", tempo=mido.bpm2tempo(bpm))) for b, bpm in zip(tm.beats, tm.bpms)]


def _to_track(events: list, head: list | None = None):
    import mido
    tr = mido.MidiTrack()
    for m in head or []:
        tr.append(m)
    last = 0
    for tick, _o, msg in sorted(events, key=lambda e: (e[0], e[1])):
        tr.append(msg.copy(time=tick - last))
        last = tick
    return tr


def write_midi(p: Project, t: Track, path: Path, channel: int = 0) -> int:
    """Write a track's clips to a type-0 MIDI file with the project's tempo map. Returns the note count."""
    import mido
    tpb = 960
    events, count = _track_events(p, t, tpb, channel)
    mf = mido.MidiFile(ticks_per_beat=tpb)
    mf.tracks.append(_to_track(_tempo_events(p, tpb) + events))
    mf.save(str(path))
    return count


# ---- audio helpers -------------------------------------------------------------------------------------------

def _read(path: Path, sr: int) -> np.ndarray:
    data, fsr = sf.read(str(path), dtype="float32", always_2d=True)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    data = data[:, :2].T
    if fsr != sr:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(sr, fsr)
        data = resample_poly(data, sr // g, fsr // g, axis=1).astype(np.float32)
    return data


def audio_track_input(p: Project, t: Track, n: int) -> np.ndarray:
    sr = p.sample_rate
    tm = p.tempo_map()
    buf = np.zeros((2, n), dtype=np.float32)
    for c in t.clips:
        if c.muted or c.kind != "audio" or not c.file:
            continue
        f = Path(c.file)
        if not f.is_absolute():
            f = p.root / f
        data = _read(f, sr)
        a = int(round(c.offset * sr))
        start = int(round(tm.sec(c.start) * sr))
        length = int(round((tm.sec(c.end) - tm.sec(c.start)) * sr))
        seg = data[:, a:a + length].copy()
        if seg.shape[1] == 0:
            continue
        seg *= 10 ** (c.gain_db / 20)
        k = min(int(c.fade_in * sr), seg.shape[1])
        if k > 0:
            seg[:, :k] *= np.linspace(0, 1, k)[None, :]
        k = min(int(c.fade_out * sr), seg.shape[1])
        if k > 0:
            seg[:, -k:] *= np.linspace(1, 0, k)[None, :]
        end = min(n, start + seg.shape[1])
        if start < n:
            buf[:, start:end] += seg[:, : end - start]
    return buf


def _env(points: list[list[float]], p: Project, n: int, default: float) -> np.ndarray | float:
    if not points:
        return default
    tm = p.tempo_map()
    xs = np.array([tm.sec(b) * p.sample_rate for b, _ in sorted(points)])
    ys = np.array([v for _, v in sorted(points)])
    return np.interp(np.arange(n), xs, ys).astype(np.float32)


def _pan(audio: np.ndarray, pan) -> np.ndarray:
    """Balance-style pan for stereo sources: centre = unity, hard side = other channel silent."""
    pan = np.clip(pan, -1, 1)
    gl = np.minimum(1.0, 1.0 - pan)
    gr = np.minimum(1.0, 1.0 + pan)
    return np.stack([audio[0] * gl, audio[1] * gr])


def limiter(audio: np.ndarray, sr: int, ceiling_db: float = -1.0, lookahead_ms: float = 5.0) -> tuple[np.ndarray, float]:
    """Brick-wall lookahead limiter (no overs at sample level). Returns (audio, max gain reduction dB)."""
    from scipy.ndimage import minimum_filter1d, uniform_filter1d
    ceil = 10 ** (ceiling_db / 20)
    peak = np.abs(audio).max(axis=0)
    req = np.minimum(1.0, ceil / np.maximum(peak, 1e-9))
    if req.min() >= 1.0:
        return audio, 0.0
    L = max(1, int(lookahead_ms * sr / 1000))
    g = minimum_filter1d(req, size=2 * L + 1, mode="nearest")
    hold = int(0.03 * sr)
    g = minimum_filter1d(g, size=hold, mode="nearest", origin=(hold - 1) // 2)  # hold ~30 ms after each peak
    g = uniform_filter1d(g, size=L, mode="nearest")
    g = np.minimum(g, req)
    return (audio * g[None, :]).astype(np.float32), float(-20 * math.log10(max(g.min(), 1e-9)))


def write_audio(path: Path, audio: np.ndarray, sr: int, fmt: str = "wav", bits: int = 24,
                loop: tuple[int, int] | None = None) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.clip(audio.T, -1, 1)
    fmt = fmt.lower()
    if fmt == "wav":
        sf.write(str(path), data, sr, subtype={16: "PCM_16", 24: "PCM_24", 32: "FLOAT"}[bits])
        if loop:
            _add_smpl_chunk(path, sr, loop[0], loop[1])
    elif fmt == "flac":
        sf.write(str(path), data, sr, subtype="PCM_24" if bits >= 24 else "PCM_16")
    elif fmt in ("ogg", "mp3"):
        # libsndfile crashes on large single writes to lossy encoders; feed it in blocks
        kind = ("OGG", "VORBIS") if fmt == "ogg" else ("MP3", "MPEG_LAYER_III")
        with sf.SoundFile(str(path), "w", sr, data.shape[1], format=kind[0], subtype=kind[1]) as f:
            for i in range(0, len(data), 8192):
                f.write(np.ascontiguousarray(data[i:i + 8192], dtype=np.float32))
    else:
        raise ValueError("format must be wav, ogg, flac or mp3")
    return str(path)


def _add_smpl_chunk(path: Path, sr: int, start: int, end: int) -> None:
    """Embed a forward loop (start..end-1, in samples) so engines/samplers that read 'smpl' loop it natively."""
    body = struct.pack("<9I", 0, 0, int(1e9 / sr), 60, 0, 0, 0, 1, 0) + struct.pack("<6I", 0, 0, start, end - 1, 0, 0)
    with open(path, "r+b") as f:
        f.seek(0, 2)
        f.write(b"smpl" + struct.pack("<I", len(body)) + body)
        size = f.tell()
        f.seek(4)
        f.write(struct.pack("<I", size - 8))


def loop_wrap(audio: np.ndarray, ls: int, le: int, keep_intro: bool) -> np.ndarray:
    """Cut audio to [0 or ls, le) and fold everything after le (release/reverb tails) back onto the loop start,
    so the file loops with no gap or cut-off tails."""
    tail = audio[:, le:]
    body = audio[:, (0 if keep_intro else ls):le].copy()
    at = ls if keep_intro else 0
    span = le - ls
    off = 0
    while off < tail.shape[1] and span > 0:
        k = min(span, tail.shape[1] - off)
        body[:, at:at + k] += tail[:, off:off + k]
        off += k
    return body


# ---- main render --------------------------------------------------------------------------------------------

@contextmanager
def _cache_entry(out: Path):
    """Render one cache file safely when several processes share the cache (agents working in takes, the editor).

    Yields the path to write to, or None when someone else finished the same render while we waited for the lock.
    The file gets its real name only once it is complete, so a killed worker never leaves a half-written entry
    that later looks like a finished render."""
    lock = out.with_suffix(".lock")
    with file_lock(lock, timeout=1900.0):
        if out.exists():
            yield None
        else:
            part = out.with_name(f"{out.stem}.part{out.suffix}")
            try:
                yield part
                if part.exists():
                    part.replace(out)
            finally:
                part.unlink(missing_ok=True)
    try:
        lock.unlink()
    except OSError:  # another process is waiting on it
        pass


class Renderer:
    def __init__(self, p: Project, progress=None, backend: str | None = None):
        self.p = p
        self.sr = p.sample_rate
        self.tm = p.tempo_map()
        self.progress = progress or (lambda msg: None)
        cache_bars = 4 * p.beats_per_bar
        end = max(p.end, p.beats_per_bar)
        self.render_end_beat = math.ceil(end / cache_bars - 1e-9) * cache_bars
        self.duration = self.tm.sec(self.render_end_beat) + p.tail
        self.n = int(round(self.duration * self.sr))
        self.cache = p.root / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.key_fp: dict[str, str] = {}  # track id -> render cache key, for tracks that key a sidechain
        self.failed: set[str] = set()
        self.backend = backend or offline_backend()
        self.by = None  # what rendered it, in every engine cache key (None: the worker, keys as they always were)
        if self.backend == "engine":
            from .live import engine_stamp
            self.by = engine_stamp()
        self._fx_eng = None  # the engine bus and master chains render in, kept for one run() or export()

    def key(self, spec: dict) -> str:
        """The cache key of a render spec. The engine's renders are kept apart from the worker's (a voice or a
        sample may differ), and from those of another engine build."""
        if self.by and not _worker_only(spec):
            return _fingerprint({**spec, "rendered_by": self.by})
        return _fingerprint(spec)

    def close(self) -> None:
        if self._fx_eng is not None:
            self._fx_eng.close()
            self._fx_eng = None

    def _base(self) -> dict:
        return {"sr": self.sr, "bpm": self.p.tempo, "tempo": [self.tm.beats, self.tm.bpms], "duration": round(self.duration, 4)}

    def _key_for(self, kid: str, owner: str, warnings: list | None) -> str:
        """The render fingerprint of a sidechain key track, or "silent" when it has no audio."""
        fp = self.key_fp.get(kid)
        if fp and kid not in self.failed:
            return fp
        if warnings is not None:
            name = next((t.name for t in self.p.tracks if t.id == kid), kid)
            warnings.append(f"{owner}: sidechain key '{name}' has no audio (no notes, or it failed to render); "
                            f"the effect hears silence")
        return "silent"

    def _job_fx(self, fx: list[dict]) -> list[dict]:
        """FX specs as the worker reads them: sidechain keys become the key track's cached render file."""
        out = []
        for d in fx:
            d = {k: v for k, v in d.items() if k != "sidechain"}  # an unlinked key (direct render_track calls)
            k = d.pop("sidechain_key", None)
            if k:
                d["sidechain_in"] = "silent" if k == "silent" else str(self.cache / f"{k}.wav")
            out.append(d)
        return out

    def track_spec(self, t: Track) -> dict | None:
        """Resolved, hashable description of everything that affects a track's pre-fader audio."""
        fx = [s for s in (resolve_device(d, self.p, False) for d in t.fx) if s]
        spec = {**self._base(), "fx": fx}
        if t.kind == "midi":
            if not t.instrument or not any(c.notes for c in t.clips if not c.muted):
                return None
            spec["instrument"] = resolve_device(t.instrument, self.p, True)
            spec["clips"] = [c.to_json() for c in t.clips if not c.muted]
            for c in spec["clips"]:
                c.pop("name", None), c.pop("color", None), c.pop("id", None)
        elif t.kind == "audio":
            clips = [c for c in t.clips if not c.muted and c.kind == "audio"]
            if not clips:
                return None
            spec["clips"] = [{**c.to_json(), "mtime": (self.p.root / c.file).stat().st_mtime_ns
                              if c.file and (self.p.root / c.file).exists() else None} for c in clips]
        return spec

    def render_track(self, t: Track, spec: dict) -> tuple[np.ndarray, dict]:
        """A track's audio before the fader (instrument and inserts), from the cache or rendered now."""
        key = self.key(spec)
        out = self.cache / f"{key}.wav"
        info = {"cached": out.exists(), "key": key}
        audio = None
        if not out.exists():
            with _cache_entry(out) as part:
                if part is None:  # another process (a second agent's take, the editor) rendered it while we waited
                    info["cached"] = True
                elif self.backend == "engine" and not _worker_only(spec):
                    res = self._engine_tracks([(t, spec, part)])
                    got = res["tracks"][t.id]
                    if got.get("error"):
                        raise RuntimeError(got["error"])
                    audio = got.pop("audio", None)
                    info.update(got, warnings=got.get("warnings", []) + res["warnings"])
                else:
                    info.update(self._worker_track(t, spec, key, part))
        return self._fit(_read(out, self.sr) if audio is None else audio), info

    def _fit(self, audio: np.ndarray) -> np.ndarray:
        if audio.shape[1] < self.n:
            audio = np.pad(audio, ((0, 0), (0, self.n - audio.shape[1])))
        return audio[:, : self.n]

    def _worker_track(self, t: Track, spec: dict, key: str, part: Path) -> dict:
        """Render one track in a worker process (DawDreamer, or the SFZ player) into `part`."""
        job = {k: v for k, v in spec.items() if k not in ("clips",)}
        job["fx"] = self._job_fx(spec.get("fx", []))
        job["out"] = str(part)
        if t.kind == "midi":
            midi = self.cache / f"{key}.mid"
            write_midi(self.p, t, midi, (spec.get("instrument") or {}).get("channel", 1) - 1)
            job["midi"] = str(midi)
        else:
            inp = self.cache / f"{key}.in.wav"
            sf.write(str(inp), audio_track_input(self.p, t, self.n).T, self.sr, subtype="FLOAT")
            job["audio_in"] = str(inp)
            job.pop("instrument", None)
        jf = self.cache / f"{key}.job.json"
        jf.write_text(json.dumps(job), encoding="utf-8")
        t0 = time.time()
        try:
            res = plugins.run_worker("render", str(jf), timeout=1800)
        finally:
            jf.unlink(missing_ok=True)
            (self.cache / f"{key}.in.wav").unlink(missing_ok=True)
        return {"seconds": round(time.time() - t0, 1), "warnings": res.get("warnings", [])}

    # ---- rendering in the engine ------------------------------------------------------------------------------

    def _engine_spec(self, strips: list[dict]) -> dict:
        """An engine spec for offline jobs: every strip switched off and sent nowhere that matters, so the only
        product is what the taps write (each strip's audio before its fader)."""
        from . import live
        head = live.spec_head(self)
        quiet = {"db": 0.0, "pan": 0.0, "vol_auto": [], "pan_auto": [], "out": "master", "sends": [], "on": False}
        return {**head, "loop": None, "tracks": [{**s, **quiet} for s in strips], "master": {"db": 0.0, "fx": []}}

    def _key_stem(self, kid: str) -> str | None:
        """The rendered pre-fader file of a sidechain key track, when there is one."""
        k = self.key_fp.get(kid)
        if not k or k == "silent" or kid in self.failed:
            return None
        f = self.cache / f"{k}.wav"
        return str(f) if f.exists() else None

    def _engine_tracks(self, batch: list[tuple[Track, dict, Path]]) -> dict:
        """Render tracks before the fader in the engine: batch = (track, render spec, file to write). Tracks that
        key each other's sidechains share an engine; the rest are spread over `render_workers` engines that run at
        once. Returns {"tracks": {id: info}, "warnings": [...]}: an info carries `error` when the track failed, and
        `audio` (the render, not kept in the cache) when it came out silent or its instrument never settled, so the
        next render tries again instead of reading a bad file forever."""
        from . import live
        out: dict = {"tracks": {}, "warnings": []}
        jobs = {t.id: (t, spec, part) for t, spec, part in batch}
        strips: dict[str, dict] = {}

        # what the worker renders goes first and in parallel, as it always did: SFZ tracks without inserts
        # (finished here) and the dry SFZ stems the engine plays under the inserts of the others
        def worker_side(tid: str) -> None:
            t, spec, part = jobs[tid]
            try:
                if _worker_only(spec):
                    out["tracks"][tid] = self._worker_track(t, spec, self.key(spec), part)
                else:
                    self.render_track(t, {**spec, "fx": []})
            except Exception as e:  # noqa: BLE001
                out["tracks"][tid] = {"error": str(e)}

        sfz = [tid for tid, (_t, spec, _p) in jobs.items() if (spec.get("instrument") or {}).get("type") == "sfz"]
        if sfz:
            with ThreadPoolExecutor(max_workers=max(1, int(config.load_config().get("render_workers", 4)))) as ex:
                list(ex.map(worker_side, sfz))
        for tid, (t, spec, part) in jobs.items():
            if tid in out["tracks"]:
                continue  # finished by the worker, or failed there
            w: list[str] = []
            try:
                strips[tid] = {"id": tid, "name": t.name, "kind": t.kind, **live.strip_source(self, t, spec, w)}
                out["tracks"][tid] = {"warnings": [x.removeprefix(f"track '{t.name}': ").removeprefix(f"track '{t.name}' ")
                                                   for x in w]}
            except Exception as e:  # noqa: BLE001
                out["tracks"][tid] = {"error": str(e)}
        if not strips:
            return out

        # groups: a track and every track whose audio keys one of its inserts render in one engine
        parent = {tid: tid for tid in strips}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        keys: dict[str, str] = {}  # key track id -> stem file, for keys rendered earlier
        for tid, s in strips.items():
            for d in s["fx"]:
                kid = d.get("key")
                if not kid or kid == "silent":
                    continue
                if kid in strips:
                    parent[find(kid)] = find(tid)
                elif (f := self._key_stem(kid)) is not None:
                    keys[kid] = f
                else:
                    d["key"] = "silent"
        groups: dict[str, list[str]] = {}
        for tid in strips:
            groups.setdefault(find(tid), []).append(tid)
        workers = max(1, min(int(config.load_config().get("render_workers", 4)), len(groups)))
        loads: list[list[str]] = [[] for _ in range(workers)]
        weight = lambda ids: sum(3 if strips[i]["source"].get("type") == "vst3" else 1 for i in ids)  # noqa: E731
        for ids in sorted(groups.values(), key=weight, reverse=True):
            min(loads, key=weight).extend(ids)

        def run(ids: list[str]) -> None:
            names = {i: strips[i]["name"] for i in ids}
            group = [strips[i] for i in ids]
            for kid, f in keys.items():
                if any(d.get("key") == kid for i in ids for d in strips[i]["fx"]):
                    group.append({"id": kid, "name": kid, "kind": "audio", "source": {"type": "stem", "file": f},
                                  "fx": []})
            res = self._engine_render(self._engine_spec(group), {i: jobs[i][2] for i in ids}, names, None)
            for i in ids:
                info = out["tracks"][i]
                if res.get("error"):
                    info["error"] = res["error"]
                    continue
                info["seconds"] = res["seconds"]
                info["warnings"] += res["by_strip"].get(i, [])
                peak = float(res["taps"].get(i, 0.0))
                if peak > 8:
                    info["warnings"].append(f"output peaked at {peak:.1f} (+{20 * math.log10(peak):.0f} dBFS) - "
                                            f"check gain staging/feedback")
                if i in res["unready"] or i in res["silent"]:
                    # heard this once, not kept: a plugin that had not finished loading must not stay silent
                    part = jobs[i][2]
                    info["audio"] = _read(part, self.sr) if part.exists() else np.zeros((2, self.n), np.float32)
                    part.unlink(missing_ok=True)
            out["warnings"].extend(res.get("other", []))

        if len(loads) == 1:
            run(loads[0])
        else:
            with ThreadPoolExecutor(max_workers=len(loads)) as ex:
                list(ex.map(run, [ids for ids in loads if ids]))
        return out

    def _engine_render(self, spec: dict, taps: dict[str, Path], names: dict[str, str], eng) -> dict:
        """Load `spec` into an engine (`eng`, or a new one closed afterwards) and render the taps. Returns
        {"taps": {id: peak}, "seconds", "unready": {id}, "silent": {id}, "by_strip": {id: [warnings]},
        "other": [warnings]} or {"error": text}; a failed render leaves no tap files behind."""
        from . import live
        import uuid
        own = eng is None
        path = self.cache / f"engine-{uuid.uuid4().hex[:10]}.json"
        t0 = time.time()
        seen = 0
        try:
            if own:
                eng = live.Engine(no_device=True, per_sample=live.synth_per_sample())
            seen = len(eng.late_warnings)
            loaded = eng.load_spec(spec, path)
            res = eng.render(None, self.n, taps={k: str(v) for k, v in taps.items()})
            events = eng.late_warnings[seen:]
        except Exception as e:  # noqa: BLE001 - a plugin that crashes the engine fails only these tracks
            for f in taps.values():
                Path(f).unlink(missing_ok=True)
            return {"error": str(e)}
        finally:
            path.unlink(missing_ok=True)
            if own and eng is not None:
                eng.close()
        by: dict[str, list[str]] = {}
        other: list[str] = []
        unready = set()
        for w in list(loaded.get("warnings", [])) + events + list(res.get("unready", [])):
            ref, _, text = str(w).partition(": ")
            sid, _, where = ref.partition("/")
            if sid in names and text:
                place = {"inst": "instrument"}.get(where, where.replace("/", " "))
                by.setdefault(sid, []).append(f"{place}: {text}")
                if w in res.get("unready", []):
                    unready.add(sid)
            else:
                other.append(str(w))
        return {"taps": res.get("taps") or {}, "seconds": round(time.time() - t0, 1), "unready": unready,
                "silent": set(res.get("silent_ids", [])), "by_strip": by, "other": other}

    def _engine_fx(self, audio: np.ndarray, devices: list[Device], owner: str, warnings: list | None,
                   part: Path) -> None:
        """Run `audio` through an insert chain (a bus's, the master's) in the engine, into `part`."""
        from . import live
        inp = part.with_name(f"{part.stem}.in.wav")
        sf.write(str(inp), audio.T, self.sr, subtype="FLOAT")
        w: list[str] = []
        fx = live.fx_chain(self.p, devices, owner, w)
        strips = [{"id": "in", "name": owner, "kind": "audio", "source": {"type": "stem", "file": str(inp)}, "fx": fx}]
        for d in fx:
            kid = d.get("key")
            if not kid or kid == "silent":
                continue
            f = self._key_stem(kid)
            if f is None:
                d["key"] = "silent"  # render_fx has said so (_key_for)
            elif not any(s["id"] == kid for s in strips):
                strips.append({"id": kid, "name": kid, "kind": "audio", "source": {"type": "stem", "file": f}, "fx": []})
        if self._fx_eng is None or not self._fx_eng.alive():
            self._fx_eng = live.Engine(no_device=True, per_sample=live.synth_per_sample())
        try:
            res = self._engine_render(self._engine_spec(strips), {"in": part}, {"in": owner}, self._fx_eng)
        finally:
            inp.unlink(missing_ok=True)
        if res.get("error"):
            self.close()  # it may be gone; the next chain starts a new one
            raise RuntimeError(f"{owner}: {res['error']}")
        if warnings is not None:
            warnings += w + [f"{owner}: {x}" for x in res["by_strip"].get("in", []) + res["other"]]

    def render_fx(self, audio: np.ndarray, devices: list[Device], tag: str, owner: str = "",
                  warnings: list | None = None) -> np.ndarray:
        specs = [s for s in (resolve_device(d, self.p, False) for d in devices) if s]
        if not specs:
            return audio
        for s in specs:
            if "sidechain" in s:
                s["sidechain_key"] = self._key_for(s.pop("sidechain"), owner or tag, warnings)
        key = self.key({**self._base(), "fx": specs, "in": hashlib.sha1(audio.tobytes()).hexdigest()})
        out = self.cache / f"{tag}-{key}.wav"
        if not out.exists():
            with _cache_entry(out) as part:
                if part is not None and self.backend == "engine":
                    self._engine_fx(audio, devices, owner or tag, warnings, part)
                elif part is not None:
                    inp = self.cache / f"{tag}-{key}.in.wav"
                    sf.write(str(inp), audio.T, self.sr, subtype="FLOAT")
                    jf = self.cache / f"{tag}-{key}.job.json"
                    jf.write_text(json.dumps({**self._base(), "fx": self._job_fx(specs), "audio_in": str(inp),
                                              "out": str(part)}), encoding="utf-8")
                    try:
                        res = plugins.run_worker("render", str(jf), timeout=1800)
                    finally:
                        jf.unlink(missing_ok=True)
                        inp.unlink(missing_ok=True)
                    if warnings is not None:
                        warnings += [f"{owner or tag}: {w}" for w in res.get("warnings", [])]
        return self._fit(_read(out, self.sr))

    def run(self) -> dict:
        p = self.p
        t0 = time.time()
        sources = [t for t in p.tracks if t.kind != "bus"]
        playing = soloed_sources(p)
        audible = [t for t in sources if not t.mute and t.id in playing]
        report: dict = {"tracks": {}, "warnings": []}
        # Sidechain keys play into the effect whatever their own mute/solo/fader say (so a muted "ghost kick" can
        # pump a pad), so they're rendered even when they aren't heard.
        keyed = sidechain_links(p)
        key_ids = {kid for _o, _d, kid in keyed}
        key_only = [t for t in sources if t.id in key_ids and t not in audible]
        if keyed:
            names = {t.id: t.name for t in p.tracks}
            report["sidechains"] = [f"{o}: {d.label()} <- {names.get(kid, kid)}" for o, d, kid in keyed]
        report["warnings"] += sidechain_switch_warnings(p)
        report["warnings"] += eqmap.warnings(p)
        specs = {}
        for t in audible + key_only:
            try:
                s = self.track_spec(t)
            except Exception as e:  # noqa: BLE001 - one broken track shouldn't stop the mix
                report["warnings"].append(f"track '{t.name}': {e}")
                continue
            if s is not None:
                specs[t.id] = s
        deps = self._link_sidechains(specs, report["warnings"])
        pre: dict[str, np.ndarray] = {}
        todo = [t for t in audible + key_only if t.id in specs]
        heard = {t.id for t in audible}
        self.progress(f"rendering {len(todo)} tracks")
        results = self._run_engine(todo, specs, report) if self.backend == "engine" else self._run_worker(todo, specs, deps)
        try:
            for t, audio, info, err in results:
                if err:
                    self.failed.add(t.id)
                    report["warnings"].append(f"track '{t.name}' failed to render: {err}")
                    continue
                for w in info.get("warnings", []):
                    report["warnings"].append(f"track '{t.name}': {w}")
                if t.id not in heard:
                    continue  # rendered only as a sidechain key
                pre[t.id] = audio
                report["tracks"][t.name] = {"render": "cached" if info["cached"] else f"{info.get('seconds')}s",
                                            **analysis.stem_stats(audio)}
                if float(np.abs(audio).max(initial=0)) < 1e-4:
                    report["warnings"].append(
                        f"track '{t.name}' rendered SILENT though it has notes - instrument loaded no sound "
                        f"(empty plugin slot? capture a patch with a program loaded) or notes out of its range")
            self.pre = pre
            self.master, self.post = self.mix(pre, report)
        finally:
            self.close()
        report["render_seconds"] = round(time.time() - t0, 1)
        self.report = report
        return report

    def _run_worker(self, todo: list[Track], specs: dict[str, dict], deps: dict[str, set[str]]):
        """(track, audio, info, error) per track, rendered by worker processes. A track renders once every track
        keying its sidechains has (the key's render is its input), so they go in rounds; the caller marks failures
        in self.failed between rounds."""
        workers = int(config.load_config().get("render_workers", 4))

        def job(t):
            bad = [x.name for x in self.p.tracks if x.id in deps.get(t.id, set()) & self.failed]
            if bad:
                return t, None, None, f"its sidechain key '{bad[0]}' failed to render"
            try:
                return t, *self.render_track(t, specs[t.id]), None
            except Exception as e:  # noqa: BLE001
                return t, None, None, str(e)

        finished: set[str] = set()
        while todo:
            ready = [t for t in todo if deps.get(t.id, set()) <= finished] or todo
            todo = [t for t in todo if t not in ready]
            with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
                for res in ex.map(job, ready):
                    finished.add(res[0].id)
                    yield res

    def _run_engine(self, todo: list[Track], specs: dict[str, dict], report: dict) -> list:
        """(track, audio, info, error) per track: cached renders read, every other track rendered in one engine
        batch (keys and the tracks they key in the same graph, so no rounds). Each cache file is locked while it
        is rendered, in name order so that two processes cannot wait on each other."""
        from contextlib import ExitStack
        keys = {t.id: self.key(specs[t.id]) for t in todo}
        cached = {t.id for t in todo if (self.cache / f"{keys[t.id]}.wav").exists()}
        infos: dict[str, dict] = {}
        with ExitStack() as stack:
            batch = []
            for t in sorted((t for t in todo if t.id not in cached), key=lambda t: keys[t.id]):
                part = stack.enter_context(_cache_entry(self.cache / f"{keys[t.id]}.wav"))
                if part is None:  # another process rendered it while we waited
                    cached.add(t.id)
                else:
                    batch.append((t, specs[t.id], part))
            if batch:
                res = self._engine_tracks(batch)
                infos = res["tracks"]
                report["warnings"] += res["warnings"]
                for t, _spec, part in batch:
                    if infos.get(t.id, {}).get("error"):
                        part.unlink(missing_ok=True)  # whatever a failed render left must not become the cache
        out = []
        for t in todo:
            info = {"cached": t.id in cached, "key": keys[t.id], **infos.get(t.id, {})}
            if info.get("error"):
                out.append((t, None, None, info["error"]))
                continue
            audio = info.pop("audio", None)
            try:
                audio = self._fit(audio if audio is not None else _read(self.cache / f"{keys[t.id]}.wav", self.sr))
            except Exception as e:  # noqa: BLE001 - a render that wrote nothing
                out.append((t, None, None, f"no audio came back ({e})"))
                continue
            out.append((t, audio, info, None))
        return out

    def _link_sidechains(self, specs: dict[str, dict], warnings: list) -> dict[str, set[str]]:
        """Replace each insert's sidechain track id with that track's render fingerprint (keys first, so a key
        that is itself keyed gets its final fingerprint). Returns track id -> ids of the key tracks it needs."""
        names = {t.id: t.name for t in self.p.tracks}
        deps: dict[str, set[str]] = {}

        def link(tid: str, stack: frozenset) -> None:
            if tid in self.key_fp:
                return
            deps[tid] = set()
            for fx in specs[tid].get("fx", []):
                kid = fx.pop("sidechain", None)
                if kid is None:
                    continue
                if kid in stack or kid == tid:
                    warnings.append(f"track '{names[tid]}': sidechain loop through '{names.get(kid, kid)}'; "
                                    f"that effect hears silence")
                    fx["sidechain_key"] = "silent"
                    continue
                if kid in specs:
                    link(kid, stack | {tid})
                    deps[tid].add(kid)
                fx["sidechain_key"] = self._key_for(kid, f"track '{names[tid]}'", warnings)
            self.key_fp[tid] = self.key(specs[tid])

        for tid in specs:
            link(tid, frozenset())
        return deps

    def mix(self, pre: dict[str, np.ndarray], report: dict | None = None) -> tuple[np.ndarray, dict]:
        """Faders, sends, buses (with their FX) and the master chain for the given pre-fader track signals.
        Returns (master, post-fader signals by track/bus id). Used for the full mix and for each layer/stem."""
        p = self.p
        buses = {t.id: t for t in p.tracks if t.kind == "bus"}
        sources = [t for t in p.tracks if t.kind != "bus"]
        bus_in = {bid: np.zeros((2, self.n), dtype=np.float32) for bid in buses}
        post: dict[str, np.ndarray] = {}

        def fader(t: Track, audio: np.ndarray) -> np.ndarray:
            vol = _env(t.automation.get("volume_db", []), p, self.n, t.volume_db)
            pan = _env(t.automation.get("pan", []), p, self.n, t.pan)
            out = _pan(audio * (10 ** (np.asarray(vol) / 20)), pan)
            return out.astype(np.float32)

        def route(t: Track, sig: np.ndarray, master: np.ndarray):
            target = t.output if t.output in buses else "master"
            if target == "master":
                master += sig
            else:
                bus_in[target] += sig
            for s in t.sends:
                if s.get("to") in buses:
                    bus_in[s["to"]] += sig * 10 ** (float(s.get("db", -12)) / 20)

        master = np.zeros((2, self.n), dtype=np.float32)
        for t in sources:
            if t.id in pre:
                post[t.id] = fader(t, pre[t.id])
                route(t, post[t.id], master)
        # buses in dependency order (a bus may feed another bus)
        done: set[str] = set()
        for _ in range(len(buses) + 1):
            for bid, b in buses.items():
                if bid in done:
                    continue
                feeders = [x for x in buses.values() if x.id != bid and x.id not in done and
                           (x.output == bid or any(s.get("to") == bid for s in x.sends))]
                if feeders:
                    continue
                sig = bus_in[bid]
                if b.mute:
                    done.add(bid)
                    continue
                if float(np.abs(sig).max(initial=0)) > 0:
                    sig = self.render_fx(sig, b.fx, f"bus{bid}", f"bus '{b.name}'",
                                         report["warnings"] if report is not None else None)
                post[bid] = fader(b, sig)
                route(b, post[bid], master)
                done.add(bid)
        if len(done) < len(buses) and report is not None:
            report["warnings"].append("bus routing has a cycle; some buses were skipped")

        if float(np.abs(master).max(initial=0)) > 0:
            master = self.render_fx(master, [d for d in p.master.get("fx", []) if isinstance(d, Device)], "master",
                                    "master", report["warnings"] if report is not None else None)
        master = master * 10 ** (float(p.master.get("volume_db", 0)) / 20)
        return master, post

    # ---- export -------------------------------------------------------------------------------------------------
    def export(self, out_dir: Path, base: str, fmt: str = "wav", bits: int = 24, loop: str = "auto",
               normalize: bool = True, stems: bool = False, layers: bool = False, midi: bool = False) -> dict:
        """loop: "auto" (use the project loop region if set), "off", "loop" (loop body only), "intro+loop"."""
        try:
            return self._export(out_dir, base, fmt, bits, loop, normalize, stems, layers, midi)
        finally:
            self.close()  # stems and layers run the bus and master chains again

    def _export(self, out_dir: Path, base: str, fmt: str, bits: int, loop: str, normalize: bool, stems: bool,
                layers: bool, midi: bool) -> dict:
        p = self.p
        sr = self.sr
        rep = self.report
        files = []
        ls = le = None
        mode = "off"
        if p.loop and loop != "off":
            ls = int(round(self.tm.sec(p.loop["start"]) * sr))
            le = int(round(self.tm.sec(p.loop["end"]) * sr))
            mode = loop if loop in ("loop", "intro+loop") else ("intro+loop" if ls > 0 else "loop")

        end_sample = None
        if mode == "off":
            # cut after the musical end once the master's tails have died away; stems use the same length
            env = np.abs(self.master).max(axis=0)
            nz = np.nonzero(env > 10 ** (-70 / 20))[0]
            music_end = int(round(self.tm.sec(p.end) * sr))
            hard_end = int(round((self.tm.sec(p.end) + p.tail) * sr))
            end_sample = min(self.n, hard_end, max(music_end, (nz[-1] + 1) if len(nz) else 0) + int(0.05 * sr))

        def shape(sig: np.ndarray) -> np.ndarray:
            if mode == "off":
                sig = sig[:, :end_sample].copy()
                k = min(int(0.05 * sr), sig.shape[1])
                sig[:, -k:] *= np.linspace(1, 0, k)[None, :]
                return sig
            return loop_wrap(sig, ls, le, keep_intro=(mode == "intro+loop"))

        mix = shape(self.master)
        measure = mix[:, ls:] if mode == "intro+loop" else mix
        gain_db = 0.0
        if normalize:
            cur = analysis.lufs(measure, sr)
            if cur > -100:
                gain_db = float(p.master.get("target_lufs", -16.0)) - cur
                gain_db = max(-30.0, min(30.0, gain_db))
        g = 10 ** (gain_db / 20)
        mix = mix * g
        mix, gr = limiter(mix, sr, float(p.master.get("ceiling_db", -1.0)))
        if mode == "loop":
            loop_pts = (0, mix.shape[1])
        elif mode == "intro+loop":
            loop_pts = (ls, mix.shape[1])
        else:
            loop_pts = None
        ext = fmt.lower()
        out_dir = Path(out_dir)
        main = out_dir / f"{base}.{ext}"
        files.append(write_audio(main, mix, sr, fmt, bits, loop_pts))
        if loop_pts:
            meta = {"loop_start_sample": loop_pts[0], "loop_end_sample": loop_pts[1], "sample_rate": sr,
                    "loop_start_sec": round(loop_pts[0] / sr, 6), "loop_end_sec": round(loop_pts[1] / sr, 6),
                    "bpm": p.tempo, "time_signature": p.time_sig, "mode": mode}
            (out_dir / f"{base}.loop.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
            files.append(str(out_dir / f"{base}.loop.json"))
        # Stems and layers go through the same buses (shared reverb, bus EQ) and master chain as the full mix, get
        # the full mix's normalisation gain (so they sum back to roughly the mix level) and the master limiter.
        part_gr: dict[str, float] = {}

        def part(ids: list[str], label: str) -> np.ndarray:
            sig, _ = self.mix({i: self.pre[i] for i in ids})
            sig, red = limiter(shape(sig) * g, sr, float(p.master.get("ceiling_db", -1.0)))
            if red > 0.05:
                part_gr[label] = round(red, 1)
            return sig

        if stems:
            for t in p.tracks:
                if t.id in self.pre and t.kind != "bus":
                    files.append(write_audio(out_dir / f"{base}_stem_{_safe(t.name)}.{ext}",
                                             part([t.id], f"stem {t.name}"), sr, fmt, bits, loop_pts))
        if layers:
            groups: dict[str, list[str]] = {}
            for t in p.tracks:
                if t.id in self.pre and t.kind != "bus":
                    groups.setdefault(t.layer or "base", []).append(t.id)
            for lay, ids in groups.items():
                files.append(write_audio(out_dir / f"{base}_layer_{_safe(lay)}.{ext}", part(ids, f"layer {lay}"),
                                         sr, fmt, bits, loop_pts))
        if part_gr:
            rep["stem_limiter_reduction_db"] = part_gr
        if midi:
            files.append(export_midi(p, out_dir / f"{base}.mid"))
        rep["files"] = files
        # what was written, so a delivery spec ("48 kHz stereo WAV") can be checked from the report: a Producer
        # that could not see this held a finished cue back and asked the user to inspect the file
        rep["file_format"] = {"format": fmt, "sample_rate": int(sr), "channels": int(mix.shape[0]),
                              **({"bits": int(bits)} if fmt in ("wav", "flac", "aiff") else {})}
        rep["loop_mode"] = mode
        rep["normalize_gain_db"] = round(gain_db, 1)
        rep["limiter_max_reduction_db"] = round(gr, 1)
        if gr > 4:
            rep["warnings"].append(f"master limiter reduced up to {gr:.1f} dB: mix is peaky; lower the loudest parts or raise target headroom")
        rep["mix"] = analysis.audio_report(mix, sr)
        rep["warnings"] += rep["mix"].pop("warnings")
        if loop_pts:
            seam = analysis.loop_seam(mix[:, loop_pts[0]:], sr)
            rep["loop_seam"] = seam
            if seam.get("warning"):
                rep["warnings"].append("loop: " + seam["warning"])
        self.final = mix
        return rep


def sidechain_links(p: Project) -> list[tuple[str, Device, str]]:
    """(owner, effect, key track id) for every active effect with a sidechain key."""
    out = []
    for owner, chain in [(f"track '{t.name}'" if t.kind != "bus" else f"bus '{t.name}'", t.fx) for t in p.tracks] \
            + [("master", [d for d in p.master.get("fx", []) if isinstance(d, Device)])]:
        out += [(owner, d, d.sidechain) for d in chain if d.sidechain and not d.bypass]
    return out


def _switch_is_on(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v) >= 0.5
    return str(v).strip().lower() in ("on", "in", "true", "yes", "ext", "external", "1")


def sidechain_switch_warnings(p: Project) -> list[str]:
    """VST3 effects whose external sidechain switch is on with no key connected: they hear silence and do nothing."""
    out = []
    for t in p.tracks:
        for i, d in enumerate(t.fx):
            if d.type == "vst3" and not d.bypass and not d.sidechain and any(
                    plugins.is_ext_sidechain_switch(k) and _switch_is_on(v) for k, v in d.params.items()):
                out.append(f"track '{t.name}' fx {i} ({d.label()}) has its external sidechain switch on but no "
                           f"sidechain key, so it won't react: set sidechain=<track> or switch it off")
    return out


def soloed_sources(p: Project) -> set[str]:
    """Ids of the source (non-bus) tracks that play given the solo buttons. Soloing a bus plays every track that
    feeds it, directly or through other buses (via output or sends). No solo anywhere = every track."""
    sources = [t for t in p.tracks if t.kind != "bus"]
    soloed = [t for t in p.tracks if t.solo]
    if not soloed:
        return {t.id for t in sources}

    def targets(t: Track) -> set[str]:
        return {t.output} | {s.get("to") for s in t.sends if s.get("to")}

    keep = {t.id for t in soloed if t.kind != "bus"}
    wanted = {t.id for t in soloed if t.kind == "bus"}
    if wanted:
        buses = {t.id: t for t in p.tracks if t.kind == "bus"}
        # every bus that (transitively) feeds a soloed bus counts as soloed too
        grow = True
        while grow:
            grow = False
            for b in buses.values():
                if b.id not in wanted and targets(b) & wanted:
                    wanted.add(b.id)
                    grow = True
        keep |= {t.id for t in sources if targets(t) & wanted}
    return keep


def _safe(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name).strip("_") or "track"


def export_midi(p: Project, path: Path) -> str:
    """Multi-track type-1 MIDI file of the whole project (for finishing in Cubase or any DAW)."""
    import mido
    tpb = 960
    mf = mido.MidiFile(type=1, ticks_per_beat=tpb)
    head = [mido.MetaMessage("track_name", name=p.name, time=0),
            mido.MetaMessage("time_signature", numerator=p.time_sig[0], denominator=p.time_sig[1], time=0)]
    markers = [(int(round(m["at"] * tpb)), 1, mido.MetaMessage("marker", text=m["name"])) for m in p.markers]
    mf.tracks.append(_to_track(_tempo_events(p, tpb) + markers, head))
    channels = iter([c for c in range(16) if c != 9])
    for t in p.tracks:
        if t.kind != "midi":
            continue
        drums = (t.role or "").lower() in ("drums", "percussion") or (t.instrument and t.instrument.name == "drums")
        ch = 9 if drums else next(channels, 0)
        events, _ = _track_events(p, t, tpb, ch)
        mf.tracks.append(_to_track(events, [mido.MetaMessage("track_name", name=t.name, time=0)]))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    mf.save(str(path))
    return str(path)


def render_project(p: Project, out: str | None = None, fmt: str = "wav", bits: int = 24, loop: str = "auto",
                   normalize: bool = True, stems: bool = False, layers: bool = False, midi: bool = False,
                   progress=None) -> dict:
    r = Renderer(p, progress)
    r.run()
    if out:
        o = Path(out)
        if o.suffix:
            out_dir, base, fmt = o.parent, o.stem, o.suffix.lstrip(".")
        else:
            out_dir, base = o, _safe(p.name)
    else:
        out_dir, base = p.path / "renders", _safe(p.name)
    rep = r.export(out_dir, base, fmt, bits, loop, normalize, stems, layers, midi)
    rep["renderer"] = r
    return rep
