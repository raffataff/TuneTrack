"""The floor manager hosted in a long-lived process (crew/host.py), seat pausing, recovery after a crashed manager,
the board read the studio UI draws, take auditions and the server routes for them. Agents are scripted stand-ins
(the "script" runner): no model is called."""
import asyncio
import json
import textwrap
import threading
import time
from pathlib import Path

import pytest

from tunetrack import api, roles, studio, takes
from tunetrack.crew import floor, host

HEAD = """
import json, re, sys
from tunetrack import api, studio, takes
prompt = sys.stdin.read()
task = re.search(r"Task (T\\d+)", prompt).group(1)
def say(**ev): print(json.dumps(ev), flush=True)
def tool(name): say(type="tool", name="mcp__tunetrack__" + name)
"""

COMPOSER = """
    tool("task_update"); studio.task_update(task, state="working")
    tool("write"); api.write("Piano", "D5:q F5 A5 D6", at="1:1")
    tool("take_submit"); takes.take_submit("Hook in bar 1, an octave up")
    say(type="text", text="Handed in.")
"""

PRODUCER = """
    tool("inbox")
    if "Plan the session" in prompt:
        tool("task_add"); studio.task_add("composer", "Write the hook", "Piano, bar 1", scope={"tracks": ["Piano"]})
        tool("task_update"); studio.task_update(task, state="done", result="One task: the hook.")
    else:
        take = re.search(r"take (tk\\d+)", prompt).group(1)
        tool("take_accept"); res = takes.take_accept(take)
        tool("task_update"); studio.task_update(task, state="done", result=f"Accepted {take}: {res['accepted']}")
"""

SLEEPER = """
    import time
    tool("task_update"); studio.task_update(task, state="working")
    time.sleep(60)
"""

FINISHER = """
    tool("task_update"); studio.task_update(task, state="done", result="ok")
"""

BOARD_SEAT_KEYS = {"seat", "title", "room", "purpose", "owns", "optional", "tier", "occupant", "label", "mode",
                   "notes", "paused", "presence", "task", "open_tasks"}


@pytest.fixture(autouse=True)
def _as_user():
    studio.seat_join(None)
    api.set_default_project(None)
    yield
    studio.seat_join(None)
    api.set_default_project(None)


@pytest.fixture(autouse=True)
def _no_hosts_left():
    yield
    studio.seat_join(None)
    host.stop_all()
    host._hosts.clear()


@pytest.fixture
def song(tmp_path):
    api.project_new("Night Drive", folder=str(tmp_path / "song"), tempo=96, key="D minor")
    api.track_add("Piano", "synth:pad")
    api.write("Piano", "D4:w | C4:w | Bb3:w | A3:w")
    return str(tmp_path / "song")


def agent(tmp_path, name: str, body: str) -> str:
    f = tmp_path / f"{name}.py"
    f.write_text(textwrap.dedent(HEAD) + textwrap.dedent(body), encoding="utf-8")
    return str(f)


def seat(name: str, script: str) -> None:
    studio.seat_assign(name, "agent", runner="script", model=script)


def piano(path) -> list:
    doc = json.loads((Path(path) / "project.json").read_text(encoding="utf-8"))
    return sorted((n[0], round(c["start"] + n[1], 3)) for t in doc["tracks"] if t["name"] == "Piano"
                  for c in t["clips"] for n in c["notes"])


def wait_for(pred, timeout: float = 10.0, step: float = 0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"timed out after {timeout}s waiting for {getattr(pred, '__name__', pred)}")


def task(tid: str) -> dict:
    return next(t for t in studio.read()["tasks"] if t["id"] == tid)


def presence(sid: str):
    return studio.read()["seats"][sid]["presence"]


class Running:
    """A Floor.run(keep_alive=True) in a thread, always stopped on the way out."""

    def __init__(self, f: floor.Floor, poll: float = 0.05):
        self.f = f
        self.error = None
        self.result = None

        def work():
            try:
                self.result = f.run(poll=poll, keep_alive=True)
            except BaseException as e:  # noqa: BLE001
                self.error = e

        self.thread = threading.Thread(target=work, daemon=True)

    def __enter__(self):
        self.thread.start()
        wait_for(lambda: floor.managed(self.f.session) or not self.thread.is_alive())
        return self

    def __exit__(self, *_):
        self.f.stop()
        self.thread.join(timeout=15)
        assert not self.thread.is_alive(), "the floor did not stop"


