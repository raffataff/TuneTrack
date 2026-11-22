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
REPORT_KINDS = ("report", "review", "note", "scorecard")
# How much of a message, result or summary an event carries. The monitor draws from events, so anything shorter
# than what people write cuts their text off on screen (240 did: a Producer's end-of-session review was unreadable).
EVENT_TEXT = 20000
MODES = ("propose", "direct")  # propose: work in a take and submit it; direct: edit the session itself
ASKS_PER_TASK = 2  # questions a seat may put to the user on one task (blockers are not counted)

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
    return {"studio": SCHEMA, "rev": 0, "seq": 0, "next": {"T": 1, "M": 1, "N": 1, "tk": 1, "R": 1},
            "seats": {}, "tasks": [], "messages": [], "attention": [], "reports": [],
            "routed": {},  # take id -> the hand-in (its "submitted" time) someone has been asked to audition
            "away": None}  # away mode (signoff.away_set): {"on", "since", "minutes"}


def _seat_default(seat: str = "") -> dict:
    # The Producer runs the session and accepts takes, so it works on the session itself; everyone else proposes.
    # signoff: the seat's work stops at the user before it goes on (signoff.py); options: takes offered per task.
    return {"occupant": None, "mode": "direct" if seat == "producer" else "propose", "notes": "", "presence": None,
            "paused": False, "signoff": False, "options": 0}


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


def _read_json(f: Path, tries: int = 20):
    """A JSON file another process may be replacing right now. On Windows a read in that moment fails; it is tried
    again. FileNotFoundError: there is none. Other errors only once it keeps failing."""
    for i in range(tries):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, ValueError):
            if i == tries - 1:
                raise
            time.sleep(0.01 * (i + 1))


