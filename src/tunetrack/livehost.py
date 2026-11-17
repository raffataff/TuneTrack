"""The editor server's live engine: one `live.Player`, started on the first play, that follows whatever the Desk
shows (the session or one of its takes) and reloads when that project file changes.

It holds the audio interface while it runs. `off()` shuts the engine down and gives the interface back (Cubase
and the engine cannot both have the ASIO driver).

Tracking lives here too: which tracks are armed and monitored (`arm`; kept by this process, not in the project),
the metronome, and `record` / `record_stop`, which writes what was recorded into the project the Desk shows
(tracking.py).
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from . import config, live, tracking

NO_DEVICE = False  # tests: run the engine without opening an audio output
SETTLE_CAP_S = 15.0  # longest the first play waits for instruments to load their sounds
POST_ROLL_S = 0.5    # how long the transport plays on after a punch out (what was played there is still arriving)

_lock = threading.RLock()
_player: live.Player | None = None
_view: Path | None = None
_status: dict = {"loading": False, "error": None, "warnings": []}
_watching = False
_armed: dict[str, dict] = {}  # {track id: {"arm": bool, "monitor": bool}} for the project in view
_inputs: dict = {}            # what the engine last said can be recorded: {"audio": [names], "midi": [names]}
_rec: dict | None = None      # the recording in progress
_done: dict = {"n": 0}        # the last recording that ended by itself (a punch out): {"n", "recorded" | "record_error"}
_click = False
_note: str | None = None      # said once the engine is up: the chosen device was not there


def _pref(key: str, value=...):
    """A setting of this computer kept in the library's config (which device, which input device)."""
    cfg = config.load_config()
    if value is ...:
        return cfg.get(key) or None
    if value:
        cfg[key] = value
    else:
        cfg.pop(key, None)
    config.save_config(cfg)
    return value


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
                tk = p.engine.tick
                if _rec is not None and tk.get("recording") and not tk.get("playing"):
                    # the transport stopped by itself after the punch out
                    out = record_stop()
                    _done.update(n=_done["n"] + 1, recorded=out.get("recorded"), record_error=out.get("record_error"))
                    continue
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
    global _player, _view, _watching, _note
    with _lock:
        if _player is not None and not _player.engine.alive():
            _player = None
        if _player is None:
            _status.update(loading=True, error=None, warnings=[])
            try:
                _player = live.Player(path, no_device=NO_DEVICE, device=None if NO_DEVICE else _pref("live_device"))
                _note = None
                if _player.engine.ready.get("device_problem") and not NO_DEVICE and _pref("live_device"):
                    # the chosen interface is not there today: whatever the engine picks by itself
                    was = _player.engine.ready["device_problem"]
                    try:
                        _player.engine.ready.update(_player.engine.call("device", timeout=60, pick=True),
                                                    device_problem=None)
                        _note = f"the chosen audio device could not be opened ({was.rstrip('; ')}): using another"
                    except live.EngineError:
                        pass  # none opens: the first problem stands
                _view = Path(path)
                _player.engine.call("ticks", hz=20)
                if _click:
                    _player.engine.call("click", on=True)
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
            if _rec is not None:
                raise RuntimeError("Recording: stop it before opening something else")
            _status["loading"] = True
            try:
                _armed.clear()
                _player.tracking = {}
                _player.point(path)  # the same engine: devices the two projects share keep playing
                _view = Path(path)
            finally:
                _status["loading"] = False
        _status["warnings"] = list(_player.loaded.get("warnings", [])) + ([_note] if _note else [])
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
                   recording=_rec is not None, click=_click, armed={k: dict(v) for k, v in _armed.items()},
                   ins=tk.get("ins", []), inputs=_inputs, done=dict(_done),
                   punch=[_rec["punch"], _rec["until"]] if _rec else None,
                   device={k: dev.get(k) for k in ("type", "name", "sr", "block", "rate", "converted")}
                   if dev.get("name") else None)
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
    if _rec is not None:
        return record_stop()
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


# ---- tracking ------------------------------------------------------------------------------------------------

def _open_inputs(p: live.Player) -> None:
    """Have the engine open the interface's inputs (it starts with outputs only) and the MIDI keyboards."""
    global _inputs
    if not NO_DEVICE and not p.engine.ready.get("ins"):
        p.engine.ready.update(p.engine.call("device", timeout=60, inputs=True, input=_pref("live_input") or ""))
    got = p.engine.call("inputs")
    # midi: the keyboards in the engine's order; a track that listens to one names it by its place here + 1
    _inputs = {"audio": list(got.get("audio") or []), "midi": list(got.get("midi") or []),
               "problem": got.get("input_problem") or None}


