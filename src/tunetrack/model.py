"""Project data model. Every position is in beats (quarter notes) from the song start unless noted.

A project lives in a folder:
    project.json   the document below
    audio/         imported audio files
    cache/         per-track render cache
    renders/       exported mixes, stems, loops
    history/       undo snapshots
"""
from __future__ import annotations

import bisect
import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = 1
HISTORY_LIMIT = 100


def _r(x: float) -> float:
    """Round for tidy JSON without losing musical precision (triplets survive)."""
    return round(float(x), 6)


@dataclass
class Note:
    pitch: int
    start: float  # beats, relative to the clip start
    dur: float
    vel: int = 90

    @property
    def end(self) -> float:
        return self.start + self.dur

    def to_json(self) -> list:
        return [int(self.pitch), _r(self.start), _r(self.dur), int(self.vel)]

    @classmethod
    def from_json(cls, a) -> "Note":
        if isinstance(a, dict):
            return cls(int(a["pitch"]), float(a["start"]), float(a["dur"]), int(a.get("vel", 90)))
        return cls(int(a[0]), float(a[1]), float(a[2]), int(a[3]) if len(a) > 3 else 90)


@dataclass
class Clip:
    id: str
    kind: str = "midi"  # midi | audio
    name: str = ""
    start: float = 0.0
    length: float = 4.0
    muted: bool = False
    color: str | None = None
    # midi
    notes: list[Note] = field(default_factory=list)
    cc: dict[str, list[list[float]]] = field(default_factory=dict)  # "1": [[beat, 0..127]], "bend": [[beat, -1..1]]
    # audio
    file: str | None = None  # relative to the project folder
    offset: float = 0.0  # seconds into the file
    gain_db: float = 0.0
    fade_in: float = 0.0  # seconds
    fade_out: float = 0.0

    @property
    def end(self) -> float:
        return self.start + self.length

    def sort(self) -> None:
        self.notes.sort(key=lambda n: (n.start, n.pitch))
        for pts in self.cc.values():
            pts.sort(key=lambda p: p[0])

    def to_json(self) -> dict:
        d: dict = {"id": self.id, "kind": self.kind, "name": self.name, "start": _r(self.start), "length": _r(self.length)}
        if self.muted:
            d["muted"] = True
        if self.color:
            d["color"] = self.color
        if self.kind == "midi":
            self.sort()
            d["notes"] = [n.to_json() for n in self.notes]
            if self.cc:
                d["cc"] = {k: [[_r(b), _r(v)] for b, v in pts] for k, pts in self.cc.items()}
        else:
            d.update(file=self.file, offset=_r(self.offset), gain_db=_r(self.gain_db),
                     fade_in=_r(self.fade_in), fade_out=_r(self.fade_out))
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Clip":
        c = cls(id=d["id"], kind=d.get("kind", "midi"), name=d.get("name", ""), start=float(d.get("start", 0)),
                length=float(d.get("length", 4)), muted=bool(d.get("muted", False)), color=d.get("color"))
        c.notes = [Note.from_json(n) for n in d.get("notes", [])]
        c.cc = {str(k): [[float(b), float(v)] for b, v in pts] for k, pts in d.get("cc", {}).items()}
        c.file = d.get("file")
        c.offset = float(d.get("offset", 0))
        c.gain_db = float(d.get("gain_db", 0))
        c.fade_in = float(d.get("fade_in", 0))
        c.fade_out = float(d.get("fade_out", 0))
        return c