# ---- 1. one manager per session -------------------------------------------------------------------------------------

def test_one_floor_manager_per_session(song):
    assert floor.managed(song) is False
    with Running(floor.Floor(song)) as r:
        assert r.error is None and floor.managed(song) is True
        with pytest.raises(floor.FloorBusy):
            floor.Floor(song).run(poll=0.05)
    assert floor.managed(song) is False
    assert floor.Floor(song).run(poll=0.05)["launches"] == 0


# ---- 2. pausing a seat ----------------------------------------------------------------------------------------------

def test_a_paused_seat_is_not_started(song, tmp_path):
    seat("composer", agent(tmp_path, "composer", FINISHER))
    studio.task_add("composer", "Write the hook")
    studio.seat_pause("composer")
    f = floor.Floor(song)
    assert f.tick() == {"running": {}, "started": [], "waiting_on_people": [], "paused": ["T1"]}
    assert f.run(poll=0.05)["launches"] == 0
    comp = next(s for s in studio.studio_status()["seats"] if s["seat"] == "composer")
    assert comp["paused"] is True
    assert task("T1")["state"] == "queued"

    studio.seat_pause("composer", False)
    assert "paused" not in next(s for s in studio.studio_status()["seats"] if s["seat"] == "composer")
    assert floor.Floor(song).run(poll=0.05)["launches"] == 1
    assert task("T1")["state"] == "done"


def test_only_the_user_or_producer_pauses_a_seat(song):
    studio.seat_join("composer")
    with pytest.raises(PermissionError):
        studio.seat_pause("composer")
    studio.seat_join("producer")
    assert studio.seat_pause("composer") == {"seat": "composer", "paused": True}


# ---- 3./4. stopping a run -------------------------------------------------------------------------------------------

def _stopped_cleanly(tid: str, sid: str) -> None:
    t = task(tid)
    assert t["state"] == "queued"
    assert t["attempts"] == 0  # the stopped run is not counted
    assert studio.attention_list()["open"] == 0  # no blocker
    assert presence(sid) is None


def test_stop_seat_requeues_its_task_uncounted(song, tmp_path):
    seat("composer", agent(tmp_path, "composer", SLEEPER))
    studio.task_add("composer", "Write the hook")
    f = floor.Floor(song)
    with Running(f):
        wait_for(lambda: "composer" in f.active())
        assert f.stop_seat("mix") is False
        # hold the seat first (as host.seat_hold does), so the keep-alive floor does not start it again at once
        studio.seat_pause("composer")
        assert f.stop_seat("composer") is True
        wait_for(lambda: not f.active())
        wait_for(lambda: task("T1")["state"] == "queued")
        _stopped_cleanly("T1", "composer")
        assert f.launches == 1


def test_stop_ends_the_floor_and_requeues_uncounted(song, tmp_path):
    seat("composer", agent(tmp_path, "composer", SLEEPER))
    studio.task_add("composer", "Write the hook")
    f = floor.Floor(song)
    r = Running(f)
    try:
        r.__enter__()
        wait_for(lambda: "composer" in f.active())
        t0 = time.monotonic()
        f.stop()
        r.thread.join(timeout=10)
        assert not r.thread.is_alive() and time.monotonic() - t0 < 10
    finally:
        r.__exit__()
    assert r.error is None
    _stopped_cleanly("T1", "composer")
    assert floor.managed(song) is False


# ---- 5. recovery after a crashed manager ----------------------------------------------------------------------------

def test_a_new_floor_recovers_what_a_crashed_one_left(song, tmp_path):
    seat("composer", agent(tmp_path, "composer", FINISHER))
    studio.task_add("composer", "Write the hook")
    studio.task_update("T1", state="working")
    studio.seat_presence("composer", "online")
    studio.seat_assign("mix", "human")
    studio.task_add("mix", "Balance it")
    studio.task_update("T2", state="working")
    seq = studio.events(limit=1)["last"]

    out = floor.Floor(song).run(poll=0.05)
    assert out["launches"] == 1
    ev = studio.events(since=seq, limit=500)["events"]
    requeued = [e for e in ev if e["kind"] == "task" and e.get("id") == "T1" and e.get("state") == "queued"]
    assert requeued and "cut off" in requeued[0]["reason"]
    assert any(e["kind"] == "presence" and e.get("seat") == "composer" and e.get("state") == "cleared" for e in ev)
    starts = [e for e in ev if e["kind"] == "run" and e.get("what") == "start"]
    assert ev.index(requeued[0]) < ev.index(starts[0])  # recovered first, then run normally
    assert task("T1")["state"] == "done" and task("T1")["attempts"] == 1
    assert presence("composer") is None
    assert task("T2")["state"] == "working"  # a person's task is theirs
    assert not any(e.get("id") == "T2" for e in ev if e["kind"] == "task")


