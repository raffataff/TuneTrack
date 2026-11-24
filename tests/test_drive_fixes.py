"""Fixes from The Drive crew test (docs/THE_DRIVE_FIXES.md): parts A-C that live outside the isolated modules."""
import pytest

from tunetrack import api, studio, takes
from tunetrack.model import Project


@pytest.fixture(autouse=True)
def _as_user():
    studio.seat_join(None)
    api.set_default_project(None)
    yield
    studio.seat_join(None)
    api.set_default_project(None)


@pytest.fixture
def song(tmp_path):
    api.project_new("Drive", folder=str(tmp_path / "song"), tempo=96, key="D minor")
    api.track_add("Pads", "synth:pad")
    api.write("Pads", "D4:q F4 A4 D5 | C4:q E4 G4 C5")
    api.track_add("Verb", kind="bus")
    return str(tmp_path / "song")


def _poke(path, fn):
    """Change the saved project behind the tools' backs (a value set before the checks existed)."""
    p = Project.load(path)
    fn(p)
    p.save()


# ---- A1 pan scale --------------------------------------------------------------------------------------------------

def test_pan_in_percent_is_read_as_percent(song):
    res = api.track_update("Pads", pan=-30)
    assert api.load().track("Pads").pan == pytest.approx(-0.3)
    assert "-0.3" in res["notes"][0]
    with pytest.raises(ValueError, match="-1 \\(left\\)"):
        api.track_update("Pads", pan=500)
    api.track_add("Lead", "synth:pad", pan=15)
    assert api.load().track("Lead").pan == pytest.approx(0.15)
    api.automation("Pads", "pan", [["1:1", -50], ["2:1", 0.5]])
    assert [x for _b, x in api.load().track("Pads").automation["pan"]] == [pytest.approx(-0.5), 0.5]


# ---- A2 built-in effect settings checked when set ------------------------------------------------------------------

def test_builtin_effect_settings_are_checked_when_set(song):
    api.fx_add("Verb", "builtin:reverb")
    with pytest.raises(ValueError, match="decay=6500.*0.1-30 s"):
        api.fx_update("Verb", 0, params={"decay": 6500})
    api.fx_add("Pads", "builtin:delay")
    with pytest.raises(KeyError, match="time_ms"):
        api.fx_update("Pads", 0, params={"time_ms": 250})
    with pytest.raises(ValueError, match="time=375.*beats"):
        api.fx_update("Pads", 0, params={"time": 375})
    with pytest.raises(ValueError, match="feedback"):
        api.fx_add("Pads", {"type": "builtin", "name": "delay", "params": {"feedback": 1.2}})
    with pytest.raises(KeyError, match="nope"):
        api.track_update("Pads", instrument_params={"nope": 1})
    api.fx_update("Verb", 0, params={"decay": 6.5, "predelay": 30})  # fine values still go in


# ---- A3 a track that would drop out blocks hand-in and accept ------------------------------------------------------

def test_a_take_that_drops_a_track_is_not_handed_in_or_accepted(song):
    api.fx_add("Pads", "builtin:delay")
    studio.seat_join("mix")
    t = takes.take_new("Echo")["take"]
    api.track_update("Pads", volume_db=-3)
    take_dir = api.load().path

    def bad(p):
        p.track("Pads").fx[0].params["time_ms"] = 250
    _poke(take_dir, bad)
    with pytest.raises(ValueError, match="TRACK MISSING FROM THE MIX.*Pads"):
        takes.take_submit("echo")
    _poke(take_dir, lambda p: p.track("Pads").fx[0].params.pop("time_ms"))
    takes.take_submit("echo")
    _poke(take_dir, bad)  # broken again after hand-in: accept refuses too
    studio.seat_join(None)
    api.set_default_project(None)
    with pytest.raises(ValueError, match="TRACK MISSING FROM THE MIX"):
        takes.take_accept(t, session=song)


def test_mix_problems_name_the_track(song):
    _poke(song, lambda p: p.track("Pads").instrument.params.update(bogus=1))
    assert any("Pads" in x for x in api.mix_problems(song))


# ---- A4 delivery check ---------------------------------------------------------------------------------------------

