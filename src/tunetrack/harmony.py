"""Harmony: the chord track, chord inference, the bar-by-bar score view and the harmony check.

chord track  Project.chord_track, one harmonic timeline: {"at": beat, "len": beats, "chord": token[, "key": key]}. A
             token is a symbol ("Am7", "F/A") or a roman numeral resolved against the project key (or the entry's own
             key), so a key change moves roman entries with it. chords()/pattern() follow it when given no progression.
infer        the chord the pitched parts sound (duration-weighted pitch classes plus a bass bonus), tried per bar,
             then per half bar, then per beat, wherever the notes change chord inside the window.
score        compact text an agent can read: per bar the chord on each beat and every part's notes in TTN, with columns
             lined up by beat so vertical relationships show.
check        conservative warnings with bar:beat and a fix: lead/bass notes that clash with the chord on strong beats
             (unless they move on by step), parallel 5ths/8ves between bass and lead, voice crossings by role, and parts
             that disagree with the chord track. Drum tracks are left out.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass

from . import plugins
from .model import Project, Track
from .notation import _fmt_dur
from .theory import Chord, Key, chord_name, note_name, parse_any

EPS = 1e-6
LEAD_WORDS = ("lead", "melody", "vocal", "solo", "theme", "hook")
DRUM_WORDS = ("drum", "perc", "kit")
TEMPLATES = {"": (0, 4, 7), "m": (0, 3, 7), "dim": (0, 3, 6), "aug": (0, 4, 8), "sus4": (0, 5, 7), "sus2": (0, 2, 7),
             "5": (0, 7), "6": (0, 4, 7, 9), "m6": (0, 3, 7, 9), "7": (0, 4, 7, 10), "maj7": (0, 4, 7, 11),
             "m7": (0, 3, 7, 10), "m7b5": (0, 3, 6, 10)}
MAX_FINDINGS = 40


def _r(x: float) -> float:
    return round(float(x), 6)


# ---- the chord track ----------------------------------------------------------------------------------------------

def resolve(p: Project, e: dict) -> Chord:
    return parse_any(e["chord"], Key(e.get("key") or p.key))


def label(p: Project, e: dict, flats: bool) -> str:
    """'Dm(i)' for a roman entry; a symbol as written."""
    tok = e["chord"]
    return tok if tok[:1] in "ABCDEFG" else f"{chord_name(resolve(p, e), flats)}({tok})"


def track_end(p: Project) -> float:
    return max((x["at"] + x["len"] for x in p.chord_track), default=0.0)


def cut(entries: list[dict], s: float, e: float) -> list[dict]:
    """The entries with [s, e) removed (entries crossing an edge are trimmed or split)."""
    out = []
    for x in entries:
        a, b = x["at"], x["at"] + x["len"]
        if b <= s + EPS or a >= e - EPS:
            out.append(x)
            continue
        if a < s - EPS:
            out.append({**x, "len": _r(s - a)})
        if b > e + EPS:
            out.append({**x, "at": _r(e), "len": _r(b - e)})
    return sorted(out, key=lambda x: x["at"])


def grab(entries: list[dict], s: float, e: float) -> list[dict]:
    """Copies of the entries inside [s, e), clipped to it, positions relative to s."""
    out = []
    for x in entries:
        a, b = max(x["at"], s), min(x["at"] + x["len"], e)
        if b > a + EPS:
            out.append({**x, "at": a - s, "len": b - a})
    return out


def put(entries: list[dict], new: list[dict], s: float, e: float) -> list[dict]:
    """Replace [s, e) with `new` (absolute positions)."""
    return sorted(cut(entries, s, e) + [{**x, "at": _r(x["at"]), "len": _r(x["len"])} for x in new],
                  key=lambda x: x["at"])


def shift(entries: list[dict], s: float, e: float, op: str) -> list[dict]:
    """Follow arrange(): insert opens a gap of e-s beats at s (splitting a chord that crosses s); delete removes [s, e)."""
    span = e - s
    if op == "insert":
        out = []
        for x in entries:
            a, b = x["at"], x["at"] + x["len"]
            if a >= s - EPS:
                out.append({**x, "at": _r(a + span)})
            elif b > s + EPS:
                out += [{**x, "len": _r(s - a)}, {**x, "at": _r(s + span), "len": _r(b - s)}]
            else:
                out.append(x)
        return out
    return [x if x["at"] < e - EPS else {**x, "at": _r(x["at"] - span)} for x in cut(entries, s, e)]


def fill(prog: list, span: float | None) -> list:
    """Repeat a progression [(chord, beats)] to fill `span` beats, cutting the last chord at the end."""
    total = sum(ln for _, ln in prog)
    if span is None or total <= EPS:
        return prog
    out, t = [], 0.0
    while t < span - EPS:
        for c, ln in prog:
            if t >= span - EPS:
                break
            out.append((c, min(ln, span - t)))
            t += ln
    return out


def progression(p: Project, s: float, e: float) -> list[tuple[Chord | None, float]]:
    """The chord track over [s, e) as (chord, beats) pairs; gaps are (None, beats)."""
    out, t = [], s
    for x in sorted(p.chord_track, key=lambda x: x["at"]):
        a, b = max(x["at"], s), min(x["at"] + x["len"], e)
        if b <= a + EPS:
            continue
        if a > t + EPS:
            out.append((None, a - t))
        out.append((resolve(p, x), b - a))
        t = b
    if e > t + EPS:
        out.append((None, e - t))
    return out


def section_range(p: Project, name: str) -> tuple[float, float | None]:
    """A marker section: from the marker to the next one (or the song / chord-track end; None if nothing is there)."""
    ms = sorted(p.markers, key=lambda m: m["at"])
    for i, m in enumerate(ms):
        if m["name"].strip().lower() == str(name).strip().lower():
            if i + 1 < len(ms):
                return m["at"], ms[i + 1]["at"]
            end = max(p.end, track_end(p))
            return m["at"], (end if end > m["at"] + EPS else None)
    raise KeyError(f"No marker {name!r}. Markers: {', '.join(m['name'] for m in ms) or 'none'}")


def listing(p: Project, s: float | None = None, e: float | None = None) -> list[dict]:
    flats = Key(p.key).flats
    out = []
    for x in sorted(p.chord_track, key=lambda x: x["at"]):
        if (s is not None and x["at"] + x["len"] <= s + EPS) or (e is not None and x["at"] >= e - EPS):
            continue
        d = {"at": p.fmt_pos(x["at"]), "to": p.fmt_pos(x["at"] + x["len"]), "chord": x["chord"]}
        name = chord_name(resolve(p, x), flats)
        if name != x["chord"]:
            d["is"] = name
        if x.get("key"):
            d["key"] = x["key"]
        out.append(d)
    return out


def summary(p: Project) -> str | None:
    if not p.chord_track:
        return None
    bpb = p.beats_per_bar
    a, b = min(x["at"] for x in p.chord_track), track_end(p)
    return f"bars {int(a // bpb) + 1}-{math.ceil(b / bpb - 1e-9)}, {len(p.chord_track)} chords"


# ---- parts ------------------------------------------------------------------------------------------------------

@dataclass
class Part:
    name: str
    tag: str  # name + role, lower case
    notes: list  # (start, end, pitch) as rendered, sorted
    drums: bool
    role: str = "inner"  # bass | lead | inner

    def __post_init__(self):
        self.starts = [n[0] for n in self.notes]
        self.longest = max((b - a for a, b, _ in self.notes), default=0.0)
        ps = sorted(n[2] for n in self.notes)
        self.median = ps[len(ps) // 2] if ps else 0
        groups: dict[float, int] = {}
        for a, _, _ in self.notes:
            groups[round(a * 48)] = groups.get(round(a * 48), 0) + 1
        self.mono = sum(1 for v in groups.values() if v == 1) / max(1, len(groups))

    def sounding(self, t: float, min_len: float = 0.0) -> list[int]:
        i = bisect.bisect_right(self.starts, t + EPS)
        out, j = [], i - 1
        while j >= 0 and self.starts[j] >= t - self.longest - EPS:
            a, b, pi = self.notes[j]
            if a <= t + EPS and b > t + EPS and b - a >= min_len - EPS:
                out.append(pi)
            j -= 1
        return out

    def line(self, top: bool) -> list[tuple[float, float, int]]:
        """One note per onset (the highest or the lowest): the part as a single voice."""
        groups: dict[int, tuple[float, float, int]] = {}
        for a, b, pi in self.notes:
            k = round(a * 48)
            g = groups.get(k)
            if g is None or (pi > g[2] if top else pi < g[2]):
                groups[k] = (a, b, pi)
        return [groups[k] for k in sorted(groups)]

    def between(self, lo: float, hi: float) -> list:
        i = bisect.bisect_left(self.starts, hi - EPS)
        return [n for n in self.notes[:i] if n[1] > lo + EPS and n[0] >= lo - self.longest - EPS]


NICE = [(x, 1.3) for x in (0.125, 0.25, 0.375, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0)] + \
    [(x, 1.05) for x in (1 / 6, 1 / 3, 2 / 3, 4 / 3)]  # (note value, how much longer than the sounding note it may be)
NICE.sort()


def written(notes) -> list[tuple[float, float, int]]:
    """Notes as written: on the 1/24-beat grid, humanized overlaps trimmed, gated lengths (pattern gate, slight
    staccato) read as their note value when that doesn't run into the next onset."""
    g = 24
    sn = sorted((round(a * g) / g, max(round(a * g) + 1, round(b * g)) / g, pi, b - a) for a, b, pi in notes)
    onsets = sorted({a for a, _, _, _ in sn})
    out = []
    for a, b, pi, d in sn:
        i = bisect.bisect_right(onsets, a + EPS)
        nxt = onsets[i] if i < len(onsets) else None
        if nxt is not None and (b > nxt + EPS and b - nxt <= 0.125 + EPS  # humanized overlap
                                or nxt > b + EPS and nxt - b <= min(0.125, 0.25 * d) + EPS):  # gated: legato
            b = nxt
        else:
            nice = next((x for x, k in NICE if d - EPS <= x <= d * k + 1 / 48), None)
            if nice is not None and (nxt is None or a + nice <= nxt + EPS):
                b = a + nice
        out.append((a, b, pi))
    return out