def _tracking() -> dict:
    """What is armed, as the engine's spec wants it: a keyboard by the engine's number for it (0 = any)."""
    names = _inputs.get("midi") or []
    return {tid: {"arm": v.get("arm"), "monitor": v.get("monitor"),
                  "key": names.index(v["keyboard"]) + 1 if v.get("keyboard") in names else 0}
            for tid, v in _armed.items()}


def devices(path: Path) -> dict:
    """The audio devices this computer has: {"devices": [{"type", "name"}], "inputs": [{"type", "name"}],
    "current": {...}, "chosen": {"device", "input"}} (chosen: what is kept for next time; None = pick for me)."""
    p = _ensure(path)
    with _lock:
        got = p.engine.call("devices", timeout=60)
    return {"devices": got.get("devices") or [], "inputs": got.get("inputs") or [], "current": got.get("current") or {},
            "chosen": {"device": _pref("live_device"), "input": _pref("live_input")}}


def device(path: Path, name: str | None = None, input: str | None = None) -> dict:  # noqa: A002
    """Play through another audio device and/or record from another input device, and keep the choice for next
    time. name: "type: name" as devices() lists it ("" = pick for me); input: an input device's name ("" = the
    device's own, or the system's). None leaves that one as it is."""
    p = _ensure(path)
    with _lock:
        if _rec is not None:
            raise RuntimeError("Recording: stop it before changing the audio device")
        if NO_DEVICE:
            raise RuntimeError("The engine is running without an audio device")
        if name is not None:
            _pref("live_device", str(name).strip())
        if input is not None:
            _pref("live_input", str(input).strip())
        p.stop()
        args = {"inputs": bool(_inputs) or bool(p.engine.ready.get("ins")), "input": _pref("live_input") or ""}
        if name is not None:
            args.update(name=str(name).strip(), pick=not str(name).strip())
        try:
            p.engine.ready.update(p.engine.call("device", timeout=60, **args), device_problem=None)
        except live.EngineError as e:
            p.engine.ready["device_problem"] = str(e)
            raise
        if _inputs:
            _open_inputs(p)
    return state()


def inputs(path: Path) -> dict:
    """What can be recorded: the interface's input channels by name and the MIDI keyboards plugged in."""
    p = _ensure(path)
    with _lock:
        _open_inputs(p)
    return state()


def arm(path: Path, track: str, arm: bool | None = None, monitor: bool | None = None,
        keyboard: str | None = None) -> dict:
    """Arm a track for recording and/or hear its input through its strip. An audio track records its `input`
    (track_update); an armed MIDI track is played by the MIDI keyboard: every one plugged in, or the one named by
    `keyboard` (a name from the state's inputs.midi; "" = any again). The first arm opens the inputs."""
    p = _ensure(path)
    with _lock:
        t = p.project.track(track)
        if t.kind == "bus":
            raise ValueError("A bus has nothing to record")
        if t.kind == "audio" and not t.input and (arm or monitor):
            raise ValueError(f"'{t.name}' has no input yet: choose which interface input it records")
        if _rec is not None and arm is not None:
            raise RuntimeError("Recording: stop it before arming or disarming a track")
        cur = dict(_armed.get(t.id, {"arm": False, "monitor": False}))
        if arm is not None:
            cur["arm"] = bool(arm)
        if monitor is not None:
            cur["monitor"] = bool(monitor)
        if keyboard is not None and t.kind == "midi":
            cur.pop("keyboard", None)
            if keyboard:
                cur["keyboard"] = str(keyboard)
        if monitor and t.kind == "audio" and p.engine.ready.get("converted"):
            raise RuntimeError("The audio device is not running at the project's sample rate, so the engine cannot "
                               "play its input back in time: use the interface's own direct monitoring")
        if cur["arm"] or cur["monitor"]:
            _armed[t.id] = cur
            if not _inputs:
                _open_inputs(p)
        else:
            _armed.pop(t.id, None)
        p.track(_tracking())
    return state()


def click(on: bool) -> dict:
    """The metronome: live playback only, never in a file."""
    global _click
    with _lock:
        _click = bool(on)
        if _player is not None and _player.engine.alive():
            _player.engine.call("click", on=_click)
    return state()


