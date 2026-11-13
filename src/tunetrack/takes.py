"""Takes: private working copies of a session's project, merged back when accepted.

    <session>/takes/<id>/project.json   the copy a seat edits (every normal tool works on it)
    <session>/takes/<id>/base.json      the session's project as it was when the take was cut (or last synced)
    <session>/takes/<id>/take.json      id, seat, task, state: open | submitted | accepted | rejected

Several seats can work at once, each in its own take, without seeing each other's half-finished work. Accepting a
take is a three-way merge: session + (take - base). The merge works on the units a studio actually divides labour
by, so a Composer's notes and a Mix engineer's fader moves on the same track combine cleanly:

    form     tempo, key, metre, loop, length; markers and the chord track (merged per bar range)
    brief    name and brief
    tracks   a track existing, its name / role / layer / colour
    sound    a track's instrument
    mix      volume, pan, mute, solo, output, sends, effects, automation
    notes    clips (two takes that changed different bars of one clip are merged note by note)
    master   master chain and loudness targets, sample rate, tail, EQ engine

Two sides changing the same unit differently is a conflict. Nothing is applied until every conflict has a decision.
A take shares its session's audio files and render cache (model.session_root), so rendering a take only renders
what the take changed.
"""
from __future__ import annotations

import copy
import json
import math
import re
import time
from collections import Counter
from pathlib import Path

from . import api, roles, studio
from .model import Project, _atomic_write, project_lock, session_root

FORM_FIELDS = ("tempo", "tempo_changes", "time_sig", "key", "length", "loop")
BRIEF_FIELDS = ("name",)
MASTER_FIELDS = ("sample_rate", "tail", "eq_engine")
TRACK_META = ("name", "kind", "role", "layer", "color")
TRACK_MIX = ("volume_db", "pan", "mute", "solo", "output", "sends", "fx")
CLIP_META = ("name", "muted", "color")
CATEGORIES = (roles.FORM, roles.BRIEF, roles.TRACKS, roles.SOUND, roles.MIX, roles.NOTES, roles.MASTER)
_EPS = 1e-6


# ---- small helpers -------------------------------------------------------------------------------------------------

def _bpb(doc: dict) -> float:
    num, den = (doc.get("time_sig") or [4, 4])[:2]
    return num * 4.0 / den


def _bars(start: float, end: float, bpb: float) -> set[int]:
    """0-based bar numbers a span touches (a point touches the bar it is in)."""
    first = int(math.floor(start / bpb + _EPS))
    last = int(math.ceil(end / bpb - _EPS)) - 1
    return set(range(first, max(first, last) + 1))


def _fmt_bars(bars) -> str:
    """{8, 9, 10, 16} -> "9-11, 17" (1-based, as the rest of TuneTrack shows bars)."""
    out, run = [], []
    for b in sorted(bars):
        if run and b == run[-1] + 1:
            run.append(b)
            continue
        if run:
            out.append(run)
        run = [b]
    if run:
        out.append(run)
    return ", ".join(str(r[0] + 1) if len(r) == 1 else f"{r[0] + 1}-{r[-1] + 1}" for r in out)


def _brief(v) -> str:
    if isinstance(v, (list, dict)):
        s = json.dumps(v)
        return s if len(s) <= 60 else f"{s[:57]}..."
    return "none" if v is None else str(v)


def _instr(d: dict | None) -> str:
    if not d:
        return "no instrument"
    return f"patch:{d['patch']}" if d.get("patch") else f"{d.get('type')}:{Path(d['name']).stem if d.get('type') == 'sfz' else d.get('name')}"


def _set(d: dict, k: str, v) -> None:
    if v is None:
        d.pop(k, None)
    else:
        d[k] = v


def _abs_notes(clips: list[dict]) -> Counter:
    return Counter((int(n[0]), round(c.get("start", 0) + n[1], 6), round(n[2], 6), int(n[3]) if len(n) > 3 else 90)
                   for c in clips if c.get("kind", "midi") == "midi" for n in c.get("notes", []))


def _abs_cc(clips: list[dict]) -> Counter:
    return Counter((str(lane), round(c.get("start", 0) + pt[0], 6), round(pt[1], 6))
                   for c in clips if c.get("kind", "midi") == "midi"
                   for lane, pts in (c.get("cc") or {}).items() for pt in pts)


def _touched(a: Counter, b: Counter, cc_a: Counter, cc_b: Counter, bpb: float) -> set[int]:
    """Bars in which two versions of some notes + controller points differ."""
    bars: set[int] = set()
    for n in ((a - b) + (b - a)):
        bars |= _bars(n[1], n[1] + n[2], bpb)
    for p in ((cc_a - cc_b) + (cc_b - cc_a)):
        bars |= _bars(p[1], p[1], bpb)
    return bars


def _ids(doc: dict) -> set[str]:
    return {t["id"] for t in doc.get("tracks", [])} | {c["id"] for t in doc.get("tracks", []) for c in t.get("clips", [])}


def _num(i: str) -> int:
    m = re.search(r"(\d+)$", str(i))
    return int(m.group(1)) if m else 0


