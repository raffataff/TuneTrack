"""The floor manager: starts an agent on each task that is ready, watches it work, and deals with what comes back.

It is plumbing, not a musician - the Producer seat makes the musical decisions. The floor manager only:
  - launches the seat's agent on a ready task (in its own take when the seat works by proposal),
  - records what the agent does on the board's activity log,
  - routes a handed-in take to whoever auditions it (the Producer's agent, or the user's Needs You list),
  - turns failures into something visible: a retry with the reason, then a blocker on Needs You - never a lamp
    that says "working" forever.

    floor = Floor(session)        # the session's project folder
    floor.run()                   # until nothing more can happen without the user

One process manages a session at a time: run() holds <session>/studio/floor.lock, and a second manager on the same
session is refused. Agents themselves run as separate processes and reach the project only through TuneTrack's MCP
server, each with its seat's tools.
"""
from __future__ import annotations

import re
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .. import config, roles, studio, takes
from ..model import LockTimeout, _os_lock, _os_unlock
from . import briefs, runners

TEXT_LIMIT = 400  # an error line
SAYS_LIMIT = studio.EVENT_TEXT  # what an agent says is read in the monitor: keep all of it


# An agent that ended because its model has no usage left (a free tier's daily limit, a rate limit, no credit).
# Every seat on that model will fail the same way, so it is one thing to tell the user, not one per seat.
_QUOTA = re.compile(r"usage limit|rate.?limit|too many requests|quota|\b429\b|out of credit|insufficient credit|billing", re.I)
_RETRY = {"label": "Try again now"}
_RESEAT = {"label": "Put someone else in this seat\u2026", "do": "seat"}


def _model_of(occ: dict | None) -> str:
    return f"{(occ or {}).get('runner')}:{(occ or {}).get('model') or ''}"


class FloorBusy(RuntimeError):
    """Another floor manager already runs this session."""


@contextmanager
def _manager_lock(path: Path):
    """Held for as long as a floor manager runs a session. The OS drops it if the process dies."""
    d = Path(path) / "studio"
    d.mkdir(parents=True, exist_ok=True)
    fh = open(d / "floor.lock", "a+b")  # noqa: SIM115 - held for the whole run
    try:
        try:
            _os_lock(fh, 0.0, d / "floor.lock")
        except LockTimeout:
            raise FloorBusy(f"The crew of {Path(path).name} is already being run by another process (the editor or "
                            f"`tunetrack crew run`). Stop that one first.") from None
        try:
            yield
        finally:
            _os_unlock(fh)
    finally:
        fh.close()


def managed(session: str | None = None) -> bool:
    """True when some process (this one or another) is running this session's crew right now."""
    try:
        with _manager_lock(studio.session_path(session)):
            return False
    except FloorBusy:
        return True


