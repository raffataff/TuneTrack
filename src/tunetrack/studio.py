"""The studio board: who sits where, what each seat is working on, what they've told each other, and what is
waiting on the user (Needs You).

A session is a project folder. Its board lives in <session>/studio/:
    state.json    seats, tasks, messages, attention items, counters
    events.jsonl  append-only activity log (the editor and the scheduler follow it)
Like project.json, every change is load -> modify -> save under a cross-process lock, so agents in separate
processes, the editor and the CLI can all use the board at once.

The board carries coordination only. Music lives in project.json and in takes (takes.py).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import api, roles
from .model import _atomic_write, file_lock, session_root

SCHEMA = 1
TASK_STATES = ("queued", "working", "submitted", "needs_you", "done", "failed", "cancelled")
OPEN = {"queued", "working", "submitted", "needs_you"}
ATTENTION_KINDS = ("decision", "input", "blocker")
MODES = ("propose", "direct")  # propose: work in a take and submit it; direct: edit the session itself

# Which seat this process speaks for. A launched agent gets it from its environment; an outside app can take a
# seat with seat_join(); anything else acts for the user.
_seat: str = (os.environ.get("TUNETRACK_SEAT") or roles.YOU).strip().lower()


def me() -> str:
    return _seat


def seat_join(seat: str | None) -> dict:
    """Make this process speak for a seat ("composer"), or for the user again (None / "you")."""
    global _seat
    _seat = roles.YOU if not seat or str(seat).lower() == roles.YOU else roles.get(seat).id
    return {"you_are": _seat}


def session_path(session: str | None = None) -> Path:
    """The session folder: given, or from TUNETRACK_SESSION, or the one the current project (or take) belongs to."""
    if session:
        return session_root(api._path(session))
    env = os.environ.get("TUNETRACK_SESSION")
    if env:
        return Path(env)
    return session_root(api._path(None))


def _fresh() -> dict:
    return {"studio": SCHEMA, "rev": 0, "seq": 0, "next": {"T": 1, "M": 1, "N": 1, "tk": 1},
            "seats": {}, "tasks": [], "messages": [], "attention": []}


def _seat_default(seat: str = "") -> dict:
    # The Producer runs the session and accepts takes, so it works on the session itself; everyone else proposes.
    return {"occupant": None, "mode": "direct" if seat == "producer" else "propose", "notes": "", "presence": None,
            "paused": False}


def _normalise(st: dict) -> dict:
    base = _fresh()
    for k, v in base.items():
        st.setdefault(k, v)
    for k, v in base["next"].items():
        st["next"].setdefault(k, v)
    for sid in roles.ORDER:
        seat = st["seats"].setdefault(sid, _seat_default(sid))
        for k, v in _seat_default(sid).items():
            seat.setdefault(k, v)
    return st


def read(session: str | None = None) -> dict:
    """The board as stored (a fresh one if this session has none yet). Read-only: changes go through the functions."""
    f = session_path(session) / "studio" / "state.json"
    try:
        return _normalise(json.loads(f.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return _normalise(_fresh())


class _Board:
    """with _Board(session) as b: b.st[...] = ...; b.emit("task", ...)  -> locked load, save, events appended."""

    def __init__(self, session: str | None):
        self.path = session_path(session)
        if not (self.path / "project.json").exists():
            raise FileNotFoundError(f"No TuneTrack project at {self.path}")
        self.dir = self.path / "studio"
        self._lock = file_lock(self.dir / ".lock")
        self._events: list[dict] = []

    def __enter__(self) -> "_Board":
        self._lock.__enter__()
        try:
            f = self.dir / "state.json"
            self.st = _normalise(json.loads(f.read_text(encoding="utf-8")) if f.exists() else _fresh())
        except BaseException:
            self._lock.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, *_):
        try:
            if exc_type is None:
                self.st["rev"] += 1
                _atomic_write(self.dir / "state.json", json.dumps(self.st, indent=1))
                if self._events:
                    with open(self.dir / "events.jsonl", "a", encoding="utf-8") as fh:
                        fh.writelines(json.dumps(e) + "\n" for e in self._events)
        finally:
            self._lock.__exit__(None, None, None)
        return False

    def new_id(self, prefix: str) -> str:
        n = self.st["next"][prefix]
        self.st["next"][prefix] = n + 1
        return f"{prefix}{n}"

    def emit(self, kind: str, **data) -> None:
        self.st["seq"] += 1
        self._events.append({"seq": self.st["seq"], "at": round(time.time(), 3), "kind": kind,
                             **{k: v for k, v in data.items() if v is not None}})

    def task(self, ref: str) -> dict:
        for t in self.st["tasks"]:
            if t["id"].lower() == str(ref).strip().lower():
                return t
        raise KeyError(f"No task {ref!r}")

    def item(self, ref: str) -> dict:
        for a in self.st["attention"]:
            if a["id"].lower() == str(ref).strip().lower():
                return a
        raise KeyError(f"No Needs You item {ref!r}")

    def close_auditions(self, take: str, outcome: str) -> None:
        """The take was accepted, returned or discarded: any request to audition it is answered."""
        for a in self.st["attention"]:
            if a["state"] == "open" and a.get("take") == take:
                a["state"], a["resolved"] = "resolved", round(time.time(), 3)
                a["answer"] = {"option": None, "text": f"take {outcome}"}
                self.emit("attention", id=a["id"], state="resolved", title=a["title"], answer=f"take {outcome}")

    def message(self, sender: str, to: str, text: str, task: str | None = None) -> dict:
        m = {"id": self.new_id("M"), "from": sender, "to": to, "text": text, "task": task,
             "at": round(time.time(), 3), "read": []}
        self.st["messages"].append(m)
        self.emit("message", id=m["id"], sender=sender, to=to, task=task, text=text[:240])
        return m


def _who(name: str | None, allow_all: bool = False) -> str:
    n = (name or "").strip().lower()
    if n in (roles.YOU, "user", "human", "me"):
        return roles.YOU
    if allow_all and n in (roles.EVERYONE, "everyone", "crew"):
        return roles.EVERYONE
    return roles.get(n).id


def _lead(who: str) -> bool:
    return who in (roles.YOU, "producer")


# ---- seats ---------------------------------------------------------------------------------------------------------

def occupant_label(occ: dict | None) -> str | None:
    if not occ:
        return None
    if occ.get("label"):
        return occ["label"]
    if occ["kind"] == "human":
        return "you"
    return " · ".join(x for x in (occ.get("runner"), occ.get("model"), occ.get("effort")) if x) or occ["kind"]


def seat_assign(seat: str, kind: str | None = None, runner: str | None = None, model: str | None = None,
                effort: str | None = None, label: str | None = None, mode: str | None = None,
                notes: str | None = None, session: str | None = None) -> dict:
    """Put someone in a seat, or change how the seat works.

    kind: "agent" (the studio launches runner/model for it), "human" (you take it), "external" (an outside app such
    as Claude Desktop sits in), "none" (leave it empty). Omit kind to keep the occupant and change only mode/notes.
    mode: "propose" (work in a take, submit for acceptance) or "direct" (edit the session itself).
    notes: standing instructions for whoever sits here ("no brass", "keep it under 8 voices")."""
    sid = roles.get(seat).id
    if not _lead(me()):
        raise PermissionError("Only the Producer or the user assigns seats")
    with _Board(session) as b:
        s = b.st["seats"][sid]
        if kind is not None:
            k = str(kind).strip().lower()
            if k in ("none", "unassigned", "empty", ""):
                s["occupant"], s["presence"] = None, None
            elif k == "agent":
                if not runner:
                    raise ValueError("An agent needs a runner (claude, codex, kilo) and usually a model")
                s["occupant"] = {"kind": "agent", "runner": runner, "model": model, "effort": effort, "label": label}
            elif k in ("human", "external"):
                s["occupant"] = {"kind": k, "label": label}
            else:
                raise ValueError("kind must be agent, human, external or none")
        elif any(v is not None for v in (runner, model, effort, label)):
            if not s["occupant"]:
                raise ValueError(f"{sid} is empty: pass kind=")
            s["occupant"].update({k: v for k, v in (("runner", runner), ("model", model), ("effort", effort),
                                                    ("label", label)) if v is not None})
        if mode is not None:
            if mode not in MODES:
                raise ValueError(f"mode must be one of {MODES}")
            s["mode"] = mode
        if notes is not None:
            s["notes"] = notes
        b.emit("seat", seat=sid, occupant=occupant_label(s["occupant"]) or "empty", mode=s["mode"])
        out = {"seat": sid, "occupant": occupant_label(s["occupant"]), "mode": s["mode"]}
        waiting = [t["id"] for t in b.st["tasks"] if t["seat"] == sid and t["state"] in OPEN]
        if waiting and not s["occupant"]:
            out["warning"] = f"{sid} is empty but has open tasks: {', '.join(waiting)}"
    return out


def seat_pause(seat: str, paused: bool = True, session: str | None = None) -> dict:
    """Hold a seat: its agent is not started on anything new until it is resumed. Its tasks stay in the queue."""
    sid = roles.get(seat).id
    if not _lead(me()):
        raise PermissionError("Only the Producer or the user pauses a seat")
    with _Board(session) as b:
        s = b.st["seats"][sid]
        if bool(s.get("paused")) != bool(paused):
            s["paused"] = bool(paused)
            b.emit("seat", seat=sid, occupant=occupant_label(s["occupant"]) or "empty", mode=s["mode"],
                   paused=bool(paused))
    return {"seat": sid, "paused": bool(paused)}


def seat_presence(seat: str, state: str | None, session: str | None = None, **info) -> dict:
    """Connection state of a seat's occupant, set by whatever runs it: "online", "offline", or None to clear.
    Separate from what the seat is working on - an offline seat still owns its tasks."""
    sid = roles.get(seat).id
    with _Board(session) as b:
        s = b.st["seats"][sid]
        old = (s["presence"] or {}).get("state")
        s["presence"] = {"state": state, "at": round(time.time(), 3), **info} if state else None
        if old != state:
            b.emit("presence", seat=sid, state=state or "cleared", **{k: v for k, v in info.items() if k == "reason"})
    return {"seat": sid, "presence": state}


# ---- tasks ---------------------------------------------------------------------------------------------------------

def _ready(st: dict, t: dict) -> bool:
    done = {x["id"] for x in st["tasks"] if x["state"] == "done"}
    return t["state"] == "queued" and all(d in done for d in t.get("depends", []))


def _task_view(st: dict, t: dict, detail: bool = False) -> dict:
    d = {"id": t["id"], "seat": t["seat"], "title": t["title"], "state": t["state"]}
    if t["state"] == "queued":
        waiting = [x for x in t.get("depends", []) if x not in {y["id"] for y in st["tasks"] if y["state"] == "done"}]
        d["ready"] = not waiting
        if waiting:
            d["waiting_for"] = waiting
    for k in ("scope", "take", "result"):
        if t.get(k):
            d[k] = t[k]
    if detail:
        d.update(detail=t.get("detail", ""), by=t.get("by"), depends=t.get("depends", []))
    return d


def task_add(seat: str, title: str, detail: str = "", depends: list | None = None, scope: dict | None = None,
             session: str | None = None) -> dict:
    """Give a seat a task. depends: task ids that must be done first. scope: {"tracks": [...], "bars": "9-16"}.

    Write the detail so the seat can work from it alone: what to make, for which bars/tracks, what it must fit
    with, and what done looks like."""
    sid = roles.get(seat).id
    if not str(title).strip():
        raise ValueError("A task needs a title")
    with _Board(session) as b:
        deps = [b.task(d)["id"] for d in depends or []]
        now = round(time.time(), 3)
        t = {"id": b.new_id("T"), "seat": sid, "title": str(title).strip(), "detail": detail or "",
             "state": "queued", "depends": deps, "scope": scope or {}, "by": me(), "take": None, "result": "",
             "created": now, "updated": now, "attempts": 0}
        b.st["tasks"].append(t)
        b.emit("task", id=t["id"], seat=sid, state="queued", title=t["title"], by=me())
        out = _task_view(b.st, t)
        if not b.st["seats"][sid]["occupant"]:
            out["warning"] = f"{sid} is empty: nobody will pick this up until the seat is assigned"
    return out


def task_update(task: str, state: str | None = None, result: str | None = None, take: str | None = None,
                title: str | None = None, detail: str | None = None, depends: list | None = None,
                session: str | None = None) -> dict:
    """Move a task along. States: queued, working, submitted (a take is waiting for acceptance), needs_you, done,
    failed, cancelled. result: one or two sentences on what was done or why it failed - the next seat reads it."""
    with _Board(session) as b:
        t = b.task(task)
        who = me()
        if who != t["seat"] and not _lead(who):
            raise PermissionError(f"{t['id']} belongs to {t['seat']}; only that seat, the Producer or the user changes it")
        if state is not None:
            if state not in TASK_STATES:
                raise ValueError(f"state must be one of {TASK_STATES}")
            if state == "working" and t["state"] != "working":
                t["attempts"] = t.get("attempts", 0) + 1
            t["state"] = state
        for k, v in (("result", result), ("take", take), ("title", title), ("detail", detail)):
            if v is not None:
                t[k] = v
        if depends is not None:
            t["depends"] = [b.task(d)["id"] for d in depends if b.task(d)["id"] != t["id"]]
        t["updated"] = round(time.time(), 3)
        b.emit("task", id=t["id"], seat=t["seat"], state=t["state"], title=t["title"], by=who,
               result=(t["result"] or "")[:240] or None, take=t.get("take"))
        out = _task_view(b.st, t)
        if t["state"] == "done":
            freed = [x["id"] for x in b.st["tasks"] if t["id"] in x.get("depends", []) and _ready(b.st, x)]
            if freed:
                out["now_ready"] = freed
    return out


def task_list(seat: str | None = None, state: str | None = None, session: str | None = None) -> dict:
    """Tasks on the board. state: one of the task states, "open" (default: everything unfinished) or "all"."""
    st = read(session)
    sid = roles.get(seat).id if seat else None
    want = (state or "open").lower()
    out = [_task_view(st, t, detail=True) for t in st["tasks"]
           if (not sid or t["seat"] == sid)
           and (want == "all" or (want == "open" and t["state"] in OPEN) or t["state"] == want)]
    return {"tasks": out}


def ready_tasks(session: str | None = None) -> list[dict]:
    """Queued tasks whose dependencies are done (what a scheduler should start next), oldest first."""
    st = read(session)
    return [t for t in st["tasks"] if _ready(st, t)]


# ---- messages (talkback) -------------------------------------------------------------------------------------------

def message_send(to: str, text: str, task: str | None = None, session: str | None = None) -> dict:
    """Say something to a seat, to "all", or to "you" (the user). task: the task it is about, so the reply has
    context. Messages are for information and requests; work goes on the board with task_add."""
    target = _who(to, allow_all=True)
    if not str(text).strip():
        raise ValueError("Empty message")
    with _Board(session) as b:
        m = b.message(me(), target, str(text).strip(), b.task(task)["id"] if task else None)
    return {"sent": m["id"], "to": target}


def inbox(seat: str | None = None, mark_read: bool = True, session: str | None = None) -> dict:
    """What is waiting for a seat (default: yours): unread messages, including answers to its questions, and its
    open tasks. Call it when you start and whenever you finish a step."""
    who = _who(seat) if seat else me()
    path = session_path(session)
    if not (path / "studio" / "state.json").exists():
        return {"you_are": who, "messages": [], "tasks": []}

    def view(st: dict) -> dict:
        msgs = [m for m in st["messages"] if m["from"] != who and who not in m["read"]
                and (m["to"] == who or (m["to"] == roles.EVERYONE and who != roles.YOU))]
        return {"you_are": who,
                "messages": [{"id": m["id"], "from": m["from"], "text": m["text"],
                              **({"task": m["task"]} if m.get("task") else {})} for m in msgs],
                "tasks": [_task_view(st, t, detail=True) for t in st["tasks"] if t["seat"] == who and t["state"] in OPEN],
                "_ids": {m["id"] for m in msgs}}

    if not mark_read:
        out = view(read(session))
        out.pop("_ids")
        return out
    with _Board(session) as b:
        out = view(b.st)
        ids = out.pop("_ids")
        for m in b.st["messages"]:
            if m["id"] in ids:
                m["read"].append(who)
    return out


# ---- Needs You -----------------------------------------------------------------------------------------------------

def ask_human(title: str, detail: str = "", kind: str = "decision", options: list | None = None,
              task: str | None = None, blocks: list | None = None, session: str | None = None) -> dict:
    """Put something on the user's Needs You list and keep working on whatever doesn't depend on it.

    kind: "decision" (choose between options), "input" (they must provide or pick something, e.g. a sound in a
    plugin window, a reference track), "blocker" (you cannot continue: a failed instrument, a missing file).
    options: labels, or {"label": ..., "take": "tk3", "audio": "<file>", "detail": ...} so they can audition each.
    task: the task this is about (it moves to needs_you). blocks: other tasks that must wait for the answer.
    Use it for real choices and blockers only - not for progress reports (message_send) or routine fixes."""
    if kind not in ATTENTION_KINDS:
        raise ValueError(f"kind must be one of {ATTENTION_KINDS}")
    if not str(title).strip():
        raise ValueError("Say what you need in the title")
    opts = []
    for i, o in enumerate(options or []):
        o = {"label": o} if isinstance(o, str) else dict(o)
        if not o.get("label"):
            raise ValueError("Every option needs a label")
        opts.append({"id": chr(ord("a") + i), **{k: o[k] for k in ("label", "detail", "take", "audio") if o.get(k)}})
    with _Board(session) as b:
        tid = b.task(task)["id"] if task else None
        blocked = [b.task(x)["id"] for x in blocks or []]
        a = {"id": b.new_id("N"), "kind": kind, "from": me(), "task": tid, "title": str(title).strip(),
             "detail": detail or "", "options": opts, "blocks": blocked, "state": "open", "answer": None,
             "created": round(time.time(), 3), "resolved": None}
        b.st["attention"].append(a)
        if tid:
            t = b.task(tid)
            if t["state"] in ("queued", "working"):
                t["state"], t["updated"] = "needs_you", a["created"]
                b.emit("task", id=tid, seat=t["seat"], state="needs_you", title=t["title"], by=me())
        b.emit("attention", id=a["id"], state="open", what=kind, sender=me(), title=a["title"], task=tid)
    return {"needs_you": a["id"], "options": [o["id"] for o in opts],
            "note": "The answer arrives in your inbox. Carry on with anything that doesn't depend on it."}


def attention_list(state: str = "open", session: str | None = None) -> dict:
    """The Needs You list. state: open | resolved | all."""
    st = read(session)
    items = [a for a in st["attention"] if state == "all" or a["state"] == state]
    return {"needs_you": items, "open": sum(1 for a in st["attention"] if a["state"] == "open")}


def attention_resolve(item: str, option: str | None = None, text: str = "", session: str | None = None) -> dict:
    """Answer a Needs You item: option = the chosen option's id or label, text = anything you want to add.
    The answer goes to the seat that asked, and the tasks that were waiting on it go back in the queue."""
    if me() != roles.YOU:
        raise PermissionError("Needs You items are answered by the user")
    pending = next((a for a in read(session)["attention"] if a["id"].lower() == str(item).strip().lower()), None)
    carried = False
    if pending and pending["state"] == "open" and pending.get("take") and option is not None:
        # an audition request: the answer is the decision, so carry it out (before the board is locked below)
        from . import takes
        choice = str(option).strip().lower()
        if choice in ("a", "accept"):
            res = takes.take_accept(pending["take"], session=session)
            if not res["accepted"]:
                raise RuntimeError(f"Take {pending['take']} conflicts with the session and was not accepted: "
                                   + "; ".join(f"{c['where']} ({c['why']})" for c in res["conflicts"])
                                   + ". Accept it with take_accept(on_conflict=...) or return it.")
            carried = True
        elif choice in ("b", "return"):
            if not str(text).strip():
                raise ValueError("Say what has to change so the seat can act on it")
            takes.take_reject(pending["take"], text, session=session)
            carried = True
    with _Board(session) as b:
        a = b.item(item)
        closed = a["state"] != "open"  # carrying out the decision above already closed the audition request
        if closed and not carried:
            raise ValueError(f"{a['id']} is already resolved")
        chosen = None
        if option is not None:
            o = str(option).strip().lower()
            chosen = next((x for x in a["options"] if x["id"] == o or x["label"].lower() == o), None)
            if not chosen:
                raise KeyError(f"No option {option!r} on {a['id']}. Options: "
                               + ", ".join(f"{x['id']}: {x['label']}" for x in a["options"]))
        if not chosen and not str(text).strip():
            raise ValueError("Choose an option or write an answer")
        a["state"], a["resolved"] = "resolved", round(time.time(), 3)
        a["answer"] = {"option": chosen["id"] if chosen else None, "text": str(text).strip()}
        said = "; ".join(x for x in ((f"chose {chosen['label']}" + (f" (take {chosen['take']})" if chosen.get("take") else ""))
                                     if chosen else "", str(text).strip()) if x)
        requeued = []
        for tid in dict.fromkeys(([a["task"]] if a["task"] else []) + a["blocks"]):
            t = b.task(tid)
            still = any(x["state"] == "open" and (x["task"] == tid or tid in x["blocks"]) for x in b.st["attention"])
            if t["state"] == "needs_you" and not still:
                t["state"], t["updated"] = "queued", a["resolved"]
                requeued.append(tid)
                b.emit("task", id=tid, seat=t["seat"], state="queued", title=t["title"], by=roles.YOU)
        if a["from"] != roles.YOU and not carried:  # a returned take already carries its reason to the seat
            b.message(roles.YOU, a["from"], f"Answer to {a['id']} \"{a['title']}\": {said}", a["task"])
        if not closed:
            b.emit("attention", id=a["id"], state="resolved", title=a["title"], answer=said[:240])
    return {"resolved": a["id"], "answer": a["answer"], "requeued": requeued}


# ---- overview ------------------------------------------------------------------------------------------------------

def take_index(path: Path) -> list[dict]:
    """Every take's take.json in a session, oldest first (takes.py creates and changes them)."""
    out = []
    for f in (Path(path) / "takes").glob("*/take.json"):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda t: t.get("created", 0))


