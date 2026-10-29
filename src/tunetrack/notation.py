"""TuneTrack note notation (TTN): a compact text format for writing and reading notes.

    D4:q!mf F4 A4:h | (D3 A3 F4):w~ | (D3 A3 F4):h r:h

Tokens (whitespace separated):
    C4  F#3  Bb5  61     a note (scientific pitch, middle C = C4) or a MIDI number
    r                    rest
    (C4 E4 G4)           chord; modifiers go after the ')'
    :q                   duration, sticky until changed: w h q e s t (whole..32nd), dotted 'q.', triplet 'e3',
                         beats '1.5' or '3/2', sums 'h+e'
    !mf  !96             velocity, sticky: ppp pp p mp mf f ff fff or 1-127
    '                    staccato (sounds 50% of its length)
    >                    accent (+16 velocity, this note only)
    ~                    tie into the next note(s) of the same pitch
    |                    bar line: checked against the time signature, warns if a bar is short/long
    @6.5                 move the cursor to an absolute beat in the clip (overlapping voices)
"""
from __future__ import annotations

import re
from fractions import Fraction

from .model import Note
from .theory import DYNAMICS, note_name, note_number

DUR_LETTERS = {"w": 4.0, "h": 2.0, "q": 1.0, "e": 0.5, "s": 0.25, "t": 0.125}
_TOKEN = re.compile(r"\(|\)[^\s()|]*|\||[^\s()|]+")
_PITCH = re.compile(r"^([A-Ga-g][#b]*-?\d+|r|R|\d{1,3})(.*)$")
_MOD = re.compile(r":([0-9./+]+|[whqest][.3]*(?:\+[whqest][.3]*)*)|!([a-z]+|\d{1,3})|(')|(>)|(~)")


def parse_duration(s: str) -> float:
    total = 0.0
    for part in s.split("+"):
        if not part:
            continue
        if part[0] in DUR_LETTERS:
            d = DUR_LETTERS[part[0]]
            for ch in part[1:]:
                if ch == ".":
                    d *= 1.5
                elif ch == "3":
                    d *= 2.0 / 3.0
            total += d
        elif "/" in part:
            a, b = part.split("/", 1)
            total += float(a) / float(b)
        else:
            total += float(part)
    if total <= 0:
        raise ValueError(f"Bad duration {s!r}")
    return total


def parse_velocity(s: str) -> int:
    if s.isdigit():
        return max(1, min(127, int(s)))
    if s not in DYNAMICS:
        raise ValueError(f"Bad velocity/dynamic {s!r}; use {' '.join(DYNAMICS)} or 1-127")
    return DYNAMICS[s]


def _mods(text: str, state: dict) -> dict:
    """Apply modifier text like ':q.!mf>' — updates sticky state, returns per-note flags."""
    flags = {"staccato": False, "accent": False, "tie": False}
    pos = 0
    while pos < len(text):
        m = _MOD.match(text, pos)
        if not m:
            raise ValueError(f"Can't read modifier {text[pos:]!r}")
        if m.group(1):
            state["dur"] = parse_duration(m.group(1))
        elif m.group(2):
            state["vel"] = parse_velocity(m.group(2))
        elif m.group(3):
            flags["staccato"] = True
        elif m.group(4):
            flags["accent"] = True
        elif m.group(5):
            flags["tie"] = True
        pos = m.end()
    return flags


