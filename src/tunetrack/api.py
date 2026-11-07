"""Every TuneTrack operation, shared by the MCP server, the CLI and the web UI.

Functions take plain JSON-able arguments and return JSON-able dicts. Each mutating call loads the project from
disk, changes it, and saves it (with an undo snapshot), so the UI and agents never overwrite each other's edits.
Positions accept beats (16) or "bar:beat" strings ("5:1" = bar 5, beat 1). Lengths accept beats or "2 bars".
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import math
import os
import re
import shutil
import time
from pathlib import Path

from . import analysis, config, edit, engine, generators, notation, plugins, synths, theory
from . import harmony as harm
from .model import Clip, Device, Note, Project, Track, project_lock, redo as _redo, undo as _undo

STATE_FILE = config.HOME / "state.json"


# ---- current project ---------------------------------------------------------------------------------------

def _state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _set_current(path: Path) -> None:
    config.HOME.mkdir(parents=True, exist_ok=True)
    st = _state()
    st["current"] = str(path)
    recent = [r for r in st.get("recent", []) if r != str(path)]
    st["recent"] = [str(path)] + recent[:19]
    STATE_FILE.write_text(json.dumps(st, indent=1), encoding="utf-8")


def _path(project: str | None) -> Path:
    if project:
        p = Path(project)
        if not p.exists() and not p.is_absolute():
            cand = config.PROJECTS_DIR / project
            if cand.exists():
                p = cand
            else:
                cand = config.PROJECTS_DIR / plugins.slug(project)
                if cand.exists():
                    p = cand
        # absolute, so worker processes (which run in another folder) find the project's cache files
        return (p.parent if p.is_file() else p).resolve()
    cur = _state().get("current")
    if not cur:
        raise RuntimeError("No project open. Use project_new or project_open first.")
    return Path(cur)


def load(project: str | None = None) -> Project:
    return Project.load(_path(project))


class ConflictError(RuntimeError):
    """The project changed since the caller last read it; the edit was not applied."""


# Set by the web editor per request: the project revision its edit was based on (None = don't check).
expected_rev: contextvars.ContextVar[int | None] = contextvars.ContextVar("expected_rev", default=None)


class _Edit:
    """with _Edit(project) as p: ...  -> locks the project, loads, yields, saves with an undo snapshot.

    The lock is held from load to save, so concurrent editors (MCP server, web UI, CLI) are serialised and
    never overwrite each other's changes."""

    def __init__(self, project: str | None):
        self.path = _path(project)
        self._lock = project_lock(self.path)

    def __enter__(self) -> Project:
        self._lock.__enter__()
        try:
            self.p = load(str(self.path))
            want = expected_rev.get()
            if want is not None and int(want) != self.p.rev:
                raise ConflictError(f"The project changed since you loaded it (revision {want} -> {self.p.rev}); "
                                    f"reload and try again")
        except BaseException:
            self._lock.__exit__(None, None, None)
            raise
        return self.p

    def __exit__(self, exc_type, *_):
        try:
            if exc_type is None:
                self.p.save()
        finally:
            self._lock.__exit__(None, None, None)
        return False


# ---- parsing helpers ------------------------------------------------------------------------------------------

def parse_device(spec, instrument: bool = True) -> Device | None:
    """'synth:pad' | 'patch:<id>' | 'vst3:<plugin>' | 'sfz:<path>' | 'builtin:reverb' | dict | None."""
    if spec in (None, "", "none"):
        return None
    if isinstance(spec, Device):
        return spec
    if isinstance(spec, dict):
        d = dict(spec)
        if d.get("type") == "patch" or ("patch" in d and "type" not in d):
            pid = d.get("patch") or d.get("name")
            meta = plugins.get_patch(pid)
            return Device("vst3", meta["plugin"], patch=pid, params=d.get("params", {}))
        if d.get("patch") and not d.get("name"):
            d["name"] = plugins.get_patch(d["patch"])["plugin"]
        return Device.from_json(d)
    s = str(spec).strip()
    if ":" in s and not re.match(r"^[A-Za-z]:[\\/]", s):
        kind, name = s.split(":", 1)
        kind, name = kind.strip().lower(), name.strip()
    else:
        kind, name = ("sfz" if s.lower().endswith(".sfz") else ("synth" if s in synths.SYNTHS else
                      "builtin" if s in synths.FX and not instrument else "vst3")), s
    if kind == "patch":
        meta = plugins.get_patch(name)
        return Device("vst3", meta["plugin"], patch=name)
    if kind in ("fx", "effect"):
        kind = "builtin"
    if kind not in ("synth", "sfz", "vst3", "builtin"):
        raise ValueError(f"Unknown device kind {kind!r}; use synth:, patch:, vst3:, sfz: or builtin:")
    if kind == "synth" and name not in synths.SYNTHS:
        raise KeyError(f"Unknown synth {name!r}. Built-in synths: {', '.join(synths.SYNTHS)}")
    if kind == "builtin" and name not in synths.FX:
        raise KeyError(f"Unknown built-in effect {name!r}. Effects: {', '.join(synths.FX)}")
    if kind == "vst3":
        name = plugins.resolve(name, "instrument" if instrument else None)["name"]
    return Device(kind, name)


def _pitch(v) -> int:
    return theory.note_number(v) if isinstance(v, str) else int(v)


def _key(p: Project, key: str | None) -> theory.Key:
    return theory.Key(key or p.key)