def read(session: str | None = None) -> dict:
    """The board as stored (a fresh one if this session has none yet). Read-only: changes go through the functions.
    Seen in tests: a read that met another process's write came back as a fresh board, so the Producer's accept
    went past the user's Co-Producer switch."""
    f = session_path(session) / "studio" / "state.json"
    try:
        return _normalise(_read_json(f))
    except FileNotFoundError:
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

    def release(self, a: dict, cancel: bool = False, not_before: float | None = None) -> list[str]:
        """A Needs You item was answered: the tasks that waited on it go back in the queue (or, with cancel, the
        item's own task is cancelled). not_before: do not start them until then."""
        out, now = [], round(time.time(), 3)
        for tid in dict.fromkeys(([a["task"]] if a.get("task") else []) + list(a.get("blocks") or [])):
            t = self.task(tid)
            still = any(x["state"] == "open" and (x.get("task") == tid or tid in (x.get("blocks") or []))
                        for x in self.st["attention"])
            if t["state"] != "needs_you" or still:
                continue
            t["state"], t["updated"] = ("cancelled" if cancel and tid == a.get("task") else "queued"), now
            t["attempts"] = 0  # waiting for an answer is not a failed try (seen live: three asks became "tried 3 times")
            if not_before and t["state"] == "queued":
                t["not_before"] = not_before
            else:
                t.pop("not_before", None)
            out.append(tid)
            self.emit("task", id=tid, seat=t["seat"], state=t["state"], title=t["title"], by=roles.YOU,
                      reason=time.strftime("starts again at %H:%M", time.localtime(not_before)) if not_before and t["state"] == "queued" else None)
        return out

    def close_auditions(self, take: str, outcome: str, why: str = "") -> None:
        """The take was accepted, returned or discarded: any request to audition it is answered, and so is the
        Producer's task to audition it. The decision is that task's result, so the Producer has nothing to write
        up afterwards, and a take the user decided first does not start a Producer run to hear it again."""
        now = round(time.time(), 3)
        for a in self.st["attention"]:
            if a["state"] == "open" and a.get("take") == take:
                a["state"], a["resolved"] = "resolved", now
                a["answer"] = {"option": None, "text": f"take {outcome}"}
                self.emit("attention", id=a["id"], state="resolved", title=a["title"], answer=f"take {outcome}")
        for t in self.st["tasks"]:
            if t.get("audition") == take and t["state"] in OPEN:
                t.update(state="done", updated=now, result=f"{take} {outcome}" + (f": {why}" if why else ""))
                self.emit("task", id=t["id"], seat=t["seat"], state="done", title=t["title"], by=me(),
                          result=t["result"][:EVENT_TEXT])

    def message(self, sender: str, to: str, text: str, task: str | None = None) -> dict:
        m = {"id": self.new_id("M"), "from": sender, "to": to, "text": text, "task": task,
             "at": round(time.time(), 3), "read": []}
        self.st["messages"].append(m)
        self.emit("message", id=m["id"], sender=sender, to=to, task=task, text=text[:EVENT_TEXT])
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
                notes: str | None = None, session: str | None = None, audition_model: str | None = None,
                signoff: bool | None = None, options: int | None = None) -> dict:
    """Put someone in a seat, or change how the seat works.

    kind: "agent" (the studio launches runner/model for it), "human" (you take it), "external" (an outside app such
    as Claude Desktop sits in), "none" (leave it empty). Omit kind to keep the occupant and change only mode/notes.
    mode: "propose" (work in a take, submit for acceptance) or "direct" (edit the session itself).
    notes: standing instructions for whoever sits here ("no brass", "keep it under 8 voices").
    audition_model: a cheaper model on the same runner for the runs whose only job is to audition handed-in takes
    (the floor's audition tasks); planning runs and the final listen stay on model. "" turns it off. When the agent
    is replaced on the same runner and audition_model is not given, the old one is kept.
    signoff: the user's SIGN OFF switch for this seat (signoff.py). options: 0, 2 or 3 takes per task to choose from.
    Both are the user's to set."""
    sid = roles.get(seat).id
    if not _lead(me()):
        raise PermissionError("Only the Producer or the user assigns seats")
    if (signoff is not None or options is not None) and me() != roles.YOU:
        raise PermissionError("Sign-off and options are the user's settings")
    from . import signoff as so
    if options is not None and int(options) not in so.OPTIONS:
        raise ValueError(f"options must be one of {so.OPTIONS}")
    with _Board(session) as b:
        s = b.st["seats"][sid]
        if kind is not None:
            k = str(kind).strip().lower()
            if k in ("none", "unassigned", "empty", ""):
                s["occupant"], s["presence"] = None, None
            elif k == "agent":
                if not runner:
                    raise ValueError("An agent needs a runner (claude, codex, kilo) and usually a model")
                was = s["occupant"] or {}
                s["occupant"] = {"kind": "agent", "runner": runner, "model": model, "effort": effort, "label": label}
                keep = was.get("audition_model") if was.get("runner") == runner else None
                if audition_model or (audition_model is None and keep):
                    s["occupant"]["audition_model"] = audition_model or keep
            elif k in ("human", "external"):
                s["occupant"] = {"kind": k, "label": label}
            else:
                raise ValueError("kind must be agent, human, external or none")
        elif any(v is not None for v in (runner, model, effort, label, audition_model)):
            if not s["occupant"]:
                raise ValueError(f"{sid} is empty: pass kind=")
            s["occupant"].update({k: v for k, v in (("runner", runner), ("model", model), ("effort", effort),
                                                    ("label", label)) if v is not None})
            if audition_model is not None:
                if s["occupant"]["kind"] != "agent":
                    raise ValueError("audition_model is for a seat with an agent in it")
                s["occupant"]["audition_model"] = audition_model or None
        if mode is not None:
            if mode not in MODES:
                raise ValueError(f"mode must be one of {MODES}")
            s["mode"] = mode
        if notes is not None:
            s["notes"] = notes
        if options is not None:
            s["options"] = int(options)
        said = so._switch(b, sid, bool(signoff)) if signoff is not None else None
        b.emit("seat", seat=sid, occupant=occupant_label(s["occupant"]) or "empty", mode=s["mode"])
        out = {"seat": sid, "occupant": occupant_label(s["occupant"]), "mode": s["mode"]}
        if said:
            out["note"] = said
        if (s["occupant"] or {}).get("audition_model"):
            out["auditions"] = s["occupant"]["audition_model"]
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

