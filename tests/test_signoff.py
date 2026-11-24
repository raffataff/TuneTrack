"""The user's stops on the crew's work (signoff.py): a seat's sign-off, Co-Producer, plans, away mode, options.
Driven by scripted stand-in agents (the "script" runner): no model is called."""
import json
import textwrap
from pathlib import Path

import pytest

from tunetrack import api, signoff, studio, takes
from tunetrack.crew import floor

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


def prompts(song) -> list[str]:
    """What each stand-in Producer was told, in order (it writes its prompt next to the session)."""
    f = Path(song) / "prompts.jsonl"
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


COMPOSER = """
    again = "came back" in prompt
    tool("write"); api.write("Piano", "A5:w" if again else "D5:q F5 A5 D6", at="1:1")
    tool("take_submit"); takes.take_submit("second try" if again else "Hook in bar 1, an octave up")
"""

# Accepts what it hears (or returns it when told to in its detail), plans one task, and logs its prompt.
PRODUCER = """
    import os
    from pathlib import Path
    with open(Path(os.environ["TUNETRACK_SESSION"]) / "prompts.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(prompt) + "\\n")
    if "Plan the session" in prompt or "Revise your plan" in prompt:
        tool("project_set"); api.project_set(tempo=100)
        tool("task_add"); studio.task_add("composer", "Write the hook" if "Plan" in prompt else "Write a bridge")
    elif "Final listen" in prompt:
        tool("task_update"); studio.task_update(task, state="done", result="Heard it.")
    else:
        take = re.search(r"take (tk\\d+)", prompt).group(1)
        if "SEND IT BACK" in prompt:
            tool("take_reject"); say(type="result", result=json.dumps(takes.take_reject(take, "Too busy in bar 1")))
        else:
            tool("take_accept"); say(type="result", result=json.dumps(takes.take_accept(take)))
"""


def run(song) -> dict:
    return floor.Floor(song).run(poll=0.05)


def open_items() -> list[dict]:
    return studio.attention_list()["needs_you"]


def launches(song, seat_id: str) -> int:
    f = Path(song) / "studio" / "events.jsonl"
    return sum(1 for x in f.read_text(encoding="utf-8").splitlines()
               if (e := json.loads(x)).get("kind") == "run" and e.get("what") == "start" and e.get("seat") == seat_id)


# ---- a seat's sign-off ---------------------------------------------------------------------------------------------

def test_a_signed_off_seat_stops_at_you_before_the_producer(song, tmp_path):
    seat("producer", agent(tmp_path, "producer", PRODUCER))
    seat("composer", agent(tmp_path, "composer", COMPOSER))
    signoff.seat_signoff("composer")
    studio.task_add("composer", "Write the hook")
    run(song)
    [item] = open_items()
    assert item["stage"] == "signoff" and item["take"] == "tk1" and item["from"] == "composer"
    assert [o["do"] for o in item["options"]] == ["pass", "return", "accept", "discard"]
    assert launches(song, "producer") == 0 and (86, 3.0) not in piano(song)  # nobody else has heard it

    studio.attention_resolve(item["id"], "pass", "Love the lift; check the top D")
    [aud] = [t for t in studio.task_list()["tasks"] if t["seat"] == "producer"]
    assert aud["title"].startswith("Audition take tk1") and aud["by"] == "you"
    run(song)
    assert (86, 3.0) in piano(song) and open_items() == []
    assert "The user listened first and passed it to you: Love the lift; check the top D" in prompts(song)[0]


def test_sending_back_from_a_sign_off_returns_the_take_and_stops_again(song, tmp_path):
    seat("producer", agent(tmp_path, "producer", PRODUCER))
    seat("composer", agent(tmp_path, "composer", COMPOSER))
    signoff.seat_signoff("composer")
    studio.task_add("composer", "Write the hook")
    run(song)
    [item] = open_items()
    with pytest.raises(ValueError, match="what has to change"):
        studio.attention_resolve(item["id"], "return")
    studio.attention_resolve(item["id"], "return", "Make it one long A")
    run(song)
    [again] = open_items()
    assert again["stage"] == "signoff" and again["take"] == "tk1" and again["id"] != item["id"]
    assert again["detail"] == "second try" and launches(song, "producer") == 0