def _clip_for(p: Project, t: Track, start: float, end: float, name: str | None = None) -> Clip:
    """The clip covering [start, end) on a track - extended or created (bar-aligned) as needed."""
    bpb = p.beats_per_bar
    hits = [c for c in t.clips if c.kind == "midi" and c.start <= end + 1e-9 and c.end >= start - 1e-9]
    if hits:
        c = min(hits, key=lambda c: c.start)
        for other in hits[1:]:
            # merge clips the new range bridges
            for n in other.notes:
                n.start += other.start - c.start
                c.notes.append(n)
            for k, pts in other.cc.items():
                c.cc.setdefault(k, []).extend([[b + other.start - c.start, v] for b, v in pts])
            c.length = max(c.end, other.end) - c.start
            t.clips.remove(other)
        if start < c.start:
            shift = c.start - (start // bpb) * bpb
            for n in c.notes:
                n.start += shift
            for pts in c.cc.values():
                for pt in pts:
                    pt[0] += shift
            c.start -= shift
            c.length += shift
        if end > c.end:
            c.length = math.ceil((end - c.start) / bpb - 1e-9) * bpb
        if name:
            c.name = name
        return c
    s = (start // bpb) * bpb
    e = max(s + bpb, math.ceil(end / bpb - 1e-9) * bpb)
    c = Clip(id=p.new_id("c"), name=name or t.name, start=s, length=e - s)
    t.clips.append(c)
    return c


def _put_notes(p: Project, t: Track, notes: list[Note], at: float, length: float, mode: str,
               name: str | None = None) -> dict:
    """Place notes (positions relative to `at`) on a track. mode replace clears notes starting in the range first."""
    if t.kind != "midi":
        raise ValueError(f"Track '{t.name}' is a {t.kind} track; notes need a midi track")
    length = max(length, max((n.end for n in notes), default=0.0), 1e-3)
    c = _clip_for(p, t, at, at + length, name)
    if mode == "replace":
        # edges shifted by 1/24 beat so humanized notes go with the beat they belong to (an old downbeat note nudged
        # just before `at` is replaced too; the next range's downbeat nudged just before the end is kept)
        lo, hi = at - c.start - 1 / 24, at - c.start + length - 1 / 24
        c.notes = [n for n in c.notes if not (lo - 1e-9 <= n.start < hi - 1e-9)]
    elif mode != "merge":
        raise ValueError("mode must be 'replace' or 'merge'")
    for n in notes:
        c.notes.append(Note(n.pitch, n.start + at - c.start, n.dur, n.vel))
    edit.trim_overlaps(c.notes)
    c.sort()
    return {"track": t.name, "clip": c.id, "added": len(notes), "range": f"{p.fmt_pos(at)} - {p.fmt_pos(at + length)}"}


def _abs_notes(t: Track) -> list[tuple[Clip, Note]]:
    return [(c, n) for c in t.clips if c.kind == "midi" for n in c.notes]


def _fit_clip(p: Project, c: Clip, moved: list[Note] | None = None) -> None:
    """Grow a MIDI clip (bar-aligned) so the notes an edit moved are still played: the renderer skips notes that
    start past a clip's end. `moved` = the edited notes that were audible before the edit (default: all notes).
    Notes pushed before the song start are dropped."""
    bpb = p.beats_per_bar
    c.notes = [n for n in c.notes if c.start + n.start >= -1e-9]
    for n in c.notes:
        n.start = max(n.start, -c.start)
    keep = {id(n) for n in c.notes}
    moved = [n for n in (c.notes if moved is None else moved) if id(n) in keep]
    if not moved:
        return
    first = min(n.start for n in moved)
    if first < -1e-9:
        shift = c.start - max(0.0, math.floor((c.start + first) / bpb + 1e-9) * bpb)
        for n in c.notes:
            n.start += shift
        for pts in c.cc.values():
            for pt in pts:
                pt[0] += shift
        c.start -= shift
        c.length += shift
    last = max(n.start for n in moved) + 1e-3
    if last > c.length + 1e-9:
        c.length = math.ceil((c.start + last) / bpb - 1e-9) * bpb - c.start


def _note_key(notes) -> list:
    return sorted((int(n[0]), round(float(n[1]), 4), round(float(n[2]), 4), int(n[3])) for n in notes)


# ---- project ----------------------------------------------------------------------------------------------------

def project_new(name: str, folder: str | None = None, tempo: float = 120, key: str = "C major",
                time_sig: str = "4/4", brief: dict | None = None, overwrite: bool = False) -> dict:
    config.ensure_dirs()
    path = Path(folder) if folder else config.PROJECTS_DIR / plugins.slug(name)
    if (path / "project.json").exists() and not overwrite:
        raise FileExistsError(f"A project already exists at {path} (open it, or pass overwrite=true)")
    num, den = (int(x) for x in str(time_sig).split("/"))
    theory.Key(key)
    p = Project(name=name, tempo=float(tempo), key=key, time_sig=[num, den], brief=brief or {}, path=path,
                sample_rate=int(config.load_config().get("default_sample_rate", 48000)))
    p.save(snapshot=False)
    _set_current(path)
    return {"project": str(path), "name": name}


def project_open(path: str) -> dict:
    p = load(path)
    _set_current(p.path)
    return project_info(str(p.path))


def project_list() -> dict:
    st = _state()
    found = []
    if config.PROJECTS_DIR.exists():
        for f in sorted(config.PROJECTS_DIR.glob("*/project.json")):
            found.append(str(f.parent))
    return {"current": st.get("current"), "recent": st.get("recent", []), "projects_dir": found}


def project_info(project: str | None = None, clips: bool = True) -> dict:
    p = load(project)
    bpb = p.beats_per_bar
    tracks = []
    for t in p.tracks:
        d = {"id": t.id, "name": t.name, "kind": t.kind}
        if t.instrument:
            d["instrument"] = t.instrument.label()
            if t.instrument.params:
                d["instrument_params"] = t.instrument.params
        d["volume_db"] = t.volume_db
        if t.pan:
            d["pan"] = t.pan
        for k in ("mute", "solo"):
            if getattr(t, k):
                d[k] = True
        if t.output != "master":
            d["output"] = t.output
        if t.sends:
            d["sends"] = t.sends
        if t.fx:
            d["fx"] = [_fx_label(p, f) + (" (bypassed)" if f.bypass else "") for f in t.fx]
        if t.role:
            d["role"] = t.role
        if t.layer:
            d["layer"] = t.layer
        if t.automation:
            d["automation"] = {k: len(v) for k, v in t.automation.items()}
        if clips:
            d["clips"] = [{"id": c.id, "name": c.name, "start": p.fmt_pos(c.start), "bars": round(c.length / bpb, 3),
                           **({"notes": len(c.notes)} if c.kind == "midi" else {"file": c.file}),
                           **({"cc": list(c.cc)} if c.cc else {}), **({"muted": True} if c.muted else {})}
                          for c in sorted(t.clips, key=lambda c: c.start)]
        tracks.append(d)
    return {
        "project": str(p.path), "name": p.name, "brief": p.brief, "tempo": p.tempo, "key": p.key,
        "time_sig": f"{p.time_sig[0]}/{p.time_sig[1]}", "length_bars": round(p.end / bpb, 2),
        "duration_s": round(p.tempo_map().sec(p.end), 1),
        "loop": {"start": p.fmt_pos(p.loop["start"]), "end": p.fmt_pos(p.loop["end"])} if p.loop else None,
        "markers": [{"at": p.fmt_pos(m["at"]), "name": m["name"]} for m in sorted(p.markers, key=lambda m: m["at"])],
        **({"chord_track": harm.summary(p)} if p.chord_track else {}),
        "master": {k: (v if k != "fx" else [_fx_label(p, f) for f in v]) for k, v in p.master.items()},
        **({"tempo_changes": p.tempo_changes} if p.tempo_changes else {}),
        "tracks": tracks,
    }


def project_set(project: str | None = None, name: str | None = None, tempo: float | None = None,
                key: str | None = None, time_sig: str | None = None, brief: dict | None = None,
                loop: str | dict | None = None, length: str | float | None = None, tail: float | None = None,
                target_lufs: float | None = None, ceiling_db: float | None = None,
                master_volume_db: float | None = None, tempo_changes: list | None = None) -> dict:
    with _Edit(project) as p:
        if name:
            p.name = name
        if tempo:
            p.tempo = float(tempo)
        if key:
            p.key = theory.Key(key).text
        if time_sig:
            p.time_sig = [int(x) for x in str(time_sig).split("/")]
        if brief:
            p.brief.update(brief)
        if loop is not None:
            if loop in ("off", "", False):
                p.loop = None
            elif isinstance(loop, dict):
                p.loop = {"start": p.pos(loop["start"]), "end": p.pos(loop["end"])}
            else:
                a, b = re.split(r"\s*(?:-|to|\.\.)\s*", str(loop), maxsplit=1)
                p.loop = {"start": p.pos(a), "end": p.pos(b)}
            if p.loop and p.loop["end"] <= p.loop["start"]:
                raise ValueError("loop end must be after its start")
        if length is not None:
            if length in ("auto", 0, ""):
                p.length = None
            elif isinstance(length, str) and ":" in length:
                p.length = p.pos(length)  # an end position, e.g. "33:1"
            else:
                p.length = p.dur(length)
        if tail is not None:
            p.tail = float(tail)
        if target_lufs is not None:
            p.master["target_lufs"] = float(target_lufs)
        if ceiling_db is not None:
            p.master["ceiling_db"] = float(ceiling_db)
        if master_volume_db is not None:
            p.master["volume_db"] = float(master_volume_db)
        if tempo_changes is not None:
            p.tempo_changes = [[p.pos(a), float(b)] for a, b in tempo_changes]
    return project_info(project, clips=False)


def markers(project: str | None = None, add: list | None = None, remove: list | None = None, clear: bool = False) -> dict:
    """add: [{"at": "9:1", "name": "Chorus"}] ; remove: names or positions."""
    with _Edit(project) as p:
        if clear:
            p.markers = []
        for r in remove or []:
            p.markers = [m for m in p.markers if m["name"] != r and p.fmt_pos(m["at"]) != str(r)]
        for m in add or []:
            p.markers.append({"at": p.pos(m["at"]), "name": m["name"]})
    return {"markers": [{"at": p.fmt_pos(m["at"]), "name": m["name"]} for m in sorted(p.markers, key=lambda m: m["at"])]}


def _span(p: Project, at=None, end=None, section: str | None = None) -> tuple[float, float | None]:
    """A range from a marker section and/or at/end positions (at/end override the section's edges)."""
    s, e = harm.section_range(p, section) if section else (0.0, None)
    if at is not None:
        s = p.pos(at)
    if end is not None:
        e = p.pos(end)
    if e is not None and e <= s + 1e-9:
        raise ValueError(f"the range ends ({p.fmt_pos(e)}) before it starts ({p.fmt_pos(s)})")
    return s, e


def harmony(progression: str | None = None, at=None, end=None, section: str | None = None, chord_length=None,
            key: str | None = None, clear: bool = False, project: str | None = None) -> dict:
    """The chord track: one harmonic timeline that chords()/pattern() follow when given no progression and that
    score() checks the parts against.

    Set: progression ("i VI III VII", "Am7 F/A G", "i:2 iv:2 V7:4", '%' repeats, 'N.C.' leaves a gap) replaces the
    range from `at` (or the marker `section`); with `end` or `section` it repeats to fill that range, else it lasts
    as written. Roman numerals follow the project key, or `key` for this range (a modulation).
    clear=True: removes the range (everything without one). Neither: read (optionally a range).
    """
    if not progression and not clear:
        p = load(project)
        s, e = _span(p, at, end, section) if (at is not None or end is not None or section) else (None, None)
        out = {"key": p.key, "chord_track": harm.listing(p, s, e)}
        if not p.chord_track:
            out["hint"] = "empty: set it with harmony(progression='i VI III VII', section='A') before writing parts"
        return out
    res: dict = {}
    with _Edit(project) as p:
        s, e = _span(p, at, end, section)
        if progression:
            k = _key(p, key)
            cl = p.dur(chord_length) if chord_length is not None else p.beats_per_bar
            prog = theory.parse_progression(progression, k, cl)
            total = sum(ln for _, ln in prog)
            if total <= 1e-9:
                raise ValueError("empty progression")
            span = (e - s) if e is not None else total
            if total < span - 1e-6:
                res["repeated"] = f"{span / total:g}x to fill {p.fmt_pos(s)} - {p.fmt_pos(s + span)}"
            elif total > span + 1e-6:
                res["warnings"] = [(f"the progression is {total / p.beats_per_bar:g} bars but the range is "
                                    f"{span / p.beats_per_bar:g}: the rest was cut")]
            extra = {"key": k.text} if key and k.text != theory.Key(p.key).text else {}
            new, t = [], s
            for c, ln in harm.fill(prog, span):
                if c is not None:
                    new.append({"at": t, "len": ln, "chord": c.symbol, **extra})
                t += ln
            p.chord_track = harm.put(p.chord_track, new, s, s + span)
            res["set"] = f"{p.fmt_pos(s)} - {p.fmt_pos(s + span)}"
        elif at is None and end is None and not section:
            p.chord_track = []
            res["cleared"] = "all"
        else:
            p.chord_track = harm.cut(p.chord_track, s, e if e is not None else math.inf)
            res["cleared"] = f"{p.fmt_pos(s)} - {p.fmt_pos(e) if e is not None else 'end'}"
    res["chord_track"] = harm.listing(p)
    return res


def undo(project: str | None = None) -> dict:
    return {"undone": _undo(_path(project))}


def redo(project: str | None = None) -> dict:
    return {"redone": _redo(_path(project))}


# ---- tracks ------------------------------------------------------------------------------------------------------

def track_add(name: str, instrument=None, kind: str = "midi", project: str | None = None, volume_db: float = 0.0,
              pan: float = 0.0, role: str | None = None, layer: str | None = None, output: str = "master",
              color: str | None = None, fx: list | None = None) -> dict:
    with _Edit(project) as p:
        if any(t.name.lower() == name.lower() for t in p.tracks):
            raise ValueError(f"A track named {name!r} already exists")
        t = Track(id=p.new_id("t"), name=name, kind=kind, instrument=parse_device(instrument) if kind == "midi" else None,
                  volume_db=float(volume_db), pan=float(pan), role=role, layer=layer, color=color)
        if output != "master":
            t.output = p.track(output).id
        for f in fx or []:
            t.fx.append(parse_device(f, instrument=False))
        p.tracks.append(t)
    return {"track": t.id, "name": t.name, "instrument": t.instrument.label() if t.instrument else None}


def track_update(track: str, project: str | None = None, name: str | None = None, instrument=None,
                 volume_db: float | None = None, pan: float | None = None, mute: bool | None = None,
                 solo: bool | None = None, output: str | None = None, sends: list | None = None,
                 role: str | None = None, layer: str | None = None, color: str | None = None,
                 instrument_params: dict | None = None) -> dict:
    """sends: [{"to": "Reverb", "db": -12}] replaces the send list. instrument_params merges into the instrument."""
    with _Edit(project) as p:
        t = p.track(track)
        if name:
            t.name = name
        if instrument is not None:
            t.instrument = parse_device(instrument)
        if instrument_params:
            if not t.instrument:
                raise ValueError("track has no instrument")
            t.instrument.params.update(instrument_params)
        for k, v in (("volume_db", volume_db), ("pan", pan), ("mute", mute), ("solo", solo), ("role", role),
                     ("layer", layer), ("color", color)):
            if v is not None:
                setattr(t, k, v)
        if output is not None:
            t.output = "master" if output == "master" else p.track(output).id
        if sends is not None:
            t.sends = [{"to": p.track(s["to"]).id, "db": float(s.get("db", -12))} for s in sends]
    return {"track": t.id, "name": t.name}


def track_remove(track: str, project: str | None = None) -> dict:
    with _Edit(project) as p:
        t = p.track(track)
        p.tracks.remove(t)
        unkeyed = []
        for o in p.tracks:
            o.sends = [s for s in o.sends if s["to"] != t.id]
            if o.output == t.id:
                o.output = "master"
        for owner, chain in [(o.name, o.fx) for o in p.tracks] + [("master", p.master.get("fx", []))]:
            for i, f in enumerate(chain):
                if isinstance(f, Device) and f.sidechain == t.id:
                    f.sidechain = None
                    unkeyed.append(f"{owner} fx {i} ({f.label()})")
    res: dict = {"removed": t.name}
    if unkeyed:
        res["warnings"] = [f"{u} lost its sidechain key '{t.name}'. A plugin whose external sidechain switch is still "
                           f"on won't react to anything now: give it a new key or turn the switch off "
                           f"(fx_update sidechain=..., params=...)" for u in unkeyed]
    return res


def track_duplicate(track: str, new_name: str | None = None, with_clips: bool = True, project: str | None = None) -> dict:
    with _Edit(project) as p:
        t = p.track(track)
        d = Track.from_json(json.loads(json.dumps(t.to_json())))
        d.id = p.new_id("t")
        d.name = new_name or f"{t.name} 2"
        d.clips = []
        if with_clips:
            for c in t.clips:
                cc = Clip.from_json(c.to_json())
                cc.id = p.new_id("c")
                d.clips.append(cc)
        p.tracks.insert(p.tracks.index(t) + 1, d)
    return {"track": d.id, "name": d.name}


def _fx_chain(p: Project, track: str) -> list[Device]:
    return p.master["fx"] if track == "master" else p.track(track).fx


def _fx_label(p: Project, f: Device) -> str:
    """An effect's label, with the track that keys its sidechain."""
    if not f.sidechain:
        return f.label()
    key = next((t.name for t in p.tracks if t.id == f.sidechain), f"missing track {f.sidechain}")
    return f"{f.label()} <- sidechain {key}"


def _sidechain_device_check(dev: Device) -> None:
    """Raise unless the effect has a sidechain input TuneTrack can feed."""
    if dev.type == "builtin":
        if dev.name not in synths.SIDECHAIN_FX:
            raise ValueError(f"builtin:{dev.name} has no sidechain input. Built-in effects with one: "
                             f"{', '.join(sorted(synths.SIDECHAIN_FX))}")
        return
    if dev.type != "vst3":
        raise ValueError(f"{dev.label()} is not an effect with a sidechain input")
    entry = plugins.resolve(dev.name)
    if entry.get("host") == "pedalboard":
        raise ValueError(f"{entry['name']} is hosted through pedalboard (a Waves-style shell), which can't feed a "
                         f"sidechain input. Use a DawDreamer-hosted compressor (e.g. SSL Native Bus Compressor 2) or "
                         f"builtin:compressor")
    if entry.get("inputs") is not None and int(entry["inputs"]) <= 2:
        raise ValueError(f"{entry['name']} has no sidechain input (it takes {entry['inputs']} input channels)")


def _sidechain_key(p: Project, owner: str, ref) -> str | None:
    """Resolve the sidechain source for an effect on `owner` (track name/id or "master") to a track id."""
    if ref in (None, "", "none", False):
        return None
    key = p.track(ref)
    if key.kind == "bus":
        raise ValueError(f"'{key.name}' is a bus. A sidechain key must be a MIDI or audio track (its own sound, "
                         f"before its fader)")
    if owner != "master":
        t = p.track(owner)
        if t.id == key.id:
            raise ValueError("An effect can't key from its own track; leave sidechain empty and it reacts to the "
                             "track's own signal")
        if t.kind != "bus" and _keys_on(p, key.id, t.id):
            raise ValueError(f"Sidechain loop: '{key.name}' already depends on '{t.name}' through its own sidechain")
    return key.id


def _keys_on(p: Project, start: str, target: str) -> bool:
    """True if track `start`'s render depends on track `target` through sidechain keys (directly or via others)."""
    by_id = {t.id: t for t in p.tracks}
    seen, todo = set(), [start]
    while todo:
        tid = todo.pop()
        if tid == target:
            return True
        if tid in seen or tid not in by_id:
            continue
        seen.add(tid)
        todo += [f.sidechain for f in by_id[tid].fx if f.sidechain]
    return False


def _sidechain_switches(dev: Device, on: bool, user_params: dict | None = None) -> tuple[dict, list[str]]:
    """Params that turn the plugin's external-sidechain switch(es) on or off ("External S/C", "Low Ext S/C",
    "EXT Sidechain"...), unless the caller already set one. Runs the plugin in a worker to read its parameters."""
    if dev.type != "vst3":
        return {}, []
    try:
        names = [d["name"] for d in plugin_params(dev.label() if not dev.patch else f"patch:{dev.patch}",
                                                  limit=100000)["params"]
                 if plugins.is_ext_sidechain_switch(d["name"])]
    except Exception as e:  # noqa: BLE001 - the key still gets connected; tell the agent what to check
        return {}, [f"couldn't read {dev.name}'s parameters ({e}); if it has an external sidechain switch, turn it "
                    f"{'on' if on else 'off'} with fx_update params"]
    if not names:
        return {}, ([f"{dev.name} has no parameter that looks like an external sidechain switch; it may use the key "
                     f"as soon as it's connected (check plugin_params)"] if on else [])
    already = {k.lower() for k in {**dev.params, **(user_params or {})}}
    if on and any(n.lower() in already for n in names):
        return {}, []
    return {n: 1.0 if on else 0.0 for n in names}, [
        f"switched {'on' if on else 'off'} {', '.join(repr(n) for n in names)}"
        + (" (set any of them in params yourself to key only some bands)" if on and len(names) > 1 else "")]


def fx_add(track: str, device, project: str | None = None, index: int | None = None, sidechain: str | None = None) -> dict:
    """track = a track name/id or "master". device = 'builtin:reverb', 'vst3:TDR Nova', 'patch:<id>' or dict.
    sidechain = a track whose sound (pre-fader, its own instrument + inserts) feeds the effect's sidechain input."""
    dev = parse_device(device, instrument=False)
    notes: list[str] = []
    if sidechain not in (None, "", "none"):
        _sidechain_device_check(dev)
        load(project).track(sidechain)  # fail fast on a bad name before starting a plugin worker
        switch, notes = _sidechain_switches(dev, True)
        dev.params.update(switch)
    with _Edit(project) as p:
        dev.sidechain = _sidechain_key(p, track, sidechain)
        chain = _fx_chain(p, track)
        chain.insert(len(chain) if index is None else int(index), dev)
        labels = [_fx_label(p, f) for f in chain]
    return {"track": track, "fx": labels, **({"params": dev.params, "notes": notes} if dev.sidechain else {})}


def fx_update(track: str, index: int, project: str | None = None, params: dict | None = None,
              bypass: bool | None = None, replace=None, sidechain: str | None = None) -> dict:
    """sidechain: a track name to key the effect from, "" to disconnect it, None to leave it as it is.
    A replaced device keeps the old one's sidechain key."""
    old = _fx_chain(load(project), track)[int(index)]
    new = parse_device(replace, instrument=False) if replace is not None else old
    key = old.sidechain if sidechain is None else (sidechain or None)
    switch, notes = {}, []
    if key and (sidechain not in (None, "") or replace is not None):  # newly keyed, or a new device to key
        _sidechain_device_check(new)
        switch, notes = _sidechain_switches(new, True, params)
    elif not key and old.sidechain and replace is None:
        switch, notes = _sidechain_switches(old, False)
    with _Edit(project) as p:
        chain = _fx_chain(p, track)
        dev = chain[int(index)]
        if replace is not None:
            dev = chain[int(index)] = new
        if sidechain is not None or replace is not None:
            dev.sidechain = _sidechain_key(p, track, key)
        dev.params.update(switch)
        if params:
            dev.params.update(params)
        if bypass is not None:
            dev.bypass = bool(bypass)
        labels = [_fx_label(p, f) for f in chain]
    return {"track": track, "fx": labels, "params": dev.params, **({"notes": notes} if notes else {})}


def fx_remove(track: str, index: int, project: str | None = None) -> dict:
    with _Edit(project) as p:
        chain = _fx_chain(p, track)
        removed = chain.pop(int(index))
        labels = [_fx_label(p, f) for f in chain]
    return {"removed": removed.label(), "fx": labels}


def automation(track: str, param: str = "volume_db", points: list | None = None, clear: bool = False,
               project: str | None = None) -> dict:
    """Track volume_db / pan automation. points: [[pos, value], ...] (linear between points)."""
    if param not in ("volume_db", "pan"):
        raise ValueError("param must be volume_db or pan (use cc for instrument expression)")
    with _Edit(project) as p:
        t = p.track(track)
        lane = [] if clear else t.automation.get(param, [])
        for pos, v in points or []:
            b = p.pos(pos)
            lane = [pt for pt in lane if abs(pt[0] - b) > 1e-6] + [[b, float(v)]]
        if lane:
            t.automation[param] = sorted(lane)
        else:
            t.automation.pop(param, None)
    return {"track": t.name, param: [[p.fmt_pos(b), v] for b, v in t.automation.get(param, [])]}


# ---- writing notes --------------------------------------------------------------------------------------------------

def write(track: str, notes: str, at=0, project: str | None = None, mode: str = "replace", vel: int | None = None,
          transpose: int = 0, clip_name: str | None = None) -> dict:
    """Write TTN notation at a position. mode=replace clears notes that start inside the written range."""
    with _Edit(project) as p:
        t = p.track(track)
        parsed, length, warnings = notation.parse(notes, p.beats_per_bar, vel=vel or 90)
        for n in parsed:
            n.pitch += int(transpose)
        res = _put_notes(p, t, parsed, p.pos(at), length, mode, clip_name)
    if warnings:
        res["warnings"] = warnings
    return res


def add_notes(track: str, notes: list, project: str | None = None, mode: str = "merge") -> dict:
    """notes: [[pitch, pos, dur_beats, vel], ...] with absolute positions; pitch as 60 or 'C4'."""
    with _Edit(project) as p:
        t = p.track(track)
        absn = [Note(_pitch(n[0]), p.pos(n[1]), float(n[2]), int(n[3]) if len(n) > 3 else 90) for n in notes]
        if not absn:
            return {"added": 0}
        at = min(n.start for n in absn)
        rel = [Note(n.pitch, n.start - at, n.dur, n.vel) for n in absn]
        res = _put_notes(p, t, rel, at, max(n.end for n in rel), mode)
    return res


def _progression(p: Project, progression: str | None, at, end, section, key, chord_length) -> tuple[float, list, bool]:
    """(start, [(chord | None, beats)], followed_the_chord_track) for chords()/pattern().

    An explicit progression works as it always has (from `at`, each chord chord_length); with `end`/`section` it
    repeats to fill that range. Without one, the chord track over [at, end) / the section is used (end defaults to
    the end of the chord track)."""
    s, e = _span(p, at, end, section)
    if progression:
        cl = p.dur(chord_length) if chord_length is not None else p.beats_per_bar
        prog = theory.parse_progression(progression, _key(p, key), cl)
        return s, (harm.fill(prog, e - s) if e is not None else prog), False
    if not p.chord_track:
        raise ValueError("No progression given and the chord track is empty: set it first with "
                         "harmony(progression='i VI III VII', section='A'), or pass progression=...")
    e = harm.track_end(p) if e is None else e
    prog = harm.progression(p, s, e) if e > s + 1e-9 else []
    if all(c is None for c, _ in prog):
        raise ValueError(f"The chord track has no chords in {p.fmt_pos(s)} - {p.fmt_pos(max(e, s))}: "
                         f"harmony() shows what it has")
    return s, prog, True


def chords(track: str, progression: str | None = None, at=None, project: str | None = None, key: str | None = None,
           chord_length=None, rhythm: str | None = None, voices: int = 4, low="C3", high="G5", vel: int = 80,
           gate: float = 1.0, bass: bool = False, bass_octave: int = 2, mode: str = "replace", end=None,
           section: str | None = None) -> dict:
    """Voice-led block chords from 'i VI III VII' (roman, in the key) or 'Dm Bb F C'; ':beats' per chord overrides.
    Without a progression they follow the chord track over [at, end) or a marker section."""
    with _Edit(project) as p:
        t = p.track(track)
        s, prog, followed = _progression(p, progression, at, end, section, key, chord_length)
        notes = generators.chord_notes(prog, rhythm, int(voices), _pitch(low), _pitch(high), int(vel), float(gate),
                                       bool(bass), int(bass_octave))
        res = _put_notes(p, t, notes, s, sum(l for _, l in prog), mode)
    res["chords"] = [c.symbol if c is not None else "N.C." for c, _ in prog]
    if followed:
        res["followed"] = "chord track"
    return res


def pattern(track: str, progression: str | None = None, pattern: str | None = None, at=None,
            project: str | None = None, key: str | None = None, chord_length=None, rate: float = 0.5, octave: int = 4,
            vel: int = 85, gate: float = 0.9, restart: bool = True, mode: str = "replace", end=None,
            section: str | None = None) -> dict:
    """Arpeggios, basslines and ostinatos from chord-degree patterns ('1 5 8 5', '1 . 1 5- 8 -') that follow chords:
    the progression given, else the chord track over [at, end) or a marker section."""
    if not pattern:
        raise ValueError("pattern is required (chord degrees like '1 5 8 5')")
    with _Edit(project) as p:
        t = p.track(track)
        s, prog, followed = _progression(p, progression, at, end, section, key, chord_length)
        notes = generators.pattern_notes(prog, pattern, float(rate), int(octave), int(vel), float(gate), 0.0, bool(restart))
        res = _put_notes(p, t, notes, s, sum(l for _, l in prog), mode)
    if followed:
        res["followed"] = "chord track"
    return res


def drums(track: str, grid: dict, at=0, bars: int = 1, project: str | None = None, steps_per_beat: int = 4,
          swing: float = 0.0, vel: int = 100, drum_map: dict | None = None, mode: str = "replace") -> dict:
    with _Edit(project) as p:
        t = p.track(track)
        dm = dict(drum_map or {})
        if not dm and t.instrument and t.instrument.patch:
            dm = plugins.get_patch(t.instrument.patch).get("drum_map", {})
        notes = generators.drum_grid(grid, int(steps_per_beat), int(bars), p.beats_per_bar, dm, float(swing), int(vel))
        res = _put_notes(p, t, notes, p.pos(at), int(bars) * p.beats_per_bar, mode)
    return res


def cc(track: str, controller, start, end=None, from_value: float | None = None, to_value: float | None = None,
       points: list | None = None, shape: str = "linear", resolution: float = 0.25, project: str | None = None,
       mode: str = "replace") -> dict:
    """Expression curves. controller: 1 (mod/dynamics), 11 (expression), 7, 64 (sustain) ... or "bend" (-1..1).

    Ramp: start/end + from_value/to_value with shape linear | exp | log | swell (up then back down).
    Points: [[pos, value], ...]. mode=replace clears this controller in [start, end) first.
    """
    lane = "bend" if str(controller).lower() in ("bend", "pitchbend", "pb") else str(int(controller))
    with _Edit(project) as p:
        t = p.track(track)
        s = p.pos(start)
        pts: list[tuple[float, float]] = []
        if points:
            pts = [(p.pos(a), float(v)) for a, v in points]
        if end is not None and from_value is not None and to_value is not None:
            e = p.pos(end)
            steps = max(1, int(round((e - s) / resolution)))
            for i in range(steps + 1):
                x = i / steps
                f = {"linear": x, "exp": x * x, "log": x ** 0.5, "swell": 1 - abs(2 * x - 1)}.get(shape)
                if f is None:
                    raise ValueError("shape must be linear, exp, log or swell")
                pts.append((s + (e - s) * x, from_value + (to_value - from_value) * f))
        if not pts:
            raise ValueError("give points, or end + from_value + to_value")
        lo, hi = min(b for b, _ in pts), max(b for b, _ in pts)
        c = _clip_for(p, t, lo, max(hi, lo + 1e-3))
        lst = c.cc.setdefault(lane, [])
        if mode == "replace":
            lst[:] = [pt for pt in lst if not (lo - c.start - 1e-9 <= pt[0] <= hi - c.start + 1e-9)]
        lst.extend([[b - c.start, v] for b, v in pts])
        lst.sort()
    return {"track": t.name, "clip": c.id, "lane": lane, "points": len(pts)}


def notes_read(track: str | None = None, start=None, end=None, project: str | None = None,
               format: str = "ttn", exact: bool = False) -> dict:
    """Read notes back. format ttn (compact notation per clip) or list ([pitch, bar:beat, dur, vel]).

    Unless exact=True, TTN shows timing on a 1/24-beat grid and velocities to the nearest 4, so humanized parts stay
    readable as the written rhythm.
    """
    p = load(project)
    s = p.pos(start) if start is not None else None
    e = p.pos(end) if end is not None else None
    flats = theory.Key(p.key).flats
    out = []
    for t in ([p.track(track)] if track else p.tracks):
        for c in sorted(t.clips, key=lambda c: c.start):
            if c.kind != "midi":
                continue
            if (s is not None and c.end <= s) or (e is not None and c.start >= e):
                continue
            ns = [n for n in c.notes if (s is None or c.start + n.start >= s - 1e-9) and (e is None or c.start + n.start < e - 1e-9)]
            item = {"track": t.name, "clip": c.id, "start": p.fmt_pos(c.start), "bars": round(c.length / p.beats_per_bar, 3)}
            if format == "list":
                item["notes"] = [[theory.note_name(n.pitch, flats), p.fmt_pos(c.start + n.start), round(n.dur, 4), n.vel] for n in ns]
            else:
                offset = (c.start % p.beats_per_bar)
                if exact:
                    shifted = [Note(n.pitch, n.start + offset, n.dur, n.vel) for n in ns]
                else:
                    shifted = [Note(n.pitch, round((n.start + offset) * 24) / 24, max(1, round(n.dur * 24)) / 24,
                                    max(1, min(127, int(round(n.vel / 4) * 4)))) for n in ns]
                txt = notation.format_notes(shifted, p.beats_per_bar, flats)
                item["ttn"] = txt
            if c.cc:
                item["cc"] = {k: len(v) for k, v in c.cc.items()}
            out.append(item)
    return {"clips": out}


def notes_edit(track: str, op: str, start=None, end=None, low=None, high=None, project: str | None = None,
               **args) -> dict:
    """Edit notes on a track in [start, end) and pitch [low, high]. ops and their args:
    transpose(semitones=0, steps=0 [diatonic]) | quantize(grid=0.25, strength=1, swing=0, ends=False) |
    humanize(timing_ms=12, velocity=6, seed=1) | velocity(set, scale, add, ramp_from, ramp_to) |
    legato(overlap=0) | length(dur, scale) | stretch(factor) | shift(beats) | reverse | invert(axis) |
    double(interval=12) | delete | fit_scale | dedupe
    """
    with _Edit(project) as p:
        t = p.track(track)
        s = p.pos(start) if start is not None else None
        e = p.pos(end) if end is not None else None
        lo = _pitch(low) if low is not None else None
        hi = _pitch(high) if high is not None else None
        key = theory.Key(p.key)
        touched = 0
        for c in t.clips:
            if c.kind != "midi":
                continue
            sel = edit.select(c.notes, None if s is None else s - c.start, None if e is None else e - c.start, lo, hi)
            if not sel:
                continue
            touched += len(sel)
            audible = [n for n in sel if n.start < c.length - 1e-9]
            if op == "transpose":
                edit.transpose(sel, int(args.get("semitones", 0)), int(args.get("steps", 0)), key)
            elif op == "quantize":
                edit.quantize(sel, float(args.get("grid", 0.25)), float(args.get("strength", 1.0)),
                              bool(args.get("ends", False)), float(args.get("swing", 0.0)))
            elif op == "humanize":
                beats = float(args.get("timing_ms", 12)) / 1000.0 * p.tempo / 60.0
                edit.humanize(sel, beats, int(args.get("velocity", 6)), int(args.get("seed", 1)))
            elif op == "velocity":
                ramp = (int(args["ramp_from"]), int(args["ramp_to"])) if "ramp_from" in args else None
                edit.velocity(sel, args.get("set"), float(args.get("scale", 1.0)), int(args.get("add", 0)), ramp)
            elif op == "legato":
                edit.legato(sel, float(args.get("overlap", 0.0)))
            elif op == "length":
                edit.set_length(sel, args.get("dur"), float(args.get("scale", 1.0)))
            elif op == "stretch":
                edit.stretch(sel, float(args["factor"]), (s - c.start) if s is not None else 0.0)
            elif op == "shift":
                edit.shift(sel, float(args["beats"]))
            elif op == "reverse":
                a = (s - c.start) if s is not None else min(n.start for n in sel)
                b = (e - c.start) if e is not None else max(n.end for n in sel)
                edit.reverse(sel, a, b)
            elif op == "invert":
                edit.invert(sel, _pitch(args["axis"]) if "axis" in args else None, key if args.get("diatonic", True) else None)
            elif op == "double":
                added = edit.double(sel, int(args.get("interval", 12)))
                c.notes.extend(added)
                audible += [n for n in added if n.start < c.length - 1e-9]
            elif op == "delete":
                ids = {id(n) for n in sel}
                c.notes = [n for n in c.notes if id(n) not in ids]
            elif op == "fit_scale":
                edit.fit_to_scale(sel, key)
            elif op == "dedupe":
                edit.dedupe(c.notes)
            else:
                raise ValueError(f"Unknown op {op!r}")
            edit.trim_overlaps(c.notes)
            _fit_clip(p, c, audible)
            c.sort()
    return {"track": t.name, "op": op, "notes": touched}


# ---- clips & arrangement -------------------------------------------------------------------------------------------

def clip_edit(clip: str, op: str, project: str | None = None, to=None, track: str | None = None,
              times: int = 1, length=None, at=None, name: str | None = None) -> dict:
    """ops: move(to[, track]) | copy(to[, track]) | duplicate(times) | resize(length) | split(at) | delete |
    rename(name) | mute | unmute"""
    with _Edit(project) as p:
        t, c = p.clip(clip)
        res: dict = {"clip": c.id}
        if op == "move":
            if to is not None:
                c.start = p.pos(to)
            if track:
                t.clips.remove(c)
                p.track(track).clips.append(c)
        elif op in ("copy", "duplicate"):
            made = []
            dest = p.track(track) if track else t
            for i in range(int(times) if op == "duplicate" else 1):
                n = Clip.from_json(c.to_json())
                n.id = p.new_id("c")
                n.start = c.start + c.length * (i + 1) if op == "duplicate" else p.pos(to)
                dest.clips.append(n)
                made.append({"clip": n.id, "start": p.fmt_pos(n.start)})
            res["created"] = made
        elif op == "resize":
            c.length = p.dur(length)
        elif op == "split":
            cut = p.pos(at) - c.start
            if not 0 < cut < c.length:
                raise ValueError("split point must be inside the clip")
            right = Clip(id=p.new_id("c"), kind=c.kind, name=c.name, start=c.start + cut, length=c.length - cut,
                         file=c.file, offset=c.offset + (p.tempo_map().sec(c.start + cut) - p.tempo_map().sec(c.start)),
                         gain_db=c.gain_db, fade_out=c.fade_out)
            right.notes = [Note(n.pitch, n.start - cut, n.dur, n.vel) for n in c.notes if n.start >= cut]
            right.cc = {k: [[b - cut, v] for b, v in pts if b >= cut] for k, pts in c.cc.items()}
            c.notes = [n for n in c.notes if n.start < cut]
            for n in c.notes:
                n.dur = min(n.dur, cut - n.start)
            c.cc = {k: [pt for pt in pts if pt[0] < cut] for k, pts in c.cc.items()}
            c.length = cut
            c.fade_out = 0
            t.clips.append(right)
            res["created"] = right.id
        elif op == "delete":
            t.clips.remove(c)
        elif op == "rename":
            c.name = name or c.name
        elif op in ("mute", "unmute"):
            c.muted = op == "mute"
        else:
            raise ValueError(f"Unknown clip op {op!r}")
    return res


def arrange(op: str, start, end, to=None, tracks: list | None = None, project: str | None = None,
            mode: str = "replace") -> dict:
    """Song-level editing across tracks (notes, CC, audio clips, automation; markers, tempo changes, loop region,
    song length and the chord track too when no `tracks` filter is given; copy takes the chord track along in
    replace mode):
    copy(start, end, to[, mode]) - copy a section (e.g. verse) to another position. mode="replace" (default)
        clears the destination's notes/CC first; mode="merge" layers the copy on top of what's there
    move(start, end, to) | insert(start, end) - open a gap of end-start beats at start, pushing later material
    delete(start, end) - remove a section and close the gap | clear(start, end) - empty a section
    """
    if mode not in ("replace", "merge"):
        raise ValueError("mode must be 'replace' or 'merge'")
    with _Edit(project) as p:
        s, e = p.pos(start), p.pos(end)
        span = e - s
        sel = [p.track(x) for x in tracks] if tracks else p.tracks
        for t in sel:
            if t.kind == "bus":
                continue
            if op in ("copy", "move"):
                d = p.pos(to)
                grabbed, gcc = [], {}
                for c in t.clips:
                    if c.kind != "midi":
                        continue
                    for n in c.notes:
                        a = c.start + n.start
                        if s - 1e-9 <= a < e - 1e-9:
                            grabbed.append(Note(n.pitch, a - s, min(n.dur, e - a + 8), n.vel))
                    for k, pts in c.cc.items():
                        gcc.setdefault(k, []).extend([[c.start + b - s, v] for b, v in pts if s <= c.start + b < e])
                if op == "move":
                    _clear(t, s, e)
                if op == "copy" and mode == "replace":
                    _clear(t, d, d + span, audio=False)
                if grabbed or gcc:
                    c = _clip_for(p, t, d, d + span)
                    for n in grabbed:
                        c.notes.append(Note(n.pitch, n.start + d - c.start, n.dur, n.vel))
                    for k, pts in gcc.items():
                        c.cc.setdefault(k, []).extend([[b + d - c.start, v] for b, v in pts])
                    edit.trim_overlaps(c.notes)
                    c.sort()
            elif op == "insert":
                for c in t.clips:
                    if c.start >= s - 1e-9:
                        c.start += span
                    elif c.end > s:
                        for n in c.notes:
                            if c.start + n.start >= s - 1e-9:
                                n.start += span
                        for pts in c.cc.values():
                            for pt in pts:
                                if c.start + pt[0] >= s:
                                    pt[0] += span
                        c.length += span
            elif op in ("delete", "clear"):
                _clear(t, s, e)
                if op == "delete":
                    for c in list(t.clips):
                        if c.start >= e - 1e-9:
                            c.start -= span
                        elif c.end > e:
                            for n in c.notes:
                                if c.start + n.start >= e - 1e-9:
                                    n.start -= span
                            c.length -= min(span, max(0.0, c.end - s))
            else:
                raise ValueError("op must be copy, move, insert, delete or clear")
        if not tracks and (op == "move" or (op == "copy" and mode == "replace")):
            got = harm.grab(p.chord_track, s, e)
            if got:
                d = p.pos(to)
                if op == "move":
                    p.chord_track = harm.cut(p.chord_track, s, e)
                p.chord_track = harm.put(p.chord_track, [{**x, "at": x["at"] + d} for x in got], d, d + span)
        if op in ("insert", "delete"):
            for t in ([p.track(x) for x in tracks] if tracks else p.tracks):
                for lane, pts in list(t.automation.items()):
                    t.automation[lane] = _shift_lane(pts, s, e, op)
            if not tracks:
                _shift_timeline(p, s, e, op)
        for t in p.tracks:
            t.clips = [c for c in t.clips if c.length > 1e-6]
    return {"op": op, "range": f"{p.fmt_pos(s)} - {p.fmt_pos(e)}", "length_bars": round(p.end / p.beats_per_bar, 2)}


def _lane_value(pts: list, at: float) -> float:
    pts = sorted(pts)
    if at <= pts[0][0]:
        return pts[0][1]
    for (b0, v0), (b1, v1) in zip(pts, pts[1:]):
        if b0 <= at <= b1:
            return v0 if b1 - b0 < 1e-9 else v0 + (v1 - v0) * (at - b0) / (b1 - b0)
    return pts[-1][1]


def _shift_lane(pts: list, s: float, e: float, op: str) -> list:
    """Automation points (linear) after inserting a gap at s (held at the value there) or deleting [s, e)."""
    if not pts:
        return pts
    span = e - s
    if op == "insert":
        v = _lane_value(pts, s)
        out = [[b, x] for b, x in pts if b < s - 1e-9] + [[b + span, x] for b, x in pts if b >= s - 1e-9]
        if pts[0][0] < s < pts[-1][0] + 1e-9 or any(abs(b - s) < 1e-9 for b, _ in pts):
            out += [[s, v], [s + span, v]]
    else:
        v = _lane_value(pts, e)
        has_after = any(b >= e - 1e-9 for b, _ in pts)
        out = [[b, x] for b, x in pts if b < s - 1e-9] + [[b - span, x] for b, x in pts if b >= e - 1e-9]
        if has_after and pts[0][0] < e:
            out.append([s, v])
    dedup: dict[float, float] = {}
    for b, x in sorted(out):
        dedup[round(b, 9)] = x
    return [[b, x] for b, x in sorted(dedup.items())]


def _shift_timeline(p: Project, s: float, e: float, op: str) -> None:
    """Move markers, tempo changes, the loop region, the explicit song length and the chord track with an
    insert/delete."""
    span = e - s
    p.chord_track = harm.shift(p.chord_track, s, e, op)
    if op == "insert":
        for m in p.markers:
            if m["at"] >= s - 1e-9:
                m["at"] += span
        for tc in p.tempo_changes:
            if tc[0] >= s - 1e-9:
                tc[0] += span
        if p.loop:
            if p.loop["start"] >= s - 1e-9:
                p.loop["start"] += span
            if p.loop["end"] > s + 1e-9:
                p.loop["end"] += span
        if p.length is not None and p.length > s + 1e-9:
            p.length += span
        return
    bpm_after = p.tempo_map().bpm_at(e)
    p.markers = [m for m in p.markers if not (s - 1e-9 <= m["at"] < e - 1e-9)]
    for m in p.markers:
        if m["at"] >= e - 1e-9:
            m["at"] -= span
    kept = [tc for tc in p.tempo_changes if tc[0] < s - 1e-9]
    later = [[tc[0] - span, tc[1]] for tc in p.tempo_changes if tc[0] >= e - 1e-9]
    p.tempo_changes = kept + later
    if abs(p.tempo_map().bpm_at(s) - bpm_after) > 1e-6:
        p.tempo_changes = sorted([tc for tc in p.tempo_changes if abs(tc[0] - s) > 1e-9] + [[s, bpm_after]])

    def moved(b: float) -> float:
        return b if b <= s else (s if b < e else b - span)

    if p.loop:
        p.loop = {"start": moved(p.loop["start"]), "end": moved(p.loop["end"])}
        if p.loop["end"] - p.loop["start"] < 1e-6:
            p.loop = None
    if p.length is not None:
        p.length = moved(p.length)


def _clear(t: Track, s: float, e: float, audio: bool = True) -> None:
    for c in list(t.clips):
        if c.end <= s or c.start >= e:
            continue
        if c.kind == "audio":
            if audio and c.start >= s and c.end <= e:
                t.clips.remove(c)
            continue
        c.notes = [n for n in c.notes if not (s - 1e-9 <= c.start + n.start < e - 1e-9)]
        for k in c.cc:
            c.cc[k] = [pt for pt in c.cc[k] if not (s <= c.start + pt[0] < e)]
        if not c.notes and c.start >= s and c.end <= e:
            t.clips.remove(c)


def _file_digest(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _audio_dest(folder: Path, src: Path) -> Path:
    """Where to copy an imported file: reuse an identical copy already in the project, else a free name
    (tone.wav, tone-2.wav, ...) so different recordings with the same filename never collide."""
    folder.mkdir(parents=True, exist_ok=True)
    digest = None
    for i in range(1, 10000):
        cand = folder / (src.name if i == 1 else f"{src.stem}-{i}{src.suffix}")
        if not cand.exists():
            return cand
        try:
            if cand.resolve() == src.resolve():
                return cand
        except OSError:
            pass
        if cand.stat().st_size == src.stat().st_size:
            digest = digest or _file_digest(src)
            if _file_digest(cand) == digest:
                return cand
    raise FileExistsError(f"Too many files named like {src.name} in {folder}")


def audio_import(track: str, file: str, at=0, project: str | None = None, copy: bool = True, gain_db: float = 0.0,
                 fade_in: float = 0.0, fade_out: float = 0.0) -> dict:
    import soundfile as sf
    with _Edit(project) as p:
        t = p.track(track)
        if t.kind != "audio":
            raise ValueError(f"Track '{t.name}' is not an audio track (create one with kind='audio')")
        src = Path(file)
        if not src.exists():
            raise FileNotFoundError(file)
        rel = src
        if copy:
            dst = _audio_dest(p.path / "audio", src)
            if not dst.exists():
                shutil.copy2(src, dst)
            rel = Path("audio") / dst.name
        info = sf.info(str(src))
        s = p.pos(at)
        tm = p.tempo_map()
        length = tm.beat(tm.sec(s) + info.duration) - s
        c = Clip(id=p.new_id("c"), kind="audio", name=src.stem, start=s, length=length, file=str(rel).replace("\\", "/"),
                 gain_db=gain_db, fade_in=fade_in, fade_out=fade_out)
        t.clips.append(c)
    return {"clip": c.id, "length_beats": round(length, 3), "duration_s": round(info.duration, 2)}


def midi_import(file: str, at=0, project: str | None = None, instrument=None) -> dict:
    """Import a MIDI file: one new track per MIDI track/channel with notes and CCs."""
    import mido
    mf = mido.MidiFile(file)
    tpb = mf.ticks_per_beat
    with _Edit(project) as p:
        base = p.pos(at)
        made = []
        for i, mt in enumerate(mf.tracks):
            tick = 0
            name = None
            open_: dict = {}
            notes: list[Note] = []
            ccs: dict[str, list] = {}
            for msg in mt:
                tick += msg.time
                b = tick / tpb
                if msg.type == "track_name":
                    name = msg.name
                elif msg.type == "note_on" and msg.velocity > 0:
                    open_.setdefault(msg.note, []).append((b, msg.velocity))
                elif msg.type in ("note_off", "note_on") and open_.get(msg.note):
                    on, v = open_[msg.note].pop(0)
                    notes.append(Note(msg.note, on, max(1e-3, b - on), v))
                elif msg.type == "control_change":
                    ccs.setdefault(str(msg.control), []).append([b, msg.value])
                elif msg.type == "pitchwheel":
                    ccs.setdefault("bend", []).append([b, msg.pitch / 8191])
            if not notes:
                continue
            tname = name or f"MIDI {i + 1}"
            while any(t.name.lower() == tname.lower() for t in p.tracks):
                tname += "'"
            t = Track(id=p.new_id("t"), name=tname, instrument=parse_device(instrument) if instrument else None)
            end = max(n.end for n in notes)
            c = _clip_for(p, t, base, base + end, tname)
            for n in notes:
                c.notes.append(Note(n.pitch, n.start + base - c.start, n.dur, n.vel))
            c.cc = {k: [[b + base - c.start, v] for b, v in pts] for k, pts in ccs.items()}
            p.tracks.append(t)
            made.append({"track": t.id, "name": tname, "notes": len(notes)})
    return {"tracks": made}


def midi_export(path: str | None = None, project: str | None = None) -> dict:
    p = load(project)
    out = Path(path) if path else p.path / "renders" / f"{engine._safe(p.name)}.mid"
    return {"file": engine.export_midi(p, out)}


# ---- theory -----------------------------------------------------------------------------------------------------------

def theory_info(scale: str | None = None, chord: str | None = None, progression: str | None = None,
                key: str | None = None, range: str | None = None, chords_in_key: str | None = None,
                sevenths: bool = False, project: str | None = None) -> dict:
    out: dict = {}
    if scale:
        out["scale"] = theory.describe_scale(theory.Key(scale))
    if chords_in_key:
        k = theory.Key(chords_in_key)
        romans = ["I", "II", "III", "IV", "V", "VI", "VII"]
        out["chords_in_key"] = dict(zip(romans, theory.diatonic_chords(k, sevenths)))
    if chord:
        c = theory.parse_chord(chord)
        out["chord"] = {"symbol": chord, "notes": [theory.SHARP_NAMES[pc] for pc in c.pcs]}
    if progression:
        if key is None:
            try:
                key = load(project).key
            except Exception:  # noqa: BLE001 - no project open; default to C major
                key = "C major"
        k = theory.Key(key)
        prog = theory.parse_progression(progression, k)
        names = theory.FLAT_NAMES if k.flats else theory.SHARP_NAMES
        out["progression"] = [{"chord": c.symbol, "root": names[c.root], "notes": [names[pc] for pc in c.pcs]}
                              if c is not None else {"chord": "N.C."} for c, _ in prog]
    if range:
        r = theory.range_of(range)
        if not r:
            out["range"] = f"unknown instrument; known: {', '.join(sorted(theory.RANGES))}"
        else:
            out["range"] = {"instrument": range, "full": f"{theory.note_name(r[0])}-{theory.note_name(r[1])}",
                            "comfortable": f"{theory.note_name(r[2])}-{theory.note_name(r[3])}"}
    return out


# ---- library -----------------------------------------------------------------------------------------------------------

def library_status() -> dict:
    reg = plugins.load_registry()
    inst = [p["name"] for p in reg["plugins"] if p["kind"] == "instrument"]
    fx = [p["name"] for p in reg["plugins"] if p["kind"] == "effect"]
    patches = plugins.list_patches()
    sfz = plugins.find_sfz()
    return {
        "library": str(config.HOME),
        "vst3_instruments": inst,
        "vst3_effects": fx,
        "patches": [{"id": m["id"], "name": m["name"], "plugin": m["plugin"], "tags": m.get("tags", [])} for m in patches],
        "sfz_instruments": len(sfz),
        "builtin_synths": list(synths.SYNTHS),
        "builtin_effects": list(synths.FX),
        "notes": ([] if patches else ["No patches yet: VST3 instruments like HALion Sonic start empty. Ask the user to "
                                      "capture patches (patch_capture opens the plugin window to pick a sound)."])
                 + ([] if sfz else ["No SFZ libraries installed in library/instruments (see README for free ones)."]),
    }


def plugins_list(kind: str | None = None, query: str | None = None) -> dict:
    reg = plugins.load_registry()
    q = (query or "").lower()
    items = [{"name": p["name"], "kind": p["kind"], "host": p["host"]} for p in reg["plugins"]
             if (not kind or p["kind"] == kind) and q in p["name"].lower()]
    return {"plugins": items, "failed": len(reg.get("failed", []))}


def plugins_scan(force: bool = False, retry_failed: bool = False) -> dict:
    return plugins.scan(force=force, retry_failed=retry_failed)


def plugin_params(device: str, query: str | None = None, limit: int = 120) -> dict:
    """Parameters of a VST3 plugin (or a patch: 'patch:<id>') with current values and display text."""
    dev = parse_device(device, instrument=False)
    spec = engine.resolve_device(dev, Project("tmp"), False)
    config.HOME.mkdir(parents=True, exist_ok=True)
    jf = config.HOME / f".params_{os.getpid()}.json"
    jf.write_text(json.dumps({"device": spec, "query": query, "limit": limit}), encoding="utf-8")
    try:
        return plugins.run_worker("params", str(jf), timeout=180)
    finally:
        jf.unlink(missing_ok=True)


def patches_list(query: str | None = None, kind: str | None = None, plugin: str | None = None) -> dict:
    return {"patches": [{k: m.get(k) for k in ("id", "name", "plugin", "kind", "tags", "description", "range",
                                                "dynamics_cc", "drum_map", "channel") if m.get(k) is not None}
                        for m in plugins.list_patches(query, kind, plugin)]}


def patch_capture(plugin: str, name: str, tags: list | None = None, description: str = "",
                  base_patch: str | None = None, role: str | None = None, dynamics_cc: int | None = None,
                  drum_map: dict | None = None, patch_id: str | None = None) -> dict:
    """Opens the plugin window on the user's screen. The user picks/tweaks a sound and closes the window;
    the plugin state is saved as a reusable patch. Requires the user at the computer."""
    extra = {k: v for k, v in (("role", role), ("dynamics_cc", dynamics_cc), ("drum_map", drum_map)) if v is not None}
    meta = plugins.capture(plugin, name, tags, description, patch_id, base_patch, extra=extra)
    try:
        meta["audition"] = audition(f"patch:{meta['id']}", phrase="drums" if drum_map or "drums" in (tags or []) else "auto")
        if meta["audition"].get("silent"):
            meta["warning"] = "the captured patch is silent - was a program loaded before closing the window?"
        plugins.save_patch({k: v for k, v in meta.items() if k not in ("warning",)})
    except Exception as e:  # noqa: BLE001 - the patch is saved either way
        meta["audition_error"] = str(e)
    return meta


def patch_import(plugin: str, preset_file: str, name: str, tags: list | None = None, description: str = "",
                 role: str | None = None) -> dict:
    extra = {"role": role} if role else None
    return plugins.import_preset(plugin, preset_file, name, tags, description, extra=extra)


def patch_import_folder(plugin: str, folder: str, tags: list | None = None, role: str | None = None) -> dict:
    return plugins.import_folder(plugin, folder, tags, role)


def patch_update(patch_id: str, name: str | None = None, tags: list | None = None, description: str | None = None,
                 role: str | None = None, dynamics_cc: int | None = None, drum_map: dict | None = None,
                 range: list | None = None, channel: int | None = None) -> dict:
    if channel is not None and not 1 <= channel <= 16:
        raise ValueError("channel must be 1-16")
    meta = plugins.get_patch(patch_id)
    for k, v in (("name", name), ("tags", tags), ("description", description), ("role", role),
                 ("dynamics_cc", dynamics_cc), ("drum_map", drum_map), ("range", range), ("channel", channel)):
        if v is not None:
            meta[k] = v
    return plugins.save_patch(meta)


def patch_delete(patch_id: str) -> dict:
    plugins.delete_patch(patch_id)
    return {"deleted": patch_id}


def synths_list() -> dict:
    return synths.catalog()


def sfz_list(query: str | None = None) -> dict:
    files = plugins.find_sfz(query)
    return {"sfz": files[:200], "total": len(files)}


def sfz_info(path: str) -> dict:
    from . import sfz
    d = sfz.describe(path)
    d["range_names"] = f"{theory.note_name(d['range'][0])}-{theory.note_name(d['range'][1])}"
    d["keyswitch_names"] = [theory.note_name(k) for k in d["keyswitches"]]
    return d


def audition(instrument, phrase: str = "auto", out: str | None = None, tempo: float = 100) -> dict:
    """Render a short test phrase through an instrument ('patch:<id>', 'synth:pad', 'sfz:<path>', 'vst3:<name>').
    phrase: auto | chords | melody | bass | drums | any TTN text. Returns level/brightness stats and the file."""
    import numpy as np
    import soundfile as sf
    dev = parse_device(instrument)
    tmp = config.HOME / "audition"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    p = Project(name="audition", tempo=tempo, path=tmp)
    t = Track(id="t1", name="inst", instrument=dev)
    texts = {
        "chords": "(C3 G3 E4):h!70 (A2 E3 C4):h | (F2 C3 A3):h!85 (G2 D3 B3):h!100 | (C3 G3 E4 C5):w!90",
        "melody": "C4:e!60 D4 E4 F4 G4 A4 B4 C5 | D5:q!90 C5 B4 G4 | C5:w!110",
        "bass": "C2:q!100 C2:e C3 G2:q E2 | F2:q F2:e F3 C2:q G2 | C2:w",
        "drums": "C2:q!100 F#2:e F#2 D2:q F#2 | C2:e C2 F#2:e A#2 D2:q C#3",
    }
    if phrase == "auto":
        lo_hi = None
        if dev.patch:
            meta = plugins.get_patch(dev.patch)
            if meta.get("drum_map") or "drums" in meta.get("tags", []):
                phrase = "drums"
            lo_hi = meta.get("range")
        if phrase == "auto":
            phrase = "chords" if not lo_hi or lo_hi[0] < 48 else "melody"
    notes, length, _ = notation.parse(texts.get(phrase, phrase))
    t.clips.append(Clip(id="c1", start=0, length=max(length, 4), notes=notes))
    p.tracks.append(t)
    p.tail = 2.0
    p.save(snapshot=False)
    r = engine.Renderer(p)
    rep = r.run()
    mix = r.master
    res = {"instrument": dev.label(), "phrase": phrase, **analysis.stem_stats(mix), "warnings": rep["warnings"]}
    res["silent"] = float(np.abs(mix).max(initial=0)) < 1e-4
    if not res["silent"]:
        from scipy.signal import welch
        f, pw = welch(mix.mean(axis=0), p.sample_rate, nperseg=4096)
        res["brightness_hz"] = int((f * pw).sum() / max(pw.sum(), 1e-12))
    dest = Path(out) if out else config.HOME / "auditions" / f"{plugins.slug(dev.label())}.wav"
    dest.parent.mkdir(parents=True, exist_ok=True)
    peak = float(np.abs(mix).max(initial=0))
    sf.write(str(dest), (mix / max(peak, 1e-9) * 0.89 if peak > 0.89 else mix).T, p.sample_rate, subtype="PCM_24")
    res["file"] = str(dest)
    return res


# ---- render & review ---------------------------------------------------------------------------------------------------

def render(project: str | None = None, out: str | None = None, format: str = "wav", bits: int = 24,
           loop: str = "auto", normalize: bool = True, stems: bool = False, layers: bool = False,
           midi: bool = False) -> dict:
    """Render the mix. out = a folder or a file path (extension picks the format). Returns files + analysis."""
    p = load(project)
    t0 = time.time()
    rep = engine.render_project(p, out, format, int(bits), loop, normalize, stems, layers, midi)
    rep.pop("renderer", None)
    rep["seconds"] = round(time.time() - t0, 1)
    (p.path / "renders").mkdir(exist_ok=True)
    (p.path / "renders" / "last.json").write_text(json.dumps(rep, indent=1), encoding="utf-8")
    return rep


def analyze(project: str | None = None, images: bool = True, start=None, end=None) -> dict:
    """Note-level checks + a piano-roll overview PNG (and a spectrogram of the last render). View the PNGs."""
    p = load(project)
    rep = analysis.notes_report(p)
    hw = harm.warnings(p, p.pos(start) if start is not None else None, p.pos(end) if end is not None else None)
    if hw:
        rep["harmony_warnings"] = hw[:12] + ([f"... {len(hw) - 12} more: score() shows them bar by bar"]
                                             if len(hw) > 12 else [])
    if images:
        out = p.path / "renders"
        out.mkdir(exist_ok=True)
        rep["piano_roll_png"] = analysis.piano_roll_png(p, out / "piano_roll.png",
                                                        p.pos(start) if start is not None else None,
                                                        p.pos(end) if end is not None else None)
        last = out / "last.json"
        if last.exists():
            files = json.loads(last.read_text(encoding="utf-8")).get("files", [])
            if files and Path(files[0]).exists() and files[0].endswith((".wav", ".flac", ".ogg")):
                import soundfile as sf
                data, sr = sf.read(files[0], dtype="float32", always_2d=True)
                rep["spectrogram_png"] = analysis.spectrogram_png(data.T, sr, out / "spectrogram.png")
    return rep


def score(start=None, end=None, section: str | None = None, tracks: list | None = None, check: bool = True,
          project: str | None = None) -> dict:
    """Bar-by-bar text score: the chord on each beat (chord track, else inferred from the notes, marked *) and every
    part's notes in TTN, columns lined up by beat, plus harmony warnings (clashes on strong beats, parallel 5ths/8ves
    between bass and lead, voice crossings, parts that disagree with the chord track) with bar:beat and a fix.
    Default range: the whole song, at most 32 bars. Drum tracks are left out unless named in `tracks`."""
    p = load(project)
    s, e = _span(p, start, end, section)
    song_end = max(p.end, harm.track_end(p))
    if e is None:
        e = min(song_end, s + 32 * p.beats_per_bar)
    if e <= s + 1e-9:
        return {"score": "nothing written yet: no notes and no chord track", "bars": None}
    res = harm.score(p, s, e, tracks, bool(check))
    if e < song_end - 1e-9 and end is None and section is None:
        res["more"] = f"showing to {p.fmt_pos(e)} of {p.fmt_pos(song_end)}: pass start/end for the rest"
    return res


def listen(project: str | None = None, reference: str | None = None, probe: bool = True,
           section_bars: int | None = None, tracks: list | None = None) -> dict:
    """Measure what the mix sounds like (the agent's ears): per-section loudness/brightness/width/balance and a
    loudness value per bar, masking between tracks, a check of each track's audio against its notes (with probe
    renders of every pitch it uses), and an optional comparison with a reference recording."""
    from . import listening
    p = load(project)
    t0 = time.time()
    r = engine.Renderer(p)
    rep = r.run()
    res = listening.report(p, r, reference=reference, probe=probe,
                           section_bars=int(section_bars) if section_bars else None, tracks=tracks)
    res["warnings"] = [w for w in rep["warnings"]] + res["warnings"]
    res["seconds"] = round(time.time() - t0, 1)
    (p.path / "renders").mkdir(exist_ok=True)
    (p.path / "renders" / "listen.json").write_text(json.dumps(res, indent=1), encoding="utf-8")
    return res


def play(file: str | None = None, project: str | None = None) -> dict:
    """Open an audio file (default: the last render) in the system player so the user can listen."""
    if not file:
        p = load(project)
        last = p.path / "renders" / "last.json"
        if not last.exists():
            raise FileNotFoundError("Nothing rendered yet")
        file = json.loads(last.read_text(encoding="utf-8"))["files"][0]
    os.startfile(file)  # noqa: S606 - user-requested playback of a local render
    return {"playing": file}


# ---- editor-only helpers (used by the web UI) -------------------------------------------------------------------

def clip_create(track: str, start=0, length="1 bar", name: str | None = None, project: str | None = None) -> dict:
    with _Edit(project) as p:
        t = p.track(track)
        c = Clip(id=p.new_id("c"), kind="midi", name=name or t.name, start=p.pos(start), length=p.dur(length))
        t.clips.append(c)
    return {"clip": c.id}


def clip_set(clip: str, notes: list | None = None, start=None, length=None, name: str | None = None,
             muted: bool | None = None, track: str | None = None, project: str | None = None,
             expect_notes: list | None = None) -> dict:
    """Replace a clip's notes ([[pitch, start_in_clip, dur, vel]]) and/or move/resize it in one step.

    expect_notes: the clip's notes as the caller last saw them. If the clip has changed since (another editor
    wrote to it), nothing is applied and ConflictError is raised, so whole-list saves never clobber edits."""
    with _Edit(project) as p:
        t, c = p.clip(clip)
        if expect_notes is not None and _note_key(n.to_json() for n in c.notes) != _note_key(expect_notes):
            raise ConflictError(f"Clip '{c.name}' was changed by another editor; reloaded - redo your edit")
        if notes is not None:
            c.notes = [Note(int(n[0]), float(n[1]), float(n[2]), int(n[3])) for n in notes]
        if start is not None:
            c.start = max(0.0, p.pos(start))
        if length is not None:
            c.length = max(0.25, p.dur(length))
        if name is not None:
            c.name = name
        if muted is not None:
            c.muted = bool(muted)
        if track and p.track(track) is not t:
            t.clips.remove(c)
            p.track(track).clips.append(c)
    return {"clip": c.id}


def track_move(track: str, index: int, project: str | None = None) -> dict:
    with _Edit(project) as p:
        t = p.track(track)
        p.tracks.remove(t)
        p.tracks.insert(max(0, min(int(index), len(p.tracks))), t)
    return {"order": [t.name for t in p.tracks]}


def preview(project: str | None = None) -> dict:
    """Render the whole timeline (no loop folding) to renders/_preview.wav for the editor's transport."""
    p = load(project)
    rep = engine.render_project(p, str(p.path / "renders" / "_preview.wav"), "wav", 24, "off", True)
    rep.pop("renderer", None)
    return rep
