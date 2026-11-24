"""The Drive session fixes (set C): pad register check, scorecard counts, review lessons, seat tools."""
import pytest

from tunetrack import api, config, roles, studio
from tunetrack.crew import craft, scorecard


@pytest.fixture(autouse=True)
def _as_user(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "library")
    studio.seat_join(None)
    api.set_default_project(None)
    yield
    studio.seat_join(None)
    api.set_default_project(None)


@pytest.fixture
def song(tmp_path):
    api.project_new("The Drive", folder=str(tmp_path / "song"), tempo=96, key="F major")
    api.track_add("Drone", "synth:pad", role="pad")
    api.write("Drone", "(F2 A2):w | (F2 A2):w | (F2 A2):w | (F2 A2):w")
    return str(tmp_path / "song")


def test_a_low_pad_gets_no_extreme_register_warning(song):
    info = api.analyze(images=False)["tracks"]["Drone"]
    assert not any("extreme register" in i for i in info.get("issues", []))
    assert "quiet a check" in craft.SOUND


def _run(seat, tools, failed, ok=True):
    studio.log("run", seat=seat, task="T1", what="start", agent="kilo · a")
    for i in range(tools):
        studio.log("run", seat=seat, task="T1", what="tool", tool="write", **({"ok": False} if i < failed else {}))
    studio.log("run", seat=seat, task="T1", what="end", ok=ok, tools=tools)


def test_scorecard_counts_only_ask_human_and_runs_with_mostly_failed_tools(song):
    studio.seat_assign("composer", "agent", runner="kilo", model="a")
    _run("composer", tools=4, failed=1)  # ok
    _run("composer", tools=4, failed=2)  # half failed: not ok
    studio.seat_join("composer")
    studio.ask_human("Which ending?", kind="decision", options=["up", "down"])
    studio.seat_join(None)
    studio.log("attention", state="open", what="signoff", sender="composer", title="Sign off the take")
    [r] = scorecard.crew_scorecard()["rows"]
    assert (r["runs"], r["ok"], r["failed"], r["tool_errors"], r["asked_you"]) == (2, 1, 1, 3, 1)
    md = scorecard.as_markdown(scorecard.crew_scorecard())
    assert "Tool errors" in md and "| 1 | 3 |" in md


def test_review_task_keeps_tool_errors_out_of_lessons(song):
    studio.seat_assign("producer", "agent", runner="kilo", model="a")
    _run("composer", tools=1, failed=0)
    out = scorecard.crew_review()
    task = next(t for t in studio.task_list("producer")["tasks"] if t["id"] == out["task"])
    assert "not about a tool error or a missing tool" in task["detail"]


def test_a_seat_cannot_list_other_projects():
    assert "project_list" not in roles.tools_for("composer")
    assert all("project_list" not in r.tools for r in roles.ROLES.values())
    assert roles.tools_for(None) is None  # the user and unseated clients keep every tool
