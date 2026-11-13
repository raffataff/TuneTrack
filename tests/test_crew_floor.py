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


def test_a_task_that_gains_a_dependency_mid_run_is_stopped_and_waits(song, tmp_path):
    # Seen live: the Producer accepted a take (which freed the mix task), then added the part that had to come first.
    import time
    seat("mix", agent(tmp_path, "mix", """
        import time
        tool("task_update"); studio.task_update(task, state="working")
        if not studio.task_list(state="done")["tasks"]:
            time.sleep(60)
        tool("track_update"); api.track_update("Piano", volume_db=-6)
        tool("take_submit"); takes.take_submit("Balanced")
    """))
    studio.task_add("mix", "Balance it")
    f = floor.Floor(song)
    assert f.tick()["started"] == ["T1"]
    studio.task_add("composer", "Write the bed")  # nobody in that seat: it waits for a person
    studio.task_update("T1", depends=["T2"])
    f.tick()
    for _ in range(200):
        if not f.active():
            break
        time.sleep(0.05)
    t = studio.task_list()["tasks"][0]
    assert (t["id"], t["state"], t["waiting_for"]) == ("T1", "queued", ["T2"])
    assert f.tick()["started"] == [] and f.launches == 1
    ends = [e for e in studio.events(limit=500)["events"] if e["kind"] == "run" and e.get("what") == "end"]
    assert "now waits on T2" in ends[0]["error"]

    studio.task_update("T2", state="done", result="The bed is in.")
    out = f.run(poll=0.05)
    assert out["launches"] == 2 and [k["id"] for k in takes.take_list()["takes"]] == ["tk1"]  # same take, continued
    assert takes.take_list()["takes"][0]["state"] == "submitted"
    assert not any(a["kind"] == "blocker" for a in out["needs_you"])  # the stop did not count as a failed attempt


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


DRY = """
    say(type="error", message="Too Many Requests: Free model usage limit reached. Please try again later")
    sys.exit(1)
"""
DONE = """
    tool("task_update"); studio.task_update(task, state="done", result="ok")
"""


def test_a_model_out_of_usage_is_one_item_for_every_seat_on_it(song, tmp_path):
    # Seen live: a free model ran out and each seat on it raised its own "not logged in?" blocker, six in all.
    dry = agent(tmp_path, "dry", DRY)
    seat("composer", dry), seat("mix", dry), seat("review", dry)
    studio.task_add("composer", "Write the hook"), studio.task_add("mix", "Balance it")
    f = floor.Floor(song, max_parallel=2)  # the first two start together and both fail
    out = f.run(poll=0.05)
    studio.task_add("review", "Listen")  # a third seat on that model is not even started
    out = f.run(poll=0.05)
    assert out["launches"] == 2
    [item] = studio.attention_list()["needs_you"]  # the full item: studio_status carries a short view
    assert item["why"] == "quota" and sorted(item["seats"]) == ["composer", "mix", "review"]
    assert "cannot work" in item["title"] and "out of usage" in item["title"] and "usage limit reached" in item["detail"]
    assert [o.get("do") for o in item["options"]] == [None, "wait", "seat"]
    assert {t["state"] for t in out["tasks"]} == {"needs_you"}

    # "try again in 15 minutes": queued, but nothing starts before then
    res = studio.attention_resolve(item["id"], "b")
    assert sorted(res["requeued"]) == ["T1", "T2", "T3"]
    tasks = studio.task_list()["tasks"]
    assert all(t["state"] == "queued" and not t["ready"] and t["not_before"] > 0 for t in tasks)
    assert f.tick()["started"] == [] and f.launches == 2


def test_two_seats_that_run_dry_in_the_same_moment_still_raise_one_item(song, tmp_path):
    # Each run's watcher thread looks for an open item and raises one if there is none. Hold every look open long
    # enough for the two to overlap: without the lock both find nothing and both raise.
    import threading
    import time
    dry = agent(tmp_path, "dry", DRY)
    seat("composer", dry), seat("mix", dry)
    studio.task_add("composer", "Write the hook"), studio.task_add("mix", "Balance it")
    f = floor.Floor(song, max_parallel=2)
    look = f._quota_items

    def slow_look(st):
        found = look(st)
        if threading.current_thread().name.startswith("floor-"):
            time.sleep(0.4)
        return found

    f._quota_items = slow_look
    f.run(poll=0.05)
    [item] = studio.attention_list()["needs_you"]
    assert sorted(item["seats"]) == ["composer", "mix"]


def test_moving_the_seats_to_another_model_closes_the_usage_item(song, tmp_path):
    seat("composer", agent(tmp_path, "dry", DRY))
    studio.task_add("composer", "Write the hook")
    f = floor.Floor(song)
    item = f.run(poll=0.05)["needs_you"][0]
    seat("composer", agent(tmp_path, "fine", DONE))  # the user picked another model with SEAT, nothing else
    out = f.run(poll=0.05)
    assert out["needs_you"] == [] and out["tasks"] == [] and out["launches"] == 2
    closed = studio.attention_list("all")["needs_you"][0]
    assert closed["id"] == item["id"] and "another model" in closed["answer"]["text"]


