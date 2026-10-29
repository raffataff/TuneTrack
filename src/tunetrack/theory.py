"""Music theory: pitches, keys/modes, chord symbols, roman numerals, voice-led voicings, instrument ranges."""
from __future__ import annotations

import itertools
import re

NOTE_PC = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
SHARP_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
FLAT_NAMES = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]

MODES = {
    "major": [0, 2, 4, 5, 7, 9, 11], "ionian": [0, 2, 4, 5, 7, 9, 11],
    "minor": [0, 2, 3, 5, 7, 8, 10], "aeolian": [0, 2, 3, 5, 7, 8, 10], "natural minor": [0, 2, 3, 5, 7, 8, 10],
    "dorian": [0, 2, 3, 5, 7, 9, 10], "phrygian": [0, 1, 3, 5, 7, 8, 10], "lydian": [0, 2, 4, 6, 7, 9, 11],
    "mixolydian": [0, 2, 4, 5, 7, 9, 10], "locrian": [0, 1, 3, 5, 6, 8, 10],
    "harmonic minor": [0, 2, 3, 5, 7, 8, 11], "melodic minor": [0, 2, 3, 5, 7, 9, 11],
    "phrygian dominant": [0, 1, 4, 5, 7, 8, 10], "hungarian minor": [0, 2, 3, 6, 7, 8, 11],
    "double harmonic": [0, 1, 4, 5, 7, 8, 11], "lydian dominant": [0, 2, 4, 6, 7, 9, 10],
    "major pentatonic": [0, 2, 4, 7, 9], "minor pentatonic": [0, 3, 5, 7, 10], "blues": [0, 3, 5, 6, 7, 10],
    "whole tone": [0, 2, 4, 6, 8, 10], "diminished": [0, 2, 3, 5, 6, 8, 9, 11], "chromatic": list(range(12)),
    "hirajoshi": [0, 2, 3, 7, 8], "in sen": [0, 1, 5, 7, 10],
}

CHORD_QUALITIES = {
    "": [0, 4, 7], "maj": [0, 4, 7], "M": [0, 4, 7], "m": [0, 3, 7], "min": [0, 3, 7], "-": [0, 3, 7],
    "dim": [0, 3, 6], "°": [0, 3, 6], "o": [0, 3, 6], "aug": [0, 4, 8], "+": [0, 4, 8],
    "sus2": [0, 2, 7], "sus4": [0, 5, 7], "sus": [0, 5, 7], "5": [0, 7],
    "6": [0, 4, 7, 9], "m6": [0, 3, 7, 9], "6/9": [0, 4, 7, 9, 14], "69": [0, 4, 7, 9, 14],
    "7": [0, 4, 7, 10], "maj7": [0, 4, 7, 11], "M7": [0, 4, 7, 11], "Δ": [0, 4, 7, 11], "Δ7": [0, 4, 7, 11],
    "m7": [0, 3, 7, 10], "min7": [0, 3, 7, 10], "-7": [0, 3, 7, 10], "mmaj7": [0, 3, 7, 11], "mM7": [0, 3, 7, 11],
    "m7b5": [0, 3, 6, 10], "ø": [0, 3, 6, 10], "ø7": [0, 3, 6, 10], "dim7": [0, 3, 6, 9], "°7": [0, 3, 6, 9],
    "o7": [0, 3, 6, 9], "7sus4": [0, 5, 7, 10], "7sus2": [0, 2, 7, 10], "aug7": [0, 4, 8, 10], "7#5": [0, 4, 8, 10],
    "7b5": [0, 4, 6, 10], "add9": [0, 4, 7, 14], "add2": [0, 2, 4, 7], "madd9": [0, 3, 7, 14], "add11": [0, 4, 7, 17],
    "9": [0, 4, 7, 10, 14], "maj9": [0, 4, 7, 11, 14], "m9": [0, 3, 7, 10, 14], "7b9": [0, 4, 7, 10, 13],
    "7#9": [0, 4, 7, 10, 15], "7#11": [0, 4, 7, 10, 18], "maj7#11": [0, 4, 7, 11, 18],
    "11": [0, 4, 7, 10, 14, 17], "m11": [0, 3, 7, 10, 14, 17], "13": [0, 4, 7, 10, 14, 21], "maj13": [0, 4, 7, 11, 14, 21],
    "m13": [0, 3, 7, 10, 14, 21],
}
_QUALITY_KEYS = sorted(CHORD_QUALITIES, key=len, reverse=True)