def _rename_ids(doc: dict, mapping: dict[str, str]) -> None:
    if not mapping:
        return
    for t in doc.get("tracks", []):
        t["id"] = mapping.get(t["id"], t["id"])
        if t.get("output"):
            t["output"] = mapping.get(t["output"], t["output"])
        for s in t.get("sends", []):
            s["to"] = mapping.get(s["to"], s["to"])
        for f in t.get("fx", []):
            if f.get("sidechain"):
                f["sidechain"] = mapping.get(f["sidechain"], f["sidechain"])
        for c in t.get("clips", []):
            c["id"] = mapping.get(c["id"], c["id"])
    for f in (doc.get("master") or {}).get("fx", []):
        if f.get("sidechain"):
            f["sidechain"] = mapping.get(f["sidechain"], f["sidechain"])


# ---- three-way merge ---------------------------------------------------------------------------------------------------

class _Merge:
    def __init__(self, prefer: str | None, bpb: float):
        self.prefer = prefer  # None: report conflicts (dst's value is kept in the result) | "src" | "dst"
        self.bpb = bpb
        self.conflicts: list[dict] = []
        self.notes: list[str] = []

    def conflict(self, where: str, kind: str, why: str, s=None, d=None) -> bool:
        """Record a conflict; True when src's version should win."""
        c = {"where": where, "kind": kind, "why": why}
        if s is not None or d is not None:
            c.update(src=_brief(s), dst=_brief(d))
        self.conflicts.append(c)
        return self.prefer == "src"

    def three(self, where: str, kind: str, b, s, d):
        if s == b or s == d:
            return d
        if d == b:
            return s
        return s if self.conflict(where, kind, "both changed it", s, d) else d

    def three_dict(self, where: str, kind: str, b: dict | None, s: dict | None, d: dict | None) -> dict:
        b, s, d = b or {}, s or {}, d or {}
        out = {}
        for k in list(dict.fromkeys([*d, *s, *b])):
            v = self.three(f"{where} / {k}", kind, b.get(k), s.get(k), d.get(k))
            if v is not None:
                out[k] = v
        return out

    def timeline(self, where: str, kind: str, b: list, s: list, d: list, span) -> list:
        """Lists of timed entries (markers, chords): each side's additions and removals apply unless both touch a bar."""
        b, s, d = b or [], s or [], d or []
        if s == b or s == d:
            return d
        if d == b:
            return s
        key = lambda e: json.dumps(e, sort_keys=True)  # noqa: E731
        B, S, D = Counter(map(key, b)), Counter(map(key, s)), Counter(map(key, d))

        def bars(changed: Counter) -> set[int]:
            out: set[int] = set()
            for e in changed:
                out |= _bars(*span(json.loads(e)), self.bpb)
            return out

        both = bars((S - B) + (B - S)) & bars((D - B) + (B - D))
        if both:
            return s if self.conflict(f"{where}, bars {_fmt_bars(both)}", kind, "both changed these bars") else d
        merged = D - (B - S) + (S - B)
        return sorted((json.loads(e) for e in merged.elements()), key=lambda e: e.get("at", 0))

    # -- clips and tracks --

    def clip(self, where: str, b: dict, s: dict, d: dict) -> dict:
        """One MIDI clip both sides changed: combine note by note when they worked in different bars."""
        nb, ns, nd = _abs_notes([b]), _abs_notes([s]), _abs_notes([d])
        cb, cs, cd = _abs_cc([b]), _abs_cc([s]), _abs_cc([d])
        both = _touched(ns, nb, cs, cb, self.bpb) & _touched(nd, nb, cd, cb, self.bpb)
        if both:
            return s if self.conflict(f"{where}, bars {_fmt_bars(both)}", roles.NOTES, "both changed these bars") else d
        notes = nd - (nb - ns) + (ns - nb)
        cc = cd - (cb - cs) + (cs - cb)
        out = dict(d)
        for k in CLIP_META:
            _set(out, k, self.three(f"{where} / {k}", roles.NOTES, b.get(k), s.get(k), d.get(k)))

        def edge(get, widen):
            vb, vs, vd = get(b), get(s), get(d)
            return vd if vs == vb else vs if vd == vb else widen(vs, vd)

        start = edge(lambda c: c.get("start", 0.0), min)
        end = edge(lambda c: c.get("start", 0.0) + c.get("length", 4.0), max)
        if notes:
            start = min(start, math.floor(min(n[1] for n in notes) / self.bpb + _EPS) * self.bpb)
            end = max(end, math.ceil((max(n[1] for n in notes) + 1e-3) / self.bpb - _EPS) * self.bpb)
        out["start"], out["length"] = round(start, 6), round(max(end - start, self.bpb), 6)
        out["notes"] = sorted(([p, round(at - start, 6), dur, vel] for (p, at, dur, vel) in notes.elements()),
                              key=lambda n: (n[1], n[0]))
        lanes: dict[str, list] = {}
        for lane, at, val in cc.elements():
            lanes.setdefault(lane, []).append([round(at - start, 6), val])
        _set(out, "cc", {k: sorted(v) for k, v in lanes.items()} or None)
        return out

    def entities(self, where, kind: str, label: str, b: dict, s: dict, d: dict, both) -> dict:
        """Merge keyed things that can be added and removed (tracks in a project, clips in a track).
        b/s/d: id -> item. Returns id -> item in dst's order, src's additions after them."""
        out: dict = {}
        for i in list(dict.fromkeys([*d, *s])):
            ib, is_, id_ = b.get(i), s.get(i), d.get(i)
            name = where(is_ or id_ or ib)
            if is_ is not None and id_ is not None:
                out[i] = id_ if is_ == id_ or is_ == ib else is_ if id_ == ib else both(name, ib, is_, id_)
            elif id_ is not None:  # not in src
                if ib is None:
                    out[i] = id_  # dst added it
                elif id_ != ib and not self.conflict(name, kind, f"{label} removed on one side, changed on the other"):
                    out[i] = id_
                # else: src removed it and dst left it alone (or src wins) -> gone
            elif ib is None:
                out[i] = is_  # src added it
            elif is_ != ib and self.conflict(name, kind, f"{label} changed on one side, removed on the other"):
                out[i] = is_
        return out

    def track(self, name: str, b: dict | None, s: dict, d: dict) -> dict:
        b = b or {}
        out = dict(d)
        for k in TRACK_META:
            _set(out, k, self.three(f"{name} / {k}", roles.TRACKS, b.get(k), s.get(k), d.get(k)))
        _set(out, "instrument", self.three(f"{name} / instrument", roles.SOUND, b.get("instrument"),
                                           s.get("instrument"), d.get("instrument")))
        for k in TRACK_MIX:
            _set(out, k, self.three(f"{name} / {k}", roles.MIX, b.get(k), s.get(k), d.get(k)))
        _set(out, "automation", self.three_dict(f"{name} / automation", roles.MIX, b.get("automation"),
                                                s.get("automation"), d.get("automation")) or None)

        def both(where, cb, cs, cd):
            if cb and all(c.get("kind", "midi") == "midi" for c in (cb, cs, cd)):
                return self.clip(where, cb, cs, cd)
            return cs if self.conflict(where, roles.NOTES, "both changed this clip") else cd

        by_id = lambda t: {c["id"]: c for c in t.get("clips", [])}  # noqa: E731
        clips = self.entities(lambda c: f"{name} / clip {c.get('name') or c['id']}", roles.NOTES, "clip",
                              by_id(b), by_id(s), by_id(d), both)
        out["clips"] = sorted(clips.values(), key=lambda c: c.get("start", 0))
        return out