def studio_status(session: str | None = None) -> dict:
    """The board at a glance: seats and who is in them, open tasks, takes waiting, and what needs the user."""
    path = session_path(session)
    st = read(session)
    seats = []
    for sid in roles.ORDER:
        s, r = st["seats"][sid], roles.ROLES[sid]
        mine = [t for t in st["tasks"] if t["seat"] == sid and t["state"] in OPEN]
        cur = next((t for t in mine if t["state"] == "working"), None) or (mine[0] if mine else None)
        if not s["occupant"] and not mine and r.optional:
            continue
        d = {"seat": sid, "title": r.title, "occupant": occupant_label(s["occupant"]), "mode": s["mode"]}
        if s["presence"]:
            d["presence"] = s["presence"]["state"]
        if s.get("paused"):
            d["paused"] = True
        if cur:
            d["task"] = f"{cur['id']} {cur['state']}: {cur['title']}"
        if len(mine) > 1:
            d["open_tasks"] = len(mine)
        if s["notes"]:
            d["notes"] = s["notes"]
        seats.append(d)
    out = {"session": str(path), "you_are": me(), "seats": seats,
           "tasks": [_task_view(st, t) for t in st["tasks"] if t["state"] in OPEN],
           "takes": [{k: t.get(k) for k in ("id", "name", "seat", "task", "state", "summary") if t.get(k)}
                     for t in take_index(path) if t.get("state") in ("open", "submitted")],
           "needs_you": [{"id": a["id"], "kind": a["kind"], "from": a["from"], "title": a["title"]}
                         for a in st["attention"] if a["state"] == "open"]}
    recent = [t for t in st["tasks"] if t["state"] in ("done", "failed")][-5:]
    if recent:
        out["recently_finished"] = [_task_view(st, t) for t in recent]
    empty = sorted({t["seat"] for t in st["tasks"] if t["state"] in OPEN and not st["seats"][t["seat"]]["occupant"]})
    if empty:
        out["warnings"] = [f"{s} has open tasks but nobody in the seat" for s in empty]
    return out