def _is_drums(t: Track) -> bool:
    if any(w in f"{t.name} {t.role or ''}".lower() for w in DRUM_WORDS):
        return True
    if t.instrument is not None and t.instrument.type == "synth" and t.instrument.name == "drums":
        return True
    if t.instrument is not None and t.instrument.patch:
        try:
            meta = plugins.get_patch(t.instrument.patch)
        except Exception:  # noqa: BLE001 - a missing patch just isn't a drum kit
            return False
        return bool(meta.get("drum_map") or {"drums", "kit", "percussion"} & set(meta.get("tags", [])))
    return False


def parts(p: Project, names: list | None = None) -> list[Part]:
    """MIDI parts (named ones, else every unmuted one) with their audible notes, roles assigned."""
    sel = list({id(t): t for t in (p.track(n) for n in names)}.values()) if names else \
        [t for t in p.tracks if not t.mute]
    out = []
    for t in sel:
        if t.kind != "midi":
            continue
        ns = [(c.start + n.start, c.start + min(n.end, c.length + 8), n.pitch) for c in t.clips
              if c.kind == "midi" and not c.muted for n in c.notes if n.start < c.length - 1e-9]
        out.append(Part(t.name, f"{t.name} {t.role or ''}".lower(), written(ns), _is_drums(t)))
    pitched = [x for x in out if not x.drums and x.notes]
    bass = min((x for x in pitched if "bass" in x.tag), key=lambda x: x.median, default=None)
    if bass is None and len(pitched) > 1:
        low = min(pitched, key=lambda x: x.median)
        if low.median < 48 and low.mono >= 0.7:
            bass = low
    lead = max((x for x in pitched if x is not bass and any(w in x.tag for w in LEAD_WORDS)),
               key=lambda x: len(x.notes), default=None)
    if lead is None:
        top = max((x for x in pitched if x is not bass and x.mono >= 0.85 and len(x.notes) >= 8),
                  key=lambda x: x.median, default=None)
        if top and all(top.median >= o.median + 5 for o in pitched if o is not top and o is not bass):
            lead = top
    for x, role in ((bass, "bass"), (lead, "lead")):
        if x is not None:
            x.role = role
    return out