def merge(base: dict, src: dict, dst: dict, prefer: str | None = None, remap: str = "src") -> tuple[dict, list[dict], list[str]]:
    """dst + (src - base) on project documents. Returns (merged, conflicts, notes).

    prefer: who wins a conflict - None (it is only reported; the merged document then keeps dst's side and must not
    be saved), "src" or "dst". remap: the side whose newly created track/clip ids are renumbered when both sides
    happened to create the same id."""
    base, src, dst = copy.deepcopy(base), copy.deepcopy(src), copy.deepcopy(dst)
    m = _Merge(prefer, _bpb(dst))
    nxt = max(int(x.get("next_id", 1)) for x in (base, src, dst))
    side, other = (src, dst) if remap == "src" else (dst, src)
    clash = (_ids(side) - _ids(base)) & (_ids(other) - _ids(base))
    mapping = {}
    for old in sorted(clash, key=_num):
        mapping[old] = f"{re.sub(r'[0-9]+$', '', old) or 'x'}{nxt}"
        nxt += 1
    _rename_ids(side, mapping)

    out = dict(dst)
    for k in FORM_FIELDS:
        _set(out, k, m.three(k, roles.FORM, base.get(k), src.get(k), dst.get(k)))
    for k in BRIEF_FIELDS:
        _set(out, k, m.three(k, roles.BRIEF, base.get(k), src.get(k), dst.get(k)))
    out["brief"] = m.three_dict("brief", roles.BRIEF, base.get("brief"), src.get("brief"), dst.get("brief"))
    for k in MASTER_FIELDS:
        _set(out, k, m.three(k, roles.MASTER, base.get(k), src.get(k), dst.get(k)))
    out["master"] = m.three_dict("master", roles.MASTER, base.get("master"), src.get("master"), dst.get("master"))
    out["markers"] = m.timeline("markers", roles.FORM, base.get("markers"), src.get("markers"), dst.get("markers"),
                                lambda e: (e.get("at", 0), e.get("at", 0)))
    out["chord_track"] = m.timeline("chord track", roles.FORM, base.get("chord_track"), src.get("chord_track"),
                                    dst.get("chord_track"), lambda e: (e.get("at", 0), e.get("at", 0) + e.get("len", 0)))

    by_id = lambda doc: {t["id"]: t for t in doc.get("tracks", [])}  # noqa: E731
    bt, st, dt = by_id(base), by_id(src), by_id(dst)
    tracks = m.entities(lambda t: f"track {t['name']}", roles.TRACKS, "track", bt, st, dt,
                        lambda where, tb, ts, td: m.track(td["name"], tb, ts, td))
    out["tracks"] = [tracks[i] for i in _order(list(bt), list(st), list(dt), tracks)]

    src_new, dst_new = {i for i in st if i not in bt}, {i for i in dt if i not in bt}
    _tidy(out, m.notes, *((src_new, dst_new - src_new) if remap == "src" else (dst_new, src_new - dst_new)))
    out["next_id"] = max([nxt] + [_num(i) + 1 for i in _ids(out)])
    return out, m.conflicts, m.notes


