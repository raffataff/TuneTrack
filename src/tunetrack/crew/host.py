"""The crew, hosted in a long-lived process (the editor's server): start it, pause it, see what it is doing.

`tunetrack crew run` manages a session until nothing more can happen and exits. The editor needs the opposite: a
floor manager that stays up, so answering a Needs You item or adding a task is picked up without restarting
anything. One floor manager per session; floor.py refuses a second one, here or in another process.

    crew_start(brief="A calm menu loop ...")   # kick off (optional) and let the seated agents work
    crew_state()                               # running? who is active? usage so far
    crew_stop()                                # stop the agents; their tasks go back in the queue
"""
from __future__ import annotations

import threading

from .. import roles, studio
from . import floor

_hosts: dict[str, dict] = {}  # session folder -> {"floor", "thread", "error"}
_lock = threading.RLock()


def _key(session: str | None) -> str:
    return str(studio.session_path(session))


def _user() -> None:
    if studio.me() != roles.YOU:
        raise PermissionError("The crew is started and stopped by the user")


def crew_start(brief: str | None = None, parallel: int = 3, max_runs: int = 40, session: str | None = None) -> dict:
    """Let the seated agents work on this session, and keep managing it until crew_stop().

    brief: what you want made - the Producer gets it as its first task. Omit it to carry on with the tasks already
    on the board. parallel: agents at once. max_runs: agent runs before the crew stops and asks you (a guard on
    usage). Each run costs whatever the seat's model costs."""
    _user()
    key = _key(session)
    with _lock:
        h = _hosts.get(key)
        if h and h["thread"].is_alive():
            if brief and str(brief).strip():
                floor.kickoff(brief, key)
            return crew_state(key)
        if floor.managed(key):
            raise floor.FloorBusy("This session's crew is already being run by another process "
                                  "(`tunetrack crew run`, or another editor). Stop that one first.")
        if brief and str(brief).strip():
            floor.kickoff(brief, key)
        f = floor.Floor(key, max_parallel=int(parallel), max_launches=int(max_runs))
        h = {"floor": f, "error": None}

        def work() -> None:
            try:
                f.run(keep_alive=True)
            except Exception as e:  # noqa: BLE001 - shown in crew_state instead of dying silently in a thread
                h["error"] = f"{type(e).__name__}: {e}"
            finally:
                try:
                    studio.log("crew", key, what="stop", error=h["error"])
                except Exception:  # noqa: BLE001 - the session folder may be gone
                    pass

        h["thread"] = threading.Thread(target=work, daemon=True, name=f"crew-{studio.session_path(key).name}")
        _hosts[key] = h
        studio.log("crew", key, what="start")
        h["thread"].start()
    return crew_state(key)


def crew_stop(session: str | None = None) -> dict:
    """Stop the crew: running agents are stopped, their tasks go back in the queue and their takes keep what they
    had done. Start again with crew_start()."""
    _user()
    key = _key(session)
    with _lock:
        h = _hosts.get(key)
    if h and h["thread"].is_alive():
        h["floor"].stop()
        h["thread"].join(timeout=15)
    return crew_state(key)


def seat_hold(seat: str, paused: bool = True, session: str | None = None) -> dict:
    """Pause one seat (and stop what its agent is doing right now), or resume it with paused=False."""
    _user()
    key = _key(session)
    out = studio.seat_pause(seat, paused, key)
    with _lock:
        h = _hosts.get(key)
    if paused and h and h["thread"].is_alive():
        out["stopped_run"] = h["floor"].stop_seat(out["seat"])
    return out


def crew_state(session: str | None = None) -> dict:
    """Whether this session's crew is running, which seats have an agent at work, and usage so far."""
    key = _key(session)
    with _lock:
        h = _hosts.get(key)
    here = bool(h and h["thread"].is_alive())
    out = {"running": here, "active": {}, "launches": 0, "usage": None}
    if h:
        f = h["floor"]
        out.update(active=f.active() if here else {}, launches=f.launches, max_runs=f.max_launches,
                   usage={**f.usage, "cost_usd": round(f.usage["cost_usd"], 4)})
        if h["error"]:
            out["error"] = h["error"]
    if not here and floor.managed(key):
        out["elsewhere"] = True  # `tunetrack crew run` in a terminal, or another editor
    return out


def stop_all() -> None:
    """Stop every hosted crew (the server is shutting down)."""
    with _lock:
        hosts = list(_hosts.values())
    for h in hosts:
        if h["thread"].is_alive():
            h["floor"].stop()
    for h in hosts:
        h["thread"].join(timeout=15)