# ---- chord inference --------------------------------------------------------------------------------------------

def _weights(notes, lo: float, hi: float) -> tuple[list[float], int | None]:
    """Pitch-class weights (beats sounding inside [lo, hi)) and the bass: the lowest note sounding on the downbeat,
    else the lowest one sounding a good while."""
    w = [0.0] * 12
    down = low = None
    need = min(0.4, 0.25 * (hi - lo))
    for a, b, pi in notes:
        ov = min(b, hi) - max(a, lo)
        if ov <= EPS:
            continue
        w[pi % 12] += ov
        if a <= lo + 0.1 and b > lo + 0.1 and (down is None or pi < down):
            down = pi
        if ov >= need - EPS and (low is None or pi < low):
            low = pi
    bass = down if down is not None else low
    return w, (bass % 12 if bass is not None else None)


def _fit(w, bass, root: int, iv: tuple, key_pcs: set) -> float:
    W = sum(w)
    pcs = {(root + i) % 12 for i in iv}
    inside = sum(w[pc] for pc in pcs)
    sc = 2 * inside - W
    for i in iv:
        if w[(root + i) % 12] < 0.05 * W:  # a triad may drop its 5th; a 6th/7th chord without it is an inversion
            sc -= (0.08 if i == 7 and len(iv) == 3 else 0.25) * W
    sc += (0.3 if bass == root else 0.1 if bass in pcs else 0) * W
    sc -= {4: 0.05, 2: 0.15}.get(len(iv), 0) * W
    if iv in ((0, 4, 8), (0, 3, 6)):
        sc -= 0.1 * W
    if not pcs <= key_pcs:
        sc -= 0.05 * W
    return sc


def _best(w, bass, key_pcs: set) -> tuple[float, int, str]:
    """(score, root, quality) of the best-fitting template; ties go to the simpler chord."""
    sc, _, r, q = max(((_fit(w, bass, r, iv, key_pcs), -len(iv), r, q) for r in range(12) for q, iv in TEMPLATES.items()),
                      key=lambda x: x[:2])
    return sc, r, q


def _conf(w, root: int, iv: tuple) -> float:
    W = sum(w)
    return sum(w[(root + i) % 12] for i in set(iv)) / W if W > EPS else 0.0


def _kids(lo: float, hi: float, unit: float) -> list[tuple[float, float]]:
    """Bar -> halves -> beats (dotted-quarter beats in compound meters)."""
    L = hi - lo
    n = L / unit
    if n <= 1 + EPS:
        return []
    if abs(n - round(n)) < EPS and round(n) % 2 == 0:
        return [(lo, lo + L / 2), (lo + L / 2, hi)]
    cuts = [lo + i * unit for i in range(int(n + EPS) + 1)]
    if hi - cuts[-1] > EPS:
        cuts.append(hi)
    cuts[-1] = hi
    return list(zip(cuts, cuts[1:]))