def auditioner(st: dict, take: str) -> str | None:
    """Who has been asked to audition a handed-in take: "the Producer" (an open audition task), "you" (an open
    Needs You item), or None when nobody has."""
    if any(a["state"] == "open" and a.get("take") == take for a in st["attention"]):
        return roles.YOU
    if any(t["state"] in OPEN and (t.get("audition") == take or t["title"].startswith(f"Audition take {take}:"))
           for t in st["tasks"]):
        return "the Producer"
    return None


def _take_in_the_way(b: "_Board", t: dict) -> tuple[str, str] | None:
    """A seat is closing its own task: (take id, its state) when that task's take still holds work that is not in
    the session (handed in, or open with changes)."""
    if not t.get("take"):
        return None
    d = b.path / "takes" / t["take"]
    try:
        meta = json.loads((d / "take.json").read_text(encoding="utf-8"))
        if meta.get("state") == "submitted":
            return meta["id"], "submitted"
        if meta.get("state") != "open":
            return None
        from . import takes
        changed = takes.changes(json.loads((d / "base.json").read_text(encoding="utf-8")),
                                json.loads((d / "project.json").read_text(encoding="utf-8")))
        return (meta["id"], "open") if changed else None
    except (OSError, ValueError):
        return None


def _ready(st: dict, t: dict) -> bool:
    done = {x["id"] for x in st["tasks"] if x["state"] == "done"}
    return (t["state"] == "queued" and all(d in done for d in t.get("depends", []))
            and float(t.get("not_before") or 0) <= time.time()  # "try again in 15 minutes"
            and not t.get("held"))  # part of a plan waiting for the user's OK (signoff.py)


def _task_view(st: dict, t: dict, detail: bool = False) -> dict:
    d = {"id": t["id"], "seat": t["seat"], "title": t["title"], "state": t["state"]}
    if t["state"] == "queued":
        waiting = [x for x in t.get("depends", []) if x not in {y["id"] for y in st["tasks"] if y["state"] == "done"}]
        d["ready"] = not waiting
        if waiting:
            d["waiting_for"] = waiting
        if float(t.get("not_before") or 0) > time.time():
            d["ready"], d["not_before"] = False, t["not_before"]
        if t.get("held"):  # "plan" while its seat is still adding, then the plan ticket's id
            d["ready"], d["held"] = False, "waits for the user's OK"
            if detail:
                d["held_by"] = t["held"]
    for k in ("scope", "take", "result", "options"):
        if t.get(k):
            d[k] = t[k]
    if detail:
        d.update(detail=t.get("detail", ""), by=t.get("by"), depends=t.get("depends", []))
        for k in ("report", "recheck", "from_review"):  # a person's review (review.py): what filed it, what it follows up
            if t.get(k):
                d[k] = t[k]
    return d


def _new_task(b: "_Board", seat: str, title: str, detail: str = "", depends: list | None = None,
              scope: dict | None = None, **extra) -> dict:
    """Put a task on the board inside an open _Board (task_add, and review.review_send, which adds several at once)."""
    now = round(time.time(), 3)
    t = {"id": b.new_id("T"), "seat": seat, "title": str(title).strip(), "detail": detail or "", "state": "queued",
         "depends": list(depends or []), "scope": scope or {}, "by": me(), "take": None, "result": "",
         "created": now, "updated": now, "attempts": 0, **extra}
    b.st["tasks"].append(t)
    b.emit("task", id=t["id"], seat=seat, state="queued", title=t["title"], by=me())
    return t


def _upstream(st: dict, tid: str) -> set[str]:
    """tid and every task it waits on, however far back."""
    deps = {t["id"]: t.get("depends", []) for t in st["tasks"]}
    seen, todo = set(), [tid]
    while todo:
        x = todo.pop()
        if x not in seen:
            seen.add(x)
            todo += deps.get(x, [])
    return seen


