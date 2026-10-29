"""Deterministic pattern builders: voiced chords, arpeggios/basslines from chord-degree patterns, drum grids.

These turn musical decisions (a progression, a rhythm, a groove) into notes. They don't invent music.
"""
from __future__ import annotations

import re

from .model import Note
from .notation import parse_duration
from .theory import Chord, voice_chord

GM_DRUMS = {
    "kick": 36, "kick2": 35, "snare": 38, "snare2": 40, "rim": 37, "sidestick": 37, "clap": 39,
    "hat": 42, "hh": 42, "closed_hat": 42, "chh": 42, "pedal_hat": 44, "open_hat": 46, "ohh": 46,
    "tom_floor": 41, "tom_low": 45, "tom_mid": 47, "tom_high": 50, "tom1": 50, "tom2": 47, "tom3": 45, "tom4": 41,
    "crash": 49, "crash2": 57, "ride": 51, "ride_bell": 53, "china": 52, "splash": 55,
    "tambourine": 54, "cowbell": 56, "shaker": 70, "maracas": 70, "cabasa": 69, "claves": 75,
    "woodblock": 76, "triangle": 81, "conga_hi": 62, "conga_lo": 64, "bongo_hi": 60, "bongo_lo": 61,
    "timbale_hi": 65, "timbale_lo": 66, "agogo": 67, "guiro": 73, "vibraslap": 58,
}


def parse_rhythm(text: str | None, length: float) -> list[tuple[float, float, bool, bool]]:
    """Rhythm string -> [(offset, duration, is_rest, accent)] repeated to fill `length` beats.

    Tokens are durations ('q', 'e.', 'h', '1.5'); prefix 'r' or 'r:' for a rest, suffix '>' for an accent.
    Example: "q. e r:q q" ; None = one hit lasting the whole length.
    """
    if not text:
        return [(0.0, length, False, False)]
    cells = []
    for tok in text.split():
        rest = tok.startswith("r")
        accent = tok.endswith(">")
        core = tok.lstrip("r").lstrip(":").rstrip(">") or "q"
        cells.append((parse_duration(core), rest, accent))
    cycle = sum(c[0] for c in cells)
    out, t = [], 0.0
    while t < length - 1e-6:
        for d, rest, accent in cells:
            if t >= length - 1e-6:
                break
            out.append((t, min(d, length - t), rest, accent))
            t += d
        if cycle <= 0:
            break
    return out


def chord_notes(progression: list[tuple[Chord, float]], rhythm: str | None = None, voices: int = 4,
                low: int = 48, high: int = 76, vel: int = 80, gate: float = 1.0, bass: bool = False,
                bass_octave: int = 2, start: float = 0.0, prev: list[int] | None = None) -> list[Note]:
    """Block chords with smooth voice leading. With bass=True a root (or slash-bass) note is added in bass_octave."""
    notes: list[Note] = []
    t = start
    for chord, length in progression:
        voicing = voice_chord(chord, voices, low, high, prev)
        prev = voicing
        for off, d, rest, accent in parse_rhythm(rhythm, length):
            if rest:
                continue
            v = min(127, vel + (14 if accent else 0))
            for p in voicing:
                notes.append(Note(p, t + off, d * gate, v))
            if bass:
                bp = (bass_octave + 1) * 12 + (chord.bass if chord.bass is not None else chord.root)
                notes.append(Note(bp, t + off, d * gate, v))
        t += length
    return notes


_PAT_TOKEN = re.compile(r"^(\d+)([b#]?)([+\-]*)(>?)$")


def pattern_notes(progression: list[tuple[Chord, float]], pattern: str, rate: float = 0.5, octave: int = 4,
                  vel: int = 85, gate: float = 0.9, start: float = 0.0, restart: bool = True,
                  accent: int = 12) -> list[Note]:
    """Arpeggios / basslines / ostinatos from chord-degree patterns, following the progression.

    pattern tokens (one per `rate` beats):
        1 3 5 7 9   chord tones (the chord's own 3rd/5th/7th), 8 / 10 / 12 = root / 3rd / 5th an octave up
        b7 #4       explicitly altered degree (b = -1, # = +1 semitone)
        5- 3+ 1--   shift down / up an octave per sign
        1>          accent
        .           rest
        -           hold (extend the previous note)
    The chord root sits in `octave` (C4 = octave 4). restart=True restarts the pattern on each chord.
    """
    toks = pattern.split()
    if not toks:
        return []
    notes: list[Note] = []
    t = start
    step = 0
    for chord, length in progression:
        if restart:
            step = 0
        n_steps = int(round(length / rate))
        base = (octave + 1) * 12 + chord.root
        last: Note | None = None
        for k in range(n_steps):
            tok = toks[step % len(toks)]
            step += 1
            at = t + k * rate
            if tok == ".":
                last = None
                continue
            if tok == "-":
                if last is not None:
                    last.dur = (at + rate) - last.start
                    last.dur = max(last.dur, 0)
                continue
            m = _PAT_TOKEN.match(tok)
            if not m:
                raise ValueError(f"Bad pattern token {tok!r} (use chord degrees like 1 3 5 8, '.', '-', 5-, 3+)")
            deg, alt, octs, acc = m.groups()
            semis = chord.tone(int(deg)) + {"b": -1, "#": 1, "": 0}[alt]
            semis += 12 * (octs.count("+") - octs.count("-"))
            n = Note(base + semis, at, rate * gate, min(127, vel + (accent if acc else 0)))
            notes.append(n)
            last = n
        t += length
    return notes


def drum_grid(grid: dict[str, str], steps_per_beat: int = 4, bars: int = 1, beats_per_bar: float = 4.0,
              drum_map: dict[str, int] | None = None, swing: float = 0.0, vel: int = 100, start: float = 0.0) -> list[Note]:
    """Step-sequencer drums. grid = {"kick": "x...x...x...x...", "snare": "....x.......x..."}.

    Step chars: x hit, X accent, o ghost (soft), . or - rest. '|' separates alternative bars that cycle
    (e.g. "x...x...|x...x.x."). Spaces are ignored. Keys are drum names (GM by default) or MIDI numbers.
    swing 0..1 delays every second step (0.5 ≈ a light shuffle, 1 = full triplet feel).
    """
    dm = {**GM_DRUMS, **(drum_map or {})}
    step_len = 1.0 / steps_per_beat
    steps_per_bar = int(round(beats_per_bar * steps_per_beat))
    notes: list[Note] = []
    for name, pat in grid.items():
        key = str(name).strip().lower()
        if key.isdigit():
            pitch = int(key)
        elif key in dm:
            pitch = dm[key]
        else:
            raise ValueError(f"Unknown drum {name!r}. Known: {', '.join(sorted(dm))}")
        variants = [v.replace(" ", "") for v in pat.split("|")]
        for b in range(bars):
            cells = variants[b % len(variants)]
            if not cells:
                continue
            for s in range(steps_per_bar):
                ch = cells[s % len(cells)]
                if ch in ".-_":
                    continue
                v = {"x": vel, "X": min(127, vel + 22), "o": max(1, int(vel * 0.45)), "O": max(1, int(vel * 0.6))}.get(ch)
                if v is None:
                    raise ValueError(f"Bad drum step {ch!r} in {name}: use x X o . (and | between bars)")
                at = start + b * beats_per_bar + s * step_len
                if swing and s % 2 == 1:
                    at += swing * step_len / 3.0
                notes.append(Note(pitch, at, step_len * 0.9, v))
    return notes