def _order(base_ids: list, src_ids: list, dst_ids: list, present) -> list:
    """Track order after a merge. Whoever reordered the tracks wins (dst if both did); the other side's new
    tracks go after the track they followed on their own side."""
    dst_moved = [i for i in dst_ids if i in base_ids] != [i for i in base_ids if i in dst_ids]
    primary, secondary = (dst_ids, src_ids) if dst_moved else (src_ids, dst_ids)
    order = [i for i in primary if i in present]
    for k, i in enumerate(secondary):
        if i in present and i not in order:
            before = [j for j in secondary[:k] if j in order]
            order.insert(order.index(before[-1]) + 1 if before else 0, i)
    return order


def _tidy(doc: dict, notes: list[str], incoming: set[str], settled: set[str]) -> None:
    """Keep a merged document consistent: unique ids, no routing to tracks that are gone, and a new name for an
    incoming new track when the other side added one of the same name meanwhile (tools find tracks by name).
    incoming / settled: ids of the tracks each side created; the incoming side is the one that gives way."""
    tracks = doc.get("tracks", [])
    seen: set[str] = set()
    nxt = max([int(doc.get("next_id", 1))] + [_num(i) + 1 for i in _ids(doc)])
    for t in tracks:
        for c in t.get("clips", []):
            if c["id"] in seen:
                c["id"], nxt = f"c{nxt}", nxt + 1
            seen.add(c["id"])
    doc["next_id"] = nxt
    taken = {t["name"].lower() for t in tracks if t["id"] in settled}
    for t in tracks:
        if t["id"] in incoming and t["name"].lower() in taken:
            base_name, n = t["name"], 2
            while t["name"].lower() in {x["name"].lower() for x in tracks if x is not t}:
                t["name"] = f"{base_name} ({n})"
                n += 1
            notes.append(f"track '{base_name}' was added on both sides: the incoming one is now '{t['name']}'")
    ids = {t["id"] for t in tracks}
    for t in tracks:
        if t.get("output") and t["output"] != "master" and t["output"] not in ids:
            notes.append(f"{t['name']}: its output bus is gone, now routed to master")
            t.pop("output")
        if any(s["to"] not in ids for s in t.get("sends", [])):
            notes.append(f"{t['name']}: dropped a send to a bus that is gone")
            _set(t, "sends", [s for s in t["sends"] if s["to"] in ids] or None)
    for owner, chain in [(t["name"], t.get("fx", [])) for t in tracks] + [("master", (doc.get("master") or {}).get("fx", []))]:
        for f in chain:
            if f.get("sidechain") and f["sidechain"] not in ids:
                notes.append(f"{owner}: {f.get('name')} lost its sidechain key (that track is gone)")
                f.pop("sidechain")


# ---- describing a take ---------------------------------------------------------------------------------------------

