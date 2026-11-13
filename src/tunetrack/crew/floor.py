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

import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .. import config, roles, studio, takes
from ..model import LockTimeout, _os_lock, _os_unlock
from . import briefs, runners

TEXT_LIMIT = 400


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
            if self.launches >= self.max_launches:
                if self._limit_item and self._resolved(self._limit_item):  # the user said carry on
                    self.max_launches += self._limit_step
                    self._limit_item = None
                else:
                    self._limit_item = self._limit_item or self._blocker(
                        t, f"The session reached its limit of {self.max_launches} agent runs",
                        "Nothing more is started, so a loop can't run away with your usage. Resolve this to let "
                        "the crew continue for another round.", hold=False)
                    break
            if t.get("attempts", 0) >= self.max_attempts:
                self._blocker(t, f"{roles.get(t['seat']).title} tried \"{t['title']}\" {t['attempts']} times without finishing",
                              "Look at the task: rewrite it, give the seat to another agent, or take it yourself.")
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

    def _resolved(self, item: str) -> bool:
        return any(a["id"] == item and a["state"] == "resolved" for a in studio.read(self.session)["attention"])

    def _launchable(self) -> bool:
        st = studio.read(self.session)
        if self._limit_item and self._resolved(self._limit_item):
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
            self._blocker(task, f"{roles.get(seat).title} could not start", f"{type(e).__name__}: {e}")
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
                               text=last_text[:TEXT_LIMIT])
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
        why = ("it ran past the time limit" if rec.get("timed_out") else
               "it was stopped (the seat was put on hold, or the crew was stopped)" if rec.get("paused") else
               (errors[-1] if errors else ""))
        studio.log("run", self.session, seat=seat, task=tid, what="end", ok=bool(done.get("ok")), tools=tools,
                   error=why or None)
        if task["state"] != "working":
            return  # the agent moved its task itself (submitted, done, needs_you, failed): nothing to tidy
        if rec.get("paused"):  # stopped by the user, not by a fault: back in the queue, attempt not counted
            with studio._Board(self.session) as b:
                t = b.task(tid)
                t["state"], t["updated"] = "queued", round(time.time(), 3)
                t["attempts"] = max(0, t.get("attempts", 0) - 1)
                b.emit("task", id=tid, seat=seat, state="queued", title=t["title"], by="floor", reason="paused")
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
        if tools == 0:  # it never reached the studio at all: login, model name, CLI - trying again won't help
            self._blocker(task, f"{roles.get(seat).title} could not work: {why[:120]}",
                          "The agent ended without using a single studio tool, which usually means its app is not "
                          "logged in or the model name is wrong. Fix it or put someone else in the seat, then "
                          "resolve this to try again.")
            return
        with studio._Board(self.session) as b:
            t = b.task(tid)
            t["state"], t["updated"] = "queued", round(time.time(), 3)
            b.message(roles.YOU, seat, f"Your last run on {tid} ended early ({why}). What you had done is still in "
                                       f"your take; pick it up from there.", tid)
            b.emit("task", id=tid, seat=seat, state="queued", title=t["title"], by="floor", reason=why[:240])

    def _blocker(self, task: dict, title: str, detail: str, hold: bool = True) -> str:
        """Put a blocker on Needs You. hold: the task waits for the answer (and gets a fresh set of attempts)."""
        with studio._Board(self.session) as b:
            t = b.task(task["id"])
            now = round(time.time(), 3)
            a = {"id": b.new_id("N"), "kind": "blocker", "from": roles.YOU, "task": t["id"] if hold else None,
                 "title": title, "detail": detail, "options": [], "blocks": [], "state": "open", "answer": None,
                 "created": now, "resolved": None}
            b.st["attention"].append(a)
            if hold:
                t["state"], t["updated"], t["attempts"] = "needs_you", now, 0
                b.emit("task", id=t["id"], seat=t["seat"], state="needs_you", title=t["title"], by="floor")
            b.emit("attention", id=a["id"], state="open", what="blocker", sender="floor", title=title,
                   task=t["id"] if hold else None)
        return a["id"]

    def _route_takes(self) -> None:
        """A handed-in take needs ears: the Producer's agent gets an audition task, otherwise the user does.
        And an audition request on Needs You closes itself once the take has been accepted or returned."""
        index = {t["id"]: t for t in studio.take_index(self.path)}
        st = studio.read(self.session)
        stale = any(a["state"] == "open" and a.get("take") and index.get(a["take"], {}).get("state") != "submitted"
                    for a in st["attention"])
        fresh = any(t["state"] == "submitted" and index.get(t.get("take") or "", {}).get("state") == "submitted"
                    and t.get("routed") != [t["take"], index[t["take"]].get("submitted")] for t in st["tasks"])
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
            for t in b.st["tasks"]:
                tk = index.get(t.get("take") or "")
                if t["state"] != "submitted" or not tk or tk.get("state") != "submitted":
                    continue
                if t.get("routed") == [tk["id"], tk.get("submitted")]:
                    continue
                t["routed"] = [tk["id"], tk.get("submitted")]
                title = f"Audition take {tk['id']}: {t['title']}"
                detail = (f"{roles.get(t['seat']).title} handed in {tk['id']} for task {t['id']}.\n"
                          f"Their summary: {tk.get('summary') or '(none)'}\n"
                          f"Listen to it against the brief, then take_accept(\"{tk['id']}\") or "
                          f"take_reject(\"{tk['id']}\", reason).")
                if producer and producer["kind"] == "agent" and t["seat"] != "producer":
                    task = {"id": b.new_id("T"), "seat": "producer", "title": title, "detail": detail,
                            "state": "queued", "depends": [], "scope": {}, "by": "floor", "take": None, "result": "",
                            "created": now, "updated": now, "attempts": 0}
                    b.st["tasks"].append(task)
                    b.emit("task", id=task["id"], seat="producer", state="queued", title=title, by="floor")
                else:
                    a = {"id": b.new_id("N"), "kind": "decision", "from": t["seat"], "task": None, "title": title,
                         "detail": tk.get("summary") or "", "take": tk["id"],
                         "options": [{"id": "a", "label": "Accept", "take": tk["id"]}, {"id": "b", "label": "Return"}],
                         "blocks": [], "state": "open", "answer": None, "created": now, "resolved": None}
                    b.st["attention"].append(a)
                    b.emit("attention", id=a["id"], state="open", what="decision", sender=t["seat"], title=title,
                           take=tk["id"])


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