def parse(text: str, beats_per_bar: float = 4.0, dur: float = 1.0, vel: int = 90) -> tuple[list[Note], float, list[str]]:
    """Parse TTN text. Returns (notes with starts relative to 0, length in beats, warnings)."""
    state = {"dur": dur, "vel": vel}
    notes: list[Note] = []
    warnings: list[str] = []
    ties: dict[int, Note] = {}
    cursor = 0.0
    end = 0.0
    bar_start = 0.0
    bar_no = 1
    tokens = _TOKEN.findall(text)
    i = 0

    def emit(pitches: list[int], flags: dict):
        nonlocal cursor, end
        d = state["dur"]
        v = min(127, state["vel"] + (16 if flags["accent"] else 0))
        new_ties = {}
        for p in pitches:
            prev = ties.get(p)
            if prev is not None and abs(prev.start + prev.dur - cursor) < 1e-6:
                prev.dur += d
                n = prev
            else:
                n = Note(p, cursor, d * (0.5 if flags["staccato"] else 1.0), v)
                notes.append(n)
            if flags["tie"]:
                new_ties[p] = n
        ties.clear()
        ties.update(new_ties)
        cursor += d
        end = max(end, cursor)

    while i < len(tokens):
        tok = tokens[i]
        if tok == "|":
            length = cursor - bar_start
            if abs(length - beats_per_bar) > 1e-6:
                warnings.append(f"bar {bar_no} has {length:g} beats (expected {beats_per_bar:g})")
            bar_start = cursor
            bar_no += 1
        elif tok == "(":
            pitches = []
            i += 1
            while i < len(tokens) and not tokens[i].startswith(")"):
                pitches.append(note_number(tokens[i]))
                i += 1
            if i >= len(tokens):
                raise ValueError("Unclosed '(' chord")
            flags = _mods(tokens[i][1:], state)
            emit(pitches, flags)
        elif tok.startswith("@"):
            cursor = float(Fraction(tok[1:])) if "/" in tok else float(tok[1:])
            bar_start = cursor - (cursor % beats_per_bar)
            bar_no = int(cursor // beats_per_bar) + 1
        else:
            m = _PITCH.match(tok)
            if not m:
                raise ValueError(f"Can't read token {tok!r}")
            p, rest = m.groups()
            flags = _mods(rest, state)
            if p in ("r", "R"):
                cursor += state["dur"]
                end = max(end, cursor)
                ties.clear()
            else:
                emit([note_number(p)], flags)
        i += 1
    return notes, end, warnings


def _fmt_dur(d: float) -> str:
    for name, base in DUR_LETTERS.items():
        if abs(d - base) < 1e-6:
            return name
        if abs(d - base * 1.5) < 1e-6:
            return name + "."
        if abs(d - base * 2 / 3) < 1e-6:
            return name + "3"
    f = Fraction(d).limit_denominator(48)
    if abs(float(f) - d) < 1e-6 and f.denominator not in (1, 2, 4, 8):
        return f"{f.numerator}/{f.denominator}"
    return f"{d:g}"


def format_notes(notes: list[Note], beats_per_bar: float = 4.0, flats: bool = False) -> str:
    """Notes -> TTN text that parses back to the same notes (bar lines where a bar boundary falls)."""
    if not notes:
        return ""
    groups: dict[float, list[Note]] = {}
    for n in sorted(notes, key=lambda n: (n.start, n.pitch)):
        groups.setdefault(round(n.start, 6), []).append(n)
    out: list[str] = []
    cursor, cur_dur, cur_vel = 0.0, None, None

    def tok(pitches: str, d: float, v: int) -> str:
        nonlocal cur_dur, cur_vel
        s = pitches
        if cur_dur is None or abs(d - cur_dur) > 1e-6:
            s += ":" + _fmt_dur(d)
            cur_dur = d
        if v != cur_vel:
            s += f"!{v}"
            cur_vel = v
        return s

    def barline(at: float):
        if at > 0 and abs(at / beats_per_bar - round(at / beats_per_bar)) < 1e-6 and (not out or out[-1] != "|"):
            out.append("|")

    for start, grp in groups.items():
        if start > cursor + 1e-6:
            gap = start - cursor
            # fill rests bar by bar so bar lines line up
            while gap > 1e-6:
                barline(cursor)
                to_bar = beats_per_bar - (cursor % beats_per_bar)
                r = min(gap, to_bar)
                out.append(tok("r", r, cur_vel if cur_vel is not None else grp[0].vel))
                cursor += r
                gap -= r
            barline(cursor)
        elif start < cursor - 1e-6:
            out.append(f"@{start:g}")
        else:
            barline(cursor)
        same = all(abs(n.dur - grp[0].dur) < 1e-6 and n.vel == grp[0].vel for n in grp)
        if same:
            names = [note_name(n.pitch, flats) for n in grp]
            label = names[0] if len(names) == 1 else "(" + " ".join(names) + ")"
            out.append(tok(label, grp[0].dur, grp[0].vel))
            cursor = start + grp[0].dur
        else:
            for k, n in enumerate(sorted(grp, key=lambda n: -n.dur)):
                if k:
                    out.append(f"@{start:g}")
                out.append(tok(note_name(n.pitch, flats), n.dur, n.vel))
            # the parser leaves the cursor after the last (shortest) emitted note
            cursor = start + min(n.dur for n in grp)
    return " ".join(out)