def changes(base: dict, doc: dict) -> list[dict]:
    """What a document changed relative to its base, as {"kind", "where", "what"} lines a person can read."""
    out: list[dict] = []
    bpb = _bpb(doc)

    def add(kind: str, where: str, what: str) -> None:
        out.append({"kind": kind, "where": where, "what": what})

    for k in FORM_FIELDS:
        if base.get(k) != doc.get(k):
            add(roles.FORM, k, f"{_brief(base.get(k))} -> {_brief(doc.get(k))}")
    for k, kind in [(k, roles.BRIEF) for k in BRIEF_FIELDS] + [(k, roles.MASTER) for k in MASTER_FIELDS]:
        if base.get(k) != doc.get(k):
            add(kind, k, f"{_brief(base.get(k))} -> {_brief(doc.get(k))}")
    if (base.get("brief") or {}) != (doc.get("brief") or {}):
        keys = [k for k in {**(base.get("brief") or {}), **(doc.get("brief") or {})}
                if (base.get("brief") or {}).get(k) != (doc.get("brief") or {}).get(k)]
        add(roles.BRIEF, "brief", "changed " + ", ".join(keys))
    bm, dm = base.get("master") or {}, doc.get("master") or {}
    for k in dict.fromkeys([*bm, *dm]):
        if bm.get(k) != dm.get(k):
            add(roles.MASTER, f"master / {k}", "effects chain changed" if k == "fx" else f"{_brief(bm.get(k))} -> {_brief(dm.get(k))}")
    for k, label, span in (("markers", "markers", lambda e: (e.get("at", 0), e.get("at", 0))),
                           ("chord_track", "chord track", lambda e: (e.get("at", 0), e.get("at", 0) + e.get("len", 0)))):
        b, d = base.get(k) or [], doc.get(k) or []
        if b != d:
            key = lambda e: json.dumps(e, sort_keys=True)  # noqa: E731
            B, D = Counter(map(key, b)), Counter(map(key, d))
            bars: set[int] = set()
            for e in ((D - B) + (B - D)):
                bars |= _bars(*span(json.loads(e)), bpb)
            add(roles.FORM, label, f"changed in bars {_fmt_bars(bars)}")

    bt = {t["id"]: t for t in base.get("tracks", [])}
    dt = {t["id"]: t for t in doc.get("tracks", [])}
    for i, t in dt.items():
        b = bt.get(i)
        name = t["name"]
        if b is None:
            n = _abs_notes(t.get("clips", []))
            bars = set().union(*[_bars(x[1], x[1] + x[2], bpb) for x in n]) if n else set()
            what = f"new {t.get('kind', 'midi')} track, {_instr(t.get('instrument'))}"
            if n:
                what += f", {sum(n.values())} notes in bars {_fmt_bars(bars)}"
            audio = [c for c in t.get("clips", []) if c.get("kind") == "audio"]
            if audio:
                what += f", {len(audio)} audio clip{'s' if len(audio) != 1 else ''}"
            add(roles.TRACKS, name, what)
            continue
        for k in TRACK_META:
            if b.get(k) != t.get(k):
                add(roles.TRACKS, name, f"{k} {_brief(b.get(k))} -> {_brief(t.get(k))}")
        if b.get("instrument") != t.get("instrument"):
            bi, ti = _instr(b.get("instrument")), _instr(t.get("instrument"))
            add(roles.SOUND, name, f"instrument {bi} -> {ti}" if bi != ti else f"instrument settings changed ({ti})")
        for k in TRACK_MIX:
            if b.get(k) != t.get(k):
                add(roles.MIX, name, f"{k} changed" if k in ("sends", "fx") else
                    f"{k} {_brief(b.get(k) or (0 if k in ('volume_db', 'pan') else False if k in ('mute', 'solo') else 'master'))}"
                    f" -> {_brief(t.get(k) or (0 if k in ('volume_db', 'pan') else False if k in ('mute', 'solo') else 'master'))}")
        if (b.get("automation") or {}) != (t.get("automation") or {}):
            add(roles.MIX, name, "automation changed")
        nb, nd = _abs_notes(b.get("clips", [])), _abs_notes(t.get("clips", []))
        cb, cd = _abs_cc(b.get("clips", [])), _abs_cc(t.get("clips", []))
        if nb != nd:
            bars = _touched(nd, nb, Counter(), Counter(), bpb)
            add(roles.NOTES, name, f"notes changed in bars {_fmt_bars(bars)} "
                                   f"(+{sum((nd - nb).values())} -{sum((nb - nd).values())})")
        if cb != cd:
            lanes = sorted({p[0] for p in ((cd - cb) + (cb - cd))})
            bars = _touched(Counter(), Counter(), cd, cb, bpb)
            add(roles.NOTES, name, f"controller {', '.join('pitch bend' if x == 'bend' else 'CC' + x for x in lanes)} "
                                   f"changed in bars {_fmt_bars(bars)}")
        strip = lambda cs, midi: [{k: v for k, v in c.items() if not (midi and k in ("notes", "cc"))}  # noqa: E731
                                  for c in cs if (c.get("kind", "midi") == "midi") == midi]
        if strip(b.get("clips", []), False) != strip(t.get("clips", []), False):
            add(roles.NOTES, name, "audio clips changed")
        elif nb == nd and cb == cd and strip(b.get("clips", []), True) != strip(t.get("clips", []), True):
            add(roles.NOTES, name, "clips rearranged (muted, renamed, resized or split)")
    for i, b in bt.items():
        if i not in dt:
            add(roles.TRACKS, b["name"], "track removed")
    if [i for i in dt if i in bt] != [i for i in bt if i in dt]:
        add(roles.TRACKS, "tracks", "order changed")
    return out


# ---- takes on disk ---------------------------------------------------------------------------------------------------

def _read_json(f: Path) -> dict:
    return json.loads(f.read_text(encoding="utf-8"))


def _take_dir(take: str | None, session: str | None) -> Path:
    """A take's folder from its id, or the take this process is working in."""
    if take:
        p = Path(str(take))
        if (p / "take.json").exists():
            return p.resolve()
        d = studio.session_path(session) / "takes" / str(take).strip().lower()
        if not (d / "take.json").exists():
            have = ", ".join(t["id"] for t in studio.take_index(studio.session_path(session))) or "none"
            raise KeyError(f"No take {take!r}. Takes: {have}")
        return d
    cur = api._path(None)
    if session_root(cur) == cur or not (cur / "take.json").exists():
        raise ValueError("You are not working in a take: pass take=<id> (take_list shows them)")
    return cur


def _meta(d: Path, **update) -> dict:
    meta = _read_json(d / "take.json")
    if update:
        meta.update(update)
        _atomic_write(d / "take.json", json.dumps(meta, indent=1))
    return meta