def test_you_can_change_a_take_yourself_then_pass_it_and_the_producer_is_told(song, tmp_path):
    seat("producer", agent(tmp_path, "producer", PRODUCER))
    seat("composer", agent(tmp_path, "composer", COMPOSER))
    signoff.seat_signoff("composer")
    studio.task_add("composer", "Write the hook")
    run(song)
    [item] = open_items()
    api.track_update("Piano", volume_db=-4.0, project=str(Path(song) / "takes" / "tk1"))  # a Desk edit in the take
    studio.attention_resolve(item["id"], "pass")
    run(song)
    told = prompts(song)[0]
    assert "The user listened first and passed it to you. They changed: " in told and "Piano" in told
    meta = json.loads((Path(song) / "takes" / "tk1" / "take.json").read_text(encoding="utf-8"))
    assert meta["state"] == "accepted" and meta["signed"]["changed"]


def test_accepting_at_a_sign_off_skips_the_producer(song, tmp_path):
    seat("producer", agent(tmp_path, "producer", PRODUCER))
    seat("composer", agent(tmp_path, "composer", COMPOSER))
    signoff.seat_signoff("composer")
    studio.task_add("composer", "Write the hook")
    run(song)
    studio.attention_resolve(open_items()[0]["id"], "accept")
    run(song)
    # no audition; the one Producer run is its final listen on what the user accepted (The Drive fixes B6)
    assert (86, 3.0) in piano(song) and launches(song, "producer") == 1
    assert [t["title"] for t in studio.task_list(state="done")["tasks"]] == ["Write the hook", "Final listen"]


def test_without_a_producer_agent_a_sign_off_is_the_audition_it_always_was(song, tmp_path):
    seat("composer", agent(tmp_path, "composer", COMPOSER))
    signoff.seat_signoff("composer")
    studio.task_add("composer", "Write the hook")
    run(song)
    [item] = open_items()
    assert item["title"] == "Audition take tk1: Write the hook"
    assert [o["do"] for o in item["options"]] == ["accept", "return", "discard"]


# ---- Co-Producer: the Producer's calls -----------------------------------------------------------------------------

def _co_producer_heard(song, tmp_path, producer: str = PRODUCER) -> dict:
    seat("producer", agent(tmp_path, "producer", producer))
    seat("composer", agent(tmp_path, "composer", COMPOSER))
    signoff.seat_signoff("producer")
    studio.task_add("composer", "Write the hook")
    run(song)
    [item] = open_items()
    return item


def test_co_producer_holds_the_producers_accept_until_you_confirm(song, tmp_path):
    item = _co_producer_heard(song, tmp_path)
    assert item["stage"] == "confirm" and item["proposal"]["decision"] == "accept"
    assert item["title"] == "The Producer wants to accept tk1: Write the hook"
    assert (86, 3.0) not in piano(song) and "Co-Producer is on" in prompts(song)[0]
    [aud] = [t for t in studio.task_list(state="done")["tasks"] if t["seat"] == "producer"]
    assert aud["result"] == "tk1 accept proposed, waiting on the user"  # the Producer's run is over: no second run
    assert launches(song, "producer") == 1

    studio.attention_resolve(item["id"], "accept")
    assert (86, 3.0) in piano(song) and open_items() == []
    meta = json.loads((Path(song) / "takes" / "tk1" / "take.json").read_text(encoding="utf-8"))
    assert meta["state"] == "accepted" and meta["proposal"] is None


def test_you_can_overrule_the_producer_and_send_the_take_back(song, tmp_path):
    item = _co_producer_heard(song, tmp_path)
    studio.attention_resolve(item["id"], "return", "The top D is too shrill")
    meta = json.loads((Path(song) / "takes" / "tk1" / "take.json").read_text(encoding="utf-8"))
    assert meta["state"] == "open" and meta["reason"] == "The top D is too shrill"
    assert [t["state"] for t in studio.task_list()["tasks"]] == ["queued"]  # the Composer goes again