@dataclass
class Device:
    """An instrument or an effect.

    type: "vst3"    name = plugin name from the registry, optional patch = saved state from the library
          "synth"   name = built-in synth preset (see synths.py)
          "sfz"     name = path to an .sfz file (native sampler)
          "builtin" name = built-in effect (eq, compressor, reverb, delay, chorus, saturator, width, filter, gain)
    params: name -> value. For vst3, values are normalised 0..1 or a display string ("-12 dB") matched to the plugin.
    """
    type: str
    name: str
    patch: str | None = None
    params: dict = field(default_factory=dict)
    bypass: bool = False

    def to_json(self) -> dict:
        d: dict = {"type": self.type, "name": self.name}
        if self.patch:
            d["patch"] = self.patch
        if self.params:
            d["params"] = self.params
        if self.bypass:
            d["bypass"] = True
        return d

    @classmethod
    def from_json(cls, d: dict | None) -> "Device | None":
        if not d:
            return None
        return cls(type=d["type"], name=d["name"], patch=d.get("patch"), params=dict(d.get("params", {})),
                   bypass=bool(d.get("bypass", False)))

    def label(self) -> str:
        if self.type == "sfz":
            return f"sfz:{Path(self.name).stem}"
        return f"{self.name} [{self.patch}]" if self.patch else f"{self.type}:{self.name}"