def _report(d: Path, meta: dict, trial: bool = True) -> dict:
    session = session_root(d)
    base, doc = _read_json(d / "base.json"), _read_json(d / "project.json")
    ch = changes(base, doc)
    out = {"take": meta["id"], "seat": meta.get("seat"), "state": meta["state"], "changes": ch}
    if meta.get("task"):
        out["task"] = meta["task"]
    if meta.get("summary"):
        out["summary"] = meta["summary"]
    seat = meta.get("seat")
    if seat in roles.ROLES:
        owns = roles.ROLES[seat].owns
        outside = [c for c in ch if c["kind"] not in owns]
        if outside:
            out["outside_seat"] = [f"{c['where']}: {c['what']} ({c['kind']} is not {seat}'s department)" for c in outside]
    if trial and meta["state"] in ("open", "submitted"):
        cur = _read_json(session / "project.json")
        if cur.get("rev") != meta.get("base_rev"):
            _m, conflicts, _n = merge(base, doc, cur)
            behind = changes(base, cur)
            out["session_moved"] = {"since_rev": meta.get("base_rev"), "now_rev": cur.get("rev"),
                                    "changes": len(behind), "conflicts": _label(conflicts, "take", "session")}
    return out


def _label(conflicts: list[dict], src: str, dst: str) -> list[dict]:
    return [{("where" if k == "where" else src if k == "src" else dst if k == "dst" else k): v for k, v in c.items()}
            for c in conflicts]


def take_new(name: str | None = None, task: str | None = None, seat: str | None = None, use: bool = True,
             session: str | None = None) -> dict:
    """Cut a take: a private copy of the session's project to work in. Your tools then edit the take, and nobody
    else's work changes under you. Finish with take_submit. task: the task this take is for."""
    path = studio.session_path(session)
    who = studio._who(seat) if seat else studio.me()
    with studio._Board(str(path)) as b:
        tid = b.new_id("tk")
        t = b.task(task) if task else None
        if t:
            t["take"] = tid
        b.emit("take", id=tid, state="open", seat=who, task=t["id"] if t else None, name=name)
    d = path / "takes" / tid
    with project_lock(path):
        text = (path / "project.json").read_text(encoding="utf-8")
        d.mkdir(parents=True, exist_ok=True)
        (d / "project.json").write_text(text, encoding="utf-8")
        (d / "base.json").write_text(text, encoding="utf-8")
        base_rev = int(json.loads(text).get("rev", 0))
    meta = {"id": tid, "name": name or (t["title"] if t else tid), "seat": who, "task": t["id"] if t else None,
            "state": "open", "base_rev": base_rev, "created": round(time.time(), 3)}
    _atomic_write(d / "take.json", json.dumps(meta, indent=1))
    if use:
        api.set_default_project(d)
    return {"take": tid, "project": str(d), "base_rev": base_rev,
            "note": "Your tools now edit this take. take_diff shows what you changed; take_submit hands it in."
            if use else "Pass project=<this path> to edit the take."}


def take_use(take: str | None = None, session: str | None = None) -> dict:
    """Point this process's tools at a take (to continue or audition it), or back at the session (take=None)."""
    if not take or str(take).strip().lower() in ("none", "null", "session"):  # how agents write "the session"
        api.set_default_project(None)
        return {"editing": "session", "project": str(studio.session_path(session))}
    d = _take_dir(take, session)
    api.set_default_project(d)
    return {"editing": _meta(d)["id"], "project": str(d)}


def take_list(state: str | None = None, session: str | None = None) -> dict:
    """Takes in the session. state: open | submitted | accepted | rejected (default: open and submitted)."""
    want = {state} if state else {"open", "submitted"}
    return {"takes": [t for t in studio.take_index(studio.session_path(session)) if state == "all" or t.get("state") in want]}


def take_pending(session: str | None = None) -> dict:
    """Takes whose work is not in the session: handed in and waiting, or still open. An export does not contain
    them, so a session is finished when this is empty or every take in it is left out on purpose. `status` says
    where each one stands; `loose` marks the ones nobody is dealing with (handed in with nobody asked to audition
    it, or left open with work in it after its task was closed)."""
    path = studio.session_path(session)
    st = studio.read(str(path))
    tasks = {t["id"]: t for t in st["tasks"]}
    out = []
    for meta in studio.take_index(path):
        if meta.get("state") not in ("open", "submitted"):
            continue
        d = path / "takes" / meta["id"]
        try:
            n = len(changes(_read_json(d / "base.json"), _read_json(d / "project.json")))
        except (OSError, ValueError):
            continue
        task = tasks.get(meta.get("task") or "")
        loose = False
        if meta["state"] == "submitted":
            asked = studio.auditioner(st, meta["id"])
            status = f"handed in, waiting for {asked} to audition it" if asked else \
                "handed in, and nobody has been asked to audition it"
            loose = not asked
        elif not n:
            status = "open and empty"
        elif task and task["state"] in studio.OPEN:
            status = f"in progress ({task['id']} is {task['state']})"
        else:
            status = (f"left open with work in it: its task {task['id']} is {task['state']}" if task
                      else "open with work in it, for no task")
            loose = bool(task)
        out.append({"take": meta["id"], "seat": meta.get("seat"), "name": meta.get("name"), "state": meta["state"],
                    "changes": n, "status": status, **({"task": task["id"]} if task else {}),
                    **({"loose": True} if loose else {})})
    return {"takes": out}