class Floor:
    def __init__(self, session: str | None = None, max_parallel: int = 3, max_launches: int = 40,
                 max_attempts: int = 3, run_timeout: float = 1800.0, start=runners.start, on_event=None):
        self.path = studio.session_path(session)
        self.session = str(self.path)
        self.max_parallel, self.max_launches, self.max_attempts = max_parallel, max_launches, max_attempts
        self.run_timeout = run_timeout
        self._start = start
        self._on_event = on_event or (lambda seat, ev: None)
        self._runs: dict[str, dict] = {}  # seat -> {"run", "task", "thread", "started", "take"}
        self._lock = threading.Lock()
        self._quota_lock = threading.Lock()
        self._looked: dict[str, float] = {}  # open takes with work in them that we have diffed: id -> file time
        self.launches = 0
        self._stopping = threading.Event()
        self._limit_step, self._limit_item = max_launches, None
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}

    # ---- what the board needs from us ------------------------------------------------------------------------------

    def tick(self) -> dict:
        """One pass: stop overdue runs, route handed-in takes, start what is ready. Returns what is going on."""
        now = time.time()
        with self._lock:
            running = dict(self._runs)
        for seat, r in running.items():
            if now - r["started"] > self.run_timeout:
                r["timed_out"] = True
                r["run"].stop()
        self._route_takes()
        started, waiting, paused = [], [], []
        st = studio.read(self.session)
        # A task can gain a dependency after its agent started (the Producer accepts a take, which frees the next
        # task, and only then adds the work that should have come first). Working on is wasted: stop and requeue.
        done = {t["id"] for t in st["tasks"] if t["state"] == "done"}
        tasks = {t["id"]: t for t in st["tasks"]}
        for seat, r in running.items():
            t = tasks.get(r["task"])
            behind = [d for d in (t or {}).get("depends", []) if d not in done]
            if t and t["state"] == "working" and behind and not r.get("paused") and not r.get("held"):
                r["held"] = behind
                r["run"].stop()
        quota = self._quota_items(st)
        for t in studio.ready_tasks(self.session):
            occ = st["seats"][t["seat"]]["occupant"]
            if not occ or occ["kind"] != "agent":
                waiting.append(t["id"])  # a person's task, or an empty seat: the board shows it
                continue
            if st["seats"][t["seat"]].get("paused"):
                paused.append(t["id"])
                continue
            with self._lock:
                busy = t["seat"] in self._runs or len(self._runs) >= self.max_parallel
            if busy:
                continue
            dry = quota.get(_model_of(occ))
            if dry:  # this seat's model is out of usage: wait on the ticket that says so instead of failing too
                self._join_quota(dry, t)
                continue
            if self.launches >= self.max_launches:
                more = self._limit_answer()
                if more == "stop":
                    self._limit_item = None
                    self._stopping.set()
                    break
                if more:  # the user said carry on
                    self.max_launches += more
                    self._limit_item = None
                else:
                    big = max(40, 2 * self._limit_step)
                    self._limit_item = self._limit_item or self._blocker(
                        t, f"The session reached its limit of {self.max_launches} agent runs",
                        "Nothing more is started, so a loop can't run away with your usage. Say how much further "
                        "the crew may go.", hold=False, why="limit",
                        options=[{"label": f"Carry on \u00b7 {self._limit_step} more runs", "runs": self._limit_step},
                                 {"label": f"Carry on \u00b7 {big} more runs", "runs": big},
                                 {"label": "Stop the crew", "do": "stop"}])
                    break
            if t.get("attempts", 0) >= self.max_attempts:
                self._blocker(t, f"{roles.get(t['seat']).title} tried \"{t['title']}\" {t['attempts']} times without finishing",
                              "Look at the task: rewrite it, give the seat to another agent, or take it yourself. "
                              "The seat's LOG tab shows how each try ended.", why="attempts",
                              options=[_RETRY, {"label": "Rewrite the task\u2026", "do": "edit"}, _RESEAT,
                                       {"label": "Cancel the task", "do": "cancel"}])
                continue
            if self._launch(t, st):
                started.append(t["id"])
        with self._lock:
            active = {seat: r["task"] for seat, r in self._runs.items()}
        return {"running": active, "started": started, "waiting_on_people": waiting,
                **({"paused": paused} if paused else {})}

    def run(self, poll: float = 0.5, keep_alive: bool = False) -> dict:
        """Manage the session until nothing more can happen without the user (or until stop() with keep_alive).
        Raises FloorBusy when another process is already managing this session."""
        with _manager_lock(self.path):
            self._stopping.clear()
            self._recover()
            try:
                while not self._stopping.is_set():
                    state = self.tick()
                    if not state["running"] and not state["started"] and not keep_alive:
                        self._route_takes()
                        if not self._launchable():
                            break
                    self._stopping.wait(poll)
            except KeyboardInterrupt:
                self.stop()
                raise
            if self._stopping.is_set():
                self._halt()
        return {**studio.studio_status(self.session), "launches": self.launches, "usage": self.usage}

    def stop(self) -> None:
        """Stop managing: running agents are stopped and their tasks go back in the queue, uncounted."""
        self._stopping.set()
        self._halt()

    def _halt(self) -> None:
        with self._lock:
            running = list(self._runs.values())
        for r in running:
            r["paused"] = True
            r["run"].stop()
        for r in running:
            if r["thread"] is not threading.current_thread():
                r["thread"].join(timeout=10)

    def stop_seat(self, seat: str) -> bool:
        """Stop the run in one seat (the user paused it). Its task is requeued and the attempt is not counted."""
        with self._lock:
            r = self._runs.get(seat)
        if not r:
            return False
        r["paused"] = True
        r["run"].stop()
        return True

    def active(self) -> dict:
        """Seats with an agent running right now: {seat: {"task", "take", "since"}}."""
        with self._lock:
            return {seat: {"task": r["task"], "take": r["take"], "since": round(r["started"], 3)}
                    for seat, r in self._runs.items()}

    def _recover(self) -> None:
        """We hold the session, so an agent's task still marked "working" belongs to a manager that died (the
        editor was closed mid-run). Put it back in the queue and clear the seat's lamp."""
        with self._lock:
            mine = {r["task"] for r in self._runs.values()}
            seated = set(self._runs)
        st = studio.read(self.session)
        lost = [t["id"] for t in st["tasks"] if t["state"] == "working" and t["id"] not in mine
                and (st["seats"][t["seat"]]["occupant"] or {}).get("kind") == "agent"]
        stale = [sid for sid, s in st["seats"].items() if s.get("presence") and sid not in seated
                 and (s["occupant"] or {}).get("kind") == "agent"]
        if not lost and not stale:
            return
        with studio._Board(self.session) as b:
            for tid in lost:
                t = b.task(tid)
                t["state"], t["updated"] = "queued", round(time.time(), 3)
                t["attempts"] = max(0, t.get("attempts", 0) - 1)
                b.emit("task", id=tid, seat=t["seat"], state="queued", title=t["title"], by="floor",
                       reason="its run was cut off when the crew last stopped")
            for sid in stale:
                b.st["seats"][sid]["presence"] = None
                b.emit("presence", seat=sid, state="cleared")

    def _limit_answer(self):
        """What the user said to the run-limit item: None (not answered), a number of further runs, or "stop"."""
        a = next((a for a in studio.read(self.session)["attention"] if a["id"] == self._limit_item), None)
        if not a or a["state"] != "resolved":
            return None
        o = next((o for o in a.get("options") or [] if o["id"] == (a.get("answer") or {}).get("option")), {})
        return "stop" if o.get("do") == "stop" else int(o.get("runs") or self._limit_step)

    def _quota_items(self, st: dict) -> dict[str, dict]:
        """Open "this model is out of usage" items by model. One whose seats have all moved to another model is
        over: it is closed here, which puts its tasks back in the queue."""
        out = {}
        for a in st["attention"]:
            if a["state"] != "open" or a.get("why") != "quota":
                continue
            if any(_model_of(st["seats"][s]["occupant"]) == a["model"] for s in a.get("seats", [])):
                out[a["model"]] = a
            else:
                studio.attention_close(a["id"], "every seat that was waiting has another model now", self.session)
        return out

    def _join_quota(self, item: dict, task: dict) -> None:
        """Another seat's task is ready on a model that is out of usage: it waits on the same item."""
        with studio._Board(self.session) as b:
            a, t = b.item(item["id"]), b.task(task["id"])
            if a["state"] != "open" or t["state"] not in ("queued", "working"):
                return
            if t["id"] != a["task"] and t["id"] not in a["blocks"]:
                a["blocks"].append(t["id"])
            if t["seat"] not in a["seats"]:
                a["seats"].append(t["seat"])
                a["title"] = self._quota_title(a)
            t["state"], t["updated"], t["attempts"] = "needs_you", round(time.time(), 3), 0
            b.emit("task", id=t["id"], seat=t["seat"], state="needs_you", title=t["title"], by="floor",
                   reason=f"waits on {a['id']}: its model is out of usage")

    @staticmethod
    def _quota_title(a: dict) -> str:
        who = [roles.get(s).title for s in a["seats"]]
        names = who[0] if len(who) == 1 else ", ".join(who[:-1]) + " and " + who[-1]
        return f"{names} cannot work: {a['model'].split(':', 1)[1] or a['model']} is out of usage"

    def _launchable(self) -> bool:
        st = studio.read(self.session)
        if self._limit_item and self._limit_answer():
            return True
        return self.launches < self.max_launches and any(
            (st["seats"][t["seat"]]["occupant"] or {}).get("kind") == "agent" and t.get("attempts", 0) < self.max_attempts
            and not st["seats"][t["seat"]].get("paused")
            for t in studio.ready_tasks(self.session))

    # ---- starting an agent -----------------------------------------------------------------------------------------

    def _launch(self, task: dict, st: dict) -> bool:
        seat = task["seat"]
        s = st["seats"][seat]
        occ = s["occupant"]
        in_take = s["mode"] == "propose" and bool(roles.get(seat).owns)
        take = None
        try:
            if in_take:
                take = next((t for t in studio.take_index(self.path)
                             if t["id"] == task.get("take") and t.get("state") == "open"), None)
                if not take:
                    made = takes.take_new(task["title"], task["id"], seat, use=False, session=self.session)
                    take = next(t for t in studio.take_index(self.path) if t["id"] == made["take"])
            project = self.path / "takes" / take["id"] if take else self.path
            env = {"TUNETRACK_SEAT": seat, "TUNETRACK_SESSION": self.session, "TUNETRACK_PROJECT": str(project),
                   "TUNETRACK_HOME": str(config.HOME), "TUNETRACK_PROJECTS": str(config.PROJECTS_DIR)}
            done = {t["id"]: t for t in st["tasks"] if t["state"] == "done"}
            work = self.path / "studio" / "work" / seat  # an empty folder: agents have no business in the repo
            (self.path / "studio" / "logs").mkdir(parents=True, exist_ok=True)
            work.mkdir(parents=True, exist_ok=True)
            launch = runners.Launch(
                runner=occ["runner"], model=occ.get("model"), effort=occ.get("effort"),
                system=briefs.system(seat, in_take, s.get("notes", "")),
                prompt=briefs.task_prompt(task, studio.studio_status(self.session), take,
                                          [done[d] for d in task.get("depends", []) if d in done],
                                          studio.inbox(seat, mark_read=False, session=self.session)["messages"]),
                cwd=str(work), env=env, allow_tools=["mcp__tunetrack"],
                mcp={"tunetrack": {"command": sys.executable, "args": ["-m", "tunetrack.mcp_server"], "env": env}},
                log=str(self.path / "studio" / "logs" / f"{task['id']}-{task.get('attempts', 0) + 1}-{seat}.jsonl"))
            if occ["runner"] == "script":
                launch.script, launch.model = occ.get("model"), None
            run = self._start(launch)
        except Exception as e:  # noqa: BLE001 - a seat that cannot start is the user's to see, not a crash
            self._blocker(task, f"{roles.get(seat).title} could not start", f"{type(e).__name__}: {e}",
                          why="start", options=[_RETRY, _RESEAT])
            return False
        self.launches += 1
        studio.task_update(task["id"], state="working", session=self.session)
        studio.seat_presence(seat, "online", session=self.session, pid=run.pid)
        studio.log("run", self.session, seat=seat, task=task["id"], what="start",
                   agent=studio.occupant_label(occ), take=take["id"] if take else None)
        rec = {"run": run, "task": task["id"], "started": time.time(), "take": take["id"] if take else None}
        rec["thread"] = threading.Thread(target=self._watch, args=(seat, rec), daemon=True,
                                         name=f"floor-{seat}-{task['id']}")
        with self._lock:
            self._runs[seat] = rec
        rec["thread"].start()
        return True

    def _watch(self, seat: str, rec: dict) -> None:
        tools, errors, last_text, done = 0, [], "", {"ok": False, "returncode": None, "result": ""}
        try:
            for ev in rec["run"].events():
                self._on_event(seat, ev)
                kind = ev.get("type")
                if kind == "tool":
                    tools += 1
                    studio.log("run", self.session, seat=seat, task=rec["task"], what="tool",
                               tool=str(ev.get("name", "")).replace("mcp__tunetrack__", ""))
                elif kind == "text" and ev.get("text"):
                    last_text = ev["text"]
                    studio.log("run", self.session, seat=seat, task=rec["task"], what="says",
                               text=last_text[:SAYS_LIMIT])
                elif kind == "error":
                    errors.append(str(ev.get("message", ""))[:TEXT_LIMIT])
                elif kind == "usage":
                    for k in ("input_tokens", "output_tokens"):
                        self.usage[k] += int(ev.get(k) or 0)
                    self.usage["cost_usd"] += float(ev.get("cost_usd") or 0.0)
                elif kind == "done":
                    done = ev
        except Exception as e:  # noqa: BLE001
            errors.append(f"{type(e).__name__}: {e}")
        try:
            self._finished(seat, rec, done, tools, errors, last_text)
        finally:
            with self._lock:
                self._runs.pop(seat, None)

    # ---- when an agent stops ----------------------------------------------------------------------------------------

    def _finished(self, seat: str, rec: dict, done: dict, tools: int, errors: list[str], last_text: str) -> None:
        tid = rec["task"]
        studio.seat_presence(seat, None, session=self.session)
        task = next(t for t in studio.read(self.session)["tasks"] if t["id"] == tid)
        said = (done.get("result") or last_text or "").strip()
        held = rec.get("held")
        why = ("it ran past the time limit" if rec.get("timed_out") else
               "it was stopped (the seat was put on hold, or the crew was stopped)" if rec.get("paused") else
               f"it was stopped: its task now waits on {', '.join(held)}" if held else
               (errors[-1] if errors else ""))
        studio.log("run", self.session, seat=seat, task=tid, what="end", ok=bool(done.get("ok")), tools=tools,
                   error=why or None)
        if task["state"] != "working":
            return  # the agent moved its task itself (submitted, done, needs_you, failed): nothing to tidy
        if rec.get("paused") or held:  # stopped by the user or the plan, not by a fault: requeued, attempt not counted
            with studio._Board(self.session) as b:
                t = b.task(tid)
                t["state"], t["updated"] = "queued", round(time.time(), 3)
                t["attempts"] = max(0, t.get("attempts", 0) - 1)
                b.emit("task", id=tid, seat=seat, state="queued", title=t["title"], by="floor",
                       reason="paused" if rec.get("paused") else f"now waits on {', '.join(held)}")
            return
        if done.get("ok") and not rec.get("timed_out"):
            if rec["take"]:
                rep = takes.take_diff(rec["take"], self.session)
                if rep["changes"] and rep["state"] == "open":
                    takes.take_submit("Handed in by the floor manager - the agent stopped without submitting. "
                                      "Its last words: " + (said[:600] or "(none)"), rec["take"], self.session)
                    return
                if not rep["changes"]:
                    why = "it stopped without changing anything in its take"
            else:
                studio.task_update(tid, state="done", result=said[:600] or "Finished (the agent left no summary).",
                                   session=self.session)
                return
        why = why or "it stopped before finishing"
        if _QUOTA.search(" ".join(errors) + " " + why):  # no usage left: true for every seat on this model
            occ = studio.read(self.session)["seats"][seat]["occupant"]
            a = {"why": "quota", "model": _model_of(occ), "seats": [seat]}
            with self._quota_lock:  # two seats on that model can fail in the same moment: look and raise as one step
                known = self._quota_items(studio.read(self.session)).get(a["model"])
                if known:  # two seats on that model were started together: still one item
                    self._join_quota(known, task)
                    return
                self._blocker(task, self._quota_title(a),
                              f"{why[:300]}\n\nNothing else is started on this model until you answer. What its "
                              "seats had done is kept in their takes.", options=[
                                  _RETRY, {"label": "Try again in 15 minutes", "do": "wait", "minutes": 15},
                                  {"label": "Put another model in these seats\u2026", "do": "seat"}], **a)
            return
        if tools == 0:  # it never reached the studio at all: login, model name, CLI - trying again won't help
            self._blocker(task, f"{roles.get(seat).title} could not work: {why[:120]}",
                          "The agent ended without using a single studio tool, which usually means its app is not "
                          "logged in or the model name is wrong. Fix it or put someone else in the seat, then "
                          "try again.", why="login", options=[_RETRY, _RESEAT])
            return
        with studio._Board(self.session) as b:
            t = b.task(tid)
            t["state"], t["updated"] = "queued", round(time.time(), 3)
            b.message(roles.YOU, seat, f"Your last run on {tid} ended early ({why}). What you had done is still in "
                                       f"your take; pick it up from there.", tid)
            b.emit("task", id=tid, seat=seat, state="queued", title=t["title"], by="floor", reason=why[:240])

    def _blocker(self, task: dict, title: str, detail: str, hold: bool = True, options: list | None = None,
                 **extra) -> str:
        """Put a blocker on Needs You. hold: the task waits for the answer (and gets a fresh set of attempts).
        options: the answers that fit this situation; "do" names what one means beyond "try again" (seat: choose
        who sits there, edit: rewrite the task, wait: start again later, cancel: drop the task, stop: stop the
        crew). extra: what kind of blocker it is ("why") and whatever that kind keeps."""
        with studio._Board(self.session) as b:
            t = b.task(task["id"])
            now = round(time.time(), 3)
            a = {"id": b.new_id("N"), "kind": "blocker", "from": roles.YOU, "task": t["id"] if hold else None,
                 "title": title, "detail": detail,
                 "options": [{"id": chr(ord("a") + i), **o} for i, o in enumerate(options or [])],
                 "blocks": [], "state": "open", "answer": None, "created": now, "resolved": None, **extra}
            b.st["attention"].append(a)
            if hold:
                t["state"], t["updated"], t["attempts"] = "needs_you", now, 0
                b.emit("task", id=t["id"], seat=t["seat"], state="needs_you", title=t["title"], by="floor")
            b.emit("attention", id=a["id"], state="open", what="blocker", sender="floor", title=title,
                   task=t["id"] if hold else None)
        return a["id"]

    def _route_takes(self) -> None:
        """A handed-in take needs ears: the Producer's agent gets an audition task, otherwise the user does. That
        goes by the take, not by its task: a seat may close its task and hand in afterwards, and a person may hand
        in a take cut for no task. An audition request on Needs You closes itself once the take has been accepted
        or returned. And an empty take whose task is over is put away."""
        index = {t["id"]: t for t in studio.take_index(self.path)}
        st = studio.read(self.session)
        self._put_away_empty(index, st)
        stale = any(a["state"] == "open" and a.get("take") and index.get(a["take"], {}).get("state") != "submitted"
                    for a in st["attention"])

        def routed(board: dict, tk: dict) -> bool:  # per hand-in: a take that came back and is handed in again is new
            return (board["routed"].get(tk["id"]) == tk.get("submitted")
                    or any(t.get("routed") == [tk["id"], tk.get("submitted")] for t in board["tasks"]))  # older boards

        fresh = [tk for tk in index.values() if tk.get("state") == "submitted" and not routed(st, tk)]
        if not stale and not fresh:
            return  # nothing to do: leave the board file alone
        with studio._Board(self.session) as b:
            producer = b.st["seats"]["producer"]["occupant"]
            now = round(time.time(), 3)
            for a in b.st["attention"]:
                if a["state"] == "open" and a.get("take") and index.get(a["take"], {}).get("state") != "submitted":
                    a["state"], a["resolved"] = "resolved", now
                    a["answer"] = {"option": None, "text": f"take {index.get(a['take'], {}).get('state', 'gone')}"}
                    b.emit("attention", id=a["id"], state="resolved", title=a["title"])
            tasks = {t["id"]: t for t in b.st["tasks"]}
            for tk in fresh:
                if routed(b.st, tk):
                    continue
                b.st["routed"][tk["id"]] = tk.get("submitted")
                t = tasks.get(tk.get("task") or "")
                seat = tk.get("seat") if tk.get("seat") in roles.ROLES else None
                who = roles.get(seat).title if seat else "The user"
                title = f"Audition take {tk['id']}: {t['title'] if t else tk.get('name') or tk['id']}"
                detail = (f"{who} handed in {tk['id']}" + (f" for task {t['id']}" if t else "") + ".\n"
                          f"Their summary: {tk.get('summary') or '(none)'}\n"
                          f"Listen to it against the brief, then take_accept(\"{tk['id']}\") or "
                          f"take_reject(\"{tk['id']}\", reason).")
                if producer and producer["kind"] == "agent" and seat != "producer":
                    task = {"id": b.new_id("T"), "seat": "producer", "title": title, "detail": detail,
                            "state": "queued", "depends": [], "scope": {}, "by": "floor", "take": None, "result": "",
                            "audition": tk["id"], "created": now, "updated": now, "attempts": 0}
                    b.st["tasks"].append(task)
                    b.emit("task", id=task["id"], seat="producer", state="queued", title=title, by="floor")
                else:
                    a = {"id": b.new_id("N"), "kind": "decision", "from": seat or roles.YOU, "task": None,
                         "title": title, "detail": tk.get("summary") or "", "take": tk["id"],
                         "options": [{"id": "a", "label": "Accept", "take": tk["id"]}, {"id": "b", "label": "Return"}],
                         "blocks": [], "state": "open", "answer": None, "created": now, "resolved": None}
                    b.st["attention"].append(a)
                    b.emit("attention", id=a["id"], state="open", what="decision", sender=seat or roles.YOU,
                           title=title, take=tk["id"])

    def _put_away_empty(self, index: dict, st: dict) -> None:
        """An open take that changed nothing, whose task is over and whose seat is not at work, is clutter (a Mix
        task that needed no change left one behind): close it. One with work in it is left for people to decide."""
        tasks = {t["id"]: t for t in st["tasks"]}
        with self._lock:
            at_work = {r["take"] for r in self._runs.values()}
        for tk in index.values():
            t = tasks.get(tk.get("task") or "")
            if tk.get("state") != "open" or not t or t["state"] in studio.OPEN or tk["id"] in at_work:
                continue
            d = self.path / "takes" / tk["id"]
            try:
                stamp = (d / "project.json").stat().st_mtime
                if self._looked.get(tk["id"]) == stamp:
                    continue  # it has work in it and has not changed since we looked
                self._looked[tk["id"]] = stamp
                if takes.changes(takes._read_json(d / "base.json"), takes._read_json(d / "project.json")):
                    continue
                takes._meta(d, state="rejected", closed=round(time.time(), 3),
                            reason=f"Put away empty: {t['id']} ended without a change")
            except (OSError, ValueError):
                continue
            tk["state"] = "rejected"
            studio.log("take", self.session, id=tk["id"], state="rejected", seat=tk.get("seat"), task=t["id"],
                       by="floor", reason="empty")