@dataclass
class Track:
    id: str
    name: str
    kind: str = "midi"  # midi | audio | bus
    instrument: Device | None = None
    clips: list[Clip] = field(default_factory=list)
    fx: list[Device] = field(default_factory=list)
    volume_db: float = 0.0
    pan: float = 0.0  # -1 (left) .. 1 (right)
    mute: bool = False
    solo: bool = False
    output: str = "master"  # "master" or a bus track id
    sends: list[dict] = field(default_factory=list)  # {"to": bus_id, "db": -12}
    automation: dict[str, list[list[float]]] = field(default_factory=dict)  # "volume_db"/"pan": [[beat, value]]
    layer: str | None = None  # adaptive-music layer tag for layered stem exports
    role: str | None = None  # instrument family for range checks, e.g. "violin", "cello", "bass"
    color: str | None = None

    def to_json(self) -> dict:
        d: dict = {"id": self.id, "name": self.name, "kind": self.kind}
        if self.instrument:
            d["instrument"] = self.instrument.to_json()
        d["volume_db"] = _r(self.volume_db)
        d["pan"] = _r(self.pan)
        for k in ("mute", "solo"):
            if getattr(self, k):
                d[k] = True
        if self.output != "master":
            d["output"] = self.output
        if self.sends:
            d["sends"] = self.sends
        if self.fx:
            d["fx"] = [f.to_json() for f in self.fx]
        if self.automation:
            d["automation"] = {k: [[_r(b), _r(v)] for b, v in sorted(pts)] for k, pts in self.automation.items()}
        for k in ("layer", "role", "color"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        d["clips"] = [c.to_json() for c in sorted(self.clips, key=lambda c: c.start)]
        return d

    @classmethod
    def from_json(cls, d: dict) -> "Track":
        return cls(id=d["id"], name=d["name"], kind=d.get("kind", "midi"),
                   instrument=Device.from_json(d.get("instrument")),
                   clips=[Clip.from_json(c) for c in d.get("clips", [])],
                   fx=[Device.from_json(f) for f in d.get("fx", [])],
                   volume_db=float(d.get("volume_db", 0)), pan=float(d.get("pan", 0)),
                   mute=bool(d.get("mute", False)), solo=bool(d.get("solo", False)),
                   output=d.get("output", "master"), sends=list(d.get("sends", [])),
                   automation={k: [list(p) for p in v] for k, v in d.get("automation", {}).items()},
                   layer=d.get("layer"), role=d.get("role"), color=d.get("color"))

    @property
    def end(self) -> float:
        return max((c.end for c in self.clips), default=0.0)


class TempoMap:
    """Beats <-> seconds with step tempo changes."""

    def __init__(self, tempo: float, changes: list[list[float]] | None = None):
        pts = {0.0: float(tempo)}
        for b, bpm in changes or []:
            pts[float(b)] = float(bpm)
        self.beats = sorted(pts)
        self.bpms = [pts[b] for b in self.beats]
        self.secs = [0.0]
        for i in range(1, len(self.beats)):
            self.secs.append(self.secs[-1] + (self.beats[i] - self.beats[i - 1]) * 60.0 / self.bpms[i - 1])

    def sec(self, beat: float) -> float:
        i = max(0, bisect.bisect_right(self.beats, beat) - 1)
        return self.secs[i] + (beat - self.beats[i]) * 60.0 / self.bpms[i]

    def beat(self, sec: float) -> float:
        i = max(0, bisect.bisect_right(self.secs, sec) - 1)
        return self.beats[i] + (sec - self.secs[i]) * self.bpms[i] / 60.0

    def bpm_at(self, beat: float) -> float:
        return self.bpms[max(0, bisect.bisect_right(self.beats, beat) - 1)]


@dataclass
class Project:
    name: str
    tempo: float = 120.0
    time_sig: list[int] = field(default_factory=lambda: [4, 4])
    key: str = "C major"
    sample_rate: int = 48000
    brief: dict = field(default_factory=dict)
    tracks: list[Track] = field(default_factory=list)
    master: dict = field(default_factory=lambda: {"volume_db": 0.0, "fx": [], "target_lufs": -16.0, "ceiling_db": -1.0})
    markers: list[dict] = field(default_factory=list)  # {"at": beat, "name": "Chorus"}
    loop: dict | None = None  # {"start": beat, "end": beat}
    length: float | None = None  # explicit song end in beats
    tail: float = 4.0  # seconds rendered past the end for release/reverb tails
    tempo_changes: list[list[float]] = field(default_factory=list)
    next_id: int = 1
    path: Path | None = None  # project folder (not serialised)

    # ---- time helpers -------------------------------------------------
    @property
    def beats_per_bar(self) -> float:
        return self.time_sig[0] * 4.0 / self.time_sig[1]

    def tempo_map(self) -> TempoMap:
        return TempoMap(self.tempo, self.tempo_changes)

    def pos(self, value) -> float:
        """Parse a position: number = beats; "5" / "5:1" / "5:2.5" = bar:beat (1-based); "bar 5" too."""
        if value is None:
            return 0.0
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip().lower().replace("bar", "").strip()
        m = re.fullmatch(r"(\d+(?:\.\d+)?)(?:[:|](\d+(?:\.\d+)?))?", s)
        if not m:
            raise ValueError(f"Can't parse position {value!r}; use beats (e.g. 16) or 'bar:beat' (e.g. '5:1')")
        bar = float(m.group(1))
        beat = float(m.group(2)) if m.group(2) else 1.0
        return (bar - 1) * self.beats_per_bar + (beat - 1)

    def dur(self, value) -> float:
        """Parse a length: number = beats; "2 bars"/"2b" = bars."""
        if isinstance(value, (int, float)):
            return float(value)
        s = str(value).strip().lower()
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(bars?|b)?", s)
        if not m:
            raise ValueError(f"Can't parse length {value!r}; use beats (8) or bars ('2 bars')")
        n = float(m.group(1))
        return n * self.beats_per_bar if m.group(2) else n

    def fmt_pos(self, beat: float) -> str:
        bar = int(beat // self.beats_per_bar) + 1
        b = beat - (bar - 1) * self.beats_per_bar + 1
        return f"{bar}:{b:g}"

    @property
    def end(self) -> float:
        if self.length:
            return self.length
        e = max((t.end for t in self.tracks), default=0.0)
        if self.loop:
            e = max(e, self.loop["end"])
        return e

    # ---- lookup -------------------------------------------------------
    def new_id(self, prefix: str) -> str:
        i = self.next_id
        self.next_id += 1
        return f"{prefix}{i}"

    def track(self, ref: str) -> Track:
        ref_l = str(ref).strip().lower()
        for t in self.tracks:
            if t.id == ref or t.name.lower() == ref_l:
                return t
        matches = [t for t in self.tracks if ref_l in t.name.lower()]
        if len(matches) == 1:
            return matches[0]
        names = ", ".join(f"{t.id}:{t.name}" for t in self.tracks)
        raise KeyError(f"No track {ref!r}. Tracks: {names or 'none'}")

    def clip(self, ref: str) -> tuple[Track, Clip]:
        for t in self.tracks:
            for c in t.clips:
                if c.id == ref:
                    return t, c
        for t in self.tracks:
            for c in t.clips:
                if c.name and c.name.lower() == str(ref).lower():
                    return t, c
        raise KeyError(f"No clip {ref!r}")

    # ---- io -------------------------------------------------------------
    def to_json(self) -> dict:
        return {
            "tunetrack": SCHEMA, "name": self.name, "brief": self.brief,
            "tempo": _r(self.tempo), "tempo_changes": self.tempo_changes, "time_sig": self.time_sig,
            "key": self.key, "sample_rate": self.sample_rate, "length": self.length, "loop": self.loop,
            "tail": self.tail, "markers": sorted(self.markers, key=lambda m: m["at"]),
            "master": {**self.master, "fx": [f.to_json() if isinstance(f, Device) else f for f in self.master.get("fx", [])]},
            "next_id": self.next_id,
            "tracks": [t.to_json() for t in self.tracks],
        }

    @classmethod
    def from_json(cls, d: dict, path: Path | None = None) -> "Project":
        p = cls(name=d.get("name", "Untitled"), tempo=float(d.get("tempo", 120)),
                time_sig=list(d.get("time_sig", [4, 4])), key=d.get("key", "C major"),
                sample_rate=int(d.get("sample_rate", 48000)), brief=dict(d.get("brief", {})),
                tracks=[Track.from_json(t) for t in d.get("tracks", [])],
                markers=list(d.get("markers", [])), loop=d.get("loop"), length=d.get("length"),
                tail=float(d.get("tail", 4.0)), tempo_changes=list(d.get("tempo_changes", [])),
                next_id=int(d.get("next_id", 1)), path=path)
        m = dict(d.get("master", {}))
        p.master = {"volume_db": 0.0, "target_lufs": -16.0, "ceiling_db": -1.0, **m,
                    "fx": [Device.from_json(f) for f in m.get("fx", [])]}
        return p

    @property
    def file(self) -> Path:
        assert self.path is not None, "project has no folder"
        return self.path / "project.json"

    @classmethod
    def load(cls, path: str | Path) -> "Project":
        path = Path(path)
        if path.is_file():
            path = path.parent
        f = path / "project.json"
        if not f.exists():
            raise FileNotFoundError(f"No TuneTrack project at {path}")
        return cls.from_json(json.loads(f.read_text(encoding="utf-8")), path=path)

    def save(self, snapshot: bool = True) -> None:
        assert self.path is not None
        self.path.mkdir(parents=True, exist_ok=True)
        for sub in ("audio", "cache", "renders", "history"):
            (self.path / sub).mkdir(exist_ok=True)
        if snapshot and self.file.exists():
            _push_history(self.path)
        tmp = self.file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=1), encoding="utf-8")
        tmp.replace(self.file)


# ---- undo / redo: snapshots of project.json --------------------------------

def _hist(path: Path) -> tuple[Path, Path]:
    h = path / "history"
    (h / "redo").mkdir(parents=True, exist_ok=True)
    return h, h / "redo"


def _push_history(path: Path) -> None:
    h, redo = _hist(path)
    shutil.copy2(path / "project.json", h / f"{time.time_ns()}.json")
    for f in redo.glob("*.json"):
        f.unlink()
    snaps = sorted(h.glob("*.json"))
    for f in snaps[:-HISTORY_LIMIT]:
        f.unlink()


def undo(path: Path) -> bool:
    h, redo = _hist(path)
    snaps = sorted(h.glob("*.json"))
    if not snaps:
        return False
    shutil.copy2(path / "project.json", redo / f"{time.time_ns()}.json")
    snaps[-1].replace(path / "project.json")
    return True


def redo(path: Path) -> bool:
    h, rd = _hist(path)
    snaps = sorted(rd.glob("*.json"))
    if not snaps:
        return False
    shutil.copy2(path / "project.json", h / f"{time.time_ns()}.json")
    snaps[-1].replace(path / "project.json")
    return True
