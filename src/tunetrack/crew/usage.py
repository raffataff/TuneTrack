"""Subscription limits (5-hour and weekly windows) as each agent app last reported them.

Nothing here asks a service: it reads what the apps already wrote to disk.
- claude: `rate_limit_event` lines in the crew's run logs (projects/*/studio/logs/*.jsonl, stream-json output).
- codex: `rate_limits` in the Codex CLI's own session files (~/.codex/sessions/**/rollout-*.jsonl).
agy and kilo have no such readout, so they come back as unknown. The formats are not documented and may change:
anything that does not parse is skipped, never guessed.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .. import config

_TAIL = 256 * 1024  # the newest record sits near the end of a log
_cache: dict = {"at": 0.0, "key": None, "val": None}


def _tail_lines(path: Path) -> list[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _TAIL))
            return f.read().decode("utf-8", "replace").splitlines()[::-1]
    except OSError:
        return []


def _newest(paths, n: int) -> list[Path]:
    out = []
    for p in paths:
        try:
            out.append((p.stat().st_mtime, p))
        except OSError:
            pass
    return [p for _, p in sorted(out, reverse=True)[:n]]


def _window(used_pct, resets_at) -> dict | None:
    try:
        w = {"used_pct": round(float(used_pct), 1)}
    except (TypeError, ValueError):
        return None
    try:
        w["resets_at"] = int(resets_at)
    except (TypeError, ValueError):
        w["resets_at"] = None
    return w


def claude_from_line(line: str) -> dict | None:
    if '"rate_limit_event"' not in line:
        return None
    try:
        info = json.loads(line).get("rate_limit_info") or {}
    except ValueError:
        return None
    wins = info.get("unifiedWindows") or {}
    out = {}
    for key, name in (("five_hour", "5h"), ("seven_day", "week")):
        w = wins.get(key)
        if isinstance(w, dict) and w.get("utilization") is not None:
            out[name] = _window(float(w["utilization"]) * 100, w.get("resetsAt"))
    if not out and info.get("utilization") is not None:  # older shape: only the window that triggered the event
        name = {"five_hour": "5h", "seven_day": "week"}.get(info.get("rateLimitType"))
        if name:
            out[name] = _window(float(info["utilization"]) * 100, info.get("resetsAt"))
    out = {k: v for k, v in out.items() if v}
    if not out:
        return None
    if info.get("status"):
        out["status"] = str(info["status"])
    return out


def codex_from_line(line: str) -> dict | None:
    if '"rate_limits"' not in line:
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    stack, rl = [rec], None
    while stack and rl is None:  # the record nests it under payload/info; find it wherever it sits
        cur = stack.pop()
        if isinstance(cur, dict):
            if isinstance(cur.get("rate_limits"), dict):
                rl = cur["rate_limits"]
            else:
                stack.extend(cur.values())
    if not rl:
        return None
    out = {}
    for key in ("primary", "secondary"):
        w = rl.get(key)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        mins = w.get("window_minutes")
        name = "5h" if mins == 300 else "week" if mins == 10080 else f"{mins}m" if mins else key
        win = _window(w["used_percent"], w.get("resets_at"))
        if win:
            out[name] = win
    return out or None


def _scan(files: list[Path], parse) -> dict | None:
    for f in files:
        for line in _tail_lines(f):
            got = parse(line)
            if got:
                got["seen_at"] = int(f.stat().st_mtime)
                return got
    return None


def _log_dirs(extra: list[str]) -> list[Path]:
    dirs = []
    if config.PROJECTS_DIR.is_dir():
        dirs += [p / "studio" / "logs" for p in config.PROJECTS_DIR.iterdir() if p.is_dir()]
    dirs += [Path(e) / "studio" / "logs" for e in extra if e]
    return [d for d in dict.fromkeys(dirs) if d.is_dir()]


def usage(projects: list[str] | None = None) -> dict:
    """{runner: {"5h": {used_pct, resets_at}, "week": {...}, seen_at} | None} - the last figures each app reported."""
    key = tuple(projects or ())
    if _cache["key"] == key and time.time() - _cache["at"] < 20:
        return _cache["val"]
    logs = [f for d in _log_dirs(list(key)) for f in d.glob("*.jsonl")]
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    rollouts = list((codex_home / "sessions").glob("*/*/*/rollout-*.jsonl"))
    val = {
        "claude": _scan(_newest(logs, 12), claude_from_line),
        "codex": _scan(_newest(rollouts, 6), codex_from_line),
        "agy": None, "kilo": None,
        "now": int(time.time()),
    }
    _cache.update(at=time.time(), key=key, val=val)
    return val
