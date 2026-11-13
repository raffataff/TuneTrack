"""The floor manager, driven by scripted stand-in agents (the "script" runner): no model is called."""
import json
import textwrap
from pathlib import Path

import pytest

from tunetrack import api, studio, takes
from tunetrack.crew import briefs, floor

# What every stand-in agent starts with: it is a separate process, seated by its environment, like a real one.
HEAD = """
import json, re, sys
from tunetrack import api, studio, takes
prompt = sys.stdin.read()
task = re.search(r"Task (T\\d+)", prompt).group(1)
def say(**ev): print(json.dumps(ev), flush=True)
def tool(name): say(type="tool", name="mcp__tunetrack__" + name)
"""


@pytest.fixture(autouse=True)
def _as_user():
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


def test_a_session_runs_from_brief_to_accepted_take(song, tmp_path):
    seat("producer", agent(tmp_path, "producer", PRODUCER))
    seat("composer", agent(tmp_path, "composer", COMPOSER))
    floor.kickoff("A hopeful night-drive theme")
    seen = []
    out = floor.Floor(song, on_event=lambda s, ev: seen.append((s, ev["type"]))).run(poll=0.05)

    assert out["launches"] == 3  # plan, compose, audition
    assert out["tasks"] == [] and out["needs_you"] == [] and out["takes"] == []
    assert (86, 3.0) in piano(song) and (62, 0.0) not in piano(song)  # the take was merged into the session
    done = {t["title"]: t for t in studio.task_list(state="done")["tasks"]}
    assert set(done) == {"Plan the session", "Write the hook", "Audition take tk1: Write the hook"}
    assert ("composer", "tool") in seen and ("producer", "done") in seen
    kinds = [(e["kind"], e.get("what") or e.get("state")) for e in studio.events(limit=500)["events"]]
    assert ("run", "start") in kinds and ("run", "tool") in kinds and ("take", "accepted") in kinds
    assert all(s.get("presence") is None for s in studio.studio_status()["seats"])  # nobody left "online"
    assert len(list((Path(song) / "studio" / "logs").glob("*.jsonl"))) == 3


def test_without_a_producer_agent_the_user_auditions(song, tmp_path):
    seat("composer", agent(tmp_path, "composer", COMPOSER))
    studio.task_add("composer", "Write the hook")
    f = floor.Floor(song)
    out = f.run(poll=0.05)
    assert out["launches"] == 1 and (86, 3.0) not in piano(song)  # handed in, not yet in the session
    item = out["needs_you"][0]
    assert item["kind"] == "decision" and item["title"] == "Audition take tk1: Write the hook"
    f.tick()
    assert len(studio.attention_list()["needs_you"]) == 1  # asked once, not on every pass

    with pytest.raises(ValueError, match="what has to change"):
        studio.attention_resolve(item["id"], "return")
    studio.attention_resolve(item["id"], "accept")
    assert (86, 3.0) in piano(song) and studio.attention_list()["open"] == 0
    assert studio.task_list(state="done")["tasks"][0]["title"] == "Write the hook"


def test_a_returned_take_goes_back_to_the_same_seat_with_the_reason(song, tmp_path):
    seat("composer", agent(tmp_path, "composer", """
        tool("task_update"); studio.task_update(task, state="working")
        again = "came back" in prompt
        tool("write"); api.write("Piano", "A5:w" if again else "D5:w", at="2:1")
        tool("take_submit"); takes.take_submit("second try" if again else "first try")
        assert not again or "Bar 2 should end on A" in prompt
    """))
    studio.task_add("composer", "Lift bar 2")
    f = floor.Floor(song)
    item = f.run(poll=0.05)["needs_you"][0]
    studio.attention_resolve(item["id"], "return", "Bar 2 should end on A")
    out = f.run(poll=0.05)
    assert out["launches"] == 2 and [t["id"] for t in takes.take_list()["takes"]] == ["tk1"]  # same take, continued
    assert takes.take_list()["takes"][0]["summary"] == "second try"
    studio.attention_resolve(out["needs_you"][0]["id"], "accept")
    assert (81, 4.0) in piano(song) and (74, 4.0) not in piano(song)


def test_an_agent_that_stops_without_handing_in_is_handed_in_for_it(song, tmp_path):
    seat("composer", agent(tmp_path, "composer", """
        tool("write"); api.write("Piano", "D5:w", at="1:1")
        say(type="text", text="I rewrote bar 1.")
    """))
    studio.task_add("composer", "Rewrite bar 1")
    out = floor.Floor(song).run(poll=0.05)
    tk = takes.take_list()["takes"][0]
    assert tk["state"] == "submitted" and "stopped without submitting" in tk["summary"] and "I rewrote bar 1." in tk["summary"]
    assert out["needs_you"][0]["title"].startswith("Audition take tk1")