def test_brief_lengths_are_read():
    p = Project(name="x", tempo=72, time_sig=[6, 8])
    lo, hi = api._brief_seconds(p, "about 2 minutes")
    assert lo < 120 < hi and not lo <= 64 <= hi
    assert api._brief_seconds(p, "1:30") == (pytest.approx(76.5), pytest.approx(103.5))
    assert api._brief_seconds(p, "60-90 seconds")[1] == pytest.approx(94.5)
    assert api._brief_seconds(p, "big and loud") is None


def test_export_reports_delivery(song, tmp_path):
    api.project_set(brief={"length": "about 2 minutes", "use": "loops seamlessly"})
    api.track_update("Pads", pan=-1)
    rep = api.render(out=str(tmp_path / "out" / "cue.wav"))
    d = rep["delivery"]
    assert not d["ok"]
    text = " ".join(d["warnings"])
    assert "length" in text and "loop" in text and "left is" in text
    rep = api.render()  # a render that isn't an export has no delivery check
    assert "delivery" not in rep


# ---- B1 needs_you only with a ticket -------------------------------------------------------------------------------

def test_needs_you_is_not_set_by_hand_and_a_stuck_one_is_requeued(song):
    from tunetrack.crew import floor
    tid = studio.task_add("composer", "Write the hook", "bar 1")["id"]
    studio.seat_join("producer")
    with pytest.raises(ValueError, match="ask_human"):
        studio.task_update(tid, state="needs_you")
    studio.seat_join(None)
    with studio._Board(song) as b:  # how The Drive got there: needs_you with nothing on Needs You
        b.task(tid)["state"] = "needs_you"
    assert not studio.ready_tasks(song)
    floor.Floor(song)._unstick(set())
    assert [t["id"] for t in studio.ready_tasks(song)] == [tid]
    # one that is asked about stays put
    studio.seat_join("composer")
    studio.ask_human("Which hook?", task=tid)
    studio.seat_join(None)
    floor.Floor(song)._unstick(set())
    assert not studio.ready_tasks(song)


# ---- B3 the user's sign-off note goes back to the seat -------------------------------------------------------------

def test_a_returned_take_carries_the_users_note(song):
    tid = studio.task_add("composer", "Write the hook", "bar 1")["id"]
    studio.seat_join("composer")
    t = takes.take_new("Hook", tid)["take"]
    api.write("Pads", "A4:w", at="1:1")
    takes.take_submit("hook")
    studio.seat_join(None)
    api.set_default_project(None)
    d = takes._take_dir(t, song)
    meta = takes._meta(d)
    takes._meta(d, signed={"by": "you", "at": 1, "hand_in": meta["submitted"], "note": "I prefer A-Session",
                           "changed": []})
    takes.take_reject(t, "melody wanders", session=song)
    msgs = studio.inbox("composer", mark_read=False, session=song)["messages"]
    assert any("melody wanders" in m["text"] and "The user said: I prefer A-Session" in m["text"] for m in msgs)


# ---- B4 the user's words reach the project and every task ----------------------------------------------------------

def test_kickoff_stores_the_request_and_tasks_name_it(song):
    from tunetrack.crew import briefs, floor
    floor.kickoff("About 2 minutes, loops seamlessly")
    assert api.load(song).brief["request"] == "About 2 minutes, loops seamlessly"
    task = {"id": "T9", "seat": "review", "title": "Review", "detail": "check it"}
    text = briefs.task_prompt(task, studio.studio_status(song), request=api.load(song).brief["request"])
    assert "The user asked for: About 2 minutes, loops seamlessly" in text


# ---- B5 project_set checks every field before changing any ---------------------------------------------------------

def test_project_set_names_the_bad_field_and_changes_nothing(song):
    with pytest.raises(ValueError, match="loop:.*1:1-25:1"):
        api.project_set(loop="24:1", brief={"use": "menu"})
    assert "use" not in api.load().brief
    with pytest.raises(ValueError, match="tempo:.*loop:"):
        api.project_set(tempo="fast", loop="24:1")
    api.project_set(loop="1:1-25:1", brief={"use": "menu"})
    assert api.load().brief["use"] == "menu" and api.load().loop["end"] == 24 * 4


# ---- B6 a final listen when the user accepts the last take ---------------------------------------------------------

