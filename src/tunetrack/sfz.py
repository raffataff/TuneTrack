"""Native offline SFZ sampler.

Supports the opcodes free libraries actually use: sample/default_path/#define/#include, key/lokey/hikey/
pitch_keycenter, lovel/hivel, lorand/hirand, seq_length/seq_position, sw_lokey/sw_hikey/sw_last/sw_default,
loccN/hiccN, xfin/xfout (vel and CC), trigger=attack|release|first|legato, rt_decay, volume/amplitude/pan/
tune/transpose/pitch_keytrack/amp_veltrack, offset/end, loop_mode/loop_start/loop_end, ampeg_* (delay,
attack, hold, decay, sustain, release), group/off_by, amplitude_onccN/volume_onccN, set_ccN, note_offset/
octave_offset, delay, bend_up/bend_down.

CC crossfades (xfin/xfout_loccN) and amplitude/volume_onccN follow the controller for the whole voice, so a
CC1/CC11 swell on a held note changes its level; loccN/hiccN pick regions at note-on. Pitch bend applies to
every sounding voice.
"""
from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from .theory import note_number


def _note_val(v: str) -> int:
    v = v.strip()
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    return note_number(v[0].upper() + v[1:])


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _expand(path: Path, root: Path, defines: dict, depth: int = 0) -> str:
    if depth > 16:
        raise ValueError("SFZ #include nesting too deep")
    text = _strip_comments(path.read_text(encoding="utf-8", errors="replace"))
    out = []
    for line in text.splitlines():
        m = re.match(r'\s*#define\s+(\$\w+)\s+(.*?)\s*$', line)
        if m:
            defines[m.group(1)] = m.group(2)
            continue
        m = re.match(r'\s*#include\s+"(.+?)"', line)
        if m:
            inc = m.group(1)
            for k, v in sorted(defines.items(), key=lambda kv: -len(kv[0])):
                inc = inc.replace(k, v)
            out.append(_expand(root / inc, root, defines, depth + 1))
            continue
        for k, v in sorted(defines.items(), key=lambda kv: -len(kv[0])):
            line = line.replace(k, v)
        out.append(line)
    return "\n".join(out)


_OPCODE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")


def _opcodes(body: str) -> dict[str, str]:
    """Parse 'a=1 sample=My Sample.wav b=2' (values may contain spaces)."""
    ops = {}
    ms = list(_OPCODE.finditer(body))
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(body)
        val = body[m.end():end].strip()
        if m.group(1) != "sample":
            val = val.split()[0] if val.split() else ""
        ops[m.group(1)] = val
    return ops


@dataclass
class Region:
    op: dict
    sample: Path | None
    lokey: int
    hikey: int
    keycenter: int
    lovel: int
    hivel: int

    def f(self, k: str, default: float = 0.0) -> float:
        v = self.op.get(k)
        if v is None or v == "":
            return default
        try:
            return float(v)
        except ValueError:
            return default