def test_an_agent_that_never_reaches_the_studio_is_a_blocker_not_a_retry(song, tmp_path):
    seat("composer", agent(tmp_path, "composer", """
        say(type="error", message="OAuth session expired")
        sys.exit(1)
    """))
    studio.task_add("composer", "Write the hook")
    out = floor.Floor(song).run(poll=0.05)
    assert out["launches"] == 1
    assert out["needs_you"][0]["kind"] == "blocker" and "OAuth session expired" in out["needs_you"][0]["title"]
    assert out["tasks"][0]["state"] == "needs_you"
    studio.attention_resolve(out["needs_you"][0]["id"], text="logged in again")
    assert studio.task_list()["tasks"][0]["state"] == "queued"  # ready for another go


def test_a_crashing_agent_is_retried_then_becomes_a_blocker(song, tmp_path):
    seat("composer", agent(tmp_path, "composer", """
        tool("task_update"); studio.task_update(task, state="working")
        sys.exit(3)
    """))
    studio.task_add("composer", "Write the hook")
    out = floor.Floor(song, max_attempts=2).run(poll=0.05)
    assert out["launches"] == 2
    assert out["needs_you"][0]["kind"] == "blocker" and "2 times" in out["needs_you"][0]["title"]
    studio.seat_join("composer")
    assert "ended early" in studio.inbox()["messages"][0]["text"]


def test_a_run_past_its_time_limit_is_stopped(song, tmp_path):
    seat("composer", agent(tmp_path, "composer", """
        import time
        tool("task_update"); studio.task_update(task, state="working")
        time.sleep(60)
    """))
    studio.task_add("composer", "Write the hook")
    out = floor.Floor(song, max_attempts=1, run_timeout=1.0).run(poll=0.1)
    assert out["launches"] == 1 and out["needs_you"][0]["kind"] == "blocker"
    ends = [e for e in studio.events(limit=500)["events"] if e["kind"] == "run" and e.get("what") == "end"]
    assert ends[0]["error"] == "it ran past the time limit"


def test_the_run_limit_stops_a_loop_until_the_user_says_carry_on(song, tmp_path):
    seat("composer", agent(tmp_path, "composer", """
        tool("task_update"); studio.task_update(task, state="done", result="ok")
    """))
    for i in range(3):
        studio.task_add("composer", f"Step {i}")
    f = floor.Floor(song, max_launches=2)
    out = f.run(poll=0.05)
    assert out["launches"] == 2 and "limit of 2 agent runs" in out["needs_you"][0]["title"]
    assert len(out["needs_you"]) == 1 and out["tasks"][0]["state"] == "queued"
    studio.attention_resolve(out["needs_you"][0]["id"], text="carry on")
    out = f.run(poll=0.05)
    assert out["launches"] == 3 and out["tasks"] == [] and out["needs_you"] == []


def test_people_and_empty_seats_are_left_alone(song, tmp_path):
    studio.seat_assign("mix", "human")
    studio.task_add("mix", "Balance it")
    studio.task_add("rhythm", "Add drums")
    f = floor.Floor(song)
    assert f.tick() == {"running": {}, "started": [], "waiting_on_people": ["T1", "T2"]}
    assert f.run(poll=0.05)["launches"] == 0


def test_a_missing_runner_is_a_blocker(song):
    studio.seat_assign("composer", "agent", runner="script", model="C:/nowhere/agent.py")
    studio.task_add("composer", "Write the hook")
    out = floor.Floor(song).run(poll=0.05)
    assert out["launches"] == 0 and out["needs_you"][0]["title"] == "Composer could not start"


def test_briefs_give_each_seat_its_own_playbook():
    comp, mix, prod = briefs.system("composer", True), briefs.system("mix", True), briefs.system("producer", False)
    assert "take_submit(summary)" in comp and "## 4. Write the music" in comp and "## 6. Mix and master" not in comp
    assert "## 6. Mix and master" in mix and "## 4. Write the music" not in mix
    assert "You run the session" in prod and "live for everyone" in prod and "take_submit" not in prod.split("---- Craft")[0]
    assert "no brass" in briefs.system("arranger", True, "no brass")
    assert "change nothing" in briefs.system("review", False)
