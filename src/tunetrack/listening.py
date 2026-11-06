"""The agent's ears: measurements of the rendered audio, not of the notes.

report(project, renderer) returns
  sections   per marker section (or every N bars): loudness, peak, brightness, stereo width, notes per bar, band
             balance and each track's level against the section mix, plus a loudness value per bar. Warns when
             neighbouring sections barely differ or the loop wraps across a big level jump.
  masking    per track: the octave band(s) it carries its energy in, how far it sits above or below everything else
             there, and what covers it. Pairs of tracks that fight for the same band, with the bars where they do.
  verify     per MIDI track: bars that have notes but no sound, and a probe render of every pitch the track uses
             through its instrument (no FX): pitches that make no sound or are much quieter than the rest, slow
             attacks, instruments that sound transposed, drum notes that don't sound like their names.
  reference  the mix next to a reference recording: loudness, loudness range, crest, band balance, brightness, width.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from . import analysis, config, generators, plugins
from .model import Clip, Note, Project, Track
from .theory import note_name

NFFT = 2048
HOP = 512
OCTAVES = [(22, 44), (44, 88), (88, 177), (177, 355), (355, 710), (710, 1420), (1420, 2840), (2840, 5680),
           (5680, 11360), (11360, 22000)]
OCT_LABEL = ["31 Hz", "63 Hz", "125 Hz", "250 Hz", "500 Hz", "1 kHz", "2 kHz", "4 kHz", "8 kHz", "16 kHz"]
LEAD_WORDS = ("lead", "melody", "vocal", "solo", "theme", "hook")
DRUM_ROLES = ("drums", "percussion", "drum kit", "kit")
EPS = 1e-20


def _db(x: float) -> float:
    return 10 * math.log10(x) if x > EPS else -200.0


def _ranges(nums) -> str:
    """[1, 2, 3, 7, 9, 10] -> '1-3, 7, 9-10'."""
    nums = sorted(set(int(n) for n in nums))
    out, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


# ---- spectral features ------------------------------------------------------------------------------------------

class Spectrum:
    """Per-frame octave-band power, the six report bands, total power and spectral-centroid numerator
    (Hann-windowed STFT, NFFT/HOP; frame i is centred on sample i*HOP)."""

    def __init__(self, mono: np.ndarray, sr: int):
        freqs = np.fft.rfftfreq(NFFT, 1 / sr).astype(np.float32)
        oct_m = np.zeros((len(freqs), len(OCTAVES)), np.float32)
        for k, (lo, hi) in enumerate(OCTAVES):
            oct_m[(freqs >= lo) & (freqs < hi), k] = 1
        named_m = np.zeros((len(freqs), len(analysis.BANDS)), np.float32)
        for k, (_n, lo, hi) in enumerate(analysis.BANDS):
            named_m[(freqs >= lo) & (freqs < hi), k] = 1
        aud = ((freqs >= 20) & (freqs <= 20000)).astype(np.float32)
        n_fr = max(1, -(-len(mono) // HOP))
        pad = np.concatenate([np.zeros(NFFT // 2, np.float32), np.asarray(mono, np.float32), np.zeros(NFFT, np.float32)])
        view = np.lib.stride_tricks.sliding_window_view(pad, NFFT)[::HOP][:n_fr]
        win = np.hanning(NFFT).astype(np.float32)
        self.oct = np.empty((n_fr, len(OCTAVES)), np.float32)
        self.named = np.empty((n_fr, len(analysis.BANDS)), np.float32)
        self.power = np.empty(n_fr, np.float32)
        self.cnum = np.empty(n_fr, np.float32)
        for i in range(0, n_fr, 1024):
            s = np.fft.rfft(view[i:i + 1024] * win, axis=1)
            pw = (s.real ** 2 + s.imag ** 2).astype(np.float32)
            k = len(pw)
            self.oct[i:i + k] = pw @ oct_m
            self.named[i:i + k] = pw @ named_m
            self.power[i:i + k] = pw @ aud
            self.cnum[i:i + k] = pw @ (freqs * aud)
        self.times = np.arange(n_fr) * HOP / sr


def _frame_beats(tm, times: np.ndarray) -> np.ndarray:
    xs = list(tm.secs) + [tm.secs[-1] + 1e6]
    ys = list(tm.beats) + [tm.beats[-1] + 1e6 * tm.bpms[-1] / 60.0]
    return np.interp(times, xs, ys)


def _k_weight(audio: np.ndarray, sr: int) -> np.ndarray:
    """BS.1770 K-weighting (pyloudnorm's filters), for ungated loudness of short spans."""
    out = np.asarray(audio, np.float64)
    try:
        import pyloudnorm as pyln
        from scipy.signal import lfilter
        for f in pyln.Meter(sr)._filters.values():
            out = f.passband_gain * lfilter(f.b, f.a, out, axis=1)
    except Exception:  # noqa: BLE001 - fall back to unweighted power
        pass
    return out


def _span_lufs(kw: np.ndarray, a: int, b: int) -> float | None:
    """Ungated loudness of kw[:, a:b] (already K-weighted)."""
    if b - a < 64:
        return None
    ms = float((kw[:, a:b] ** 2).mean(axis=1).sum())
    return round(-0.691 + 10 * math.log10(ms), 1) if ms > 1e-12 else None


def _correlation(seg: np.ndarray) -> float | None:
    if seg.shape[0] < 2 or seg.shape[1] == 0:
        return None
    l, r = seg[0].astype(np.float64), seg[1].astype(np.float64)
    den = math.sqrt(float((l ** 2).sum() * (r ** 2).sum()))
    return round(float((l * r).sum()) / den, 2) if den > 1e-12 else None


# ---- sections -----------------------------------------------------------------------------------------------------

def section_spans(p: Project, end: float, section_bars: int | None = None) -> list[tuple[str, float, float]]:
    """(name, start beat, end beat) from the markers, or every `section_bars` bars (default 8, or 4 for short songs)."""
    bpb = p.beats_per_bar
    marks = sorted((m for m in p.markers if m["at"] < end - 1e-6), key=lambda m: m["at"])
    if marks and not section_bars:
        out = [("Start", 0.0, float(marks[0]["at"]))] if marks[0]["at"] > 1e-6 else []
        for i, m in enumerate(marks):
            e = marks[i + 1]["at"] if i + 1 < len(marks) else end
            if e - m["at"] > 1e-6:
                out.append((m["name"], float(m["at"]), float(e)))
        return out
    n = int(section_bars or (8 if end / bpb > 16 else 4))
    out, s = [], 0.0
    while s < end - 1e-6:
        e = min(end, s + n * bpb)
        out.append((f"bars {int(s // bpb) + 1}-{math.ceil(e / bpb - 1e-9)}", s, e))
        s = e
    return out


def _base_name(name: str) -> str:
    """'A - Pulse' / "A' - Surge" / 'Verse 2' -> 'a' / 'a' / 'verse' (variants of one section aren't contrast-checked)."""
    return re.sub(r"[\s'’0-9]+$", "", name.split(" - ")[0]).strip().lower()


def contrast_warnings(sections: list[dict], bpb: float) -> list[str]:
    warns = []
    for a, b in zip(sections, sections[1:]):
        if _base_name(a["name"]) == _base_name(b["name"]) or a.get("lufs") is None or b.get("lufs") is None:
            continue
        if min(a["_beats"], b["_beats"]) < 4 * bpb:
            continue
        d_lu = b["lufs"] - a["lufs"]
        ba, bb = a.get("brightness_hz") or 1, b.get("brightness_hz") or 1
        da, dbb = a["notes_per_bar"], b["notes_per_bar"]
        dens_ratio = max(da, dbb) / max(min(da, dbb), 1e-9) if max(da, dbb) > 0 else 1.0
        if (abs(d_lu) < 1.0 and max(ba, bb) / min(ba, bb) < 1.15 and dens_ratio < 1.25
                and a["_active"] == b["_active"]):
            warns.append(f"section '{b['name']}' sounds much like '{a['name']}' ({d_lu:+.1f} LU, same instruments, "
                         f"similar brightness and density): give it contrast - register, density, instrumentation "
                         f"or dynamics")
    return warns


# ---- masking ------------------------------------------------------------------------------------------------------

def _band_label(bands: list[int]) -> str:
    bands = sorted(bands)
    if bands == list(range(bands[0], bands[-1] + 1)) and len(bands) > 1:
        return f"{OCT_LABEL[bands[0]]}-{OCT_LABEL[bands[-1]]}"
    return ", ".join(OCT_LABEL[b] for b in bands)


def masking(spectra: dict[str, Spectrum], buses: set[str], leads: set[str], frame_bars: np.ndarray,
            in_song: np.ndarray) -> tuple[dict, list[dict], list[str]]:
    """Per-track signal-to-rest ratio in its main bands, and pairs of tracks fighting for one octave band."""
    names = list(spectra)
    if not names:
        return {}, [], []
    total = sum(spectra[n].oct for n in names)
    frame_energy = total.sum(axis=1)
    live = in_song & (frame_energy > max(float(frame_energy.max(initial=0)), EPS) * 1e-6)
    tracks: dict[str, dict] = {}
    warns: list[str] = []
    if not live.any():
        return tracks, [], warns
    T = total[live]
    for n in names:
        if n in buses:
            continue
        o = spectra[n].oct[live]
        e = o.sum(axis=0)
        if e.sum() <= EPS:
            continue
        order = np.argsort(e)[::-1]
        main, acc = [], 0.0
        for k in order:
            main.append(int(k))
            acc += e[k] / e.sum()
            if acc >= 0.6 or len(main) == 3:
                break
        own = o[:, main].sum(axis=1)
        on = own > own.max() * 1e-4  # while it plays (within 40 dB of its own peak)
        if on.sum() < 5:
            continue
        rest = T[on][:, main].sum(axis=1) - own[on]
        smr = float(np.median(10 * np.log10((own[on] + EPS) / (np.maximum(rest, 0) + EPS))))
        smr = max(-60.0, min(60.0, smr))
        cover = max((m for m in names if m != n), key=lambda m: float(spectra[m].oct[live][on][:, main].sum()),
                    default=None)
        info = {"band": _band_label(main), "vs_rest_db": round(smr, 1)}
        if smr < 0 and cover:
            info["covered_by"] = cover
        tracks[n] = info
        if n in leads and smr < -3:
            warns.append(f"'{n}' (lead) sits {-smr:.0f} dB under the rest of the mix in its main band "
                         f"({info['band']}), mostly under '{cover}': raise it, EQ-dip '{cover}' there, or move one "
                         f"of them to another register")
        elif smr < -15:
            warns.append(f"'{n}' is {-smr:.0f} dB under the rest of the mix in its main band ({info['band']}), "
                         f"mostly under '{cover}': barely audible - raise it, carve space for it, or drop it")

    parts = [n for n in names if n not in buses]
    share = {n: spectra[n].oct[live] / (T + EPS) for n in parts}
    significant = T >= 0.1 * T.sum(axis=1, keepdims=True)
    bars = frame_bars[live]
    pairs = []
    for i, a in enumerate(parts):
        for b in parts[i + 1:]:
            both = (share[a] >= 0.25) & (share[b] >= 0.25) & significant
            frac = both.mean(axis=0)
            k = int(np.argmax(frac))
            if frac[k] < 0.15:
                continue
            if k <= 2:
                hint = "low-end clash: let one own the lows (high-pass or EQ-dip the other), or duck it when the other hits"
            elif k <= 4:
                hint = "low-mid build-up (mud): EQ-dip one of them there or voice it higher"
            else:
                hint = ("both compete for attention here: separate by register, rhythm (call and response), pan or an "
                        "EQ dip in one - fine if it's a deliberate doubling")
            pairs.append({"tracks": [a, b], "band": OCT_LABEL[k], "share_of_time": round(float(frac[k]), 2),
                          "bars": _ranges(bars[both[:, k]])[:80], "hint": hint})
    pairs.sort(key=lambda d: -d["share_of_time"])
    for pr in pairs:
        a, b = pr["tracks"]
        lowband = OCT_LABEL.index(pr["band"]) <= 4
        if pr["share_of_time"] >= 0.35 and (lowband or a in leads or b in leads):
            warns.append(f"'{a}' and '{b}' fight for the {pr['band']} band {pr['share_of_time']:.0%} of the time "
                         f"(bars {pr['bars']}): {pr['hint']}")
    return tracks, pairs[:8], warns


# ---- probe renders: what each pitch a track uses actually sounds like ---------------------------------------------

def _gm_names() -> dict[int, str]:
    inv: dict[int, str] = {}
    for name, num in generators.GM_DRUMS.items():
        inv.setdefault(num, name)
    return inv


# Classes a drum name may measure as. Neighbouring classes blur (a snare with little wire noise reads tom-like,
# a closed hat can read as a click), so only gross mismatches warn: a kick that's a click, a hat that's a thump.
DRUM_EXPECT = [("kick", {"kick", "tom"}), ("snare", {"snare", "click", "tom", "hat"}),
               ("rim", {"click", "snare", "hat", "tom"}), ("stick", {"click", "snare", "hat", "tom"}),
               ("clap", {"snare", "click", "hat"}), ("hat", {"hat", "cymbal", "click", "snare"}),
               ("hh", {"hat", "cymbal", "click", "snare"}), ("crash", {"cymbal", "hat", "snare"}),
               ("ride", {"cymbal", "hat", "snare"}), ("china", {"cymbal", "hat", "snare"}),
               ("splash", {"cymbal", "hat", "snare"}), ("cymbal", {"cymbal", "hat", "snare"}),
               ("tom", {"tom", "kick", "snare"})]
DRUM_LABEL = {"kick": "low thump (kick-like)", "tom": "tonal low-mid drum (tom-like)",
              "snare": "noisy mid hit (snare/clap-like)", "hat": "short bright noise (hat-like)",
              "cymbal": "long bright wash (cymbal-like)", "click": "short click"}


def _env(seg: np.ndarray, sr: int, ms: float = 5.0) -> np.ndarray:
    w = max(1, int(sr * ms / 1000))
    k = len(seg) // w
    if k == 0:
        return np.zeros(1)
    return np.sqrt((seg[:k * w].astype(np.float64) ** 2).reshape(k, w).mean(axis=1))


def drum_class(seg: np.ndarray, sr: int, pre: np.ndarray | None = None) -> tuple[str, dict]:
    """Classify one drum hit: kick / tom / snare / hat / cymbal / click / other.

    seg starts at the hit; pre is the audio just before it (another hit's fading tail), whose spectrum is subtracted
    so only what the hit adds is measured. Features: energy below 150 Hz (low), 150-700 Hz (body), above 5 kHz
    (high), spectral centroid, spectral flatness 200 Hz-16 kHz (noisiness), time from the peak to 20 dB below it
    (decay) and to 30 dB below it (ring)."""
    env = _env(seg, sr)
    floor = 2 * float(np.sqrt((pre[-int(0.05 * sr):].astype(np.float64) ** 2).mean())) if pre is not None and len(pre) else 0.0
    pk = int(np.argmax(env))

    def fall(ratio: float) -> float:
        below = np.nonzero(env[pk:] < max(env[pk] * ratio, floor))[0]
        return float((below[0] if len(below) else len(env) - pk) * 5)

    decay_ms, ring_ms = fall(0.1), fall(0.0316)
    n = min(len(seg), int(0.35 * sr))
    f = np.fft.rfftfreq(8192, 1 / sr)
    P = np.abs(np.fft.rfft(seg[:n].astype(np.float64) * np.hanning(n), 8192)) ** 2
    if pre is not None and len(pre) >= n:
        P = np.maximum(P - np.abs(np.fft.rfft(pre[-n:].astype(np.float64) * np.hanning(n), 8192)) ** 2, 0)
    aud = (f >= 20) & (f <= 20000)
    tot = P[aud].sum() + EPS
    low = float(P[(f >= 20) & (f < 150)].sum() / tot)
    body = float(P[(f >= 150) & (f < 700)].sum() / tot)
    high = float(P[(f >= 5000) & (f <= 20000)].sum() / tot)
    centroid = float((f[aud] * P[aud]).sum() / tot)
    band = P[(f >= 200) & (f <= 16000)] + EPS
    flat = float(np.exp(np.log(band).mean()) / band.mean())
    feats = {"low_pct": round(100 * low), "body_pct": round(100 * body), "high_pct": round(100 * high),
             "centroid_hz": int(centroid), "decay_ms": int(decay_ms), "ring_ms": int(ring_ms),
             "noisiness": round(flat, 2)}
    if decay_ms <= 60 and centroid > 1500 and low < 0.2:
        cls = "click"
    elif low >= 0.45 and centroid < 600:
        cls = "kick"
    elif low + body >= 0.6 and centroid < 1200:
        cls = "tom"
    elif body >= 0.12 and high < 0.7 and centroid >= 700 and flat >= 0.05:
        cls = "snare"  # strong tonal body plus noise
    elif high >= 0.5 or centroid >= 5000 or (centroid >= 2500 and body < 0.06 and ring_ms >= 800):
        cls = "cymbal" if ring_ms >= 800 else "hat"
    elif body >= 0.06 and centroid >= 700 and flat >= 0.05:
        cls = "snare"  # tonal body plus noise
    elif 700 <= centroid < 5000 and flat >= 0.1:
        cls = "snare"  # clap-like noisy mid
    else:
        cls = "other"
    return cls, feats


def pitch_offset(seg: np.ndarray, sr: int, midi: int, pre: np.ndarray | None = None) -> int | None:
    """Semitones between the written MIDI pitch and what an isolated note actually sounds like (0 = in tune;
    +12 = sounds an octave higher). None when no clear pitch can be found (noise, inharmonic sounds).
    pre: audio just before the note (another note's fading tail) of at least len(seg); its spectrum is subtracted."""
    n = len(seg)
    if n < int(0.08 * sr):
        return None
    nfft = 1 << max(15, int(math.ceil(math.log2(n))))
    win = np.hanning(n)
    P = np.abs(np.fft.rfft(seg.astype(np.float64) * win, nfft)) ** 2
    if pre is not None and len(pre) >= n:
        P = np.maximum(P - np.abs(np.fft.rfft(pre[-n:].astype(np.float64) * win, nfft)) ** 2, 0)
    df = sr / nfft
    top = min(sr / 2 - 1, 16000.0)
    pmax = float(P.max(initial=0))
    if pmax <= EPS:
        return None

    def peak(fr: float) -> float:
        if fr >= top or fr < 20:
            return 0.0
        i0, i1 = int(math.floor(fr / 1.012 / df)), int(math.ceil(fr * 1.012 / df))
        return float(P[i0:i1 + 1].max(initial=0))

    def hz(m: float) -> float:
        return 440.0 * 2 ** ((m - 69) / 12)

    def sal(m: int) -> float:
        return sum(peak(k * hz(m)) / k for k in range(1, 9))

    cands = [0, -1, 1, -2, 2, -12, 12, -24, 24]
    s = {d: sal(midi + d) for d in cands}
    best = max(cands, key=lambda d: s[d])
    if s[best] <= pmax * 1e-3:
        return None
    if best == 0 or s[best] < 1.5 * s[0]:
        return 0
    if best > 6 and peak(hz(midi)) > 0.02 * peak(hz(midi + best)):
        return 0  # energy at the written fundamental: it's a strong upper partial, not a transposition
    if best < -6 and peak(hz(midi + best)) < 0.1 * pmax:
        return 0  # no real energy at the lower fundamental
    return best


def _track_notes(t: Track) -> list[tuple[float, Note]]:
    return [(c.start + n.start, n) for c in t.clips if not c.muted and c.kind == "midi"
            for n in c.notes if n.start < c.length - 1e-9]


def _is_drum_track(t: Track, meta: dict | None) -> bool:
    if (t.role or "").lower() in DRUM_ROLES:
        return True
    if t.instrument is not None and t.instrument.type == "synth" and t.instrument.name == "drums":
        return True
    return bool(meta and (meta.get("drum_map") or {"drums", "kit"} & set(meta.get("tags", []))))


def _new_power(before: np.ndarray, during: np.ndarray, sr: int) -> tuple[float, float]:
    """(power `during` adds over `before`, total power of `during`), mean-square units. `before` is another note's
    fading tail: its spectrum is subtracted, so the first value is ~0 when nothing new starts."""
    from scipy.signal import welch
    tot = float((during.astype(np.float64) ** 2).mean()) if len(during) else 0.0
    if len(before) < 1024 or len(during) < 1024:
        return tot, tot
    n = min(4096, len(before), len(during))
    f, pb = welch(before.astype(np.float64), sr, nperseg=n)
    _f, pd = welch(during.astype(np.float64), sr, nperseg=n)
    df = float(f[1] - f[0])
    return float(np.maximum(pd - pb, 0).sum()) * df, float(pd.sum()) * df


def probe_track(p: Project, t: Track) -> dict:
    """Render every pitch the track uses, one at a time, through its instrument (no FX), and measure each."""
    from .engine import Renderer
    meta = plugins.get_patch(t.instrument.patch) if t.instrument and t.instrument.patch else None
    drum = _is_drum_track(t, meta)
    notes = _track_notes(t)
    pitches = sorted({n.pitch for _, n in notes})
    if len(pitches) > 48:
        pitches = [pitches[int(round(i))] for i in np.linspace(0, len(pitches) - 1, 48)]
    vel = int(np.median([n.vel for _, n in notes]))
    # seconds (the probe runs at 60 bpm, so also beats); long gaps so one probe's tail doesn't colour the next
    slot = 3.5 if drum else 2.0  # cymbals ring for seconds
    durs = {pt: 0.7 for pt in pitches}
    if drum:  # drums as written: some instruments stop a hit at note-off, so length changes the sound
        tm = p.tempo_map()
        for pt in pitches:
            ds = [tm.sec(s + n.dur) - tm.sec(s) for s, n in notes if n.pitch == pt]
            durs[pt] = float(min(1.0, max(0.05, np.median(ds))))
    cc: dict[str, list[list[float]]] = {}
    for c in t.clips:
        for lane, pts in c.cc.items():
            if lane.isdigit() and pts:
                cc.setdefault(lane, []).extend(v for _, v in pts)
    cc = {lane: [[0.0, float(np.median(vals))]] for lane, vals in cc.items()}
    if meta and meta.get("dynamics_cc") and str(meta["dynamics_cc"]) not in cc:
        cc[str(meta["dynamics_cc"])] = [[0.0, 100.0]]
    clip = Clip(id="probe", start=0.0, length=len(pitches) * slot,
                notes=[Note(pt, i * slot, durs[pt], vel) for i, pt in enumerate(pitches)], cc=cc)
    ptrack = Track(id=f"{t.id}-probe", name=f"{t.name} probe", instrument=t.instrument, clips=[clip])
    tmp = Project(name="probe", tempo=60.0, sample_rate=p.sample_rate, tail=1.0, tracks=[ptrack],
                  path=Path(p.path).resolve())
    r = Renderer(tmp)
    spec = r.track_spec(ptrack)
    audio, info = r.render_track(ptrack, spec)
    sr = p.sample_rate
    mono = audio.mean(axis=0)
    early = int(0.015 * sr)  # hosts can start a note up to one processing block (~10 ms) before its time
    res = []
    for i, pitch in enumerate(pitches):
        on = int(round(i * slot * sr))
        s0 = max(0, on - early)
        seg = mono[s0:s0 + int(slot * sr)]
        nn = int(durs[pitch] * sr) + (on - s0)
        peak = float(np.abs(seg).max(initial=0))
        row = {"pitch": pitch, "dur_ms": int(durs[pitch] * 1000), "peak_db": round(20 * math.log10(max(peak, 1e-10)), 1)}
        pre = mono[max(0, s0 - int(0.7 * sr)):s0]  # the previous probe's tail, ending clear of this onset
        tail = len(pre) > 0 and float(np.abs(pre).max(initial=0)) > 1e-5
        new, total = _new_power(pre if tail else pre[:0], seg[:int((0.3 if drum else durs[pitch] + 0.1) * sr)], sr)
        row["level_db"] = round(_db(new), 1)
        # silent: nothing there, or only the previous probe's fading tail (no new spectral energy when this starts)
        row["silent"] = peak < 1e-4 or (tail and new < 0.1 * total)
        if not row["silent"]:
            env = _env(seg[:nn], sr)
            row["attack_ms"] = max(0, int(np.nonzero(env >= 0.5 * env.max())[0][0] * 5) - (on - s0) * 1000 // sr)
            if drum:
                row["class"], row["features"] = drum_class(seg, sr, pre[-int(0.4 * sr):] if tail else None)
            else:
                a = on + int(sr * min(0.3, max(0.05, row["attack_ms"] / 1000 + 0.03)))
                b = on + int(durs[pitch] * sr)
                row["offset"] = pitch_offset(mono[a:b], sr, pitch, pre if tail else None)
        res.append(row)
    return {"drum": drum, "meta": meta, "vel": vel, "pitches": res, "cached": info.get("cached")}


def _drum_name(t: Track, meta: dict | None, pitch: int) -> tuple[str | None, bool]:
    """(name, check_it): the drum name this pitch has in the kit's map, and whether it's safe to warn on it."""
    if meta and meta.get("drum_map"):
        inv = {int(v): k for k, v in meta["drum_map"].items()}
        return inv.get(pitch), True
    gm = (t.instrument is not None and t.instrument.type == "synth") or bool(meta and "gm" in meta.get("tags", []))
    name = _gm_names().get(pitch)
    return name, gm


def verify_track(p: Project, t: Track, pre: np.ndarray | None, probe: dict | None) -> tuple[dict, list[str]]:
    sr, tm, bpb = p.sample_rate, p.tempo_map(), p.beats_per_bar
    notes = _track_notes(t)
    out: dict = {}
    warns: list[str] = []
    if pre is not None and notes:
        mono = pre.mean(axis=0).astype(np.float64)
        cs = np.concatenate([[0.0], np.cumsum(mono ** 2)])
        first: dict[int, float] = {}
        for s, _n in notes:
            b = int(s // bpb)
            first[b] = min(first.get(b, s), s)
        ms = {}
        for b, s in first.items():
            a = int(tm.sec(s) * sr)
            e = min(len(mono), int(tm.sec((b + 1) * bpb) * sr))
            if e - a > 64:
                ms[b] = (cs[e] - cs[a]) / (e - a)
        if ms:
            ref = float(np.percentile(list(ms.values()), 90))
            quiet = [b + 1 for b, v in ms.items() if v < max(ref * 1e-4, 1e-12)]
            if quiet and len(quiet) < len(ms):
                out["bars_without_sound"] = _ranges(quiet)
                warns.append(f"'{t.name}': bars {out['bars_without_sound']} have notes but almost no sound (40+ dB "
                             f"under the rest of the track): check CC levels (CC1/CC11 at 0?), keyswitches, or notes "
                             f"outside the instrument's range")
    if not probe:
        return out, warns
    rows = probe["pitches"]
    sounding = [r for r in rows if not r["silent"]]
    by_pitch: dict[int, list[float]] = {}
    for s, n in notes:
        by_pitch.setdefault(n.pitch, []).append(s)

    def where(pitch: int) -> str:
        starts = by_pitch.get(pitch, [])
        return f"{len(starts)} notes, bars {_ranges(int(s // bpb) + 1 for s in starts)[:60]}"

    silent = [r["pitch"] for r in rows if r["silent"]]
    if silent and sounding:
        out["silent_pitches"] = [f"{note_name(x)} ({x})" for x in silent]
        warns.append(f"'{t.name}': {', '.join(out['silent_pitches'])} make no sound on this instrument "
                     f"({'; '.join(f'{note_name(x)}: {where(x)}' for x in silent[:4])}) - outside its range or "
                     f"unmapped: move them or change instrument")
    out["probe"] = {"pitches_probed": len(rows), "velocity": probe["vel"], "rendered": "cached" if probe["cached"] else "now"}
    if not sounding:
        return out, warns
    med = float(np.median([r["level_db"] for r in sounding]))
    out["probe"]["level_spread_db"] = round(max(r["level_db"] for r in sounding) - min(r["level_db"] for r in sounding), 1)
    attack = int(np.median([r["attack_ms"] for r in sounding]))
    out["probe"]["attack_ms"] = attack

    if probe["drum"]:
        loud = max(r["level_db"] for r in sounding)
        lines = []
        for r in rows:
            name, check = _drum_name(t, probe["meta"], r["pitch"])
            label = "silent" if r["silent"] else DRUM_LABEL.get(r["class"], f"other ({r['features']['centroid_hz']} Hz)")
            lvl = "" if r["silent"] else f", {r['level_db'] - loud:+.0f} dB"
            lines.append(f"{note_name(r['pitch'])} ({r['pitch']}){' ' + name if name else ''}: {label}{lvl}")
            if r["silent"] or not name or not check:
                continue
            allowed = next((ok for word, ok in DRUM_EXPECT if word in name.lower()), None)
            rings = any(w in name.lower() for w in ("crash", "ride", "china", "splash", "cymbal", "open"))
            cut = r["features"]["decay_ms"] <= r["dur_ms"] + 100 and r["dur_ms"] < 500
            if rings and cut and r["class"] in ("hat", "click"):
                warns.append(f"'{t.name}': note {r['pitch']} ('{name}') stops when its note ends ({r['dur_ms']} ms "
                             f"notes), so it can't ring ({where(r['pitch'])}): lengthen those notes to 1-2 s")
            elif allowed and r["class"] not in allowed and r["class"] != "other":
                warns.append(f"'{t.name}': note {r['pitch']} ('{name}') sounds like a {DRUM_LABEL[r['class']]}, not "
                             f"a {name} ({where(r['pitch'])}) - use another note for it")
        out["drum_notes"] = lines
        return out, warns

    quiet = [r for r in sounding if r["level_db"] < med - 15]
    if quiet:
        out["quiet_pitches"] = [f"{note_name(r['pitch'])} ({r['level_db'] - med:+.0f} dB)" for r in quiet]
        warns.append(f"'{t.name}': {', '.join(out['quiet_pitches'])} are much quieter than its other notes at the same "
                     f"velocity (edge of the instrument's range?): move them or raise their velocity")
    offs = [r.get("offset") for r in sounding if r.get("offset") is not None]
    if len(offs) >= 2:
        common, cnt = Counter(offs).most_common(1)[0]
        if common != 0 and cnt >= max(2, 0.6 * len(offs)):
            out["sounds_transposed"] = common
            seen = f"{cnt}/{len(offs)} probed pitches"
            if common < 0 and common % 12 == 0:
                warns.append(f"'{t.name}': its pitch reads {-common // 12} octave(s) lower than the written notes "
                             f"({seen}) - a loud sub-oscillator does this - so the part sits lower in the mix than its "
                             f"notes say: fine if intended, otherwise transpose by {-common:+d} or turn the sub down")
            elif common % 12 == 0:
                warns.append(f"'{t.name}': it sounds {common // 12} octave(s) higher than the written notes ({seen}; "
                             f"patch transpose, or an instrument like glockenspiel that sounds up): write the part "
                             f"{-common:+d} semitones lower if that register isn't intended")
            else:
                warns.append(f"'{t.name}': it sounds {common:+d} semitones off the written notes ({seen}): the patch is "
                             f"transposed or tuned off - fix its tuning or transpose the part by {-common:+d}")
    durs = [(tm.sec(s + n.dur) - tm.sec(s)) * 1000 for s, n in notes]
    med_dur = float(np.median(durs)) if durs else 0
    if attack > 120 and med_dur < 2.5 * attack:
        warns.append(f"'{t.name}': the instrument takes ~{attack} ms to speak but most notes last ~{int(med_dur)} ms, "
                     f"so they barely bloom: lengthen them, start them earlier, or use a faster-attack sound")
    return out, warns


# ---- reference comparison -----------------------------------------------------------------------------------------

def mix_summary(audio: np.ndarray, sr: int) -> dict:
    rep = analysis.audio_report(audio, sr)
    rep.pop("warnings", None)
    kw = _k_weight(audio, sr)
    st = []
    w, h = int(3 * sr), int(sr)
    for a in range(0, max(1, audio.shape[1] - w + 1), h):
        v = _span_lufs(kw, a, a + w)
        if v is not None:
            st.append(v)
    st = [v for v in st if v > -70 and v > rep["lufs"] - 20]
    rep["loudness_range_lu"] = round(float(np.percentile(st, 95) - np.percentile(st, 10)), 1) if len(st) >= 3 else None
    from scipy.signal import welch
    f, pw = welch(audio.mean(axis=0), sr, nperseg=8192)
    rep["brightness_hz"] = int((f * pw).sum() / max(pw.sum(), EPS))
    return {k: rep.get(k) for k in ("duration_s", "lufs", "loudness_range_lu", "true_peak_dbtp", "crest_db",
                                    "brightness_hz", "stereo_correlation", "bands_pct")}


def compare_reference(ours: dict, ref: dict, target: float) -> list[str]:
    d = []
    if ref["lufs"] > -100 and abs(ref["lufs"] - target) > 2:
        d.append(f"the reference plays at {ref['lufs']} LUFS and this mix is delivered at {target}: set "
                 f"project_set(target_lufs={round(ref['lufs'])}) to match it (quieter suits background game music)")
    if ours.get("loudness_range_lu") is not None and ref.get("loudness_range_lu") is not None:
        x = ref["loudness_range_lu"] - ours["loudness_range_lu"]
        if abs(x) > 3:
            d.append(f"the reference {'moves more' if x > 0 else 'is steadier'} in level (loudness range "
                     f"{ref['loudness_range_lu']} vs {ours['loudness_range_lu']} LU): "
                     f"{'make quiet sections quieter and builds bigger' if x > 0 else 'even out section levels'}")
    if ours.get("crest_db") and ref.get("crest_db") and abs(ref["crest_db"] - ours["crest_db"]) > 3:
        more = ref["crest_db"] > ours["crest_db"]
        d.append(f"the reference is {'punchier (more transient peaks)' if more else 'denser/more compressed'} "
                 f"(crest {ref['crest_db']} vs {ours['crest_db']} dB)")
    bands = []
    for k, v in (ref.get("bands_pct") or {}).items():
        mine = (ours.get("bands_pct") or {}).get(k, 0)
        if abs(mine - v) >= 5:
            bands.append(f"{k} {mine}% vs {v}%")
    if bands:
        d.append("band balance (this mix vs reference): " + ", ".join(bands))
    if ours.get("brightness_hz") and ref.get("brightness_hz"):
        r = ours["brightness_hz"] / ref["brightness_hz"]
        if r < 0.8 or r > 1.25:
            d.append(f"this mix is {'darker' if r < 1 else 'brighter'} than the reference (spectral centroid "
                     f"{ours['brightness_hz']} vs {ref['brightness_hz']} Hz)")
    if ours.get("stereo_correlation") is not None and ref.get("stereo_correlation") is not None:
        x = ours["stereo_correlation"] - ref["stereo_correlation"]
        if abs(x) > 0.2:
            d.append(f"this mix is {'narrower' if x > 0 else 'wider'} than the reference (stereo correlation "
                     f"{ours['stereo_correlation']} vs {ref['stereo_correlation']}; 1 = mono)")
    return d


# ---- the report ---------------------------------------------------------------------------------------------------

def report(p: Project, r, reference: str | None = None, probe: bool = True, section_bars: int | None = None,
           tracks: list[str] | None = None) -> dict:
    """Everything above for a project whose Renderer has run (r.master, r.post, r.pre)."""
    sr, tm, bpb = r.sr, r.tm, p.beats_per_bar
    end = max(p.end, bpb)
    end_s = tm.sec(end)
    music_n = min(r.master.shape[1], int(round(end_s * sr)))
    target = float(p.master.get("target_lufs", -16.0))
    if p.loop and p.loop["end"] > p.loop["start"]:
        a, b = int(tm.sec(p.loop["start"]) * sr), int(tm.sec(p.loop["end"]) * sr)
        cur = analysis.lufs(r.master[:, a:b], sr)
    else:
        cur = analysis.lufs(r.master[:, :music_n], sr)
    gain = 10 ** (max(-30.0, min(30.0, target - cur)) / 20) if cur > -100 else 1.0
    master = r.master * gain
    warns: list[str] = []

    names = {t.id: t.name for t in p.tracks}
    buses = {f"{t.name} (bus)" for t in p.tracks if t.kind == "bus"}
    label = {tid: (f"{names[tid]} (bus)" if f"{names[tid]} (bus)" in buses else names[tid]) for tid in r.post}
    leads = {t.name for t in p.tracks if any(w in f"{t.name} {t.role or ''}".lower() for w in LEAD_WORDS)}
    mix_spec = Spectrum(master.mean(axis=0), sr)
    spectra = {label[tid]: Spectrum((sig * gain).mean(axis=0), sr) for tid, sig in r.post.items()
               if float(np.abs(sig).max(initial=0)) > 1e-6}
    fbeats = _frame_beats(tm, mix_spec.times)
    fbars = (fbeats // bpb).astype(int) + 1
    in_song = mix_spec.times <= end_s

    # sections + per-bar loudness
    kw = _k_weight(master, sr)
    n_bars = math.ceil(end / bpb - 1e-9)
    bar_lufs = [_span_lufs(kw, int(tm.sec(i * bpb) * sr), int(tm.sec(min(end, (i + 1) * bpb)) * sr))
                for i in range(n_bars)]
    bar_lufs = [None if v is None or v < -70 else int(round(v)) for v in bar_lufs]
    audible_midi = [t for t in p.tracks if t.kind == "midi" and t.id in r.pre]
    onsets = np.array(sorted(s for t in audible_midi for s, _ in _track_notes(t)))
    sections = []
    for name, s, e in section_spans(p, end, section_bars):
        a, b = int(tm.sec(s) * sr), int(tm.sec(e) * sr)
        seg = master[:, a:b]
        fr = (fbeats >= s) & (fbeats < e)
        mp = float(mix_spec.power[fr].sum())
        lu = analysis.lufs(seg, sr) if seg.shape[1] >= int(0.5 * sr) else -200.0
        entry: dict = {"name": name, "bars": f"{int(s // bpb) + 1}-{math.ceil(e / bpb - 1e-9)}",
                       "lufs": round(lu, 1) if lu > -100 else None}
        entry["peak_db"] = round(20 * math.log10(max(float(np.abs(seg).max(initial=0)), 1e-10)), 1)
        entry["brightness_hz"] = int(float(mix_spec.cnum[fr].sum()) / mp) if mp > EPS else None
        entry["width"] = _correlation(seg)
        cnt = int(((onsets >= s) & (onsets < e)).sum()) if len(onsets) else 0
        entry["notes_per_bar"] = round(cnt / max((e - s) / bpb, 1e-9), 1)
        if mp > EPS:
            nb = mix_spec.named[fr].sum(axis=0)
            entry["bands_pct"] = {bn: round(100 * float(v) / max(float(nb.sum()), EPS), 1)
                                  for (bn, _lo, _hi), v in zip(analysis.BANDS, nb)}
            levels = {n: _db(float(sp.power[fr].sum()) / mp) for n, sp in spectra.items()}
            levels = {n: round(v, 1) for n, v in sorted(levels.items(), key=lambda kv: -kv[1]) if v >= -30}
            entry["tracks_db"] = dict(list(levels.items())[:8])
            pres = {n: float(sp.oct[fr][:, 5:8].sum()) for n, sp in spectra.items() if n not in buses}
            if pres and max(pres.values()) > EPS:
                entry["most_present"] = max(pres, key=pres.get)
        else:
            levels = {}
        entry["_beats"] = e - s
        entry["_active"] = {n for n, v in levels.items() if v >= -20 and n not in buses}
        sections.append(entry)
    warns += contrast_warnings(sections, bpb)
    for entry in sections:
        entry.pop("_beats"), entry.pop("_active")
    arc = " | ".join(f"{s_['name']} {s_['lufs'] if s_['lufs'] is not None else 'silent'}" for s_ in sections)
    if p.loop:
        first, last = int(p.loop["start"] // bpb), int((p.loop["end"] - 1e-6) // bpb)
        if 0 <= first < len(bar_lufs) and 0 <= last < len(bar_lufs) and bar_lufs[first] is not None \
                and bar_lufs[last] is not None and abs(bar_lufs[last] - bar_lufs[first]) > 6:
            warns.append(f"the loop wraps from bar {last + 1} ({bar_lufs[last]} LUFS) to bar {first + 1} "
                         f"({bar_lufs[first]} LUFS), a {abs(bar_lufs[last] - bar_lufs[first])} LU jump: if that's not "
                         f"intended, ease the last bar toward the first so the wrap doesn't sound like a cut")

    # masking
    mask_tracks, pairs, mwarn = masking(spectra, buses, leads, fbars, in_song)
    warns += mwarn

    # verify: silent bars from the real render, probe renders of every pitch used
    sel = [t for t in audible_midi if not tracks or t.name in tracks or t.id in tracks]
    probes: dict[str, dict] = {}
    if probe and sel:
        def job(t):
            try:
                return t, probe_track(p, t), None
            except Exception as e:  # noqa: BLE001 - one broken instrument shouldn't stop the report
                return t, None, str(e)
        with ThreadPoolExecutor(max_workers=max(1, int(config.load_config().get("render_workers", 4)))) as ex:
            for t, res, err in ex.map(job, sel):
                if err:
                    warns.append(f"'{t.name}': probe render failed: {err}")
                else:
                    probes[t.id] = res
    verify = {}
    for t in sel:
        info, w = verify_track(p, t, r.pre.get(t.id), probes.get(t.id))
        if info:
            verify[t.name] = info
        warns += w

    out = {"levels": f"as delivered: mix normalised to {target} LUFS (before the limiter)",
           "energy_arc": arc, "sections": sections, "bar_lufs": bar_lufs,
           "masking": {"tracks": mask_tracks, "pairs": pairs}, "verify": verify}
    if reference:
        from .engine import _read
        ref_audio = _read(Path(reference), sr)
        ours = mix_summary(master[:, :music_n], sr)
        theirs = mix_summary(ref_audio, sr)
        out["reference"] = {"file": str(reference), "this_mix": ours, "reference": theirs,
                            "differences": compare_reference(ours, theirs, target)}
    out["warnings"] = warns
    return out