def test_the_run_limit_offers_how_far_to_go_or_to_stop(song, tmp_path):
    seat("composer", agent(tmp_path, "composer", DONE))
    for i in range(4):
        studio.task_add("composer", f"Step {i}")
    f = floor.Floor(song, max_launches=1)
    f.run(poll=0.05)
    item = studio.attention_list()["needs_you"][0]
    assert [(o.get("runs"), o.get("do")) for o in item["options"]] == [(1, None), (40, None), (None, "stop")]
    studio.attention_resolve(item["id"], "a")
    out = f.run(poll=0.05)
    assert out["launches"] == 2 and len(out["tasks"]) == 2
    studio.attention_resolve(out["needs_you"][0]["id"], "stop the crew")
    out = f.run(poll=0.05)
    assert out["launches"] == 2 and len(out["tasks"]) == 2 and out["needs_you"] == []  # stopped, nothing asked again


def test_a_task_that_keeps_failing_can_be_cancelled_from_its_item(song, tmp_path):
    seat("composer", agent(tmp_path, "composer", """
        tool("task_update"); studio.task_update(task, state="working")
        sys.exit(3)
    """))
    studio.task_add("composer", "Write the hook")
    floor.Floor(song, max_attempts=1).run(poll=0.05)
    item = studio.attention_list()["needs_you"][0]
    assert item["why"] == "attempts" and [o.get("do") for o in item["options"]] == [None, "edit", "seat", "cancel"]
    studio.attention_resolve(item["id"], "d")
    assert studio.task_list(state="all")["tasks"][0]["state"] == "cancelled"


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
    assert "three at most" in mix.split("---- Craft")[0] and "three at most" not in comp  # Mix's stopping rule
    assert "You run the session" in prod and "live for everyone" in prod and "take_submit" not in prod.split("---- Craft")[0]
    assert "no brass" in briefs.system("arranger", True, "no brass")
    assert "change nothing" in briefs.system("review", False)

# ---- loose ends: takes that never reach the session, and questions that cost a run each -------------------------------

def test_a_seat_cannot_close_its_task_while_its_take_holds_work(song, tmp_path):
    # Seen live: Rhythm set its task done and then handed in. The task was no longer "submitted", so nobody was
    # asked to audition the take, and the final export went out without the part.
    seat("composer", agent(tmp_path, "composer", """
        tool("write"); api.write("Piano", "D5:q F5 A5 D6", at="1:1")
        try:
            studio.task_update(task, state="done", result="Wrote the hook")
            say(type="text", text="closed it")
        except ValueError as e:
            assert "take_submit" in str(e), e
        tool("take_submit"); takes.take_submit("Hook in bar 1")
        res = studio.task_update(task, state="done", result="Wrote the hook")
        assert res["state"] == "submitted" and "accepted" in res["note"], res
    """))
    studio.task_add("composer", "Write the hook")
    out = floor.Floor(song).run(poll=0.05)
    assert [t["state"] for t in out["tasks"]] == ["submitted"]
    assert out["needs_you"][0]["title"] == "Audition take tk1: Write the hook"
    ends = [e for e in studio.events(limit=500)["events"] if e["kind"] == "run" and e.get("what") == "end"]
    assert ends[0]["ok"] is True  # none of the stand-in's own checks failed


def test_a_take_handed_in_after_its_task_was_closed_is_still_auditioned(song):
    studio.seat_assign("composer", "human")
    studio.task_add("composer", "Write the hook")
    tk = takes.take_new("Hook", "T1", "composer", use=False)
    api.write("Piano", "D5:w", at="1:1", project=tk["project"])
    studio.task_update("T1", state="done", result="closed by hand")  # the user, before the take came in
    studio.seat_join("composer")
    takes.take_submit("Hook in bar 1", tk["take"])
    studio.seat_join(None)
    assert studio.task_list()["tasks"][0]["state"] == "submitted"  # its work is not in the session: not done
    f = floor.Floor(song)
    f.tick(), f.tick()
    [item] = studio.attention_list()["needs_you"]
    assert item["take"] == "tk1" and item["from"] == "composer"


def test_a_take_cut_for_no_task_is_auditioned_too(song):
    tk = takes.take_new("An idea for bar 3", None, "composer", use=False)
    api.write("Piano", "A4:w", at="3:1", project=tk["project"])
    takes.take_submit("Try this", tk["take"])
    pending = takes.take_pending()["takes"][0]
    assert pending["loose"] and "nobody has been asked" in pending["status"]
    f = floor.Floor(song)
    f.tick(), f.tick()
    [item] = studio.attention_list()["needs_you"]
    assert item["title"] == "Audition take tk1: An idea for bar 3"
    pending = takes.take_pending()["takes"][0]
    assert "loose" not in pending and "waiting for you" in pending["status"]
    studio.attention_resolve(item["id"], "accept")
    assert (69, 8.0) in piano(song) and takes.take_pending() == {"takes": []}