class Instrument:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        root = self.path.parent
        text = _expand(self.path, root, {})
        self.control: dict[str, str] = {}
        self.regions: list[Region] = []
        self.cc_defaults: dict[int, float] = {}
        parts = re.split(r"<(\w+)>", text)
        scope = {"global": {}, "master": {}, "group": {}}
        for i in range(1, len(parts), 2):
            header, body = parts[i].lower(), parts[i + 1]
            ops = _opcodes(body)
            if header == "control":
                self.control.update(ops)
            elif header == "global":
                scope = {"global": ops, "master": {}, "group": {}}
            elif header == "master":
                scope["master"] = ops
                scope["group"] = {}
            elif header == "group":
                scope["group"] = ops
            elif header == "region":
                merged = {**scope["global"], **scope["master"], **scope["group"], **ops}
                self._add_region(merged)
        for k, v in self.control.items():
            m = re.fullmatch(r"set_cc(\d+)", k)
            if m:
                self.cc_defaults[int(m.group(1))] = float(v)
        self._samples: dict[Path, tuple[np.ndarray, int]] = {}

    def _add_region(self, op: dict) -> None:
        default_path = self.control.get("default_path", "").replace("\\", "/")
        note_offset = int(float(self.control.get("note_offset", 0))) + 12 * int(float(self.control.get("octave_offset", 0)))
        sample = op.get("sample")
        spath = None
        if sample:
            sample = sample.replace("\\", "/")
            if sample.startswith("*"):
                spath = Path(sample)
            else:
                spath = (self.path.parent / (default_path + sample)).resolve()
        if "key" in op:
            k = _note_val(op["key"])
            lo = hi = center = k
        else:
            lo = _note_val(op.get("lokey", "0"))
            hi = _note_val(op.get("hikey", "127"))
            center = _note_val(op["pitch_keycenter"]) if "pitch_keycenter" in op else lo
        if "pitch_keycenter" in op:
            center = _note_val(op["pitch_keycenter"])
        self.regions.append(Region(op, spath, lo + note_offset, hi + note_offset, center + note_offset,
                                   int(op.get("lovel", 1)), int(op.get("hivel", 127))))

    def key_range(self) -> tuple[int, int]:
        playable = [r for r in self.regions if r.op.get("trigger", "attack") != "release"]
        if not playable:
            return (0, 127)
        return min(r.lokey for r in playable), max(r.hikey for r in playable)

    def keyswitches(self) -> list[int]:
        return sorted({_note_val(r.op["sw_last"]) for r in self.regions if "sw_last" in r.op})

    def load(self, p: Path, sr: int) -> np.ndarray:
        """Sample data as float32 (frames, 2) at its own rate; playback ratio handles rate differences."""
        if p not in self._samples:
            if str(p).startswith("*"):
                n = sr
                t = np.arange(n) / sr
                data = (np.sin(2 * np.pi * 261.6256 * t) if str(p) == "*sine" else np.zeros(n)).astype(np.float32)
                data = np.stack([data, data], axis=1)
                self._samples[p] = (data, sr)
            else:
                data, ssr = sf.read(str(p), dtype="float32", always_2d=True)
                if data.shape[1] == 1:
                    data = np.repeat(data, 2, axis=1)
                self._samples[p] = (data[:, :2], ssr)
        return self._samples[p]

    # ------------------------------------------------------------------------------------------
    def render(self, events: list[tuple], duration: float, sr: int = 48000, seed: int = 7) -> np.ndarray:
        """events: ("note", t_on, t_off, key, vel), ("cc", t, number, value) and ("bend", t, -8192..8191).
        Returns (2, n) float32."""
        n_out = int(math.ceil(duration * sr))
        out = np.zeros((n_out, 2), dtype=np.float32)
        rng = random.Random(seed)
        cc_events = sorted((e for e in events if e[0] == "cc"), key=lambda e: e[1])
        lanes: dict[int, tuple[np.ndarray, np.ndarray]] = {}
        for num in {int(e[2]) for e in cc_events}:
            pts = [(e[1], float(e[3])) for e in cc_events if int(e[2]) == num]
            lanes[num] = (np.array([p[0] for p in pts]), np.array([p[1] for p in pts]))
        bend_ev = sorted((e for e in events if e[0] == "bend"), key=lambda e: e[1])
        bend_lane = (np.array([e[1] for e in bend_ev]), np.array([float(e[2]) for e in bend_ev])) if bend_ev else None
        smooth = max(1, int(0.004 * sr))  # ~4 ms de-zipper on controller steps

        def lane_curve(lane, default: float, t0: float, n: int) -> np.ndarray:
            if lane is None:
                return np.full(n, default, dtype=np.float32)
            ts, vs = lane
            pad = smooth
            tt = t0 + (np.arange(-pad, n, dtype=np.float64) / sr)
            idx = np.searchsorted(ts, tt + 1e-9, side="right") - 1
            arr = np.where(idx >= 0, vs[np.maximum(idx, 0)], default).astype(np.float32)
            if smooth > 1:
                from scipy.ndimage import uniform_filter1d
                arr = uniform_filter1d(arr, size=smooth, mode="nearest", origin=(smooth - 1) // 2)
            return arr[pad:]

        def cc_curve(num: int, t0: float, n: int) -> np.ndarray:
            return lane_curve(lanes.get(num), self.cc_defaults.get(num, 127.0 if num in (7, 11) else 0.0), t0, n)

        def bend_curve(t0: float, n: int) -> np.ndarray | None:
            return None if bend_lane is None else lane_curve(bend_lane, 0.0, t0, n)
        notes = sorted((e for e in events if e[0] == "note"), key=lambda e: e[1])
        ks = self.keyswitches()
        sw_lo = min((_note_val(r.op["sw_lokey"]) for r in self.regions if "sw_lokey" in r.op), default=None)
        sw_hi = max((_note_val(r.op["sw_hikey"]) for r in self.regions if "sw_hikey" in r.op), default=None)
        sw_default = next((_note_val(r.op["sw_default"]) for r in self.regions if "sw_default" in r.op), None)
        if sw_default is None and ks:
            sw_default = ks[0]

        def cc_at(num: int, t: float) -> float:
            val = self.cc_defaults.get(num, 127.0 if num in (7, 11) else 0.0)
            for e in cc_events:
                if e[1] > t + 1e-9:
                    break
                if e[2] == num:
                    val = e[3]
            return val

        def is_switch(key: int) -> bool:
            if sw_lo is not None and sw_hi is not None:
                return sw_lo <= key <= sw_hi
            return key in ks

        voices = []  # (region, t_on, t_off, key, vel, gain_extra, release_trigger)
        last_sw = sw_default
        seq_counter: dict[tuple, int] = {}
        for _, t_on, t_off, key, vel in notes:
            if is_switch(key):
                last_sw = key
                continue
            rnd = rng.random()
            for trig in ("attack", "release"):
                for r in self.regions:
                    rt = r.op.get("trigger", "attack")
                    if (trig == "attack" and rt not in ("attack", "first", "legato")) or (trig == "release" and rt != "release"):
                        continue
                    if not (r.lokey <= key <= r.hikey and r.lovel <= vel <= r.hivel):
                        continue
                    if not (r.f("lorand", 0) <= rnd < r.f("hirand", 1.0001)):
                        continue
                    if "sw_last" in r.op and _note_val(r.op["sw_last"]) != last_sw:
                        continue
                    ok = True
                    for k, v in r.op.items():
                        m = re.fullmatch(r"(lo|hi)cc(\d+)", k)
                        if m:
                            c = cc_at(int(m.group(2)), t_on)
                            if (m.group(1) == "lo" and c < float(v)) or (m.group(1) == "hi" and c > float(v)):
                                ok = False
                                break
                    if not ok:
                        continue
                    seq_len = int(r.f("seq_length", 1))
                    if seq_len > 1:
                        gkey = (r.lokey, r.hikey, r.lovel, r.hivel, r.op.get("group"), trig)
                        pos = seq_counter.get(gkey, 0) % seq_len + 1
                        if int(r.f("seq_position", 1)) != pos:
                            continue
                    xf = _xfade(r, vel, None)  # velocity part; CC crossfades are applied per sample
                    if xf <= 0:
                        continue
                    if trig == "attack":
                        voices.append([r, t_on, t_off, key, vel, xf, None])
                    else:
                        held = max(0.0, t_off - t_on)
                        att = 10 ** (-(r.f("rt_decay", 0) * held) / 20)
                        voices.append([r, t_off, None, key, vel, xf * att, None])
            for gk in {(r.lokey, r.hikey, r.lovel, r.hivel, r.op.get("group"), "attack") for r in self.regions
                       if r.lokey <= key <= r.hikey and r.lovel <= vel <= r.hivel and r.f("seq_length", 1) > 1}:
                seq_counter[gk] = seq_counter.get(gk, 0) + 1
            for gk in {(r.lokey, r.hikey, r.lovel, r.hivel, r.op.get("group"), "release") for r in self.regions
                       if r.lokey <= key <= r.hikey and r.f("seq_length", 1) > 1}:
                seq_counter[gk] = seq_counter.get(gk, 0) + 1

        # choke groups: a voice with off_by=G is cut when a voice of group G starts
        for v in voices:
            ob = v[0].op.get("off_by")
            if ob is None:
                continue
            for w in voices:
                if w is v or w[0].op.get("group") != ob or w[1] <= v[1] + 1e-6:
                    continue
                if v[6] is None or w[1] < v[6]:
                    v[6] = w[1]

        for r, t_on, t_off, key, vel, xf, cut in voices:
            if r.sample is None:
                continue
            self._voice(out, sr, r, t_on, t_off, key, vel, xf, cut, cc_curve, bend_curve)
        return out.T.copy()

    def _voice(self, out, sr, r: Region, t_on, t_off, key, vel, xf, cut, cc_curve, bend_curve) -> None:
        data, ssr = self.load(r.sample, sr)
        n_src = len(data)
        offset = int(r.f("offset", 0))
        end = int(r.f("end", n_src - 1)) + 1
        end = min(end, n_src)
        if offset >= end:
            return
        keytrack = r.f("pitch_keytrack", 100)
        cents = (key - r.keycenter) * keytrack + r.f("transpose", 0) * 100 + r.f("tune", 0)
        ratio = 2 ** (cents / 1200.0) * (ssr / sr)
        loop_mode = r.op.get("loop_mode", r.op.get("loopmode", "no_loop" if "loop_start" not in r.op and "loopstart" not in r.op else "loop_continuous"))
        ls = int(r.f("loop_start", r.f("loopstart", 0)))
        le = int(r.f("loop_end", r.f("loopend", end - 1))) + 1
        looping = loop_mode in ("loop_continuous", "loop_sustain") and le - ls > 16
        one_shot = loop_mode == "one_shot" or t_off is None

        delay = r.f("delay", 0) + r.f("ampeg_delay", 0)
        a = max(r.f("ampeg_attack", 0.0), 0.0015)
        h = r.f("ampeg_hold", 0)
        d = r.f("ampeg_decay", 0)
        s = r.f("ampeg_sustain", 100) / 100.0
        rel = max(r.f("ampeg_release", 0.0), 0.004 if not one_shot else 0.0)
        start = t_on + delay
        if one_shot:
            natural = (end - offset) / ratio / sr
            note_len = natural
            rel = max(rel, 0.004)
        else:
            note_len = max(0.0, t_off - start)
        if cut is not None:
            note_len = min(note_len, max(0.0, cut - start))
            rel = 0.006
        total = note_len + rel
        if not looping:
            total = min(total, (end - offset) / ratio / sr)
        n = int(total * sr)
        i0 = int(round(start * sr))
        if n <= 0 or i0 >= len(out):
            return
        n = min(n, len(out) - i0)
        # controller curves over this voice's lifetime (CC crossfades + amplitude/volume_onccN)
        cc_gain = _xfade_cc(r, lambda num: cc_curve(num, start, n))
        for k, v in r.op.items():
            m = re.fullmatch(r"(amplitude|volume)_oncc(\d+)", k)
            if m:
                c = cc_curve(int(m.group(2)), start, n) / 127.0
                fac = (1 + (float(v) / 100.0) * (c - 1)) if m.group(1) == "amplitude" else 10 ** (float(v) * c / 20)
                cc_gain = cc_gain * fac
        if np.ndim(cc_gain) and float(np.max(cc_gain, initial=0.0)) <= 1e-6:
            return
        bend = bend_curve(start, n)
        if bend is not None and np.any(bend):
            up, down = r.f("bend_up", 200), abs(r.f("bend_down", -200))
            bc = np.where(bend >= 0, bend / 8191.0 * up, bend / 8192.0 * down).astype(np.float64)
            step = ratio * 2 ** (bc / 1200.0)
            pos = offset + np.concatenate(([0.0], np.cumsum(step[:-1])))
        else:
            step = None
            pos = offset + ratio * np.arange(n, dtype=np.float64)
        if looping:
            sustain_loop = loop_mode == "loop_sustain"
            limit = n if not sustain_loop else min(n, int(note_len * sr))
            beyond = pos[:limit] >= le
            if beyond.any():
                pos[:limit][beyond] = ls + np.mod(pos[:limit][beyond] - ls, le - ls)
            if sustain_loop and limit < n:
                # after release, continue forward from where the loop was
                p_last = pos[limit - 1] if limit > 0 else offset
                tail_steps = step[limit:] if step is not None else np.full(n - limit, ratio)
                pos[limit:] = p_last + np.cumsum(tail_steps)
        valid = pos < end - 1
        pos = np.where(valid, pos, end - 2)
        i = pos.astype(np.int64)
        frac = (pos - i).astype(np.float32)[:, None]
        sig = data[i] * (1 - frac) + data[np.minimum(i + 1, n_src - 1)] * frac
        sig[~valid] = 0

        # envelope
        t = np.arange(n, dtype=np.float32) / sr
        env = np.ones(n, dtype=np.float32)
        env = np.minimum(env, t / a)
        if d > 0:
            dec_t = np.clip(t - a - h, 0, None)
            dec = s + (1 - s) * np.exp(-dec_t * 5.0 / d)
            env = np.where(t > a + h, dec, env)
        if not one_shot or cut is not None:
            rel_t = t - note_len
            level = env[min(len(env) - 1, max(0, int(note_len * sr) - 1))] if len(env) else 1
            if rel > 0:
                rel_env = level * np.exp(-np.clip(rel_t, 0, None) * 6.9 / rel)
                env = np.where(rel_t > 0, rel_env, env)
        # gain
        veltrack = r.f("amp_veltrack", 100) / 100.0
        vel_gain = (1 - veltrack) + veltrack * (vel / 127.0) ** 2
        g = 10 ** (r.f("volume", 0) / 20) * (r.f("amplitude", 100) / 100.0) * vel_gain * xf * cc_gain
        pan = r.f("pan", 0) / 100.0
        gl = math.cos((pan + 1) * math.pi / 4) * math.sqrt(2)
        gr = math.sin((pan + 1) * math.pi / 4) * math.sqrt(2)
        sig *= (env * g)[:, None]
        out[i0:i0 + n, 0] += sig[:, 0] * min(gl, 1.0)
        out[i0:i0 + n, 1] += sig[:, 1] * min(gr, 1.0)


def _ramp(x, lo, hi, rising):
    """Equal-power crossfade gain; x may be a scalar or a numpy array."""
    x = np.asarray(x, dtype=np.float32)
    if hi <= lo:
        g = (x >= lo) if rising else (x <= hi)
    else:
        f = np.clip((x - lo) / (hi - lo), 0.0, 1.0)
        g = np.sqrt(f) if rising else np.sqrt(1 - f)
    g = np.asarray(g, dtype=np.float32)
    return g if g.ndim else float(g)


def _xfade(r: Region, vel: int, cc=None):
    """Velocity crossfade gain; with a `cc` callable (num -> value) the CC crossfades are included too."""
    g = 1.0
    if "xfin_lovel" in r.op or "xfin_hivel" in r.op:
        g *= _ramp(vel, r.f("xfin_lovel", 0), r.f("xfin_hivel", 0), True)
    if "xfout_lovel" in r.op or "xfout_hivel" in r.op:
        g *= _ramp(vel, r.f("xfout_lovel", 127), r.f("xfout_hivel", 127), False)
    if cc is not None:
        g = g * _xfade_cc(r, cc)
    return g


def _xfade_cc(r: Region, cc):
    """CC crossfade gain. `cc(num)` returns a value or a per-sample array; so does this."""
    ramp = _ramp
    g = 1.0
    for k in list(r.op):
        m = re.fullmatch(r"xfin_locc(\d+)", k)
        if m:
            n = int(m.group(1))
            g *= ramp(cc(n), r.f(f"xfin_locc{n}", 0), r.f(f"xfin_hicc{n}", 0), True)
        m = re.fullmatch(r"xfout_locc(\d+)", k)
        if m:
            n = int(m.group(1))
            g *= ramp(cc(n), r.f(f"xfout_locc{n}", 127), r.f(f"xfout_hicc{n}", 127), False)
    return g


def describe(path: str | Path) -> dict:
    inst = Instrument(path)
    lo, hi = inst.key_range()
    return {"path": str(path), "regions": len(inst.regions), "range": [lo, hi], "keyswitches": inst.keyswitches(),
            "uses_cc": sorted({int(m.group(2)) for r in inst.regions for k in r.op
                               for m in [re.fullmatch(r"(xfin_locc|xfout_locc|locc|hicc|amplitude_oncc|volume_oncc)(\d+)", k)] if m})}