def keys(msgs: list) -> dict:
    """Play the armed MIDI track as a keyboard would: [[status, data1, data2], ...] (the Desk's on-screen keys)."""
    with _lock:
        if _player is not None and _player.engine.alive():
            _player.engine.call("midi", msgs=[[int(x) & 0xFF for x in m[:3]] for m in msgs][:64])
    return {}


def record(path: Path, beat: float = 0.0, count_in: float = 1.0, loop: bool | None = None,
           until: float | None = None, quantise: float = 0.0) -> dict:
    """Record every armed track from a beat (the punch in), after `count_in` bars of click (0 = none); from
    anywhere but the song's start those bars play the music too, so they are the pre-roll. The transport plays;
    with the loop on, each time round is another pass. Stop with record_stop() (or stop()).
    until: a punch out, in beats: one pass from `beat` to there, then the transport stops by itself a moment later
    and the state's `done` says what was recorded (the loop is off for it). While recording, what an armed audio
    track already holds is silent between the punch points. quantise: move each recorded note's start to the
    nearest multiple of this many beats (0 = as played)."""
    global _rec
    p = _ensure(path)
    with _lock:
        if _rec is not None:
            raise RuntimeError("Already recording")
        p.sync()
        proj = p.project
        armed = [proj.track(tid) for tid, v in _armed.items() if v.get("arm") and any(t.id == tid for t in proj.tracks)]
        if not armed:
            raise RuntimeError("No track is armed: arm the track to record on first")
        stamp = time.strftime("%Y%m%d-%H%M%S")
        files, audio = {}, {}
        for t in armed:
            if t.kind != "audio":
                continue
            rel = f"audio/rec/{stamp}-{t.id}.wav"
            files[t.id] = rel
            audio[t.id] = {"file": str(proj.root / rel), "ch": [int(c) - 1 for c in (t.input or [1])[:2]]}
        beat = max(0.0, float(beat))
        tm, sr = proj.tempo_map(), proj.sample_rate
        punch = p.sample(beat)
        lead = int(round(max(0.0, float(count_in)) * proj.beats_per_bar * 60.0 / tm.bpm_at(beat) * sr))
        args = {"audio": audio, "from": punch - lead, "punch": [punch]}
        if lead:
            args["count_until"] = punch
        if until is not None:
            until = float(until)
            if until <= beat:
                raise ValueError("The punch out must come after the punch in")
            loop = False
            args["punch"].append(p.sample(until))
            args["stop_at"] = p.sample(until) + int(POST_ROLL_S * sr)
        if loop is not None:
            args["loop"] = bool(loop)
        p.engine.call("record", **args)
        keys = _tracking()
        _rec = {"path": Path(path), "punch": beat, "until": until, "quantise": max(0.0, float(quantise or 0)),
                "files": files, "keys": {t.id: keys[t.id]["key"] for t in armed if t.kind == "midi"}}
        _done.pop("recorded", None)
        _done.pop("record_error", None)
        p.engine.tick = {**p.engine.tick, "playing": True}
    return state()


def record_stop() -> dict:
    """Stop recording and put what was recorded on the armed tracks: one clip per pass, the last full one heard.
    The state carries `recorded` ({"clips", "heard"}) or `record_error`."""
    global _rec
    with _lock:
        rec, _rec = _rec, None
        if rec is None or _player is None or not _player.engine.alive():
            return state()
        res = _player.engine.call("record_stop", timeout=60)
        _player.engine.tick = {**_player.engine.tick, "playing": False}
        out = state()
        try:
            out["recorded"] = tracking.commit(rec["path"], res, rec["files"], rec["keys"], rec["punch"],
                                              rec["until"], rec["quantise"])
            if out["recorded"].get("dropped"):
                _status["warnings"] = [*_status["warnings"], "the disk fell behind while recording: "
                                       f"{out['recorded']['dropped']} blocks are missing from the files"]
        except tracking.NothingRecorded as e:
            out["record_error"] = str(e)
            for rel in rec["files"].values():  # nothing kept: the files go too
                try:
                    (_player.project.root / rel).unlink()
                except OSError:
                    pass
        try:
            _player.sync()
        except (OSError, ValueError, KeyError, live.EngineError):
            pass  # the watcher tries again
    return out


def off() -> dict:
    """Stop the engine and give the audio interface back."""
    global _player, _view, _rec, _inputs
    if _rec is not None:
        try:
            record_stop()
        except Exception:  # noqa: BLE001 - the engine is going away either way
            _rec = None
    with _lock:
        if _player is not None:
            _player.close()
        _player, _view = None, None
        _armed.clear()
        _inputs = {}
        _status.update(loading=False, error=None, warnings=[])
    return state()