# (lowest, highest, comfortable_low, comfortable_high) in MIDI note numbers
RANGES = {
    "piccolo": (74, 108, 76, 103), "flute": (60, 96, 62, 93), "alto flute": (55, 91, 57, 86), "oboe": (58, 91, 60, 86),
    "english horn": (52, 84, 55, 81), "clarinet": (50, 94, 52, 89), "bass clarinet": (34, 77, 38, 72),
    "bassoon": (34, 75, 36, 70), "contrabassoon": (22, 53, 24, 50),
    "horn": (34, 77, 41, 72), "trumpet": (54, 82, 57, 79), "trombone": (40, 72, 43, 70),
    "bass trombone": (34, 67, 36, 65), "tuba": (26, 65, 29, 58),
    "violin": (55, 103, 55, 93), "viola": (48, 88, 48, 81), "cello": (36, 76, 36, 69), "contrabass": (28, 60, 28, 55),
    "double bass": (28, 60, 28, 55), "harp": (23, 104, 28, 98), "piano": (21, 108, 28, 100),
    "celesta": (60, 108, 60, 103), "glockenspiel": (79, 108, 79, 105), "xylophone": (65, 108, 65, 103),
    "marimba": (45, 96, 48, 93), "vibraphone": (53, 89, 53, 89), "timpani": (40, 57, 41, 55),
    "soprano": (60, 81, 62, 79), "alto": (53, 74, 55, 72), "tenor": (48, 69, 50, 67), "bass voice": (40, 62, 41, 60),
    "choir": (40, 81, 43, 79), "guitar": (40, 88, 40, 84), "bass guitar": (28, 67, 28, 60), "bass": (28, 67, 28, 60),
    "sub bass": (24, 52, 28, 48), "pad": (36, 96, 48, 84), "lead": (48, 96, 55, 91), "organ": (36, 96, 36, 91),
    "strings": (28, 103, 36, 93), "brass": (26, 82, 34, 79), "woodwinds": (34, 96, 36, 93),
}

DYNAMICS = {"ppp": 20, "pp": 33, "p": 49, "mp": 64, "mf": 80, "f": 96, "ff": 112, "fff": 124}


def note_number(name: str | int) -> int:
    """'C4' -> 60 (scientific pitch, middle C = C4). Accepts ints and digit strings."""
    if isinstance(name, int):
        return name
    s = str(name).strip()
    if s.lstrip("-").isdigit():
        return int(s)
    m = re.fullmatch(r"([A-Ga-g])([#b♯♭x]*)(-?\d+)", s)
    if not m:
        raise ValueError(f"Bad note name {name!r} (use e.g. C4, F#3, Bb5)")
    pc = NOTE_PC[m.group(1).upper()]
    for ch in m.group(2):
        pc += {"#": 1, "♯": 1, "x": 2, "b": -1, "♭": -1}[ch]
    return pc + 12 * (int(m.group(3)) + 1)


def pitch_class(name: str) -> int:
    m = re.fullmatch(r"([A-Ga-g])([#b♯♭]*)", name.strip())
    if not m:
        raise ValueError(f"Bad pitch class {name!r}")
    pc = NOTE_PC[m.group(1).upper()]
    for ch in m.group(2):
        pc += 1 if ch in "#♯" else -1
    return pc % 12