def _infer(notes, lo: float, hi: float, unit: float, key_pcs: set) -> list[tuple[float, float, object]]:
    """[(start, end, (root, quality, bass) | '?' | None)]: '?' = notes but no clear chord, None = nothing sounding."""
    w, bass = _weights(notes, lo, hi)
    if sum(w) < 0.05:
        return [(lo, hi, None)]
    _, root, q = _best(w, bass, key_pcs)
    iv = TEMPLATES[q]
    kids = _kids(lo, hi, unit)
    if kids:
        split = _conf(w, root, iv) < 0.7
        for a, b in kids if not split else ():
            wk, bk = _weights(notes, a, b)
            if sum(wk) < 0.05:
                continue
            ks, kr, kq = _best(wk, bk, key_pcs)
            if (kr, kq) != (root, q) and ks - _fit(wk, bk, root, iv, key_pcs) > 0.2 * sum(wk):
                split = True
                break
        if split:
            return [seg for a, b in kids for seg in _infer(notes, a, b, unit, key_pcs)]
    elif _conf(w, root, iv) < 0.55:
        return [(lo, hi, "?")]
    return [(lo, hi, (root, q, bass))]


def _chord(root: int, q: str, bass: int | None, flats: bool) -> Chord:
    iv = list(TEMPLATES[q])
    pcs = {(root + i) % 12 for i in iv}
    c = Chord(root, iv, bass if bass is not None and bass != root and bass in pcs else None)
    c.symbol = chord_name(c, flats)
    return c


@dataclass
class Seg:
    start: float
    end: float
    chord: Chord | None
    text: str  # what the score prints: Dm(i) / Am7 (chord track), Dm* (inferred), ? (unclear), . (nothing)
    source: str  # track | inferred | unclear | none
    token: str = ""  # the chord-track entry as written

    @property
    def desc(self) -> str:
        if self.source == "track":
            return f"{self.chord.symbol} ({self.token + ', ' if self.token != self.chord.symbol else ''}chord track)"
        return f"{self.chord.symbol} (implied by the parts)"


def beat_unit(p: Project) -> float:
    """The felt beat in quarter notes: a dotted quarter in compound meters (6/8, 9/8, 12/8), else a quarter."""
    num, den = p.time_sig
    return 1.5 if den == 8 and num % 3 == 0 and num > 3 else 1.0