def _review_links(b: "_Board", t: dict) -> list[str]:
    """The Producer is handing out the findings of a person's review (its task carries from_review, see review.py):
    what it adds or rewrites meanwhile is part of the fixes, so the tasks waiting on its task (Mix's fixes, the
    reviewer's second listen) wait on this one too. Nobody keeps depends lists by hand, and no cycle is made."""
    if me() != "producer" or t["seat"] == "producer" or t["state"] not in OPEN:
        return []
    acting = {p["id"] for p in b.st["tasks"]
              if p["seat"] == "producer" and p["state"] == "working" and p.get("from_review")}
    if not acting:
        return []
    above, linked = _upstream(b.st, t["id"]), []
    for w in b.st["tasks"]:
        if (w["state"] == "queued" and acting & set(w.get("depends", [])) and t["id"] not in w["depends"]
                and w["id"] not in above):
            w["depends"].append(t["id"])
            linked.append(w["id"])
    return linked


WRITERS = ("composer", "arranger", "rhythm")  # seats that write notes and may grow the cast with a part of their own


def new_parts_to_sound(b: "_Board", parts: list[dict], seat: str | None, take: str) -> dict | None:
    """A writing seat's accepted take added parts (name, role, stand-in instrument). Choosing their instrument is
    Sound's, so they join the Sound task that has not started yet, or a new one that a waiting Mix task waits for.
    Without that a part grown mid-session keeps its stand-in through the mix and into the export."""
    if seat not in WRITERS or not parts:
        return None
    names = [p["name"] for p in parts]
    lines = [f"- {p['name']} ({p.get('role') or 'no role'}, stand-in {p['instrument']})" for p in parts]
    said = f"{roles.get(seat).title} added in {take}:\n" + "\n".join(lines)
    if not b.st["seats"]["sound"]["occupant"]:
        return {"note": f"Sound is empty: {', '.join(names)} keep their stand-in instruments."}
    t = next((x for x in b.st["tasks"] if x["seat"] == "sound" and x["state"] == "queued"), None)
    if t:
        t["detail"] = (t.get("detail") or "").rstrip() + "\n\n" + said
        if (t.get("scope") or {}).get("tracks"):
            t["scope"]["tracks"] = list(dict.fromkeys([*t["scope"]["tracks"], *names]))
        t["updated"] = round(time.time(), 3)
        return {"task": t["id"], "added_to": True, "tracks": names}
    t = _new_task(b, "sound", "Instrument for " + ", ".join(names),
                  said + "\nChoose and shape each one on the notes it plays, like the rest of the cast.",
                  scope={"tracks": names})
    out = {"task": t["id"], "tracks": names}
    waiting = [m for m in b.st["tasks"] if m["seat"] == "mix" and m["state"] == "queued"]
    for m in waiting:
        m["depends"].append(t["id"])
    if waiting:
        out["mix_waits"] = [m["id"] for m in waiting]
    elif any(m["seat"] == "mix" and m["state"] in ("working", "submitted", "done") for m in b.st["tasks"]):
        out["note"] = f"Mix has started or finished without {', '.join(names)}: give it a task if they need balancing."
    return out


