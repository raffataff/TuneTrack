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
    path, _, query = path.partition("?")
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": query.encode(), "headers": hdrs,
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


# ---- 10. a take open on the Desk --------------------------------------------------------------------------------------

GET = {"host": "127.0.0.1:7373"}


def _call(fn, args=None, **body):
    from tunetrack import server
    good = {**GET, "content-type": "application/json", "x-tunetrack-token": server.TOKEN}
    status, data = _asgi(server.app, "POST", "/api/call", good, json.dumps({"fn": fn, "args": args or {}, **body}).encode())
    return status, json.loads(data)


def _state(query=""):
    from tunetrack import server
    status, data = _asgi(server.app, "GET", "/api/state" + query, GET)
    assert status == 200
    return json.loads(data)


def test_take_cut_is_for_the_desk_and_picks_up_where_a_task_left_off(song):
    studio.seat_assign("composer", "human")
    t = studio.task_add("composer", "Write the hook", "Piano, bar 2")["id"]
    status, res = _call("take_cut", {"task": t})
    assert status == 200 and res["result"] == {"take": "tk1", "continued": False}
    meta = json.loads((Path(song) / "takes" / "tk1" / "take.json").read_text(encoding="utf-8"))
    assert (meta["seat"], meta["task"], meta["state"]) == ("composer", t, "open")
    assert task(t)["state"] == "working" and task(t)["take"] == "tk1"
    assert api._default_project is None  # the server's own tools still point at the session

    assert _call("take_cut", {"task": t})[1]["result"] == {"take": "tk1", "continued": True}
    assert _call("take_cut", {"name": "Sketch"})[1]["result"] == {"take": "tk2", "continued": False}
    assert json.loads((Path(song) / "takes" / "tk2" / "take.json").read_text(encoding="utf-8"))["seat"] == roles.YOU
    assert _call("take_cut", {"task": "T99"})[0] == 400


def test_the_desk_edits_plays_and_undoes_the_take_it_has_open(song):
    _call("take_cut", {"seat": "composer"})
    before = piano(song)
    session = _state()
    assert session["take"] is None and session["version"] == session["session_version"]

    status, res = _call("clip_create", {"track": "Piano", "start": 16, "length": 4}, take="tk1", session=song)
    assert status == 200, res
    clip = res["result"]["clip"]
    assert _call("clip_set", {"clip": clip, "notes": [[77, 0, 1, 90]]}, take="tk1")[0] == 200
    take_dir = Path(song) / "takes" / "tk1"
    assert piano(song) == before and (77, 16.0) in piano(take_dir)

    st = _state("?take=tk1")
    assert st["take"]["id"] == "tk1" and st["take"]["seat"] == "composer" and st["take"]["state"] == "open"
    assert st["take"]["changes"] == 1 and st["take"]["session_moved"] is False
    assert st["version"].startswith("tk1@") and st["version"] != st["session_version"]
    assert any(c["id"] == clip for t in st["doc"]["tracks"] for c in t["clips"])
    assert Path(st["project"]).resolve() == Path(song).resolve() and st["board"]["takes"][0]["id"] == "tk1"

    assert _call("undo", take="tk1")[0] == 200  # the take's own history, not the session's
    assert (77, 16.0) not in piano(take_dir) and piano(song) == before
    assert _call("redo", take="tk1")[0] == 200
    status, res = _call("preview", take="tk1")
    assert status == 200 and Path(res["result"]["files"][0]).resolve().is_relative_to(take_dir.resolve())

    api.write("Piano", "G4:w", at="3:1")  # the session moves on under the take
    assert _state("?take=tk1")["take"]["session_moved"] is True
    assert _call("take_sync", {"take": "tk1"})[1]["result"]["synced"] is True
    assert _state("?take=tk1")["take"]["session_moved"] is False

    status, res = _call("take_submit", {"take": "tk1", "summary": "A high F in bar 5"})
    assert status == 200 and res["result"]["state"] == "submitted"
    assert _state("?take=tk1")["take"]["state"] == "submitted"
    assert _call("take_accept", {"take": "tk1"})[1]["result"]["accepted"] is True
    assert (77, 16.0) in piano(song)