def test_confirming_a_return_sends_the_producers_reason_with_yours(song, tmp_path):
    seat("producer", agent(tmp_path, "producer", PRODUCER))
    seat("composer", agent(tmp_path, "composer", COMPOSER))
    signoff.seat_signoff("producer")
    studio.task_add("composer", "Write the hook", "SEND IT BACK if bar 1 is busy")  # its stand-in Producer reads this
    run(song)
    [item] = open_items()
    assert item["stage"] == "confirm" and item["proposal"]["decision"] == "return"
    assert item["title"] == "The Producer wants to send back tk1: Write the hook"
    assert "The Producer's reason: Too busy in bar 1" in item["detail"]
    assert [o["do"] for o in item["options"]] == ["return", "accept", "ask"]
    meta = json.loads((Path(song) / "takes" / "tk1" / "take.json").read_text(encoding="utf-8"))
    assert meta["state"] == "submitted"  # nothing went back to the Composer yet
    studio.attention_resolve(item["id"], "return", "and keep the low end")
    meta = json.loads((Path(song) / "takes" / "tk1" / "take.json").read_text(encoding="utf-8"))
    assert meta["state"] == "open" and meta["reason"] == "Too busy in bar 1\n\nThe user adds: and keep the low end"


def test_asking_the_producer_again_starts_it_with_your_note(song, tmp_path):
    item = _co_producer_heard(song, tmp_path)
    with pytest.raises(ValueError, match="listen for"):
        studio.attention_resolve(item["id"], "ask")
    studio.attention_resolve(item["id"], "ask", "Listen to the top D in bar 1")
    run(song)
    assert launches(song, "producer") == 2
    assert "The user asks you to hear it again before deciding: Listen to the top D in bar 1" in prompts(song)[1]
    [again] = open_items()
    assert again["stage"] == "confirm" and again["id"] != item["id"]


def test_a_take_you_passed_is_not_stopped_again_when_the_producer_accepts_it(song, tmp_path):
    seat("producer", agent(tmp_path, "producer", PRODUCER))
    seat("composer", agent(tmp_path, "composer", COMPOSER))
    signoff.crew_involve("every")
    studio.task_add("composer", "Write the hook")
    run(song)
    studio.attention_resolve(open_items()[0]["id"], "pass")
    run(song)
    assert (86, 3.0) in piano(song) and open_items() == []


def test_the_producers_discard_waits_for_you_too(song):
    studio.seat_assign("producer", "external")
    signoff.seat_signoff("producer")
    tk = takes.take_new("An idea", None, "composer", use=False)
    api.write("Piano", "A4:w", at="3:1", project=tk["project"])
    takes.take_submit("Try this", tk["take"])
    studio.seat_join("producer")
    out = takes.take_discard(tk["take"])
    studio.seat_join(None)
    assert out["proposed"] == "discard"
    [item] = [a for a in open_items() if a.get("stage") == "confirm"]
    assert [o["do"] for o in item["options"]] == ["discard", "accept", "return", "ask"]
    studio.attention_resolve(item["id"], "accept")
    assert (69, 8.0) in piano(song)


# ---- plans ---------------------------------------------------------------------------------------------------------

def test_co_producer_holds_the_plan_until_you_approve_it(song, tmp_path):
    seat("producer", agent(tmp_path, "producer", PRODUCER))
    seat("composer", agent(tmp_path, "composer", COMPOSER))
    signoff.seat_signoff("producer")
    floor.kickoff("A hopeful night-drive theme")
    out = run(song)
    assert launches(song, "composer") == 0
    [item] = open_items()
    assert item["stage"] == "plan" and item["title"] == "The Producer's plan: 1 task" and item["tasks"] == ["T2"]
    assert any("tempo" in c.lower() for c in item["changes"])  # what it changed on the session itself
    [t2] = [t for t in out["tasks"] if t["id"] == "T2"]
    assert t2["held"] == "waits for the user's OK" and not t2["ready"]

    studio.attention_resolve(item["id"], "plan_ok")
    run(song)
    assert launches(song, "composer") == 1