def timeline(p: Project, pitched: list[Part], s: float, e: float) -> list[Seg]:
    """The harmony over [s, e): chord-track entries where there are some, inferred from the notes elsewhere."""
    bpb = p.beats_per_bar
    key = Key(p.key)
    flats, key_pcs = key.flats, key.pcs
    unit = beat_unit(p)
    segs: list[Seg] = []
    covered = []
    for x in sorted(p.chord_track, key=lambda x: x["at"]):
        a, b = max(x["at"], s), min(x["at"] + x["len"], e)
        if b > a + EPS:
            c = resolve(p, x)
            c.symbol = chord_name(c, flats)
            segs.append(Seg(a, b, c, label(p, x, flats), "track", x["chord"]))
            covered.append((a, b))
    notes = sorted(n for x in pitched for n in x.notes)
    starts = [n[0] for n in notes]
    longest = max((b - a for a, b, _ in notes), default=0.0)
    for bar in range(int(s // bpb + EPS), math.ceil(e / bpb - 1e-9)):
        lo, hi = max(bar * bpb, s), min((bar + 1) * bpb, e)
        gaps, t = [], lo
        for a, b in covered:
            if b <= lo + EPS or a >= hi - EPS:
                continue
            if a > t + EPS:
                gaps.append((t, a))
            t = max(t, b)
        if hi > t + EPS:
            gaps.append((t, hi))
        if not gaps:
            continue
        i0, i1 = bisect.bisect_left(starts, lo - longest - EPS), bisect.bisect_left(starts, hi - EPS)
        bar_notes = [n for n in notes[i0:i1] if n[1] > lo + EPS]
        for a, b, res in _infer(bar_notes, bar * bpb, (bar + 1) * bpb, unit, key_pcs):
            for ga, gb in gaps:
                x0, x1 = max(a, ga), min(b, gb)
                if x1 <= x0 + EPS:
                    continue
                if res is None:
                    segs.append(Seg(x0, x1, None, ".", "none"))
                elif res == "?":
                    segs.append(Seg(x0, x1, None, "?", "unclear"))
                else:
                    c = _chord(*res, flats)
                    segs.append(Seg(x0, x1, c, c.symbol + "*", "inferred"))
    segs.sort(key=lambda g: g.start)
    merged: list[Seg] = []
    for g in segs:
        m = merged[-1] if merged else None
        if m and m.source == g.source and m.text == g.text and abs(m.end - g.start) < EPS and g.source != "track":
            m.end = g.end
        else:
            merged.append(g)
    return merged


def _seg_at(segs: list[Seg], t: float) -> Seg | None:
    i = bisect.bisect_right([g.start for g in segs], t + EPS) - 1
    return segs[i] if i >= 0 and segs[i].end > t + EPS else None


# ---- harmony check ------------------------------------------------------------------------------------------------

@dataclass
class Finding:
    at: float
    parts: tuple
    msg: str


def _avoid(pc: int, ch: Chord) -> bool:
    """A clear clash: a semitone above a chord tone (a minor 9th against it), or a tritone over a minor chord's root.
    Whole-step tensions (9, 11, 13), b7 and maj7 are colours; b9/b13 are fine on a dominant 7th."""
    rel = {(x - ch.root) % 12 for x in ch.pcs}
    x = (pc - ch.root) % 12
    if any((pc - t) % 12 == 1 for t in ch.pcs):
        return not (4 in rel and 10 in rel and x in (1, 8))
    return x == 6 and 3 in rel and 6 not in rel


def _why(pc: int, ch: Chord, flats: bool) -> str:
    names = [note_name(t + 60, flats)[:-1] for t in ch.pcs if (pc - t) % 12 == 1]
    return f"a semitone above {names[0]}" if names else "a tritone above the root of a minor chord"


def _near(pitch: int, pcs, exclude: int | None = None) -> list[int]:
    """Nearest pitches below and above `pitch` whose pitch class is in pcs."""
    below = next((pitch - d for d in range(1, 12) if (pitch - d) % 12 in pcs and pitch - d != exclude), None)
    above = next((pitch + d for d in range(1, 12) if (pitch + d) % 12 in pcs and pitch + d != exclude), None)
    return [x for x in (below, above) if x is not None]


def _strong(p: Project) -> list[float]:
    num, den = p.time_sig
    half = p.beats_per_bar / 2
    return [0.0, half] if num in (4, 6, 12) or (num == 2 and den == 2) else [0.0]


def _pedals(bass: Part, segs: list[Seg], key: Key) -> list[tuple[float, float]]:
    """Spans where the bass holds one pitch class under two or more different chords: a deliberate pedal when it's the
    tonic or dominant, or lasts three chords or more starting on a chord tone."""
    runs, cur = [], []
    for g in segs:
        if g.chord is None:
            runs.append(cur)
            cur = []
            continue
        w, _ = _weights(bass.between(g.start, g.end), g.start, g.end)
        W = sum(w)
        pc = max(range(12), key=lambda k: w[k]) if W > EPS else None
        if pc is not None and w[pc] >= 0.9 * W and (not cur or cur[-1][1] == pc):
            cur.append((g, pc))
        else:
            runs.append(cur)
            cur = [(g, pc)] if pc is not None and w[pc] >= 0.9 * W else []
    runs.append(cur)
    home = {key.tonic, (key.tonic + 7) % 12}
    return [(r[0][0].start, r[-1][0].end) for r in runs
            if len(r) >= 2 and len({(g.chord.root, tuple(g.chord.intervals)) for g, _ in r}) >= 2
            and (r[0][1] in home or (len(r) >= 3 and r[0][1] in r[0][0].chord.pcs))]


def check(p: Project, prts: list[Part], segs: list[Seg], s: float, e: float) -> list[Finding]:
    bpb = p.beats_per_bar
    flats = Key(p.key).flats
    key_pcs = Key(p.key).pcs

    def fmt(b: float) -> str:
        return p.fmt_pos(round(b * 24) / 24)

    def nm(x: int) -> str:
        return note_name(x, flats)

    def pcn(x: int) -> str:
        return nm(x + 60)[:-1]

    pitched = [x for x in prts if not x.drums and x.notes]
    bass = next((x for x in pitched if x.role == "bass"), None)
    lead = next((x for x in pitched if x.role == "lead"), None)
    out: list[Finding] = []
    strong = _strong(p)
    pedal = _pedals(bass, segs, Key(p.key)) if bass else []

    # 1. lead / bass notes that clash with the chord on a strong beat (unless they move on by step)
    seen: dict[tuple, tuple[Finding, list]] = {}
    for part in (x for x in (lead, bass) if x is not None):
        line = part.line(top=part is lead)
        for i, (a, b, pi) in enumerate(line):
            if not s - EPS <= a < e - EPS or b - a < 0.4 - EPS:
                continue
            off = (a + 0.2) % bpb - 0.2
            if not any(abs(off - x) <= 0.13 for x in strong):
                continue
            g = _seg_at(segs, a + 0.02)
            if g is None or g.chord is None:
                continue
            ch, pc = g.chord, pi % 12
            if pc in ch.pcs or pc == ch.bass or (part is lead and not _avoid(pc, ch)):
                continue
            if part is bass and g.source != "track" and not any((t - pc) % 12 == 1 for t in ch.pcs):
                continue  # under implied harmony only a minor 9th against the bass is clear (D/E, F/G are colours)
            nxt = line[i + 1] if i + 1 < len(line) else None
            step = nxt is not None and (1 <= abs(nxt[2] - pi) <= 2 or (part is bass and (nxt[2] - pi) % 12 in (1, 2, 10, 11)))
            if step and nxt[0] <= b + 1.0:
                continue  # passing / neighbour / appoggiatura: it moves on by step (a bass may jump an octave too)
            if part is bass and any(x0 - EPS <= a < x1 - EPS for x0, x1 in pedal):
                continue
            key = (part.name, pi, id(g))
            if key in seen:  # the same note again under the same chord: one finding
                seen[key][1].append(fmt(a))
                continue
            if part is bass:
                target = ch.bass if ch.bass is not None else ch.root
                root = min((pi + d for d in range(-6, 7) if (pi + d) % 12 == target), key=lambda q: (abs(q - pi), q))
                others = "/".join(pcn(t) for t in ch.pcs if t != target)
                if g.source == "track":
                    msg = (f"{part.name} plays {nm(pi)} under {g.desc}: play {nm(root)} (root) or {others}, "
                           f"or change the chord track there (harmony) if {pcn(pc)} is meant")
                else:
                    above = next(pcn(t) for t in ch.pcs if (t - pc) % 12 == 1)
                    msg = (f"{part.name} {nm(pi)} clashes with {g.desc}, a semitone under {above}: use {nm(root)} "
                           f"(root) or {others}, or make it a passing note that moves on by step")
            else:
                alts = " or ".join(nm(x) for x in _near(pi, set(ch.pcs)))
                msg = (f"{part.name} {nm(pi)} clashes with {g.desc} on a strong beat ({_why(pc, ch, flats)}): "
                       f"use {alts}, or let it move on by step")
            f = Finding(a, (part.name,), msg)
            seen[key] = (f, [fmt(a)])
            out.append(f)
    for f, where in seen.values():
        f.msg = f"{', '.join(where)} {f.msg}"

    # 2. parallel fifths / octaves between the outer voices (runs of 4+ are a deliberate doubling)
    if bass and lead:
        bl, ll = bass.line(top=False), lead.line(top=True)
        b_starts = [x[0] for x in bl]
        events = []
        for j in range(1, len(ll)):
            t = ll[j][0]
            k = bisect.bisect_left(b_starts, t - 1 / 12)
            if k >= len(bl) or abs(bl[k][0] - t) > 1 / 12 or k == 0:
                events.append(None)
                continue
            pl, nl, pb, nb = ll[j - 1], ll[j], bl[k - 1], bl[k]
            kind = None
            if pl[1] >= t - 0.5 and pb[1] >= t - 0.5:
                dl, db = nl[2] - pl[2], nb[2] - pb[2]
                i1, i2 = pl[2] - pb[2], nl[2] - nb[2]
                if dl and db and (dl > 0) == (db > 0) and i1 > 0 and i2 > 0 and i1 % 12 == i2 % 12 and i1 % 12 in (0, 7):
                    kind = "fifths" if i1 % 12 == 7 else "octaves"
            events.append((t, kind, nb, nl) if kind else None)
        run: list = []
        for ev in events + [None]:
            if ev and (not run or run[-1][1] == ev[1]):
                run.append(ev)
                continue
            if 0 < len(run) < 4:
                for t, kind, nb, nl in run:
                    if not s - EPS <= t < e - EPS:
                        continue
                    g = _seg_at(segs, t + 0.02)
                    fix = "move one voice by step the other way (contrary motion)"
                    if g and g.chord:
                        cands = [q for q in range(nb[2] - 7, nb[2] + 8) if q % 12 in g.chord.pcs and q != nb[2]
                                 and (nl[2] - q) % 12 in (3, 4, 8, 9) and q < nl[2]]
                        if cands:
                            q = min(cands, key=lambda q: abs(q - nb[2]))
                            fix = (f"e.g. {bass.name} {nm(q)} there (a {'3rd' if (nl[2] - q) % 12 in (3, 4) else '6th'} "
                                   f"under {nm(nl[2])}), or contrary motion")
                    out.append(Finding(t, (bass.name, lead.name),
                                       f"{fmt(t)} parallel {kind} between {bass.name} and {lead.name} "
                                       f"({nm(nb[2])}/{nm(nl[2])}): {fix}"))
            run = [ev] if ev else []

    # 3. voice crossings by role: the bass above a part that normally sits over it, the lead under one normally below
    def crossings(low: Part, high: Part, use_low: bool):
        line = (low if use_low else high).line(top=not use_low)
        other = high if use_low else low
        outer = low is bass and high is lead
        samples = []
        for a, b, pi in line:
            if b - a < (0.4 if outer or not use_low else 1.0) - EPS:  # a bass octave pop is not a crossing
                continue
            snd = other.sounding(a + 0.02, 0.0 if outer else 1.0)  # a passing arpeggio note isn't a voice crossing
            if snd:
                samples.append((a, pi, min(snd) if use_low else max(snd)))
        if not samples:
            return
        ok = [(pi < q) if use_low else (pi > q) for _, pi, q in samples]
        if not outer and sum(ok) / len(ok) < 0.8:
            return  # these parts share a register on purpose
        hits = []
        for a, pi, q in samples:
            bad = (pi > q) if use_low else (pi < q - 2)
            if bad and s - EPS <= a < e - EPS:
                hits.append((a, pi, q))
            elif hits:
                yield hits
                hits = []
        if hits:
            yield hits

    if bass:
        for other in pitched:
            if other is bass:
                continue
            for hits in list(crossings(bass, other, True))[:3]:
                a, pi, q = hits[0]
                span = fmt(a) + (f"-{fmt(hits[-1][0])}" if len(hits) > 1 else "")
                lower = pi - 12 if pi - 12 >= 28 else None
                fix = (f"drop {bass.name} an octave there ({nm(lower)})" if lower else f"move {bass.name} lower") + \
                    f" or voice {other.name} higher"
                out.append(Finding(a, (bass.name, other.name),
                                   f"{span} {bass.name} {nm(pi)} crosses above {other.name} ({nm(q)}): {fix}"))
    if lead:
        for other in pitched:
            if other is lead or other is bass:
                continue
            for hits in list(crossings(other, lead, False))[:3]:
                a, pi, q = hits[0]
                span = fmt(a) + (f"-{fmt(hits[-1][0])}" if len(hits) > 1 else "")
                out.append(Finding(a, (lead.name, other.name),
                                   f"{span} {lead.name} {nm(pi)} dips under {other.name}'s top note {nm(q)}: keep "
                                   f"{lead.name} above it or voice {other.name} lower there (chords(high=...))"))

    # 4. inner parts that don't play the chord track's chord
    flagged: dict[str, list] = {}
    for x in sorted(p.chord_track, key=lambda x: x["at"]):
        a, b = max(x["at"], s), min(x["at"] + x["len"], e)
        if b <= a + EPS:
            continue
        ch = resolve(p, x)
        for part in pitched:
            if part.role != "inner":
                continue
            w, pb = _weights(part.between(a, b), a, b)
            W = sum(w)
            off = [pc for pc in range(12) if w[pc] > EPS and pc not in ch.pcs and pc != ch.bass]
            avoid = sum(w[pc] for pc in off if _avoid(pc, ch))
            # a chordal part mostly off the chord, a line more than half off it, or clear clashes (colour tones pass)
            bad = W >= 1.0 and (avoid >= 0.25 * W or sum(w[pc] for pc in off) >= (0.5 if part.mono >= 0.85 else 0.4) * W)
            own = _chord(*_best(w, pb, key_pcs)[1:], pb, flats) if bad else None
            if not bad or set(own.pcs) == set(ch.pcs):
                if part.name in flagged:
                    out.append(_disagree(p, part, flagged.pop(part.name), flats))
                continue
            flagged.setdefault(part.name, []).append((a, b, own.symbol, label(p, x, flats)))
    for name, items in flagged.items():
        out.append(_disagree(p, next(x for x in pitched if x.name == name), items, flats))
    out.sort(key=lambda f: f.at)
    if len(out) > MAX_FINDINGS:
        out = out[:MAX_FINDINGS] + [Finding(out[MAX_FINDINGS].at, (), f"... {len(out) - MAX_FINDINGS} more harmony "
                                            f"warnings: check a shorter range with score(start, end)")]
    return out


def _disagree(p: Project, part: Part, items: list, flats: bool) -> Finding:
    a, b = items[0][0], items[-1][1]
    rng = f"{p.fmt_pos(a)}-{p.fmt_pos(b)}"
    pairs = ", ".join(f"{own} where it says {want}" for _, _, own, want in items[:4]) + (" ..." if len(items) > 4 else "")
    return Finding(a, (part.name,), f"{rng} {part.name} doesn't follow the chord track (plays {pairs}): re-run "
                                    f"chords()/pattern() for {rng} without a progression so it follows the chord "
                                    f"track, or change the chord track there")


# ---- score view -----------------------------------------------------------------------------------------------------

def _bar_tokens(notes, lo: float, hi: float, flats: bool) -> list[tuple[float, str]]:
    """One part's notes in one bar as TTN (positions snapped to 1/24 beat, no velocities): [(beat_in_bar, token)].
    Notes crossing the bar line are cut there and tied ('~') into the next bar."""
    ev: dict[float, list] = {}
    for a, b, pi in notes:
        if b <= lo + EPS or a >= hi - EPS:
            continue
        x0 = round((max(a, lo) - lo) * 24) / 24
        x1 = max(x0 + 1 / 24, round((min(b, hi) - lo) * 24) / 24)
        ev.setdefault(x0, []).append((x1 - x0, pi, b > hi + EPS))
    out: list[tuple[float, str]] = []
    cursor, cur = 0.0, None

    def tok(text: str, d: float) -> str:
        nonlocal cur
        if cur is None or abs(d - cur) > EPS:
            cur = d
            return f"{text}:{_fmt_dur(d)}"
        return text

    for x0 in sorted(ev):
        grp = sorted(ev[x0], key=lambda g: (-g[0], g[1]))
        if x0 > cursor + EPS:
            out.append((cursor, tok("r", x0 - cursor)))
        elif x0 < cursor - EPS:
            out.append((x0, f"@{x0:g}"))
        durs = sorted({round(g[0], 6) for g in grp}, reverse=True)
        for k, d in enumerate(durs):
            sub = sorted((g for g in grp if abs(g[0] - d) < EPS), key=lambda g: g[1])
            names = [note_name(g[1], flats) for g in sub]
            if k:
                out.append((x0, f"@{x0:g}"))
            out.append((x0, tok(names[0] if len(names) == 1 else "(" + " ".join(names) + ")", d)
                        + ("~" if any(g[2] for g in sub) else "")))
        cursor = x0 + durs[-1]
    return out


def score(p: Project, s: float, e: float, names: list | None = None, check_harmony: bool = True) -> dict:
    bpb = p.beats_per_bar
    key = Key(p.key)
    flats = key.flats
    every = parts(p)
    pitched = [x for x in every if not x.drums and x.notes]
    shown = parts(p, names) if names else [x for x in every if not x.drums]
    if names:  # same roles as in the whole arrangement
        roles = {x.name: x.role for x in every}
        for x in shown:
            x.role = roles.get(x.name, x.role)
    shown = sorted((x for x in shown if x.notes), key=lambda x: (x.drums, -x.median))  # high to low, like a score
    segs = timeline(p, pitched, s, e)
    findings = check(p, every, segs, s, e) if check_harmony else []
    if names:
        keep = {x.name for x in shown}
        findings = [f for f in findings if keep & set(f.parts)]
    by_bar: dict[int, list[Finding]] = {}
    for f in findings:
        by_bar.setdefault(int(f.at // bpb + EPS), []).append(f)
    unit = beat_unit(p)
    ncols = max(1, math.ceil(bpb / unit - EPS))
    name_w = min(12, max([5] + [len(x.name) for x in shown]))
    first, last = int(s // bpb + EPS), math.ceil(e / bpb - 1e-9)
    hidden = [t.name for t in p.tracks if t.kind == "midi" and t.name not in {x.name for x in shown}
              and any(c.notes for c in t.clips)]
    bars = f"{first + 1}-{last}" if last > first + 1 else f"{first + 1}"
    lines = [f"{p.name}: {key.text}, {p.time_sig[0]}/{p.time_sig[1]}, {p.tempo:g} bpm, bars {bars}"
             + (f" (not shown: {', '.join(hidden)})" if hidden else ""),
             ("chord per beat: Dm(i) chord track (roman numerals in the key), Dm* inferred from the notes, ? unclear, "
              "- same chord, . none"),
             ("parts: TTN per bar, no velocities; ~ ties into the next bar; % same as the bar before; tokens sit "
              "under the beat they start on; ! harmony warning")]
    prev: dict[str, list] = {}
    empty: list[int] = []

    def flush():
        if empty:
            lines.append(f"bar {empty[0] + 1}" + (f"-{empty[-1] + 1}" if len(empty) > 1 else "") + ": no notes")
            empty.clear()

    for bar in range(first, last):
        lo, hi = bar * bpb, (bar + 1) * bpb
        rows = []
        for x in shown:
            toks = _bar_tokens(x.between(lo, hi), lo, hi, flats)
            if not toks:
                prev.pop(x.name, None)
                continue
            if prev.get(x.name) == toks:
                rows.append((x.name, [["%"]] + [[] for _ in range(ncols - 1)]))
                continue
            prev[x.name] = toks
            cols = [[] for _ in range(ncols)]
            for at, t in toks:
                cols[min(ncols - 1, int(at / unit + EPS))].append(t)
            rows.append((x.name, cols))
        bar_segs = [g for g in segs if g.end > lo + EPS and g.start < hi - EPS]
        if not rows and all(g.source == "none" for g in bar_segs):
            empty.append(bar)
            continue
        flush()
        chord_cols: list[list[str]] = [[] for _ in range(ncols)]
        last_seg = None
        for c in range(ncols):
            t = lo + c * unit
            g = _seg_at(segs, t)
            chord_cols[c].append("." if g is None else "-" if g is last_seg and c else g.text)
            last_seg = g
            for g2 in bar_segs:
                if t + EPS < g2.start < min(t + unit, hi) - EPS:
                    chord_cols[c].append(f"{g2.text}@{g2.start - lo + 1:g}")
                    last_seg = g2
        rows.insert(0, ("chord", chord_cols))
        widths = [max(len(" ".join(r[1][c])) for r in rows) for c in range(ncols)]
        head = f"bar {bar + 1}"
        for m in sorted(p.markers, key=lambda m: m["at"]):
            if lo - EPS <= m["at"] < hi - EPS:
                head += f" [{m['name']}]"
        if p.loop and lo - EPS <= p.loop["start"] < hi - EPS:
            head += " loop start"
        if p.loop and lo + EPS < p.loop["end"] <= hi + EPS:
            head += " loop end"
        lines.append(head)
        for name, cols in rows:
            cells = [" ".join(cols[c]).ljust(widths[c]) for c in range(ncols)]
            lines.append((f"  {name[:name_w].ljust(name_w)}  " + "  ".join(cells)).rstrip())
        for f in by_bar.get(bar, []):
            lines.append(f"  ! {f.msg}")
    flush()
    res = {"bars": bars, "score": "\n".join(lines)}
    if check_harmony:
        res["warnings"] = [f.msg for f in findings]
    return res


def warnings(p: Project, s: float | None = None, e: float | None = None) -> list[str]:
    """The harmony check over the whole song (or [s, e)) as messages, for analyze()."""
    s = 0.0 if s is None else s
    e = max(p.end, track_end(p)) if e is None else e
    if e <= s + EPS:
        return []
    prts = parts(p)
    pitched = [x for x in prts if not x.drums and x.notes]
    if not pitched:
        return []
    return [f.msg for f in check(p, prts, timeline(p, pitched, s, e), s, e)]