def note_name(n: int, flats: bool = False) -> str:
    return (FLAT_NAMES if flats else SHARP_NAMES)[n % 12] + str(n // 12 - 1)


class Key:
    def __init__(self, text: str):
        s = text.strip()
        m = re.fullmatch(r"([A-Ga-g][#b♯♭]?)\s*(.*)", s)
        if not m:
            raise ValueError(f"Bad key {text!r} (use e.g. 'D minor', 'F# dorian', 'Ebm')")
        self.tonic = pitch_class(m.group(1))
        mode = m.group(2).strip().lower() or "major"
        mode = {"m": "minor", "min": "minor", "maj": "major", "M": "major"}.get(mode, mode)
        if mode not in MODES:
            raise ValueError(f"Unknown mode {mode!r}. Known: {', '.join(sorted(MODES))}")
        self.mode = mode
        self.intervals = MODES[mode]
        self.text = f"{m.group(1)[0].upper()}{m.group(1)[1:]} {mode}"
        self.flats = "b" in m.group(1) or (mode in ("minor", "aeolian", "dorian", "phrygian", "locrian") and self.tonic in (0, 2, 5, 7, 10)) \
            or (mode == "major" and self.tonic in (5, 10, 3, 8, 1))

    @property
    def pcs(self) -> set[int]:
        return {(self.tonic + i) % 12 for i in self.intervals}

    def degree_pc(self, degree: int) -> int:
        """1-based scale degree (wraps) -> pitch class."""
        n = len(self.intervals)
        return (self.tonic + self.intervals[(degree - 1) % n]) % 12

    def contains(self, pitch: int) -> bool:
        return pitch % 12 in self.pcs

    def snap(self, pitch: int) -> int:
        if self.contains(pitch):
            return pitch
        for d in (-1, 1, -2, 2):
            if self.contains(pitch + d):
                return pitch + d
        return pitch

    def step(self, pitch: int, steps: int) -> int:
        """Move a pitch by diatonic steps (pitch snapped into the scale first)."""
        p = self.snap(pitch)
        scale = sorted(self.pcs)
        octave, pc = divmod(p, 12)
        idx = scale.index(pc) + steps
        o, i = divmod(idx, len(scale))
        return (octave + o) * 12 + scale[i]

    def name(self, pitch: int) -> str:
        return note_name(pitch, self.flats)


class Chord:
    def __init__(self, root: int, intervals: list[int], bass: int | None = None, symbol: str = ""):
        self.root = root % 12
        self.intervals = intervals
        self.bass = bass % 12 if bass is not None else None
        self.symbol = symbol

    @property
    def pcs(self) -> list[int]:
        return [(self.root + i) % 12 for i in self.intervals]

    def tone(self, degree: int) -> int:
        """Chord degree -> semitones above the root. 1 3 5 7 9 11 13; 8/10/12 = root/3rd/5th an octave up.

        Uses the chord's own tone for that degree (a sus4's "3" is the 4th), else the natural tone.
        """
        base, octave = degree, 0
        while base > 7 and base not in (9, 11, 13):
            base -= 7
            octave += 12
        rel = {iv % 12 for iv in self.intervals}
        targets = {1: (0,), 2: (2,), 3: (4, 3, 2, 5), 4: (5,), 5: (7, 6, 8), 6: (9, 8), 7: (10, 11, 9),
                   9: (2, 1, 3), 11: (5, 6), 13: (9, 8)}
        semis = next((c for c in targets.get(base, (0,)) if c in rel), targets.get(base, (0,))[0])
        if base in (9, 11, 13):
            semis += 12
        return semis + octave

    def __repr__(self) -> str:
        return self.symbol or f"Chord({self.root},{self.intervals})"


def parse_chord(symbol: str) -> Chord:
    """'Dm7', 'F#maj7/C#', 'Bbsus2', 'Gadd9', 'C5'."""
    s = symbol.strip()
    m = re.fullmatch(r"([A-G][#b♯♭]?)(.*?)(?:/([A-G][#b♯♭]?))?", s)
    if not m:
        raise ValueError(f"Bad chord symbol {symbol!r}")
    root = pitch_class(m.group(1))
    q = m.group(2)
    if q not in CHORD_QUALITIES:
        raise ValueError(f"Unknown chord quality {q!r} in {symbol!r}. Known: {' '.join(k or 'maj' for k in _QUALITY_KEYS)}")
    bass = pitch_class(m.group(3)) if m.group(3) else None
    return Chord(root, CHORD_QUALITIES[q], bass, s)


_ROMAN = ["VII", "VI", "V", "IV", "III", "II", "I"]
_ROMAN_VAL = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7}


