"""Rendering: per-track renders (cached, parallel, isolated in worker processes), mixing (volume/pan automation,
sends, buses), master chain, loudness normalisation, limiting, seamless loops, stems and layer exports."""
from __future__ import annotations

import hashlib
import json
import math
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import soundfile as sf

from . import analysis, config, plugins
from .model import Device, Project, Track
from .synths import fx_dsp, synth_dsp


# ---- device resolution ------------------------------------------------------------------------------------

def resolve_device(dev: Device, p: Project, instrument: bool) -> dict | None:
    """Device -> fully resolved spec the worker can load (paths, states, compiled DSP text)."""
    if dev.bypass:
        return None
    if dev.type == "synth":
        return {"type": "synth", "dsp": synth_dsp(dev.name, dev.params), "voices": 32}
    if dev.type == "builtin":
        return {"type": "faust", "dsp": fx_dsp(dev.name, dev.params, p.tempo)}
    if dev.type == "sfz":
        path = Path(dev.name)
        if not path.is_absolute():
            for base in [p.path] + [Path(d) for d in config.load_config().get("sample_dirs", [])]:
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
        return {"type": "vst3", "path": entry["path"], "host": entry["host"], "shell": entry.get("shell"),
                "state": state, "params": dev.params, "name": entry["name"], **extra}
    raise ValueError(f"Unknown device type {dev.type!r}")


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
            f = p.path / f
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

class Renderer:
    def __init__(self, p: Project, progress=None):
        self.p = p
        self.sr = p.sample_rate
        self.tm = p.tempo_map()
        self.progress = progress or (lambda msg: None)
        cache_bars = 4 * p.beats_per_bar
        end = max(p.end, p.beats_per_bar)
        self.render_end_beat = math.ceil(end / cache_bars - 1e-9) * cache_bars
        self.duration = self.tm.sec(self.render_end_beat) + p.tail
        self.n = int(round(self.duration * self.sr))
        self.cache = p.path / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)

    def _base(self) -> dict:
        return {"sr": self.sr, "bpm": self.p.tempo, "tempo": [self.tm.beats, self.tm.bpms], "duration": round(self.duration, 4)}

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
            spec["clips"] = [{**c.to_json(), "mtime": (self.p.path / c.file).stat().st_mtime_ns
                              if c.file and (self.p.path / c.file).exists() else None} for c in clips]
        return spec

    def render_track(self, t: Track, spec: dict) -> tuple[np.ndarray, dict]:
        key = _fingerprint(spec)
        out = self.cache / f"{key}.wav"
        info = {"cached": out.exists(), "key": key}
        if not out.exists():
            job = {k: v for k, v in spec.items() if k not in ("clips",)}
            job["out"] = str(out)
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
            res = plugins.run_worker("render", str(jf), timeout=1800)
            info.update(seconds=round(time.time() - t0, 1), warnings=res.get("warnings", []))
            jf.unlink(missing_ok=True)
            (self.cache / f"{key}.in.wav").unlink(missing_ok=True)
        audio = _read(out, self.sr)
        if audio.shape[1] < self.n:
            audio = np.pad(audio, ((0, 0), (0, self.n - audio.shape[1])))
        return audio[:, : self.n], info

    def render_fx(self, audio: np.ndarray, devices: list[Device], tag: str) -> np.ndarray:
        specs = [s for s in (resolve_device(d, self.p, False) for d in devices) if s]
        if not specs:
            return audio
        key = _fingerprint({**self._base(), "fx": specs, "in": hashlib.sha1(audio.tobytes()).hexdigest()})
        out = self.cache / f"{tag}-{key}.wav"
        if not out.exists():
            inp = self.cache / f"{tag}-{key}.in.wav"
            sf.write(str(inp), audio.T, self.sr, subtype="FLOAT")
            jf = self.cache / f"{tag}-{key}.job.json"
            jf.write_text(json.dumps({**self._base(), "fx": specs, "audio_in": str(inp), "out": str(out)}), encoding="utf-8")
            plugins.run_worker("render", str(jf), timeout=1800)
            jf.unlink(missing_ok=True)
            inp.unlink(missing_ok=True)
        res = _read(out, self.sr)
        if res.shape[1] < self.n:
            res = np.pad(res, ((0, 0), (0, self.n - res.shape[1])))
        return res[:, : self.n]

    def run(self) -> dict:
        p = self.p
        t0 = time.time()
        sources = [t for t in p.tracks if t.kind != "bus"]
        playing = soloed_sources(p)
        audible = [t for t in sources if not t.mute and t.id in playing]
        report: dict = {"tracks": {}, "warnings": []}
        specs = {}
        for t in audible:
            try:
                s = self.track_spec(t)
            except Exception as e:  # noqa: BLE001 - one broken track shouldn't stop the mix
                report["warnings"].append(f"track '{t.name}': {e}")
                continue
            if s is not None:
                specs[t.id] = s
        pre: dict[str, np.ndarray] = {}
        workers = int(config.load_config().get("render_workers", 4))

        def job(t):
            try:
                return t, *self.render_track(t, specs[t.id]), None
            except Exception as e:  # noqa: BLE001
                return t, None, None, str(e)

        todo = [t for t in audible if t.id in specs]
        self.progress(f"rendering {len(todo)} tracks")
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            for t, audio, info, err in ex.map(job, todo):
                if err:
                    report["warnings"].append(f"track '{t.name}' failed to render: {err}")
                    continue
                pre[t.id] = audio
                report["tracks"][t.name] = {"render": "cached" if info["cached"] else f"{info.get('seconds')}s",
                                            **analysis.stem_stats(audio)}
                for w in info.get("warnings", []):
                    report["warnings"].append(f"track '{t.name}': {w}")
                if float(np.abs(audio).max(initial=0)) < 1e-4:
                    report["warnings"].append(
                        f"track '{t.name}' rendered SILENT though it has notes - instrument loaded no sound "
                        f"(empty plugin slot? capture a patch with a program loaded) or notes out of its range")

        self.pre = pre
        self.master, self.post = self.mix(pre, report)
        report["render_seconds"] = round(time.time() - t0, 1)
        self.report = report
        return report

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
                    sig = self.render_fx(sig, b.fx, f"bus{bid}")
                post[bid] = fader(b, sig)
                route(b, post[bid], master)
                done.add(bid)
        if len(done) < len(buses) and report is not None:
            report["warnings"].append("bus routing has a cycle; some buses were skipped")

        if float(np.abs(master).max(initial=0)) > 0:
            master = self.render_fx(master, [d for d in p.master.get("fx", []) if isinstance(d, Device)], "master")
        master = master * 10 ** (float(p.master.get("volume_db", 0)) / 20)
        return master, post

    # ---- export -------------------------------------------------------------------------------------------------
    def export(self, out_dir: Path, base: str, fmt: str = "wav", bits: int = 24, loop: str = "auto",
               normalize: bool = True, stems: bool = False, layers: bool = False, midi: bool = False) -> dict:
        """loop: "auto" (use the project loop region if set), "off", "loop" (loop body only), "intro+loop"."""
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