def task_add(seat: str, title: str, detail: str = "", depends: list | None = None, scope: dict | None = None,
             session: str | None = None, plan: str | None = None) -> dict:
    """Give a seat a task. depends: task ids that must be done first. scope: {"tracks": [...], "bars": "9-16"}.

    Write the detail so the seat can work from it alone: what to make, for which bars/tracks, what it must fit
    with, and what done looks like. plan: (the user) the open plan item this task joins: it waits for that OK too."""
    sid = roles.get(seat).id
    if not str(title).strip():
        raise ValueError("A task needs a title")
    from .signoff import PLAN_SEATS
    who = me()
    with _Board(session) as b:
        # A signed-off Producer (Co-Producer) or Review: what it hands out waits for the user's OK as one plan,
        # put to them when its run is over (signoff.gather_plans). Nobody starts it before.
        held = "plan" if who in PLAN_SEATS and bool(b.st["seats"][who].get("signoff")) and (
            b.st["seats"][who]["occupant"] or {}).get("kind") in ("agent", "external") else None
        same = " ".join(str(title).split()).casefold()
        twin = next((x for x in b.st["tasks"] if x["seat"] == sid and x["state"] in OPEN
                     and " ".join(x["title"].split()).casefold() == same), None)
        if twin and who != roles.YOU:  # seen live: a second copy with no depends started at once, ahead of its music
            raise ValueError(f"{sid} already has {twin['id']} \"{twin['title']}\" ({_task_view(b.st, twin)['state']}). "
                             f"Change that one with task_update(\"{twin['id']}\", detail=...) instead.")
        item = None
        if plan:
            item = b.item(plan)
            if who != roles.YOU or item.get("stage") != "plan" or item["state"] != "open":
                raise ValueError(f"{item['id']} is not an open plan of yours to add to")
            held = item["id"]
        t = _new_task(b, sid, title, detail, [b.task(d)["id"] for d in depends or []], scope,
                      **({"held": held} if held else {}))
        if item:
            item["tasks"].append(t["id"])
        out = _task_view(b.st, t)
        linked = _review_links(b, t)
        if linked:
            out["waited_for_by"] = linked
        if not b.st["seats"][sid]["occupant"]:
            out["warning"] = f"{sid} is empty: nobody will pick this up until the seat is assigned"
        elif twin:
            out["warning"] = f"{sid} already has {twin['id']} with this title"
    return out


def task_update(task: str, state: str | None = None, result: str | None = None, take: str | None = None,
                title: str | None = None, detail: str | None = None, depends: list | None = None,
                session: str | None = None, options: int | None = None) -> dict:
    """Move a task along. States: queued, working, submitted (a take is waiting for acceptance), needs_you, done,
    failed, cancelled. result: one or two sentences on what was done or why it failed - the next seat reads it.
    options: the user asks for 2 or 3 takes to choose from on this task (0: one, as usual)."""
    with _Board(session) as b:
        t = b.task(task)
        who = me()
        if who != t["seat"] and not _lead(who):
            raise PermissionError(f"{t['id']} belongs to {t['seat']}; only that seat, the Producer or the user changes it")
        if options is not None:
            from .signoff import OPTIONS
            if who != roles.YOU:
                raise PermissionError("Options are the user's to ask for")
            if int(options) not in OPTIONS:
                raise ValueError(f"options must be one of {OPTIONS}")
            if int(options):
                t["options"] = int(options)
            else:
                t.pop("options", None)
        if state is not None:
            if state not in TASK_STATES:
                raise ValueError(f"state must be one of {TASK_STATES}")
            if state == "done" and who == t["seat"] and not _lead(who):
                # The seat's work is in a take, and a take is not in the session until it is accepted. A task
                # closed here would never reach anyone's ears (seen live: an export went out without the part).
                held = _take_in_the_way(b, t)
                if held and held[1] == "submitted":
                    return {**_task_view(b.st, t), "note": f"{held[0]} is handed in, and this task is finished when "
                            "it is accepted - that is not yours to set. Nothing more to do: end your turn."}
                if held:
                    raise ValueError(f"Your take {held[0]} holds changes that are not in the session yet. Hand it in "
                                     f"with take_submit(summary): the task is finished when the take is accepted. "
                                     f"If the task needed no change after all, undo() the changes first.")
            if state == "done" and who != t["seat"] and who != roles.YOU:
                # Seen live: the Producer said a take was accepted without accepting it. Closing the task anyway
                # frees the next seat on a session that has none of that work in it.
                held = _take_in_the_way(b, t)
                if held:
                    raise ValueError(f"{held[0]} holds {t['seat']}'s work for {t['id']} and is not in the session. "
                                     f"take_accept(\"{held[0]}\") puts it in and closes {t['id']}; take_reject or "
                                     f"take_discard if it should not land.")
            if state == "working" and t["state"] != "working" and who != roles.YOU:
                # "working" means someone is on it. Set by hand, it skips what the task waits on and nobody starts.
                if who != t["seat"]:
                    raise PermissionError(f"{t['id']} is set working when {t['seat']} starts on it. To let it start, "
                                          f"finish what it waits on (a seat's task is done when its take is "
                                          f"accepted) or change its depends.")
                waits = [d for d in t.get("depends", [])
                         if d not in {x["id"] for x in b.st["tasks"] if x["state"] == "done"}]
                if waits:
                    raise ValueError(f"{t['id']} still waits on {', '.join(waits)}")
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
               result=(t["result"] or "")[:EVENT_TEXT] or None, take=t.get("take"))
        out = _task_view(b.st, t)
        linked = _review_links(b, t) if detail is not None else []
        if linked:
            out["waited_for_by"] = linked
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