def parse_roman(token: str, key: Key) -> Chord:
    """Roman numeral relative to the key's own scale: in D minor 'VI' = Bb. Case sets triad quality.

    Suffixes: ° (dim), ø (half-dim 7), + (aug), 7, maj7, 9, sus2, sus4, add9, 6.
    Accidentals: bVI, #iv°. Secondary chords: V/V, V7/ii, vii°7/V.
    """
    t = token.strip()
    if "/" in t:
        head, target = t.split("/", 1)
        target_root = parse_roman(target, key).root
        c = parse_roman(head, Key(f"{SHARP_NAMES[target_root]} major"))
        c.symbol = t
        return c
    m = re.fullmatch(r"([b#♭♯]?)(VII|VI|V|IV|III|II|I|vii|vi|v|iv|iii|ii|i)(.*)", t)
    if not m:
        raise ValueError(f"Bad roman numeral {token!r}")
    acc, num, suf = m.groups()
    degree = _ROMAN_VAL[num.upper()]
    root = key.degree_pc(degree) + {"b": -1, "♭": -1, "#": 1, "♯": 1}.get(acc, 0)
    upper = num.isupper()
    suf = suf.replace("dim", "°").replace("hd", "ø")
    if suf.startswith("°"):
        quality = "°7" if "7" in suf else "dim"
        rest = ""
    elif suf.startswith("ø"):
        quality, rest = "m7b5", ""
    elif suf.startswith("+"):
        quality = "aug7" if "7" in suf else "aug"
        rest = ""
    else:
        rest = suf
        base = "" if upper else "m"
        if rest in ("7", "9", "11", "13"):
            quality = (rest if upper else "m" + rest)
        elif rest in ("maj7", "M7", "maj9"):
            quality = (rest if upper else "mmaj7")
        elif rest == "6":
            quality = "6" if upper else "m6"
        elif rest in ("sus2", "sus4", "sus", "5", "7sus4"):
            quality = rest
        elif rest == "add9":
            quality = "add9" if upper else "madd9"
        elif rest == "":
            quality = base
        else:
            q2 = base + rest
            if q2 not in CHORD_QUALITIES:
                raise ValueError(f"Unknown roman numeral suffix {rest!r} in {token!r}")
            quality = q2
    return Chord(root % 12, CHORD_QUALITIES[quality], None, t)


def parse_progression(text: str, key: Key | None = None, default_len: float = 4.0) -> list[tuple[Chord, float]]:
    """'i VI III VII' or 'Dm Bb F C' (each one bar by default); durations with ':': 'i:2 iv:2 V7:4'.

    '|' separators are ignored. Returns (chord, length_in_beats) pairs.
    """
    out = []
    for tok in re.split(r"[\s|,]+", text.strip()):
        if not tok or tok == "-":
            continue
        length = default_len
        if ":" in tok:
            tok, ln = tok.rsplit(":", 1)
            length = float(eval_fraction(ln))
        if tok[0] not in "ABCDEFG":
            if key is None:
                raise ValueError("Roman numerals need a key")
            out.append((parse_roman(tok, key), length))
        else:
            out.append((parse_chord(tok), length))
    return out


def eval_fraction(s: str) -> float:
    s = s.strip()
    if "/" in s:
        a, b = s.split("/", 1)
        return float(a) / float(b)
    return float(s)


