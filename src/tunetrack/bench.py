"""A seat's bench: the work in front of that seat, in the form the seat reads it in.

The rack shows who is doing what; the bench shows the work itself. The Composer's bench is a piano roll of its
take with the notes it added picked out, the Mix seat's is the console with every moved fader next to where it
was, Sound's is the instrument rack, the Producer's is the form and the arrangement. One read-only snapshot per
call, cheap enough to poll while an agent is at work:

    seat_bench("composer")              # the seat's take in progress (else its handed-in take, else the session)
    seat_bench("mix", take="tk3")       # a particular take of this session ("session": the accepted music)
    seat_bench("mix", have=version)     # {"unchanged": True} when nothing moved since that version

Nothing here edits anything. Editing a take is the Desk's job (or the seat's own tools).
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from . import harmony, roles, studio, takes
from . import picture as pic
from .model import Project

# How each seat reads the work.
VIEW = {"producer": "plan", "spotting": "picture", "composer": "notes", "arranger": "notes", "rhythm": "notes",
        "tracking": "notes", "sound": "sound", "mix": "mix", "review": "report"}
MIX_FIELDS = ("volume_db", "pan", "mute", "solo", "output", "sends", "fx", "automation")
_MIX_DEFAULT = {"volume_db": 0.0, "pan": 0.0, "mute": False, "solo": False, "output": "master", "sends": [],
                "fx": [], "automation": {}}
MAX_NOTES = 6000  # a bench is a glance, not an export: past this the oldest tracks keep their notes, the rest say so
LIVE = ("open", "submitted")


def _mtime(f: Path) -> int:
    try:
        return f.stat().st_mtime_ns
    except OSError:
        return 0


def _source(path: Path, seat: str, take: str | None) -> tuple[Path, dict | None, list[dict]]:
    """The folder the bench shows, that take's take.json (None for the session), and the seat's live takes."""
    mine = [t for t in studio.take_index(path) if t.get("seat") == seat and t.get("state") in LIVE]
    if take:
        tid = str(take).strip().lower()
        if tid == "session":  # the accepted music, even while the seat has a take going
            return path, None, mine
        if not re.fullmatch(r"tk\d+", tid):  # an id, never a path: this is called from the browser
            raise KeyError(f"No take {take!r} in this session")
        d = takes._take_dir(tid, str(path))
        return d, takes._meta(d), mine
    # the take being worked on now, else the one waiting to be auditioned, else the music as accepted
    for state in LIVE:
        found = [t for t in mine if t["state"] == state]
        if found:
            return path / "takes" / found[-1]["id"], found[-1], mine
    return path, None, mine


def _fx(f: dict) -> dict:
    out = {"label": f"{f['name']} · {f['patch']}" if f.get("patch") else f["name"]}
    if f.get("bypass"):
        out["bypass"] = True
    if f.get("automation"):
        out["auto"] = sorted(f["automation"])
    return out


def _mix(t: dict, names: dict[str, str]) -> dict:
    # names to read, ids to edit with: two tracks may share a name, and the console routes and sends by id
    return {"volume_db": float(t.get("volume_db", 0.0)), "pan": float(t.get("pan", 0.0)),
            "mute": bool(t.get("mute")), "solo": bool(t.get("solo")),
            "output": names.get(t.get("output") or "master", "master"), "output_id": t.get("output") or "master",
            "sends": [{"to": names.get(s.get("to"), "?"), "id": s.get("to"), "db": s.get("db", 0.0)}
                      for s in t.get("sends") or []],
            "fx": [_fx(f) for f in t.get("fx") or []], "automation": sorted(t.get("automation") or {})}


def _form(doc: dict, d: Path) -> dict:
    num, den = (doc.get("time_sig") or [4, 4])[:2]
    end = max([c.get("start", 0) + c.get("length", 0) for t in doc.get("tracks", []) for c in t.get("clips", [])]
              + [(doc.get("loop") or {}).get("end") or 0, doc.get("length") or 0,
                 *[e.get("at", 0) + e.get("len", 0) for e in doc.get("chord_track") or []]] + [0])
    chords = []
    if doc.get("chord_track"):
        try:  # names need the key: "i" in D minor reads "Dm"
            p = Project.from_json(doc, d)
            flats = harmony.Key(p.key).flats
            chords = [{"at": e["at"], "len": e["len"], "chord": e["chord"],
                       "name": harmony.chord_name(harmony.resolve(p, e), flats)}
                      for e in sorted(p.chord_track, key=lambda e: e["at"])]
        except Exception:  # noqa: BLE001 - an unreadable chord is shown as written rather than hiding the bench
            chords = [{"at": e.get("at", 0), "len": e.get("len", 0), "chord": e.get("chord", "?"),
                       "name": e.get("chord", "?")} for e in doc["chord_track"]]
    return {"name": doc.get("name"), "tempo": doc.get("tempo"), "key": doc.get("key"), "time_sig": [num, den],
            "bpb": num * 4.0 / den, "end": end, "loop": doc.get("loop") or None,
            "markers": sorted(doc.get("markers") or [], key=lambda m: m.get("at", 0)), "chords": chords,
            "brief": doc.get("brief") or {}}


def _render(d: Path) -> dict | None:
    """The last render of what the bench shows, and whether the music has changed since."""
    last = d / "renders" / "last.json"
    try:
        rep = json.loads(last.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mix = rep.get("mix") or {}
    return {"lufs": mix.get("lufs"), "true_peak_dbtp": mix.get("true_peak_dbtp"), "bands_pct": mix.get("bands_pct") or {},
            "stereo_correlation": mix.get("stereo_correlation"), "duration_s": mix.get("duration_s"),
            "limiter_db": rep.get("limiter_max_reduction_db"), "loop_mode": rep.get("loop_mode"),
            "warnings": list(rep.get("warnings") or []), "tracks": rep.get("tracks") or {},
            "stale": _mtime(d / "project.json") > _mtime(last)}


def _picture(doc: dict, d: Path, base: dict | None) -> dict:
    """Spotting's bench: the picture, its cuts and loud sound, the bars over it, the cue sheet and the stills. Only
    what the scan file already holds: the bench is polled, so no ffmpeg and no image work here."""
    p = Project.from_json(doc, d)
    tm, info = p.tempo_map(), dict(p.picture or {})
    start, fps = pic.start_of(p), pic._fps(p)
    at = lambda t: p.fmt_pos(round(tm.beat(t + start), 2)) if t + start >= 0 else "before the music"  # noqa: E731
    out: dict = {"picture": None, "cuts": [], "loud": [], "peaks": [], "strip": None}
    scan = {}
    if info.get("file"):
        out["picture"] = {"file": Path(info["file"]).name, "duration": float(info.get("duration") or 0), "fps": fps,
                          "size": info.get("size"), "start": start, "audio": bool(info.get("audio")),
                          # the first frame's timecode as the picture counts it (drop-frame: HH:MM:SS;FF)
                          "tc": pic.timecode(0.0, *pic.tc_args(p)) if info.get("tc") else None}
        try:
            scan = json.loads(pic._side(p, ".scan.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            scan = {}
        out["picture"]["mark"] = scan.get("mark")  # size and time of the video: a frame URL changes with the file
        last = -1.0
        for t, score in scan.get("scores", []):  # the cut rule picture.view() uses
            if score >= pic.CUT and t - last >= 0.25:
                out["cuts"].append([t, at(t)])
                last = t
        sound = scan.get("sound") or {}
        out["loud"], out["peaks"] = pic._loud(sound.get("level") or []), sound.get("peaks") or []
        out["strip"] = scan.get("strip") if pic._side(p, ".strip.jpg").exists() else None
    rows = pic.hit_rows(p, tm=tm)
    near = 0.5 / fps + 1e-6
    if base is not None:  # what the take did to the cue sheet, by moment (a renamed hit is the same hit)
        was = [float(h["t"]) for h in base.get("hits") or []]
        now = [float(h["t"]) for h in p.hits]
        for r in rows:
            if not any(abs(r["t"] - w) <= near for w in was):
                r["new"] = True
        gone = [h for h in base.get("hits") or [] if not any(abs(float(h["t"]) - n) <= near for n in now)]
        out["gone"] = pic.hit_rows(p, hits=[dict(h) for h in gone], tm=tm) if gone else []
    out["hits"] = rows
    # bar lines over the span shown (the picture, else the hits), in picture seconds: the page has no tempo map
    span = out["picture"]["duration"] if out["picture"] else max([h["t"] for h in rows] + [0.0]) + 2.0
    bpb, bars = p.beats_per_bar, []
    if span > 0:
        b0, b1 = tm.beat(max(0.0, start)), tm.beat(max(0.0, start + span))
        b = math.ceil(b0 / bpb - 1e-9) * bpb
        while b <= b1 + 1e-9 and len(bars) < 2000:
            bars.append([round(tm.sec(b) - start, 4), int(round(b / bpb)) + 1])
            b += bpb
    out.update(span=span, bars=bars, stills=[{"n": i, "file": Path(s.get("file", "")).name, "note": s.get("note", "")}
                                             for i, s in enumerate(p.brief.get("stills") or [])])
    return out


def seat_bench(seat: str, take: str | None = None, have: str | None = None, session: str | None = None) -> dict:
    """What a seat's workstation shows: its take in progress (else its handed-in take, else the session) as form,
    tracks, mix and - for the seats that write notes - every note, each marked with what the take changed.

    take: show this take instead ("session": the session itself). have: the "version" of the snapshot the caller already holds; when nothing has
    changed since, the answer is just {"unchanged": True, "version": ...}."""
    role = roles.get(seat)
    path = studio.session_path(session)
    d, meta, mine = _source(path, role.id, take)
    files = [d / "project.json", d / "base.json", d / "take.json", d / "renders" / "last.json"]
    if VIEW[role.id] == "picture":
        files.append(path / "picture")  # a scan landing beside the video (the folder's time moves when a file is added)
    version = f"{d.name if meta else 'session'}:" + ":".join(str(_mtime(f)) for f in files) + f":{len(mine)}"
    if have and have == version:
        return {"unchanged": True, "version": version}
    doc = takes._read_json(d / "project.json")
    base = takes._read_json(d / "base.json") if meta else None
    view = VIEW[role.id]
    names = {t["id"]: t["name"] for t in doc.get("tracks", [])}
    was = {t["id"]: t for t in (base or {}).get("tracks", [])}
    was_names = {i: t["name"] for i, t in was.items()}
    render = _render(d)
    levels = (render or {}).get("tracks") or {}
    budget = MAX_NOTES
    tracks, truncated = [], False
    for t in doc.get("tracks", []):
        b = was.get(t["id"])
        e = {"id": t["id"], "name": t["name"], "kind": t.get("kind", "midi"), "role": t.get("role"),
             "color": t.get("color"), "instrument": takes._instr(t.get("instrument")) if t.get("kind", "midi") == "midi" else None,
             **_mix(t, names),              # [start, length, kind, recording pass (0 = not recorded), 1 when muted (a pass not heard there)]
             "clips": [[c.get("start", 0), c.get("length", 0), c.get("kind", "midi"), int(c.get("lane") or 0),
                        1 if c.get("muted") else 0] for c in t.get("clips", [])]}
        if base is not None and b is None:
            e["new"] = True
        elif b is not None:
            old = _mix(b, was_names)
            # compare what is stored, not the labels: an effect whose settings moved keeps its name
            moved = {k: old[k] for k in MIX_FIELDS if (b.get(k) or _MIX_DEFAULT[k]) != (t.get(k) or _MIX_DEFAULT[k])}
            if b.get("instrument") != t.get("instrument"):
                moved["instrument"] = takes._instr(b.get("instrument"))
            if b.get("name") != t.get("name"):
                moved["name"] = b.get("name")
            if moved:
                e["was"] = moved
        if t["name"] in levels:
            lv = levels[t["name"]]
            e["level"] = {"peak_dbfs": lv.get("peak_dbfs"), "rms_dbfs": lv.get("rms_dbfs")}
        if view == "notes":
            now = takes._abs_notes(t.get("clips", []))
            then = takes._abs_notes(b.get("clips", [])) if b is not None else None
            added = (now - then) if then is not None else (now if base is not None else None)
            notes = []
            for (pitch, start, length, vel), n in sorted(now.items(), key=lambda kv: (kv[0][1], kv[0][0])):
                fresh = added[(pitch, start, length, vel)] if added else 0
                notes += [[pitch, start, length, vel, 1 if i < fresh else 0] for i in range(n)]
            if len(notes) > budget:
                notes, truncated = notes[:budget], True
            budget -= len(notes)
            e["notes"] = notes
            if then:
                e["gone"] = [[p_, s_, l_] for (p_, s_, l_, _v), n in sorted((then - now).items(), key=lambda kv: kv[0][1])
                             for _ in range(n)][:500]
        tracks.append(e)
    out = {"seat": role.id, "view": view, "version": version,
           "source": {"take": meta["id"] if meta else None, "state": meta.get("state") if meta else None,
                      "name": meta.get("name") if meta else None, "task": meta.get("task") if meta else None,
                      "returns": int(meta.get("returns") or 0) if meta else 0, "rev": doc.get("rev")},
           "takes": [t["id"] for t in mine], "form": _form(doc, d), "tracks": tracks,
           "master": {**{k: (doc.get("master") or {}).get(k) for k in ("volume_db", "target_lufs", "ceiling_db")},
                      "fx": [_fx(f) for f in (doc.get("master") or {}).get("fx") or []]},
           "render": render}
    if view == "picture":
        out["picture"] = _picture(doc, d, base)
    if base is not None:
        out["removed"] = [b["name"] for i, b in was.items() if i not in names]
        out["changes"] = len(takes.changes(base, doc))
        bm, dm = base.get("master") or {}, doc.get("master") or {}
        moved = {k: bm.get(k) for k in dict.fromkeys([*bm, *dm]) if bm.get(k) != dm.get(k)}
        if moved:
            out["master"]["was"] = {k: (True if k == "fx" else v) for k, v in moved.items()}
        for k in ("tempo", "key", "time_sig", "loop"):
            if base.get(k) != doc.get(k):
                out["form"].setdefault("was", {})[k] = base.get(k)
    if truncated:
        out["truncated"] = True
    return out