def test_a_plan_sent_back_is_revised_and_comes_back_as_one(song, tmp_path):
    seat("producer", agent(tmp_path, "producer", PRODUCER))
    seat("composer", agent(tmp_path, "composer", COMPOSER))
    signoff.seat_signoff("producer")
    floor.kickoff("A hopeful night-drive theme")
    run(song)
    item = open_items()[0]
    with pytest.raises(ValueError, match="what to change"):
        studio.attention_resolve(item["id"], "plan_back")
    studio.attention_resolve(item["id"], "plan_back", "Add a bridge")
    run(song)
    assert "The user sent your plan back: Add a bridge" in prompts(song)[1]
    [again] = open_items()
    assert again["stage"] == "plan" and again["tasks"] == ["T2", "T4"] and launches(song, "composer") == 0


def test_turning_sign_off_off_lets_a_plan_in_progress_go(song):
    studio.seat_assign("producer", "external")
    signoff.seat_signoff("producer")
    studio.seat_join("producer")
    studio.task_add("composer", "Write the hook")
    studio.seat_join(None)
    assert studio.ready_tasks(song) == []
    signoff.seat_signoff("producer", False)
    assert [t["id"] for t in studio.ready_tasks(song)] == ["T1"]


def test_an_outside_apps_plan_is_put_to_you_once_it_stops_adding(song, monkeypatch):
    studio.seat_assign("producer", "external")
    signoff.seat_signoff("producer")
    studio.seat_join("producer")
    studio.task_add("composer", "Write the hook")
    studio.task_add("mix", "Mix it", depends=["T1"])
    studio.seat_join(None)
    assert signoff.gather_plans(song, set(), {}) == []  # it may still be adding
    monkeypatch.setattr(signoff, "_PLAN_QUIET", 0.0)
    [nid] = signoff.gather_plans(song, set(), {})
    item = next(a for a in open_items() if a["id"] == nid)
    assert item["tasks"] == ["T1", "T2"] and "T2 mix: Mix it (after T1)" in item["detail"]


# ---- presets, away mode, who may switch ----------------------------------------------------------------------------

def test_presets_set_every_switch_and_a_direct_seat_moves_to_takes(song):
    studio.seat_assign("mix", mode="direct")
    out = signoff.crew_involve("every")
    assert out["involve"] == "every" and any("Mix now works in takes" in n for n in out["notes"])
    b = studio.board()
    assert b["involve"] == "every" and all(s["signoff"] for s in b["seats"])
    assert next(s for s in b["seats"] if s["seat"] == "mix")["mode"] == "propose"
    signoff.crew_involve("co_producer")
    assert [s["seat"] for s in studio.board()["seats"] if s["signoff"]] == ["producer"]
    signoff.seat_signoff("sound")
    assert studio.board()["involve"] == "custom"
    with pytest.raises(ValueError, match="preset"):
        signoff.crew_involve("sometimes")


def test_only_you_set_sign_off_and_options(song):
    studio.seat_join("producer")
    with pytest.raises(PermissionError):
        signoff.seat_signoff("composer")
    with pytest.raises(PermissionError):
        studio.seat_assign("composer", signoff=True)
    with pytest.raises(PermissionError):
        studio.seat_assign("composer", options=2)


def test_away_mode_lets_a_stop_through_after_its_minutes(song, tmp_path):
    seat("producer", agent(tmp_path, "producer", PRODUCER))
    seat("composer", agent(tmp_path, "composer", COMPOSER))
    signoff.seat_signoff("composer")
    studio.task_add("composer", "Write the hook")
    run(song)
    assert open_items()[0]["stage"] == "signoff"
    signoff.away_set(True, minutes=20)
    assert signoff.away_pass(song) == []  # it has not waited 20 minutes since you went away
    signoff.away_set(True, minutes=0)
    run(song)
    assert (86, 3.0) in piano(song) and open_items() == []
    resolved = [a for a in studio.attention_list("resolved")["needs_you"] if a.get("stage") == "signoff"]
    assert resolved[0]["answer"]["text"] == "Away: let through after 0 minutes"