def voice_chord(chord: Chord, voices: int = 4, low: int = 48, high: int = 79, prev: list[int] | None = None,
                center: int | None = None, bass_root: bool = False) -> list[int]:
    """Pick a voicing for `chord` inside [low, high] that moves smoothly from `prev`.

    Root, third and seventh are required where they exist; the fifth may be dropped; adjacent
    upper voices stay within an octave. With bass_root the lowest note is the chord root (or slash bass).
    """
    pcs = chord.pcs
    root = chord.root
    bass_pc = chord.bass if chord.bass is not None else root
    third = next((p for p in pcs if (p - root) % 12 in (3, 4)), None)
    seventh = next((p for p in pcs if (p - root) % 12 in (9, 10, 11) and len(pcs) > 3), None)
    fifth = next((p for p in pcs if (p - root) % 12 in (6, 7, 8)), None)
    required = {root} | ({third} if third is not None else set()) | ({seventh} if seventh is not None else set())
    if len(pcs) <= 2:
        required = set(pcs)
    allowed = set(pcs) | ({chord.bass} if chord.bass is not None else set())
    cands = [p for p in range(low, high + 1) if p % 12 in allowed]
    target = center if center is not None else (low + high) // 2
    best, best_cost = None, float("inf")
    for combo in itertools.combinations(cands, voices):
        cover = {p % 12 for p in combo}
        if not required <= cover:
            continue
        if bass_root and combo[0] % 12 != bass_pc:
            continue
        cost = 0.0
        # spacing: upper voices within an octave, bass gap up to a 12th
        for i in range(1, len(combo)):
            gap = combo[i] - combo[i - 1]
            limit = 19 if i == 1 else 12
            if gap > limit:
                cost += (gap - limit) * 3
            if gap < 3 and combo[i] < 60:
                cost += 4  # muddy close intervals low down
        if fifth is not None and fifth not in cover and voices > len(required):
            cost += 2
        dup = voices - len(cover)
        if dup > 0:
            counts = {pc: sum(1 for p in combo if p % 12 == pc) for pc in cover}
            for pc, c in counts.items():
                if c > 1 and pc != root and pc != fifth:
                    cost += 3 * (c - 1)  # avoid doubling 3rds/7ths
        if chord.bass is not None and combo[0] % 12 != chord.bass:
            cost += 6
        if prev:
            pv = sorted(prev)
            if len(pv) == len(combo):
                cost += sum(abs(a - b) for a, b in zip(pv, combo))
                cost += 1.5 * max(0, abs(combo[-1] - pv[-1]) - 4)  # keep the top voice smooth
            else:
                cost += sum(min(abs(p - q) for q in pv) for p in combo)
        else:
            cost += abs(sum(combo) / len(combo) - target) * 1.5
        if cost < best_cost:
            best, best_cost = list(combo), cost
    if best is None:
        # range too small: stack chord tones upward from low
        best = []
        p = low
        for pc in (pcs * 3)[:voices]:
            while p % 12 != pc:
                p += 1
            best.append(p)
    return best


def range_of(role: str | None) -> tuple[int, int, int, int] | None:
    if not role:
        return None
    r = role.strip().lower()
    if r in RANGES:
        return RANGES[r]
    for k in sorted(RANGES, key=len, reverse=True):
        if k in r:
            return RANGES[k]
    return None


def describe_scale(key: Key) -> dict:
    return {"key": key.text, "notes": [FLAT_NAMES[p] if key.flats else SHARP_NAMES[p] for p in
                                       [(key.tonic + i) % 12 for i in key.intervals]]}


def diatonic_chords(key: Key, sevenths: bool = False) -> list[str]:
    """Chord symbols built on each scale degree (7-note modes only)."""
    if len(key.intervals) != 7:
        return []
    names = FLAT_NAMES if key.flats else SHARP_NAMES
    out = []
    for d in range(7):
        r = key.intervals[d]
        tones = [(key.intervals[(d + k) % 7] - r) % 12 for k in (0, 2, 4, 6)]
        triad = tones[:3]
        q = {(0, 4, 7): "", (0, 3, 7): "m", (0, 3, 6): "dim", (0, 4, 8): "aug"}.get(tuple(triad), "?")
        if sevenths:
            q = {(0, 4, 7, 11): "maj7", (0, 4, 7, 10): "7", (0, 3, 7, 10): "m7", (0, 3, 6, 10): "m7b5",
                 (0, 3, 6, 9): "dim7", (0, 3, 7, 11): "mmaj7", (0, 4, 8, 11): "maj7#5"}.get(tuple(tones), q)
        out.append(names[(key.tonic + r) % 12] + q)
    return out