def kickoff(brief: str, session: str | None = None) -> dict:
    """Start a session from a prompt: the Producer gets the brief as its first task and plans from there."""
    if not str(brief).strip():
        raise ValueError("Say what you want made")
    return studio.task_add(
        "producer", "Plan the session",
        "The user asked for:\n\n" + str(brief).strip() + "\n\nPlan it: set the musical contract on the session, "
        "then hand out the tasks. When you have, set this task done with a two-line summary of the plan.",
        session=session)


def crew(session: str | None = None) -> dict:
    """Who is in the session, and which seats that have work can't be started (empty, or a runner that isn't there)."""
    from . import detect
    found = {r["id"]: r for r in detect.detect()["runners"]}
    st = studio.read(session)
    out, problems = [], []
    for sid in roles.ORDER:
        occ = st["seats"][sid]["occupant"]
        if not occ:
            continue
        d = {"seat": sid, "occupant": studio.occupant_label(occ), "mode": st["seats"][sid]["mode"]}
        if occ["kind"] == "agent" and occ["runner"] != "script":
            r = found.get(occ["runner"])
            if not r or not r["available"]:
                problems.append(f"{sid}: {occ['runner']} is not installed")
            elif r["auth"] == "missing":
                problems.append(f"{sid}: {r['name']} is not logged in ({'; '.join(r['notes'][:1])})")
        out.append(d)
    return {"crew": out, **({"problems": problems} if problems else {})}