def take_audition(take: str | None = None, session: str | None = None) -> dict:
    """Render a take so it can be heard next to the session (take=None renders the session itself: the "A" of an
    A/B). Returns the audio file, its loudness and warnings, and the revision that was rendered."""
    d = _take_dir(take, session) if take else studio.session_path(session)
    rep = api.preview(str(d))
    mix = rep.get("mix") or {}
    return {"take": _meta(d)["id"] if take else None, "file": rep["files"][0], "rev": _read_json(d / "project.json").get("rev"),
            "lufs": mix.get("lufs"), "duration_s": mix.get("duration_s"), "warnings": rep.get("warnings", [])}


def take_diff(take: str | None = None, session: str | None = None) -> dict:
    """What a take changed (default: the one you are working in), what falls outside its seat's department, and
    whether the session has moved on since it was cut - with the conflicts accepting it would hit right now."""
    d = _take_dir(take, session)
    return _report(d, _meta(d))


def take_submit(summary: str, take: str | None = None, session: str | None = None) -> dict:
    """Hand in a take for acceptance. summary: what you made and anything the listener should check - written for
    the Producer and the user, who will audition it before accepting."""
    d = _take_dir(take, session)
    meta = _meta(d)
    if meta["state"] not in ("open", "submitted"):
        raise ValueError(f"{meta['id']} is already {meta['state']}")
    if studio.me() not in (meta.get("seat"), roles.YOU, "producer"):
        raise PermissionError(f"{meta['id']} belongs to {meta.get('seat')}")
    rep = _report(d, meta)
    if not rep["changes"]:
        raise ValueError("This take changes nothing yet. Did the edits go to another project? "
                         "(project_info shows which one your tools are editing.) If the task needed no change, "
                         "do not hand in an empty take: task_update(task, state=\"done\", result=why).")
    meta = _meta(d, state="submitted", summary=str(summary).strip(), submitted=round(time.time(), 3))
    with studio._Board(str(session_root(d))) as b:
        if meta.get("task"):
            t = b.task(meta["task"])
            # "done" too: a seat that closes its task and then hands in (seen live) has work that is not in the
            # session yet. Left as done, nobody is asked to audition the take and the export goes out without it.
            if t["state"] in ("queued", "working", "needs_you", "done"):
                t.update(state="submitted", result=meta["summary"], updated=meta["submitted"])
                b.emit("task", id=t["id"], seat=t["seat"], state="submitted", title=t["title"], by=studio.me(),
                       take=meta["id"])
        b.emit("take", id=meta["id"], state="submitted", seat=meta.get("seat"), task=meta.get("task"),
               summary=meta["summary"][:studio.EVENT_TEXT], changes=len(rep["changes"]))
    return {**rep, "state": "submitted", "summary": meta["summary"]}


def take_accept(take: str, on_conflict: str = "fail", session: str | None = None) -> dict:
    """Merge a take into the session (one undo step). on_conflict: "fail" (default: change nothing and list the
    conflicts), "take" (the take's version wins each conflict) or "session" (the session's version is kept)."""
    prefer = {"fail": None, "take": "src", "session": "dst"}.get(on_conflict, 0)
    if prefer == 0:
        raise ValueError('on_conflict must be "fail", "take" or "session"')
    if studio.me() not in (roles.YOU, "producer"):
        raise PermissionError("Takes are accepted by the Producer or the user")
    d = _take_dir(take, session)
    path = session_root(d)
    meta = _meta(d)
    if meta["state"] not in ("open", "submitted"):
        raise ValueError(f"{meta['id']} is already {meta['state']}")
    base, doc = _read_json(d / "base.json"), _read_json(d / "project.json")
    ch = changes(base, doc)
    with project_lock(path):
        cur = _read_json(path / "project.json")
        merged, conflicts, notes = merge(base, doc, cur, prefer)
        if conflicts and prefer is None:
            return {"accepted": False, "take": meta["id"], "conflicts": _label(conflicts, "take", "session"),
                    "note": 'Nothing was changed. Accept with on_conflict="take" or "session", or have the seat '
                            "take_sync and resolve them in the take."}
        p = Project.from_json(merged, path=path)
        p.rev = int(cur.get("rev", 0))
        p.save()
    meta = _meta(d, state="accepted", accepted=round(time.time(), 3), merged_rev=p.rev)
    with studio._Board(str(path)) as b:
        done = None
        if meta.get("task"):
            t = b.task(meta["task"])
            if t["state"] not in ("done", "cancelled"):
                t.update(state="done", updated=meta["accepted"], result=t.get("result") or meta.get("summary", ""))
                done = t["id"]
                b.emit("task", id=t["id"], seat=t["seat"], state="done", title=t["title"], by=studio.me(), take=meta["id"])
        b.emit("take", id=meta["id"], state="accepted", seat=meta.get("seat"), task=meta.get("task"), rev=p.rev,
               by=studio.me())
        b.close_auditions(meta["id"], "accepted")
    out = {"accepted": True, "take": meta["id"], "rev": p.rev, "changes": len(ch)}
    if done:
        out["task_done"] = done
    if conflicts:
        out["conflicts_resolved"] = {"winner": on_conflict, "conflicts": _label(conflicts, "take", "session")}
    if notes:
        out["notes"] = notes
    return out