def messages_read(seat: str, ids: list[str], session: str | None = None) -> int:
    """Mark messages as read by a seat. The floor manager puts a seat's unread messages in its task message; once
    that run has ended well they have been read, whether or not the agent called inbox() (a seat that never did
    was sent every old message again with each new task)."""
    who, want = _who(seat), set(ids or [])
    if not want:
        return 0
    n = 0
    with _Board(session) as b:
        for m in b.st["messages"]:
            if m["id"] in want and who not in m["read"]:
                m["read"].append(who)
                n += 1
    return n


# ---- Needs You -----------------------------------------------------------------------------------------------------

def _answer_text(a: dict) -> str:
    """What the user answered to a Needs You item, in words: the option they chose and anything they wrote."""
    ans = a.get("answer") or {}
    label = next((o["label"] for o in a.get("options") or [] if o["id"] == ans.get("option")), "")
    return "; ".join(s for s in (label, ans.get("text")) if s) or "answered"


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
        if tid and kind != "blocker" and me() != roles.YOU:
            # Every answer starts the seat again (one agent run), and a seat that asks, is answered and asks again
            # re-checks the same work each time (seen live: three rounds on one accepted take).
            had = [x for x in b.st["attention"] if x["from"] == me() and x.get("task") == tid
                   and x["kind"] != "blocker" and x["state"] == "resolved"]
            if len(had) >= ASKS_PER_TASK:
                said = "; ".join(f"{x['id']} \"{x['title']}\": {_answer_text(x)}" for x in had[-ASKS_PER_TASK:])
                raise ValueError(f"You have asked the user {len(had)} times on {tid} and were answered each time "
                                 f"({said}). Take those answers as settled and finish the task with what you have; "
                                 f"put what is still open in its result. Only kind=\"blocker\" (something you "
                                 f"cannot get past) goes on Needs You for this task now.")
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


# ---- reports -------------------------------------------------------------------------------------------------------
# Anything longer than talkback: a session review, a mix report, notes on what a seat found. Kept as markdown
# files in <session>/studio/reports/ with an index on the board, and read in the studio's REPORTS.

def report_write(title: str, body: str, kind: str = "report", report: str | None = None,
                 session: str | None = None, by: str | None = None) -> dict:
    """File a document for the user to read in the studio's REPORTS: a review, a report, notes too long for
    talkback. body: markdown. kind: "report", "review" or "note". report: the id of one to replace (yours, or any
    for the Producer and the user); omit it to file a new one."""
    if kind not in REPORT_KINDS:
        raise ValueError(f"kind must be one of {REPORT_KINDS}")
    if not str(title).strip() or not str(body).strip():
        raise ValueError("A report needs a title and a body")
    who = by or me()
    with _Board(session) as b:
        r = None
        if report:
            r = next((x for x in b.st["reports"] if x["id"].lower() == str(report).strip().lower()), None)
            if not r:
                raise KeyError(f"No report {report!r}")
            if who != r["by"] and not _lead(who) and who != "floor":
                raise PermissionError(f"{r['id']} was filed by {r['by']}")
        r = _file_report(b, r["id"] if r else b.new_id("R"), who, title, body, kind)
    return {"report": r["id"], "title": r["title"], "note": "Filed. The user reads it under REPORTS in the studio."}


