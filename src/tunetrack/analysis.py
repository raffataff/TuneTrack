"""Objective checks an agent can't hear for itself: loudness, peaks, spectrum balance, loop seams,
note-level sanity (key, ranges, overlaps, low-end clutter), plus piano-roll / spectrogram images."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from .model import Project
from .theory import Key, note_name, range_of

BANDS = [("sub", 20, 60), ("low", 60, 250), ("low_mid", 250, 500), ("mid", 500, 2000), ("high_mid", 2000, 6000),
         ("high", 6000, 20000)]


def db(x: float) -> float:
    return 20 * math.log10(x) if x > 1e-10 else -200.0


def lufs(audio: np.ndarray, sr: int) -> float:
    import pyloudnorm as pyln
    if audio.shape[1] < int(0.5 * sr):
        return -200.0
    val = pyln.Meter(sr).integrated_loudness(audio.T.astype(np.float64))
    return float(val) if np.isfinite(val) else -200.0


def true_peak(audio: np.ndarray) -> float:
    from scipy.signal import resample_poly
    peak = 0.0
    for ch in audio:
        # oversample in chunks to bound memory
        for i in range(0, len(ch), 48000 * 30):
            seg = ch[max(0, i - 64): i + 48000 * 30]
            peak = max(peak, float(np.abs(resample_poly(seg, 4, 1)).max(initial=0)))
    return peak


def band_balance(audio: np.ndarray, sr: int) -> dict:
    from scipy.signal import welch
    mono = audio.mean(axis=0)
    if len(mono) < 4096:
        return {}
    f, p = welch(mono, sr, nperseg=8192)
    total = p[(f >= 20) & (f <= 20000)].sum() or 1e-12
    return {name: round(100 * float(p[(f >= lo) & (f < hi)].sum() / total), 1) for name, lo, hi in BANDS}


def audio_report(audio: np.ndarray, sr: int) -> dict:
    peak = float(np.abs(audio).max(initial=0))
    rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
    rep = {
        "duration_s": round(audio.shape[1] / sr, 2),
        "lufs": round(lufs(audio, sr), 1),
        "peak_dbfs": round(db(peak), 2),
        "true_peak_dbtp": round(db(true_peak(audio)), 2),
        "crest_db": round(db(peak) - db(rms), 1) if rms > 0 else None,
        "bands_pct": band_balance(audio, sr),
    }
    if audio.shape[0] == 2 and audio.shape[1] > 0:
        l, r = audio[0], audio[1]
        den = math.sqrt(float((l ** 2).sum() * (r ** 2).sum())) or 1e-12
        rep["stereo_correlation"] = round(float((l * r).sum()) / den, 2)
    w = []
    b = rep["bands_pct"]
    if b:
        if b.get("low_mid", 0) > 30:
            w.append(f"low-mids are heavy ({b['low_mid']}% of energy, 250-500 Hz): likely muddy - thin pads/chords there (EQ dip or higher voicings)")
        if b.get("sub", 0) + b.get("low", 0) > 70:
            w.append("low end dominates (>70%): bass/kick too loud or too many low parts")
        if b.get("high_mid", 0) + b.get("high", 0) < 3:
            w.append("very little energy above 2 kHz: mix may sound dull/muffled")
        if b.get("high", 0) > 25:
            w.append("lots of energy above 6 kHz: may sound harsh/hissy")
    if rep.get("stereo_correlation") is not None and rep["stereo_correlation"] < 0.1:
        w.append(f"stereo correlation {rep['stereo_correlation']}: phasey/wide image, may collapse in mono")
    if rep["true_peak_dbtp"] > -0.1:
        w.append(f"true peak {rep['true_peak_dbtp']} dBTP: risk of clipping after encoding")
    rep["warnings"] = w
    return rep


def stem_stats(audio: np.ndarray) -> dict:
    peak = float(np.abs(audio).max(initial=0))
    rms = float(np.sqrt(np.mean(audio ** 2))) if audio.size else 0.0
    return {"peak_dbfs": round(db(peak), 1), "rms_dbfs": round(db(rms), 1)}


def loop_seam(audio: np.ndarray, sr: int) -> dict:
    """How well the end of a loop flows into its start (jump between last and first sample, level match)."""
    w = int(0.02 * sr)
    if audio.shape[1] < 4 * w:
        return {}
    jump = float(np.abs(audio[:, -1] - audio[:, 0]).max())
    rms_end = float(np.sqrt(np.mean(audio[:, -w:] ** 2)))
    rms_start = float(np.sqrt(np.mean(audio[:, :w] ** 2)))
    level_diff = abs(db(rms_end + 1e-9) - db(rms_start + 1e-9))
    out = {"sample_jump": round(jump, 4), "edge_level_diff_db": round(level_diff, 1)}
    if jump > 0.05:
        out["warning"] = "audible click likely at the loop point (large sample jump)"
    elif level_diff > 9 and max(rms_end, rms_start) > 0.01:
        out["warning"] = "loop end and start differ a lot in level: the wrap may sound abrupt"
    return out


# ---- note-level checks ------------------------------------------------------------------------------

def notes_report(p: Project) -> dict:
    try:
        key = Key(p.key)
    except ValueError:
        key = None
    bpb = p.beats_per_bar
    tracks = {}
    low_hits: dict[int, list[tuple[int, str]]] = {}
    for t in p.tracks:
        if t.kind != "midi":
            continue
        notes = [(c.start + n.start, n) for c in t.clips if not c.muted for n in c.notes]
        if not notes:
            continue
        is_drums = (t.role or "").lower() in ("drums", "percussion", "drum kit", "kit") or \
            (t.instrument is not None and t.instrument.name == "drums")
        pitches = [n.pitch for _, n in notes]
        info = {"notes": len(notes), "range": f"{note_name(min(pitches))}-{note_name(max(pitches))}",
                "velocity": f"{min(n.vel for _, n in notes)}-{max(n.vel for _, n in notes)}"}
        issues = []
        if key and not is_drums:
            out_key = [(s, n) for s, n in notes if not key.contains(n.pitch)]
            if out_key:
                bars = sorted({int(s // bpb) + 1 for s, _ in out_key})[:8]
                info["out_of_key"] = len(out_key)
                if len(out_key) > max(2, 0.15 * len(notes)):
                    issues.append(f"{len(out_key)} notes outside {key.text} (bars {bars}) - intended chromaticism?")
        rng = range_of(t.role)
        if rng and not is_drums:
            lo, hi, clo, chi = rng
            bad = [n for _, n in notes if n.pitch < lo or n.pitch > hi]
            hard = [n for _, n in notes if (clo > n.pitch >= lo) or (chi < n.pitch <= hi)]
            if bad:
                issues.append(f"{len(bad)} notes outside the playable {t.role} range {note_name(lo)}-{note_name(hi)}")
            if len(hard) > 0.3 * len(notes):
                issues.append(f"many notes in the extreme register of {t.role}")
        by_pitch: dict[int, list[tuple[float, float]]] = {}
        for s, n in notes:
            by_pitch.setdefault(n.pitch, []).append((s, s + n.dur))
        overlaps = 0
        for spans in by_pitch.values():
            spans.sort()
            for a, b in zip(spans, spans[1:]):
                if b[0] < a[1] - 1e-6:
                    overlaps += 1
        if overlaps:
            issues.append(f"{overlaps} overlapping same-pitch notes (dedupe/legato edit fixes this)")
        if not is_drums:
            for s, n in notes:
                if n.pitch < 48:
                    low_hits.setdefault(int(s * 2), []).append((n.pitch, t.name))
        if issues:
            info["issues"] = issues
        tracks[t.name] = info
    clutter = []
    for half_beat, hits in sorted(low_hits.items()):
        ps = sorted({h[0] for h in hits})
        if any(b - a < 7 and b - a not in (0, 12) for a, b in zip(ps, ps[1:])):
            bar = int(half_beat / 2 // bpb) + 1
            if not clutter or clutter[-1] != bar:
                clutter.append(bar)
    rep = {"key": key.text if key else p.key, "tracks": tracks}
    if clutter:
        rep["low_register_clutter_bars"] = clutter[:16]
        rep["hint"] = "intervals smaller than a fifth below C3 sound muddy; spread voicings upward"
    return rep


# ---- images ----------------------------------------------------------------------------------------------

def _font(size: int):
    from PIL import ImageFont
    for name in ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def piano_roll_png(p: Project, path: str | Path, start: float | None = None, end: float | None = None,
                   tracks: list[str] | None = None, width: int = 1800) -> str:
    """Arrangement overview: one lane per track with its notes, bar grid, markers and loop region."""
    from PIL import Image, ImageDraw
    start = start or 0.0
    end = end or max(p.end, p.beats_per_bar)
    sel = [t for t in p.tracks if t.kind != "bus" and (not tracks or t.id in tracks or t.name in tracks)]
    label_w, top = 170, 40
    lane_h = []
    for t in sel:
        ps = [n.pitch for c in t.clips for n in c.notes]
        span = (max(ps) - min(ps) + 3) if ps else 6
        lane_h.append(int(min(240, max(54, span * 4))))
    H = top + sum(lane_h) + 10
    img = Image.new("RGB", (width, H), (24, 26, 30))
    d = ImageDraw.Draw(img)
    f_small, f = _font(11), _font(13)
    span_b = end - start
    x0 = label_w

    def X(b):
        return x0 + (b - start) / span_b * (width - x0 - 10)

    bpb = p.beats_per_bar
    if p.loop:
        d.rectangle([X(p.loop["start"]), 0, X(p.loop["end"]), H], fill=(30, 40, 34))
    bar = math.floor(start / bpb)
    every = max(1, int(round(span_b / bpb / 40)))
    while bar * bpb <= end:
        x = X(bar * bpb)
        d.line([x, top - 8, x, H], fill=(52, 55, 62) if bar % 4 else (78, 82, 92))
        if bar % every == 0:
            d.text((x + 2, 4), str(bar + 1), fill=(150, 150, 160), font=f_small)
        bar += 1
    for m in p.markers:
        if start <= m["at"] <= end:
            d.line([X(m["at"]), top - 20, X(m["at"]), H], fill=(220, 170, 60))
            d.text((X(m["at"]) + 3, 18), m["name"], fill=(230, 190, 90), font=f)
    palette = [(94, 156, 236), (236, 120, 94), (120, 200, 120), (210, 150, 230), (240, 200, 80), (90, 200, 200),
               (230, 110, 160), (170, 170, 90)]
    y = top
    for i, (t, h) in enumerate(zip(sel, lane_h)):
        col = palette[i % len(palette)]
        d.rectangle([0, y, width, y + h - 1], outline=(40, 43, 50))
        d.text((8, y + 6), t.name[:22], fill=(230, 230, 235), font=f)
        d.text((8, y + 24), (t.instrument.label() if t.instrument else t.kind)[:28], fill=(130, 135, 145), font=f_small)
        ps = [n.pitch for c in t.clips for n in c.notes]
        lo, hi = (min(ps) - 1, max(ps) + 1) if ps else (59, 61)
        for c in t.clips:
            if c.end < start or c.start > end:
                continue
            d.rectangle([X(max(c.start, start)), y + 1, X(min(c.end, end)), y + h - 2], outline=(60, 64, 74))
            for n in c.notes:
                s = c.start + n.start
                if s > end or s + n.dur < start:
                    continue
                ny = y + 4 + (hi - n.pitch) / max(1, hi - lo) * (h - 10)
                shade = 0.45 + 0.55 * n.vel / 127
                d.rectangle([X(s), ny - 1.5, max(X(s) + 1, X(s + n.dur) - 1), ny + 1.5],
                            fill=tuple(int(c_ * shade) for c_ in col))
        y += h
    path = str(path)
    img.save(path)
    return path


def spectrogram_png(audio: np.ndarray, sr: int, path: str | Path, width: int = 1600, height: int = 500) -> str:
    from PIL import Image
    from scipy.signal import stft
    mono = audio.mean(axis=0)
    hop = max(256, len(mono) // width)
    f, _t, Z = stft(mono, sr, nperseg=4096, noverlap=4096 - hop if hop < 4096 else 0, padded=False)
    mag = 20 * np.log10(np.abs(Z) + 1e-9)
    fl = np.geomspace(30, min(20000, sr / 2 - 1), height)
    idx = np.clip(np.searchsorted(f, fl), 0, len(f) - 1)
    img = mag[idx][::-1]
    img = np.clip((img - img.max() + 80) / 80, 0, 1) ** 2.2  # 80 dB below the loudest bin, quiet parts darkened
    rgb = np.stack([img ** 0.6 * 255, img ** 1.4 * 200, (img ** 3) * 120 + 20], axis=-1).astype(np.uint8)
    Image.fromarray(rgb).resize((width, height)).save(str(path))
    return str(path)
