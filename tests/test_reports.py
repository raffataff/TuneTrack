"""Reports (documents longer than talkback) and the crew scorecard counted from the activity log."""
import pytest

from tunetrack import api, config, studio, takes
from tunetrack.crew import scorecard


@pytest.fixture(autouse=True)
def _as_user(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "library")  # crew history goes to a scratch library
    studio.seat_join(None)
    api.set_default_project(None)
    yield
    studio.seat_join(None)
    api.set_default_project(None)


@pytest.fixture
def song(tmp_path):
    api.project_new("Night Drive", folder=str(tmp_path / "song"), tempo=96, key="D minor")
    api.track_add("Piano", "synth:pad")
    api.write("Piano", "D4:w | C4:w | Bb3:w | A3:w")
    return str(tmp_path / "song")


def test_a_seat_files_a_report_and_the_user_reads_all_of_it(song):
    body = "# Session review\n\n" + "The composer held the motif. " * 200
    studio.seat_join("producer")
    res = studio.report_write("Session review", body, "review")
    studio.seat_join(None)
    assert res["report"] == "R1"
    [r] = studio.report_list()["reports"]
    assert (r["title"], r["kind"], r["by"], r["chars"]) == ("Session review", "review", "producer", len(body))
    assert studio.report_read("r1")["body"] == body  # nothing cut off
    assert studio.board()["reports"][0]["id"] == "R1"
    ev = [e for e in studio.events(limit=50)["events"] if e["kind"] == "report"]
    assert ev[0]["id"] == "R1" and ev[0]["sender"] == "producer"


def test_a_report_is_replaced_by_its_author_or_a_lead_only(song):
    studio.seat_join("mix")
    studio.report_write("Mix notes", "first", "note")
    studio.report_write("Mix notes", "second", "note", report="R1")
    studio.seat_join("composer")
    with pytest.raises(PermissionError):
        studio.report_write("Mix notes", "not mine", "note", report="R1")
    studio.seat_join(None)
    studio.report_write("Mix notes, edited", "third", "note", report="R1")
    assert studio.report_read("R1")["body"] == "third" and len(studio.report_list()["reports"]) == 1
    with pytest.raises(ValueError):
        studio.report_write("", "x")
    with pytest.raises(KeyError):
        studio.report_read("R9")


def _run(seat, agent, tools=0, ok=True, error=None):
    studio.log("run", seat=seat, task="T1", what="start", agent=agent)
    for _ in range(tools):
        studio.log("run", seat=seat, task="T1", what="tool", tool="write")
    studio.log("run", seat=seat, task="T1", what="end", ok=ok, tools=tools, error=error)


def _take(seat, edit):
    studio.seat_join(seat)
    tk = takes.take_new()["take"]
    edit()
    takes.take_submit("made it", tk)
    studio.seat_join(None)
    api.set_default_project(None)
    return tk


def test_the_scorecard_counts_runs_takes_and_asks_per_seat_and_agent(song):
    studio.seat_assign("composer", "agent", runner="kilo", model="free-a")
    _run("composer", "kilo · free-a", tools=3)
    _run("composer", "kilo · free-a", ok=False, error="Too Many Requests: Free model usage limit reached")
    tk = _take("composer", lambda: api.write("Piano", "D5:w", at="1:1"))
    takes.take_reject(tk, "Bar 1 loses the motif")
    studio.seat_join("composer")
    takes.take_submit("second go", tk)
    studio.ask_human("Which ending?", kind="decision", options=["up", "down"])
    studio.seat_join(None)
    takes.take_accept(tk)
    studio.seat_assign("composer", "agent", runner="codex", model="paid-b")
    _run("composer", "codex · paid-b", tools=2, ok=False, error="exit 3")
    tk2 = _take("composer", lambda: api.write("Piano", "F5:w", at="2:1"))
    takes.take_accept(tk2)

    card = scorecard.crew_scorecard()
    a, b = card["rows"]
    assert (a["agent"], a["runs"], a["ok"], a["out_of_usage"], a["failed"], a["tool_calls"]) == ("kilo · free-a", 2, 1, 1, 0, 3)
    assert (a["handed_in"], a["returned"], a["accepted"], a["first_time"], a["asked_you"]) == (1, 1, 1, 0, 1)
    assert (b["agent"], b["runs"], b["failed"], b["handed_in"], b["accepted"], b["first_time"]) == ("codex · paid-b", 1, 1, 1, 1, 1)
    assert card["totals"]["runs"] == 3 and card["totals"]["accepted"] == 2
    assert "| Composer | kilo · free-a | 2 (1) | 1 |" in scorecard.as_markdown(card)


def test_a_review_files_the_scorecard_remembers_it_and_tasks_the_producer(song, tmp_path):
    with pytest.raises(ValueError, match="Nothing to review"):
        scorecard.crew_review()
    studio.seat_assign("composer", "agent", runner="kilo", model="free-a")
    _run("composer", "kilo · free-a", tools=4)
    out = scorecard.crew_review()
    assert out["scorecard"] == "R1" and "No agent sits in the Producer seat" in out["note"]
    assert "| Composer | kilo · free-a |" in studio.report_read("R1")["body"]

    studio.seat_assign("producer", "agent", runner="kilo", model="free-a")
    _run("composer", "kilo · free-a", tools=1)
    out = scorecard.crew_review()
    assert out["scorecard"] == "R1" and len(studio.report_list()["reports"]) == 1  # the scorecard is replaced, not piled up
    task = studio.task_list("producer")["tasks"][0]
    assert task["id"] == out["task"] and "report_write" in task["detail"] and "kilo · free-a" in task["detail"]

    [h] = scorecard.crew_history("composer")["history"]  # one session, counted once however often it is reviewed
    assert (h["agent"], h["sessions"], h["runs"], h["tool_calls"]) == ("kilo · free-a", 1, 2, 5)
    assert scorecard.crew_history("mix")["history"] == []


def test_agents_slips_at_the_tool_door_are_understood(song):
    assert api.project_info("none")["name"] == "Night Drive"  # project="none" means "the one I am in"
    studio.seat_join("composer")
    tk = takes.take_new()["take"]
    assert takes.take_use("None")["editing"] == "session"
    takes.take_use(tk)
    with pytest.raises(ValueError, match='state=\\\\?"done\\\\?"'):
        takes.take_submit("nothing to do")