# ---- 6. the host module ---------------------------------------------------------------------------------------------

def _crew_threads() -> list:
    return [t for t in threading.enumerate() if t.name.startswith("crew-") and t.is_alive()]


def test_crew_start_hosts_the_session_until_stopped(song, tmp_path):
    seat("producer", agent(tmp_path, "producer", PRODUCER))
    seat("composer", agent(tmp_path, "composer", COMPOSER))
    st = host.crew_start(brief="A hopeful night-drive theme", session=song)
    assert st["running"] is True
    assert floor.managed(song) is True

    def finished():
        tasks = studio.read(song)["tasks"]
        return len(tasks) == 3 and all(t["state"] == "done" for t in tasks)
    wait_for(finished, timeout=40)
    assert (86, 3.0) in piano(song)  # the take was merged
    time.sleep(1.0)  # longer than the host's poll: it would have exited by now without keep-alive
    s = host.crew_state()
    assert s["running"] is True and s["launches"] == 3 and isinstance(s["usage"], dict)
    assert floor.managed(song) is True

    studio.seat_pause("producer")  # so the next plan task waits instead of starting a new round
    thread = host._hosts[str(studio.session_path(song))]["thread"]
    again = host.crew_start(brief="Another one", session=song)
    assert again["running"] is True
    assert host._hosts[str(studio.session_path(song))]["thread"] is thread and len(_crew_threads()) == 1
    plans = [t for t in studio.read(song)["tasks"] if t["title"] == "Plan the session"]
    assert len(plans) == 2 and plans[-1]["state"] == "queued"

    stopped = host.crew_stop(session=song)
    assert stopped["running"] is False
    assert floor.managed(song) is False
    assert not _crew_threads()


def test_crew_state_of_a_session_never_started(song):
    s = host.crew_state()
    assert s["running"] is False and s["launches"] == 0 and "elsewhere" not in s


def test_seat_hold_pauses_and_stops_the_run(song, tmp_path):
    seat("composer", agent(tmp_path, "composer", SLEEPER))
    studio.task_add("composer", "Write the hook")
    host.crew_start(session=song)
    wait_for(lambda: "composer" in host.crew_state()["active"])
    out = host.seat_hold("composer")
    assert out["paused"] is True and out["stopped_run"] is True
    assert studio.read()["seats"]["composer"]["paused"] is True
    wait_for(lambda: not host.crew_state()["active"])
    wait_for(lambda: task("T1")["state"] == "queued")
    _stopped_cleanly("T1", "composer")
    time.sleep(0.8)  # a poll and more: a held seat is not started again
    assert host.crew_state()["launches"] == 1

    res = host.seat_hold("composer", False)
    assert res["paused"] is False and "stopped_run" not in res
    assert studio.read()["seats"]["composer"]["paused"] is False
    wait_for(lambda: host.crew_state()["launches"] == 2)
    host.crew_stop()


def test_a_seated_process_cannot_run_the_crew(song):
    studio.seat_join("composer")
    with pytest.raises(PermissionError):
        host.crew_start(session=song)
    with pytest.raises(PermissionError):
        host.crew_stop(session=song)
    with pytest.raises(PermissionError):
        host.seat_hold("composer", session=song)
    assert not host._hosts


def test_crew_start_refuses_a_session_run_elsewhere(song):
    with Running(floor.Floor(song)):
        with pytest.raises(floor.FloorBusy):
            host.crew_start(session=song)
        s = host.crew_state(song)
        assert s["running"] is False and s.get("elsewhere") is True
    assert "elsewhere" not in host.crew_state(song)


# ---- 7. the board ---------------------------------------------------------------------------------------------------