def take_reject(take: str, reason: str, session: str | None = None) -> dict:
    """Send a take back. reason: what has to change, specific enough to act on (bars, tracks, what you heard).
    The seat gets it as a message and its task goes back in the queue; the take stays open for it to continue."""
    if studio.me() not in (roles.YOU, "producer"):
        raise PermissionError("Takes are returned by the Producer or the user")
    if not str(reason).strip():
        raise ValueError("Say what has to change")
    d = _take_dir(take, session)
    meta = _meta(d)
    if meta["state"] not in ("open", "submitted"):
        raise ValueError(f"{meta['id']} is already {meta['state']}")
    # the reason stays on the take: a person in the seat reads it on their task and on the Desk, not in a log
    meta = _meta(d, state="open", returned=round(time.time(), 3), returns=int(meta.get("returns", 0)) + 1,
                 reason=str(reason).strip())
    with studio._Board(str(session_root(d))) as b:
        if meta.get("task"):
            t = b.task(meta["task"])
            if t["state"] in ("submitted", "needs_you", "working"):
                t.update(state="queued", updated=meta["returned"])
                b.emit("task", id=t["id"], seat=t["seat"], state="queued", title=t["title"], by=studio.me(), take=meta["id"])
        if meta.get("seat") in roles.ROLES:
            b.message(studio.me(), meta["seat"], f"Take {meta['id']} came back: {str(reason).strip()}", meta.get("task"))
        b.emit("take", id=meta["id"], state="returned", seat=meta.get("seat"), task=meta.get("task"),
               reason=str(reason).strip()[:studio.EVENT_TEXT], by=studio.me())
        b.close_auditions(meta["id"], "returned")
    return {"returned": meta["id"], "to": meta.get("seat"), "task": meta.get("task")}


def take_discard(take: str, session: str | None = None) -> dict:
    """Close a take without merging it (it stays on disk for reference, marked rejected)."""
    if studio.me() not in (roles.YOU, "producer"):
        raise PermissionError("Takes are discarded by the Producer or the user")
    d = _take_dir(take, session)
    meta = _meta(d)
    if meta["state"] not in ("open", "submitted"):
        raise ValueError(f"{meta['id']} is already {meta['state']}")
    meta = _meta(d, state="rejected", closed=round(time.time(), 3))
    out = {"discarded": meta["id"]}
    with studio._Board(str(session_root(d))) as b:
        b.emit("take", id=meta["id"], state="rejected", seat=meta.get("seat"), task=meta.get("task"), by=studio.me())
        b.close_auditions(meta["id"], "discarded")
        t = b.task(meta["task"]) if meta.get("task") else None
        if t and t["state"] in studio.OPEN:  # the work is gone but the task is not: whoever discards decides
            out["task"] = (f"{t['id']} is still {t['state']}: task_update(\"{t['id']}\", state=\"cancelled\") if the "
                           f"work is not wanted, or state=\"queued\" to have it done again in a new take.")
    return out


def take_sync(take: str | None = None, on_conflict: str = "fail", session: str | None = None) -> dict:
    """Bring what the session has accepted since your take was cut into the take, so you hear and build on the
    current music. on_conflict: "fail" (default: change nothing, list the conflicts), "mine" (keep your version)
    or "session" (take theirs)."""
    prefer = {"fail": None, "mine": "dst", "session": "src"}.get(on_conflict, 0)
    if prefer == 0:
        raise ValueError('on_conflict must be "fail", "mine" or "session"')
    d = _take_dir(take, session)
    path = session_root(d)
    meta = _meta(d)
    if meta["state"] not in ("open", "submitted"):
        raise ValueError(f"{meta['id']} is already {meta['state']}")
    with project_lock(path):
        cur = _read_json(path / "project.json")
    if int(cur.get("rev", 0)) == int(meta.get("base_rev", -1)):
        return {"synced": True, "take": meta["id"], "note": "Already up to date"}
    with project_lock(d):
        base, doc = _read_json(d / "base.json"), _read_json(d / "project.json")
        brought = changes(base, cur)
        merged, conflicts, notes = merge(base, cur, doc, prefer, remap="dst")
        if conflicts and prefer is None:
            return {"synced": False, "take": meta["id"], "conflicts": _label(conflicts, "session", "mine"),
                    "note": 'Nothing was changed. Sync with on_conflict="mine" or "session", or adjust your take.'}
        p = Project.from_json(merged, path=d)
        p.rev = int(doc.get("rev", 0))
        p.save()
        _atomic_write(d / "base.json", json.dumps(cur, indent=1))
    _meta(d, base_rev=int(cur.get("rev", 0)))
    out = {"synced": True, "take": meta["id"], "session_rev": cur.get("rev"),
           "brought_in": [f"{c['where']}: {c['what']}" for c in brought]}
    if conflicts:
        out["conflicts_resolved"] = {"winner": on_conflict, "conflicts": _label(conflicts, "session", "mine")}
    if notes:
        out["notes"] = notes
    return out