def test_user_accepting_the_last_take_gets_a_final_listen(song, tmp_path):
    from tunetrack.crew import floor
    studio.seat_assign("producer", "agent", runner="script", model=str(tmp_path / "none.py"))
    studio.seat_join("composer")
    t = takes.take_new("Hook")["take"]
    api.write("Pads", "A4:w", at="1:1")
    takes.take_submit("hook")
    studio.seat_join(None)
    api.set_default_project(None)
    takes.take_accept(t, session=song)
    f = floor.Floor(song)
    f._final_listen()
    f._final_listen()  # once per accepted revision
    fl = [x for x in studio.read(song)["tasks"] if x["title"] == "Final listen"]
    assert len(fl) == 1 and fl[0]["seat"] == "producer"
    studio.task_update(fl[0]["id"], state="done", result="fine")
    f._final_listen()
    assert len([x for x in studio.read(song)["tasks"] if x["title"] == "Final listen"]) == 1


# ---- B7 / A4 Producer brief lines ----------------------------------------------------------------------------------

def test_producer_brief_has_the_new_lines():
    from tunetrack.crew import briefs
    assert "seat's tools can change" in briefs.PRODUCER and "requirement" in briefs.PRODUCER
    assert "delivery" in briefs.PRODUCER and "1:1-25:1" in briefs.PRODUCER


# ---- C1 / C2 string arguments as meant -----------------------------------------------------------------------------

def test_tool_wrapper_strips_quotes_and_none(song):
    from tunetrack import mcp_server
    api.set_default_project(song)
    mcp_server.track_update(track='"Pads"', volume_db=-3, project="none")
    assert api.load(song).track("Pads").volume_db == -3
    mcp_server.fx_add(track="'Pads'", device='"builtin:eq"', project="null")
    assert api.load(song).track("Pads").fx[0].name == "eq"


# ---- C3 end bar is inclusive for reading ---------------------------------------------------------------------------

def test_a_bare_end_bar_includes_that_bar(song):
    got = api.notes_read("Pads", start="1", end="2", format="list")["clips"][0]["notes"]
    assert any(n[1].startswith("2:") for n in got)
    got = api.notes_read("Pads", start="1", end="2:1", format="list")["clips"][0]["notes"]
    assert not any(n[1].startswith("2:") for n in got)
    assert "bars 1-2" in api.score(start="1", end="2")["score"]


# ---- C4 a beat past the bar is an error ----------------------------------------------------------------------------

def test_a_beat_past_the_bar_raises(song):
    api.project_set(time_sig="6/8")
    with pytest.raises(ValueError, match="6/8 has 3 beats per bar"):
        api.markers(add=[{"at": "1:5", "name": "x"}])
    api.markers(add=[{"at": "1:3.5", "name": "y"}])


# ---- C6 / C10 run logs and quiet runs ------------------------------------------------------------------------------

class _FakeRun:
    pid = 1

    def __init__(self):
        self.stopped = False

    def events(self):
        return iter(())

    def stop(self):
        self.stopped = True


def test_each_run_has_its_own_log_and_a_quiet_run_is_flagged(song, tmp_path):
    import threading
    import time
    from tunetrack.crew import floor
    logs = []

    def start(launch):
        logs.append(launch.log)
        return _FakeRun()
    studio.seat_assign("composer", "agent", runner="script", model=str(tmp_path / "none.py"))
    tid = studio.task_add("composer", "Write the hook", "bar 1")["id"]
    f = floor.Floor(song, start=start)
    f._launch(next(t for t in studio.read(song)["tasks"] if t["id"] == tid), studio.read(song))
    assert logs and logs[0].endswith(".jsonl") and len(logs[0].rsplit("-", 3)) == 4
    # a run that has said nothing for longer than QUIET_AFTER is shown, not stopped
    old = time.time() - floor.QUIET_AFTER - 5
    run = _FakeRun()
    f._runs = {"mix": {"run": run, "task": tid, "started": old, "last_event": old, "take": None,
                       "thread": threading.current_thread()}}
    f.tick()
    assert f.active()["mix"]["quiet"] and not run.stopped
    evs = studio.events(session=song)
    assert any(e.get("what") == "quiet" for e in (evs.get("events") or evs.get("items") or []))