def test_the_desk_writes_the_chord_track_in_the_session_or_in_its_take(song):
    # What the chord lane sends: one harmony() call per edit, positions in beats.
    def chords(d):
        doc = json.loads((Path(d) / "project.json").read_text(encoding="utf-8"))
        return [(c["chord"], c["at"], c["len"]) for c in doc["chord_track"]]

    assert _call("harmony", {"progression": "Dm", "at": 0, "end": 4, "chord_length": 4})[0] == 200
    assert _call("harmony", {"progression": "Gm A7", "at": 4, "end": 8, "chord_length": 2})[0] == 200  # two share a bar
    assert chords(song) == [("Dm", 0, 4), ("Gm", 4, 2), ("A7", 6, 2)]
    assert _call("harmony", {"progression": "A7", "at": 6, "end": 12, "chord_length": 6})[0] == 200    # edge dragged out
    assert _call("harmony", {"clear": True, "at": 8, "end": 12})[0] == 200                             # and back in
    assert chords(song) == [("Dm", 0, 4), ("Gm", 4, 2), ("A7", 6, 2)]
    status, res = _call("harmony", {"progression": "Xq9z", "at": 0, "end": 4})
    assert status == 400 and "Xq9z" in res["error"] and chords(song)[0] == ("Dm", 0, 4)  # refused, nothing changed

    _call("take_cut", {"seat": "composer"})
    assert _call("harmony", {"progression": "Bb", "at": 0, "end": 4, "chord_length": 4}, take="tk1", session=song)[0] == 200
    assert chords(Path(song) / "takes" / "tk1")[0] == ("Bb", 0, 4) and chords(song)[0] == ("Dm", 0, 4)
    diff = takes.take_diff("tk1")
    assert [c["kind"] for c in diff["changes"]] == ["form"] and "outside_seat" not in diff  # the Composer's to change


def test_the_desk_names_a_take_of_the_open_session_by_id_only(song, tmp_path):
    _call("take_cut")
    before = piano(song)
    for bad in ("tk9", "../tk1", "takes/tk1", str(Path(song) / "takes" / "tk1"), "tk1/..", "TK1 "):
        status, res = _call("track_update", {"track": "Piano", "volume_db": -3}, take=bad)
        if bad == "TK1 ":  # ids are case-blind, as everywhere else
            assert status == 200, res
            continue
        assert status == 409 and res["conflict"], (bad, res)
        st = _state("?take=" + bad.replace("\\", "/"))
        assert st["take"] is None and "take_error" in st and st["version"] == st["session_version"]
    assert piano(song) == before
    doc = json.loads((Path(song) / "project.json").read_text(encoding="utf-8"))
    assert doc["tracks"][0].get("volume_db", 0) == 0

    # the page still thinks another project is open: refuse instead of editing this session's take of the same id
    status, res = _call("track_update", {"track": "Piano", "volume_db": -6}, take="tk1", session=str(tmp_path / "other"))
    assert status == 409 and res["conflict"]
    # board calls ignore the desk's take; calls with no project (the library) pass through
    assert _call("board", take="tk1")[0] == 200 and _call("synths_list", take="tk1")[0] == 200


def test_the_event_stream_follows_the_open_take(song):
    from tunetrack import server
    _call("take_cut")
    msg, seq = server._pulse(Path(song), 0, "tk1")
    assert msg["take"]["id"] == "tk1" and msg["take"]["version"].startswith("tk1@")
    _call("track_update", {"track": "Piano", "volume_db": -3}, take="tk1")
    again, _ = server._pulse(Path(song), seq, "tk1")
    assert again["take"]["version"] != msg["take"]["version"] and again["version"] == msg["version"]
    assert server._pulse(Path(song), seq, "tk7")[0]["take"] == {"id": "tk7", "version": None}
    assert "take" not in server._pulse(Path(song), seq)[0]

    status, data = _asgi(server.app, "GET", "/api/version?take=tk1", GET)
    v = json.loads(data)
    assert status == 200 and v["version"] == again["take"]["version"] and v["session_version"] == again["version"]
    assert json.loads(_asgi(server.app, "GET", "/api/version?take=nope", GET)[1])["version"] is None