def board(session: str | None = None, finished: int = 12, messages: int = 80) -> dict:
    """Everything the studio UI draws, in one read: every seat (filled or not) with its role, the open tasks and
    the last few finished ones, the Needs You items in full, open takes, and recent talkback."""
    path = session_path(session)
    st = read(session)
    seats = []
    for sid in roles.ORDER:
        s, r = st["seats"][sid], roles.ROLES[sid]
        mine = [t for t in st["tasks"] if t["seat"] == sid and t["state"] in OPEN]
        cur = next((t for t in mine if t["state"] == "working"), None) or (mine[0] if mine else None)
        seats.append({"seat": sid, "title": r.title, "room": r.room, "purpose": r.purpose, "owns": sorted(r.owns),
                      "optional": r.optional, "tier": r.tier, "occupant": s["occupant"],
                      "label": occupant_label(s["occupant"]), "mode": s["mode"], "notes": s["notes"],
                      "paused": bool(s.get("paused")), "presence": (s["presence"] or {}).get("state"),
                      "task": cur["id"] if cur else None, "open_tasks": len(mine)})
    closed = [t for t in st["tasks"] if t["state"] not in OPEN]
    return {"session": str(path), "rev": st["rev"], "seq": st["seq"], "seats": seats,
            "tasks": [_task_view(st, t, detail=True) for t in st["tasks"] if t["state"] in OPEN]
                     + [_task_view(st, t, detail=True) for t in closed[-int(finished):]],
            "needs_you": [a for a in st["attention"] if a["state"] == "open"],
            "takes": [t for t in take_index(path) if t.get("state") in ("open", "submitted")],
            "messages": [{k: m.get(k) for k in ("id", "from", "to", "text", "task", "at")}
                         for m in st["messages"][-int(messages):]]}


def log(kind: str, session: str | None = None, **data) -> None:
    """Add a line to the session's activity log (what an agent is doing, a run starting or ending)."""
    with _Board(session) as b:
        b.emit(kind, **data)


def events(since: int = 0, limit: int = 200, session: str | None = None) -> dict:
    """Activity since event number `since` (the newest `limit` of them), plus the number to pass next time."""
    f = session_path(session) / "studio" / "events.jsonl"
    out = []
    if f.exists():
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if e.get("seq", 0) > since:
                    out.append(e)
    out = out[-int(limit):]
    return {"events": out, "last": out[-1]["seq"] if out else int(since)}