def _file_report(b: "_Board", rid: str, who: str, title: str, body: str, kind: str) -> dict:
    """Write report rid (new or replaced) inside an open _Board: its file, its line in the index, its event."""
    now = round(time.time(), 3)
    r = next((x for x in b.st["reports"] if x["id"] == rid), None)
    if not r:
        r = {"id": rid, "by": who, "created": now}
        b.st["reports"].append(r)
    r.update(title=str(title).strip(), kind=kind, updated=now, chars=len(str(body)))
    d = b.path / "studio" / "reports"
    d.mkdir(parents=True, exist_ok=True)
    _atomic_write(d / f"{rid}.md", str(body))
    b.emit("report", id=rid, title=r["title"], what=kind, sender=who)
    return r


def report_list(session: str | None = None) -> dict:
    """The reports filed in this session, oldest first."""
    return {"reports": list(read(session)["reports"])}


def report_read(report: str, session: str | None = None) -> dict:
    """One report in full."""
    r = next((x for x in read(session)["reports"] if x["id"].lower() == str(report).strip().lower()), None)
    if not r:
        raise KeyError(f"No report {report!r}")
    f = session_path(session) / "studio" / "reports" / f"{r['id']}.md"
    return {**r, "body": f.read_text(encoding="utf-8") if f.exists() else ""}


def attention_close(item: str, text: str, session: str | None = None) -> dict:
    """The floor manager closes one of its own items because the situation it described is over (every seat that
    was out of usage now has another model). The tasks that waited on it go back in the queue."""
    with _Board(session) as b:
        a = b.item(item)
        if a["state"] != "open":
            return {"resolved": a["id"], "requeued": []}
        a["state"], a["resolved"] = "resolved", round(time.time(), 3)
        a["answer"] = {"option": None, "text": str(text)}
        requeued = b.release(a)
        b.emit("attention", id=a["id"], state="resolved", title=a["title"], answer=str(text))
    return {"resolved": a["id"], "requeued": requeued}


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
    if pending and pending["state"] == "open" and pending.get("stage") and option is not None:
        # a stop where the user signs off the crew's work (signoff.py): the answer is carried out here, and the
        # seat hears about it through what that does (a returned take's reason, the Producer's audition task)
        from . import signoff
        signoff.carry(pending, option, text, session)
        carried = True
    elif pending and pending["state"] == "open" and pending.get("take") and option is not None:
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
        elif choice in ("c", "discard") and any(o["id"] == "c" for o in pending.get("options") or []):
            takes.take_discard(pending["take"], session=session)
            carried = True
    with _Board(session) as b:
        a = b.item(item)
        closed = a["state"] != "open"  # carrying out the decision above already closed the audition request
        if closed and not carried:
            raise ValueError(f"{a['id']} is already resolved")
        chosen = None
        if option is not None:
            o = str(option).strip().lower()
            chosen = next((x for x in a["options"] if x["id"] == o or x["label"].lower() == o
                           or (a.get("stage") and x.get("do") == o)), None)
            if not chosen:
                raise KeyError(f"No option {option!r} on {a['id']}. Options: "
                               + ", ".join(f"{x['id']}: {x['label']}" for x in a["options"]))
        if not chosen and not str(text).strip():
            raise ValueError("Choose an option or write an answer")
        a["state"], a["resolved"] = "resolved", round(time.time(), 3)
        a["answer"] = {"option": chosen["id"] if chosen else None, "text": str(text).strip()}
        said = "; ".join(x for x in ((f"chose {chosen['label']}" + (f" (take {chosen['take']})" if chosen.get("take") else ""))
                                     if chosen else "", str(text).strip()) if x)
        do = (chosen or {}).get("do")  # what the floor manager's own options mean (an agent's options carry none)
        if a.get("stage"):
            do = None  # a stop's options were carried out above; its own task is not cancelled or delayed
        requeued = b.release(a, cancel=do == "cancel",
                             not_before=round(time.time() + 60 * float(chosen.get("minutes") or 15), 3) if do == "wait" else None)
        if a["from"] != roles.YOU and not carried:  # a returned take already carries its reason to the seat
            b.message(roles.YOU, a["from"], f"Answer to {a['id']} \"{a['title']}\": {said}", a["task"])
        if not closed:
            b.emit("attention", id=a["id"], state="resolved", title=a["title"], answer=said[:EVENT_TEXT])
    return {"resolved": a["id"], "answer": a["answer"], "requeued": requeued}