def test_board_on_a_project_without_a_studio(song):
    assert not (Path(song) / "studio" / "state.json").exists()
    b = studio.board(song)
    assert [s["seat"] for s in b["seats"]] == list(roles.ORDER) and len(b["seats"]) == 9
    assert all(set(s) == BOARD_SEAT_KEYS for s in b["seats"])
    assert all(s["occupant"] is None and s["task"] is None and s["open_tasks"] == 0 for s in b["seats"])
    assert b["needs_you"] == [] and b["tasks"] == [] and b["takes"] == [] and b["messages"] == []


def test_board_shows_everything_the_ui_draws(song, tmp_path):
    seat("composer", agent(tmp_path, "composer", FINISHER))
    studio.task_add("composer", "Write the hook", "Piano, bar 1")
    studio.task_add("mix", "Balance it", "Everything")
    studio.task_update("T2", state="done", result="balanced")
    studio.ask_human("Which ending?", "Pick one for the loop", options=["Fade", {"label": "Hard stop", "detail": "x"}])
    studio.message_send("composer", "Keep it under 8 voices")
    takes.take_new("Alt hook", "T1", "composer", use=False)
    studio.seat_pause("composer")

    b = studio.board()
    assert [s["seat"] for s in b["seats"]] == list(roles.ORDER)
    assert all(set(s) == BOARD_SEAT_KEYS for s in b["seats"])
    comp = next(s for s in b["seats"] if s["seat"] == "composer")
    assert comp["occupant"]["kind"] == "agent" and comp["paused"] is True and comp["task"] == "T1"
    assert comp["open_tasks"] == 1
    item = b["needs_you"][0]
    assert item["title"] == "Which ending?" and item["detail"] == "Pick one for the loop"
    assert [o["label"] for o in item["options"]] == ["Fade", "Hard stop"]
    by_id = {t["id"]: t for t in b["tasks"]}
    assert set(by_id) == {"T1", "T2"} and by_id["T2"]["state"] == "done"
    assert by_id["T1"]["detail"] == "Piano, bar 1" and by_id["T2"]["detail"] == "Everything"
    assert [t["id"] for t in b["takes"]] == ["tk1"]
    msg = b["messages"][-1]
    assert msg["from"] == roles.YOU and msg["to"] == "composer" and msg["text"] == "Keep it under 8 voices"


# ---- 8. take auditions ----------------------------------------------------------------------------------------------

def test_take_audition_renders_session_and_take(song):
    takes.take_new("Alt", use=True)
    api.write("Piano", "F4:w", at="2:1")
    takes.take_use(None)
    a = takes.take_audition()
    b = takes.take_audition("tk1")
    for r in (a, b):
        assert {"file", "rev", "lufs", "warnings"} <= set(r)
        assert Path(r["file"]).is_file() and r["file"].lower().endswith(".wav")
        assert isinstance(r["warnings"], list)
    assert a["take"] is None and b["take"] == "tk1"
    assert Path(a["file"]).resolve() != Path(b["file"]).resolve()


# ---- 9. the server --------------------------------------------------------------------------------------------------

def _asgi(app, method, path, headers=None, body=b"", port=7373):
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "headers": hdrs,
             "server": ("127.0.0.1", port), "client": ("127.0.0.1", 5555), "root_path": ""}
    sent = []
    done = False

    async def receive():
        nonlocal done
        if done:
            return {"type": "http.disconnect"}
        done = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, data


def test_server_serves_the_board_and_crew(song):
    from tunetrack import server
    status, data = _asgi(server.app, "GET", "/api/state", {"host": "127.0.0.1:7373"})
    assert status == 200
    st = json.loads(data)
    assert Path(st["project"]).resolve() == Path(song).resolve()
    assert [s["seat"] for s in st["board"]["seats"]] == list(roles.ORDER)
    assert st["crew"]["running"] is False

    good = {"host": "127.0.0.1:7373", "content-type": "application/json", "x-tunetrack-token": server.TOKEN}
    for fn in ("crew_state", "board"):
        status, data = _asgi(server.app, "POST", "/api/call", good, json.dumps({"fn": fn, "args": {}}).encode())
        assert status == 200, data
    assert json.loads(data)["result"]["seats"][0]["seat"] == roles.ORDER[0]

    want = {"board", "crew_start", "crew_stop", "crew_state", "seat_hold", "crew_detect", "take_audition"}
    assert want <= set(server.STUDIO)
    assert not set(server.STUDIO) & set(server.CALLABLE)