# ---- options -------------------------------------------------------------------------------------------------------

OPTIONS = """
    assert "The user wants 2 options to choose from" in prompt
    tool("write"); api.write("Piano", "A5:w", at="1:1")
    tool("take_submit"); takes.take_submit("Option A: a long A")
    tool("take_new"); takes.take_new("Option B: a run", task)
    tool("write"); api.write("Piano", "D5:q F5 A5 D6", at="1:1")
    tool("take_submit"); takes.take_submit("Option B: a run up")
"""


def test_a_seat_asked_for_options_hands_in_several_and_you_keep_one(song, tmp_path):
    seat("composer", agent(tmp_path, "composer", OPTIONS))
    studio.task_add("composer", "Write the hook")
    studio.task_update("T1", options=2)
    run(song)
    [item] = open_items()
    assert item["stage"] == "choose" and item["takes"] == ["tk1", "tk2"] and item["task"] == "T1"
    assert [o["do"] for o in item["options"]] == ["choose", "choose", "return"]
    studio.attention_resolve(item["id"], "b")  # Use Option B
    assert (86, 3.0) in piano(song) and (81, 0.0) not in piano(song)
    states = {t["id"]: t["state"] for t in takes.take_list("all")["takes"]}
    assert states == {"tk1": "rejected", "tk2": "accepted"}
    assert studio.task_list(state="done")["tasks"][0]["id"] == "T1"


def test_options_go_to_the_producer_once_you_have_chosen(song, tmp_path):
    seat("producer", agent(tmp_path, "producer", PRODUCER))
    seat("composer", agent(tmp_path, "composer", OPTIONS))
    studio.seat_assign("composer", options=2)
    studio.task_add("composer", "Write the hook")
    run(song)
    item = open_items()[0]
    assert item["stage"] == "choose" and launches(song, "producer") == 0
    studio.attention_resolve(item["id"], "a", "the simpler one")
    run(song)
    assert "The user chose this one from 2 options: the simpler one" in prompts(song)[0]
    assert (81, 0.0) in piano(song) and (86, 3.0) not in piano(song)


def test_none_of_the_options_sends_the_first_back_and_drops_the_rest(song, tmp_path):
    seat("composer", agent(tmp_path, "composer", OPTIONS))
    studio.task_add("composer", "Write the hook")
    studio.task_update("T1", options=2)
    run(song)
    item = open_items()[0]
    with pytest.raises(ValueError, match="wrong with all of them"):
        studio.attention_resolve(item["id"], "return")
    studio.attention_resolve(item["id"], "return", "Both too high")
    states = {t["id"]: t["state"] for t in takes.take_list("all")["takes"]}
    assert states == {"tk1": "open", "tk2": "rejected"}
    t1 = studio.task_list()["tasks"][0]
    assert t1["state"] == "queued" and t1["take"] == "tk1"


def test_you_can_add_a_task_into_a_plan_and_it_waits_with_it(song):
    studio.seat_assign("producer", "external")
    signoff.seat_signoff("producer")
    studio.seat_join("producer")
    studio.task_add("composer", "Write the hook")
    studio.seat_join(None)
    with studio._Board(song) as b:
        nid = signoff.plan_ticket(b, "producer", ["T1"])["id"]
    t = studio.task_add("mix", "Mix it", depends=["T1"], plan=nid)
    assert t["held"] == "waits for the user's OK"
    assert next(a for a in open_items() if a["id"] == nid)["tasks"] == ["T1", "T2"]
    studio.attention_resolve(nid, "plan_ok")
    assert [x["id"] for x in studio.ready_tasks(song)] == ["T1"]  # T2 waits on T1 now, not on you
    with pytest.raises(ValueError, match="not an open plan"):
        studio.task_add("mix", "Late", plan=nid)