# ---- overview ------------------------------------------------------------------------------------------------------

def take_index(path: Path) -> list[dict]:
    """Every take's take.json in a session, oldest first (takes.py creates and changes them)."""
    out = []
    for f in (Path(path) / "takes").glob("*/take.json"):
        try:  # a take missed here is cut again by the floor, or not heard: retried like the board
            out.append(_read_json(f, tries=10))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda t: t.get("created", 0))


def _status_lean(path: Path, st: dict) -> dict:
    """The board for an agent the studio started, which reads it to act and pays for every character on each later
    step: seats that are filled (a word each), open tasks and takes without their texts, the last few finished
    tasks by title. Empty seats, model names, role descriptions, summaries and results are left out."""
    seats, open_tasks = {}, [t for t in st["tasks"] if t["state"] in OPEN]
    for sid in roles.ORDER:
        occ = st["seats"][sid]["occupant"]
        if occ:
            seats[sid] = ("user" if occ["kind"] == "human" else occ["kind"]) + (
                " (on hold)" if st["seats"][sid].get("paused") else "")
    tasks = []
    for t in open_tasks:
        d = _task_view(st, t)
        d.pop("result", None)
        d.pop("scope", None)
        tasks.append(d)
    out = {"you_are": me(), "seats": seats, "tasks": tasks,
           "takes": [{k: t.get(k) for k in ("id", "seat", "task", "state") if t.get(k)}
                     for t in take_index(path) if t.get("state") in ("open", "submitted")],
           "needs_you": [{"id": a["id"], "kind": a["kind"], "from": a["from"], "title": a["title"]}
                         for a in st["attention"] if a["state"] == "open"]}
    recent = [t for t in st["tasks"] if t["state"] in ("done", "failed")][-5:]
    if recent:
        out["recently_finished"] = [f"{t['id']} {t['seat']} {t['state']}: {t['title']}" for t in recent]
    empty = sorted({t["seat"] for t in open_tasks if not st["seats"][t["seat"]]["occupant"]})
    if empty:
        out["warnings"] = [f"{s} has open tasks but nobody in the seat" for s in empty]
    return out


def studio_status(session: str | None = None, lean: bool = False) -> dict:
    """The board at a glance: seats and who is in them, open tasks, takes waiting, and what needs the user.
    lean: the short form a launched agent gets (_status_lean)."""
    path = session_path(session)
    st = read(session)
    if lean:
        return _status_lean(path, st)
    seats = []
    for sid in roles.ORDER:
        s, r = st["seats"][sid], roles.ROLES[sid]
        mine = [t for t in st["tasks"] if t["seat"] == sid and t["state"] in OPEN]
        cur = next((t for t in mine if t["state"] == "working"), None) or (mine[0] if mine else None)
        if not s["occupant"] and not mine and r.optional:
            continue
        d = {"seat": sid, "title": r.title, "occupant": occupant_label(s["occupant"]), "mode": s["mode"]}
        if (s["occupant"] or {}).get("audition_model"):
            d["auditions"] = s["occupant"]["audition_model"]
        if s["presence"]:
            d["presence"] = s["presence"]["state"]
        if s.get("paused"):
            d["paused"] = True
        if s.get("signoff"):  # the Producer's brief says what Co-Producer means for its decisions and its plan
            d["signoff"] = True
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
                      "signoff": bool(s.get("signoff")), "options": int(s.get("options") or 0),
                      "task": cur["id"] if cur else None, "open_tasks": len(mine)})
    closed = [t for t in st["tasks"] if t["state"] not in OPEN]
    from .signoff import involve_name
    return {"session": str(path), "rev": st["rev"], "seq": st["seq"], "seats": seats,
            "involve": involve_name(st), "away": st.get("away"),
            "tasks": [_task_view(st, t, detail=True) for t in st["tasks"] if t["state"] in OPEN]
                     + [_task_view(st, t, detail=True) for t in closed[-int(finished):]],
            "needs_you": [a for a in st["attention"] if a["state"] == "open"],
            "reports": st["reports"],
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
