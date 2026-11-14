"""The editor server's live engine: one `live.Player`, started on the first play, that follows whatever the Desk
shows (the session or one of its takes) and reloads when that project file changes.

It holds the audio interface while it runs. `off()` shuts the engine down and gives the interface back (Cubase
and the engine cannot both have the ASIO driver).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from . import live

NO_DEVICE = False  # tests: run the engine without opening an audio output
SETTLE_CAP_S = 15.0  # longest the first play waits for instruments to load their sounds

_lock = threading.RLock()
_player: live.Player | None = None
_view: Path | None = None
_status: dict = {"loading": False, "error": None, "warnings": []}
_watching = False


def available() -> bool:
    return live.engine_exe() is not None


def _watch() -> None:
    """Keep the engine in step with the project file it plays, for as long as the engine lives."""
    global _watching
    try:
        while True:
            time.sleep(0.3)
            with _lock:
                p = _player
                if p is None or not p.engine.alive():
                    return
                try:
                    res = p.sync()
                    if res is not None:
                        _status.update(warnings=list(res.get("warnings", [])), error=None)
                except (OSError, ValueError, KeyError):
                    pass  # the file is mid-write or briefly unreadable: look again
                except live.EngineError as e:
                    _status["error"] = str(e)
    finally:
        _watching = False


def _ensure(path: Path) -> live.Player:
    """The running player, pointed at `path`. Starting the engine and loading a big session takes seconds."""
    global _player, _view, _watching
    with _lock:
        if _player is not None and not _player.engine.alive():
            _player = None
        if _player is None:
            _status.update(loading=True, error=None, warnings=[])
            try:
                _player = live.Player(path, no_device=NO_DEVICE)
                _view = Path(path)
                _player.engine.call("ticks", hz=20)
                # a fresh engine: instruments are still loading their sounds, and would come in late
                time.sleep(min(SETTLE_CAP_S, _player.loaded.get("settle_ms", 0) / 1000))
            except Exception as e:
                _status["error"] = str(e)
                if _player is not None:
                    _player.close()
                _player = None
                raise
            finally:
                _status["loading"] = False
            if not _watching:
                _watching = True
                threading.Thread(target=_watch, daemon=True).start()
        elif Path(path) != _view:
            _status["loading"] = True
            try:
                _player.point(path)  # the same engine: devices the two projects share keep playing
                _view = Path(path)
            finally:
                _status["loading"] = False
        _status["warnings"] = list(_player.loaded.get("warnings", []))
        if _player.loaded.get("device_problem"):
            _status["warnings"].append(_player.loaded["device_problem"])
        return _player


def state() -> dict:
    """What the transport shows. Cheap: read from the engine's last tick, no round trip."""
    p = _player
    out = {"available": available(), "on": bool(p and p.engine.alive()), **_status,
           "view": str(_view) if _view and p else None, "playing": False}
    if out["on"]:
        tk, dev = p.engine.tick, p.engine.ready
        out.update(playing=bool(tk.get("playing")), beat=tk.get("beat", 0.0), looping=bool(tk.get("looping")),
                   peaks=tk.get("peaks", {}), cpu=tk.get("cpu", 0.0), dropouts=tk.get("xruns", 0), rev=p.rev,
                   device={k: dev.get(k) for k in ("type", "name", "sr", "block")} if dev.get("name") else None)
        if dev.get("device_problem") and not out["error"]:
            out["error"] = dev["device_problem"]
    return out


def play(path: Path, beat: float = 0.0, loop: bool | None = None) -> dict:
    p = _ensure(path)
    with _lock:
        p.sync()
        p.play(max(0.0, float(beat)), loop)
        p.engine.tick = {**p.engine.tick, "playing": True}  # the answer should not wait for the next tick
    return state()


def stop() -> dict:
    with _lock:
        if _player is not None and _player.engine.alive():
            _player.stop()
            _player.engine.tick = {**_player.engine.tick, "playing": False}
    return state()


def seek(beat: float) -> dict:
    with _lock:
        if _player is not None and _player.engine.alive():
            _player.engine.call("seek", to=_player.sample(max(0.0, float(beat))))
    return state()


def loop(on: bool) -> dict:
    with _lock:
        if _player is not None and _player.engine.alive():
            _player.engine.call("loop", on=bool(on))
    return state()


def window(path: Path, track: str, slot: str | int = "inst") -> dict:
    """Open a hosted plugin's own window. track: a track id or name, or "master"; slot: "inst" or the effect's
    place in the track's list (0 = first). Built-in devices have no window."""
    p = _ensure(path)
    proj = p.project
    if str(track).lower() == "master":
        tid, chain = "master", [d for d in proj.master.get("fx", []) if not isinstance(d, dict)]
    else:
        t = next((t for t in proj.tracks if t.id == track or t.name.lower() == str(track).lower()), None)
        if t is None:
            raise KeyError(f"No track {track!r}")
        tid, chain = t.id, t.fx
    if str(slot) == "inst":
        ref = f"{tid}/inst"
    else:
        i = int(slot)
        if not 0 <= i < len(chain):
            raise KeyError(f"No effect {i} on {track}")
        if chain[i].bypass:
            raise RuntimeError("A bypassed effect is not loaded in the engine: enable it first")
        ref = f"{tid}/fx/{sum(1 for d in chain[:i] if not d.bypass)}"  # the engine does not load bypassed ones
    with _lock:
        p.engine.call("editor", ref=ref)
    return {"opened": ref}


def off() -> dict:
    """Stop the engine and give the audio interface back."""
    global _player, _view
    with _lock:
        if _player is not None:
            _player.close()
        _player, _view = None, None
        _status.update(loading=False, error=None, warnings=[])
    return state()
