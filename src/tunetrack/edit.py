"""Note editing operations. All work in place on lists of Note (positions relative to their clip)."""
from __future__ import annotations

import random

from .model import Note
from .theory import Key


def select(notes: list[Note], start: float | None = None, end: float | None = None,
           low: int | None = None, high: int | None = None) -> list[Note]:
    return [n for n in notes
            if (start is None or n.start >= start - 1e-9) and (end is None or n.start < end - 1e-9)
            and (low is None or n.pitch >= low) and (high is None or n.pitch <= high)]


def transpose(notes: list[Note], semitones: int = 0, steps: int = 0, key: Key | None = None) -> None:
    for n in notes:
        if steps and key:
            n.pitch = key.step(n.pitch, steps)
        n.pitch = max(0, min(127, n.pitch + semitones))


def fit_to_scale(notes: list[Note], key: Key) -> int:
    changed = 0
    for n in notes:
        p = key.snap(n.pitch)
        if p != n.pitch:
            n.pitch = p
            changed += 1
    return changed


def quantize(notes: list[Note], grid: float = 0.25, strength: float = 1.0, ends: bool = False, swing: float = 0.0) -> None:
    """Move note starts toward the grid (keeping durations); ends=True also snaps note ends."""
    for n in notes:
        end = n.end
        k = round(n.start / grid)
        target = k * grid + (swing * grid / 3.0 if swing and k % 2 == 1 else 0.0)
        n.start += (target - n.start) * strength
        if ends:
            n.dur = max(grid * 0.5, round(end / grid) * grid - n.start)


def humanize(notes: list[Note], timing: float = 0.01, velocity: int = 6, seed: int = 1) -> None:
    """timing in beats (±), velocity in steps (±). Deterministic for a given seed."""
    rng = random.Random(seed)
    for n in notes:
        if timing:
            shift = rng.gauss(0, timing / 2)
            shift = max(-timing, min(timing, shift))
            n.start = max(0.0, n.start + shift)
        if velocity:
            n.vel = max(1, min(127, n.vel + int(round(rng.gauss(0, velocity / 2)))))


def velocity(notes: list[Note], set_to: int | None = None, scale: float = 1.0, add: int = 0,
             ramp: tuple[int, int] | None = None) -> None:
    """Set / scale / offset velocities, or ramp linearly (crescendo/diminuendo) across the selection."""
    if ramp and notes:
        s0 = min(n.start for n in notes)
        s1 = max(n.start for n in notes)
        span = (s1 - s0) or 1.0
        for n in notes:
            n.vel = int(round(ramp[0] + (ramp[1] - ramp[0]) * (n.start - s0) / span))
    for n in notes:
        if set_to is not None:
            n.vel = set_to
        n.vel = max(1, min(127, int(round(n.vel * scale + add))))


def legato(notes: list[Note], overlap: float = 0.0) -> None:
    """Extend each note to the start of the next later note (plus overlap, for legato-transition patches)."""
    starts = sorted({round(n.start, 6) for n in notes})
    for n in notes:
        nxt = next((s for s in starts if s > n.start + 1e-6), None)
        if nxt is not None:
            n.dur = nxt - n.start + overlap


def set_length(notes: list[Note], dur: float | None = None, scale: float = 1.0) -> None:
    for n in notes:
        if dur is not None:
            n.dur = dur
        n.dur = max(1e-3, n.dur * scale)


def stretch(notes: list[Note], factor: float, origin: float = 0.0) -> None:
    for n in notes:
        n.start = origin + (n.start - origin) * factor
        n.dur *= factor


def shift(notes: list[Note], beats: float) -> None:
    """Move notes in time. Starts may go below 0 (before the clip); the caller re-fits the clip."""
    for n in notes:
        n.start = n.start + beats


def reverse(notes: list[Note], start: float, end: float) -> None:
    for n in notes:
        n.start = start + (end - n.end)


def invert(notes: list[Note], axis: int | None = None, key: Key | None = None) -> None:
    if not notes:
        return
    axis = axis if axis is not None else notes[0].pitch
    for n in notes:
        n.pitch = 2 * axis - n.pitch
        if key:
            n.pitch = key.snap(n.pitch)


def double(notes: list[Note], interval: int = 12) -> list[Note]:
    """Return copies of notes moved by an interval (e.g. 12 = octave up, -12, 7 = fifth)."""
    return [Note(n.pitch + interval, n.start, n.dur, n.vel) for n in notes if 0 <= n.pitch + interval <= 127]


def trim_overlaps(notes: list[Note]) -> int:
    """Shorten notes that run into the next note of the same pitch (MIDI can't hold one key twice)."""
    trimmed = 0
    by_pitch: dict[int, list[Note]] = {}
    for n in notes:
        by_pitch.setdefault(n.pitch, []).append(n)
    for group in by_pitch.values():
        group.sort(key=lambda n: n.start)
        for a, b in zip(group, group[1:]):
            if a.end > b.start + 1e-9 and b.start > a.start + 1e-9:
                a.dur = max(0.01, b.start - a.start)
                trimmed += 1
    return trimmed


def dedupe(notes: list[Note]) -> int:
    """Remove exact duplicates and trim same-pitch overlaps (they cause stuck/doubled voices in samplers)."""
    notes.sort(key=lambda n: (n.pitch, n.start))
    keep: list[Note] = []
    removed = 0
    for n in notes:
        if keep and keep[-1].pitch == n.pitch:
            prev = keep[-1]
            if abs(prev.start - n.start) < 1e-6:
                prev.dur = max(prev.dur, n.dur)
                prev.vel = max(prev.vel, n.vel)
                removed += 1
                continue
            if prev.end > n.start:
                prev.dur = n.start - prev.start
        keep.append(n)
    notes[:] = keep
    notes.sort(key=lambda n: (n.start, n.pitch))
    return removed
