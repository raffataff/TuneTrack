"""Prune a session's render cache (<session>/cache): delete the files no live project would read again.

Live = the session's project.json plus every take that is open or handed in (takes share the session's cache).
Track renders (<key>.wav, .mid ...) and stretched clips (stretch-<key>.wav) are kept when a live project's current
state has that key, computed the way the renderer computes it, without rendering. Bus and master FX renders are
keyed by a hash of their input audio, which only a render knows: the newest few per bus (one per live project) are
kept, and those of buses no live project has are dropped. Files being written (locks, recent files) are kept.
Finished take folders are take_cleanup's job (takes.py), not this one's."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .model import Project, session_root

RECENT = 600.0  # seconds: anything this fresh may be a render in progress
_TRACK = re.compile(r"^[0-9a-f]{8,}$")
_TAGGED = re.compile(r"^(master|bus[^-]*)-([0-9a-f]{8,})$")


def _live_projects(session: Path) -> list[Project]:
    out = [Project.load(session)]
    for tj in sorted((session / "takes").glob("*/take.json")):
        try:
            state = json.loads(tj.read_text(encoding="utf-8")).get("state")
        except (OSError, ValueError):
            state = "open"  # unreadable: assume it is in use
        if state in ("open", "submitted") and (tj.parent / "project.json").exists():
            try:
                out.append(Project.load(tj.parent))
            except Exception:  # noqa: BLE001 - a broken take must not make its renders look dead
                raise RuntimeError(f"cannot read take {tj.parent.name}; nothing cleaned") from None
    return out


def live_keys(p: Project) -> tuple[set[str], set[str], set[str]]:
    """(track render keys, stretch keys, bus ids) this project would use if rendered now. Every source track counts,
    muted or not, so unmuting does not cost a render."""
    from . import engine
    r = engine.Renderer(p)
    specs = {}
    for t in p.tracks:
        if t.kind == "bus":
            continue
        try:
            s = r.track_spec(t)
        except Exception:  # noqa: BLE001 - a track that cannot render has no key
            continue
        if s is not None:
            specs[t.id] = s
    r._link_sidechains(specs, [])
    keys = set(r.key_fp.values())
    stretch = set()
    tm, sr = p.tempo_map(), p.sample_rate
    for t in p.tracks:
        for c in t.clips if t.kind == "audio" else []:
            if c.muted or c.kind != "audio" or not c.file or not (c.bpm or c.pitch):
                continue
            f = Path(c.file) if Path(c.file).is_absolute() else p.root / c.file
            if f.exists():
                try:
                    stretch.add(engine._stretch_key(c, f, tm, sr))
                except Exception:  # noqa: BLE001
                    pass
    return keys, stretch, {t.id for t in p.tracks if t.kind == "bus"}


def cleanup(path: Path, dry_run: bool = False) -> dict:
    session = session_root(Path(path))
    cache = session / "cache"
    if not cache.is_dir():
        return {"freed_mb": 0.0, "removed": 0, "kept": 0}
    projects = _live_projects(session)
    keys, stretch, buses = set(), set(), set()
    for p in projects:
        k, s, b = live_keys(p)
        keys |= k
        stretch |= s
        buses |= b
    now = time.time()
    files = [f for f in cache.iterdir() if f.is_file()]
    locked = {f.name.split(".")[0] for f in files if f.suffix == ".lock"}
    tagged: dict[str, list[tuple[float, str]]] = {}  # tag -> (mtime, base) of finished FX renders
    for f in files:
        base = f.name.split(".")[0]
        m = _TAGGED.match(base)
        if m and f.name == f"{base}.wav":
            tagged.setdefault(m.group(1), []).append((f.stat().st_mtime, base))
    keep_tagged = set()
    for tag, got in tagged.items():
        if tag == "master" or tag[3:] in buses:
            keep_tagged |= {b for _, b in sorted(got, reverse=True)[:len(projects)]}

    removed = kept = 0
    freed = 0
    for f in files:
        base = f.name.split(".")[0]
        try:
            st = f.stat()
        except OSError:
            continue
        if f.suffix == ".lock" or base in locked or now - st.st_mtime < RECENT:
            dead = False
        elif _TRACK.match(base):
            dead = base not in keys
        elif base.startswith("stretch-"):
            dead = base[len("stretch-"):] not in stretch
        elif _TAGGED.match(base):
            dead = base not in keep_tagged
        else:
            dead = False  # not a render (live-spec.json, ...): not ours to judge
        if not dead:
            kept += 1
            continue
        if not dry_run:
            try:
                f.unlink()
            except OSError:  # open in another process: it stays
                kept += 1
                continue
        removed += 1
        freed += st.st_size
    return {"freed_mb": round(freed / 1e6, 1), "removed": removed, "kept": kept}
