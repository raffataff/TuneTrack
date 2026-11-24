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


# ---- the whole-machine sweep (`tunetrack clean`) -----------------------------------------------------------------------

def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _newest(p: Path) -> float:
    """Newest mtime in a file or folder tree, so a folder still being written to counts as fresh."""
    times = [p.stat().st_mtime] + ([f.stat().st_mtime for f in p.rglob("*")] if p.is_dir() else [])
    return max(times)


def _old_entries(folder: Path, days: float, dry_run: bool) -> dict:
    """Delete the top-level entries of `folder` untouched for `days` days."""
    import shutil
    before = time.time() - 86400 * max(0.0, days)
    gone, freed, kept = [], 0, 0
    for e in sorted(folder.iterdir()) if folder.is_dir() else []:
        try:
            if _newest(e) > before:
                kept += 1
                continue
            size = _size(e)
            if not dry_run:
                shutil.rmtree(e) if e.is_dir() else e.unlink()
        except OSError:
            kept += 1
            continue
        gone.append(e.name)
        freed += size
    return {"mb": round(freed / 1e6, 1), "removed": len(gone), "kept": kept}


def sweep(days: float = 7.0, dry_run: bool = True) -> dict:
    """Everything TuneTrack can rebuild or no longer needs, across every project:
    renders  - each session's cached renders no live project would read (cleanup() above; never fresh ones)
    takes    - folders of takes that are over (accepted or closed) and ended `days` ago or more
    auditions- instrument/gear preview WAVs in the library untouched for `days`
    previews - scratch check runs in output/_preview untouched for `days`
    Songs, open or handed-in takes, renders/, patches, instruments, knowledge, lessons, the app profile and the
    engine build are never touched. Leftover agent worktrees are only reported (they can hold unmerged work).
    dry_run (the default) changes nothing and says what would go."""
    from . import config, takes
    out: dict = {"dry_run": dry_run, "renders": {}, "takes": {}, "errors": {}}
    for pj in sorted(config.PROJECTS_DIR.glob("*/project.json")) if config.PROJECTS_DIR.is_dir() else []:
        d = pj.parent
        try:
            r = cleanup(d, dry_run=dry_run)
            if r["removed"]:
                out["renders"][d.name] = r["freed_mb"]
        except Exception as e:  # noqa: BLE001 - one broken project must not stop the sweep
            out["errors"][d.name] = f"renders: {e}"
        if (d / "takes").is_dir():
            try:
                t = takes.take_cleanup(days, dry_run=dry_run, session=str(d))
                if t["mb"]:
                    out["takes"][d.name] = t["mb"]
            except Exception as e:  # noqa: BLE001
                out["errors"][d.name] = f"takes: {e}"
    out["auditions"] = _old_entries(config.HOME / "auditions", days, dry_run)
    out["previews"] = _old_entries(config.ROOT / "output" / "_preview", days, dry_run)
    wt = config.ROOT / ".claude" / "worktrees"
    out["worktrees"] = {e.name: round(_size(e) / 1e6, 1) for e in sorted(wt.iterdir()) if e.is_dir()} \
        if wt.is_dir() else {}
    out["total_mb"] = round(sum(out["renders"].values()) + sum(out["takes"].values())
                            + out["auditions"]["mb"] + out["previews"]["mb"], 1)
    if not out["errors"]:
        del out["errors"]
    return out


def report(r: dict) -> str:
    """sweep()'s result as a few lines for the terminal."""
    verb = "Would free" if r["dry_run"] else "Freed"
    lines = [f"{verb} {r['total_mb'] / 1000:.2f} GB"]
    def row(label, mb, detail=""):
        lines.append(f"  {label:<10} {mb:>8.1f} MB  {detail}".rstrip())
    row("renders", sum(r["renders"].values()), ", ".join(f"{k} {v:.0f}" for k, v in r["renders"].items()))
    row("takes", sum(r["takes"].values()), ", ".join(f"{k} {v:.0f}" for k, v in r["takes"].items()))
    row("auditions", r["auditions"]["mb"], f"{r['auditions']['removed']} files")
    row("previews", r["previews"]["mb"], f"{r['previews']['removed']} entries (output/_preview)")
    for k, v in r.get("errors", {}).items():
        lines.append(f"  skipped {k}: {v}")
    if r["worktrees"]:
        lines.append("Leftover agent worktrees (not touched; remove with `git worktree remove <path>` once their "
                     "branch is merged or unwanted):")
        lines += [f"  .claude/worktrees/{k}  {v:.0f} MB" for k, v in r["worktrees"].items()]
    if r["dry_run"]:
        lines.append("Nothing deleted. Run `tunetrack clean --yes` to delete.")
    return "\n".join(lines)