def test_with_a_producer_agent_the_late_take_becomes_its_audition_task(song, tmp_path):
    seat("producer", agent(tmp_path, "producer", PRODUCER))
    tk = takes.take_new("An idea for bar 3", None, "composer", use=False)
    api.write("Piano", "A4:w", at="3:1", project=tk["project"])
    takes.take_submit("Try this", tk["take"])
    f = floor.Floor(song)
    f._route_takes()
    assert "waiting for the Producer" in takes.take_pending()["takes"][0]["status"]
    out = f.run(poll=0.05)
    assert out["launches"] == 1 and (69, 8.0) in piano(song) and out["takes"] == []


def test_an_empty_take_is_put_away_when_its_task_ends(song, tmp_path):
    # Seen live: a Mix task that needed no change left an open take behind for the rest of the session.
    seat("mix", agent(tmp_path, "mix", """
        tool("task_update"); studio.task_update(task, state="working")
        tool("task_update"); studio.task_update(task, state="done", result="No change needed: the seam is clean.")
    """))
    studio.task_add("mix", "Check the loop seam")
    f = floor.Floor(song)
    out = f.run(poll=0.05)
    f.tick()
    assert out["tasks"] == [] and takes.take_list()["takes"] == []
    [tk] = takes.take_list("rejected")["takes"]
    assert "ended without a change" in tk["reason"]


def test_a_take_with_work_in_it_is_never_put_away(song):
    studio.seat_assign("composer", "human")
    studio.task_add("composer", "Write the hook")
    tk = takes.take_new("Hook", "T1", "composer", use=False)
    api.write("Piano", "D5:w", at="1:1", project=tk["project"])
    assert "in progress (T1 is queued)" in takes.take_pending()["takes"][0]["status"]
    studio.task_update("T1", state="cancelled")
    f = floor.Floor(song)
    f.tick(), f.tick()
    [left] = takes.take_pending()["takes"]
    assert left["state"] == "open" and left["loose"] and "its task T1 is cancelled" in left["status"]


def test_an_export_says_which_takes_it_does_not_contain(song):
    tk = takes.take_new("Hook", None, "composer", use=False)
    assert "not_in_this_render" not in api.render(out="renders/final.wav")  # an empty take holds nothing back
    api.write("Piano", "D5:w", at="1:1", project=tk["project"])
    rep = api.render(out="renders/final.wav")
    assert rep["not_in_this_render"] == ["tk1 (composer) Hook: open with work in it, for no task"]
    assert any("1 take with work in it is not in this export (tk1)" in w for w in rep["warnings"])
    assert "not_in_this_render" not in api.render()  # a working render is not an export
    assert "not_in_this_render" not in api.render(project=tk["project"], out="renders/take.wav")
    with pytest.raises(ValueError, match="already rejected"):
        takes.take_discard("tk1"), takes.take_discard("tk1")
    assert "not_in_this_render" not in api.render(out="renders/final.wav")


def test_discarding_a_handed_in_take_says_what_is_left_of_its_task(song):
    studio.task_add("composer", "Write the hook")
    tk = takes.take_new("Hook", "T1", "composer", use=False)
    api.write("Piano", "D5:w", at="1:1", project=tk["project"])
    takes.take_submit("Hook", tk["take"])
    assert "T1 is still submitted" in takes.take_discard("tk1")["task"]


def test_a_seat_gets_two_questions_per_task_and_an_answer_is_not_a_failed_try(song):
    # Seen live: ask, answer, a run that re-checked everything, ask again - three rounds on one accepted take, and
    # a Review task that was "tried 3 times without finishing" when all it had done was wait for answers.
    studio.task_add("review", "Review the loop")
    for n in (1, 2):
        studio.seat_join("review")
        studio.task_update("T1", state="working")
        studio.ask_human(f"Is take {n} calm enough?", task="T1", options=["Yes", "No"])
        studio.seat_join(None)
        studio.attention_resolve(f"N{n}", "a")
        t = studio.read()["tasks"][0]
        assert (t["state"], t["attempts"]) == ("queued", 0)
    studio.seat_join("review")
    with pytest.raises(ValueError, match="asked the user 2 times on T1.*N2 \"Is take 2 calm enough\\?\": Yes"):
        studio.ask_human("And now?", task="T1")
    assert studio.ask_human("The patch will not load", kind="blocker", task="T1")["needs_you"] == "N3"
    inbox = studio.inbox(mark_read=False)["messages"]
    prompt = briefs.task_prompt(studio.read()["tasks"][0], studio.studio_status(), None, None, inbox)
    assert "Take the answer as settled" in prompt
    assert "settled" not in briefs.task_prompt(studio.read()["tasks"][0], studio.studio_status())


def test_the_producer_is_told_to_clear_the_takes_before_it_finishes():
    prod = briefs.system("producer", False)
    assert "take_discard" in prod and "not_in_this_render" in prod
    from tunetrack import roles
    assert "take_discard" in roles.tools_for("producer") and "take_discard" not in roles.tools_for("mix")
