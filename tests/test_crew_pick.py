"""Picking a crew from history, and the cost per accepted take the review weighs (script runner: no model calls)."""
import json
import textwrap

import pytest

from tunetrack import api, config, studio, takes
from tunetrack.crew import floor, scorecard


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


COMPOSER = """
import json, sys
from tunetrack import api, takes
sys.stdin.read()
print(json.dumps({"type": "tool", "name": "mcp__tunetrack__write"}), flush=True)
api.write("Piano", "D5:q F5 A5 D6", at="1:1")
takes.take_submit("Hook in bar 1")
print(json.dumps({"type": "usage", "input_tokens": 40000, "output_tokens": 2000, "cost_usd": None}), flush=True)
"""


def _row(agent, runner, model, handed_in, accepted, returned, **kw):
    return {"seat": "composer", "agent": agent, "runner": runner, "model": model, "effort": None, "runs": kw.pop("runs", handed_in),
            "ok": handed_in, "handed_in": handed_in, "accepted": accepted, "returned": returned, "first_time": accepted,
            "tasks_done": accepted, **kw}


def _history(*rows):
    config.HOME.mkdir(parents=True, exist_ok=True)
    (config.HOME / "crew_history.json").write_text(json.dumps(
        {f"s{i}": {"name": f"s{i}", "at": 0, "rows": [r]} for i, r in enumerate(rows)}), encoding="utf-8")


def test_a_crew_is_proposed_from_a_scripted_session_and_applied_only_by_the_user(song, tmp_path):
    script = tmp_path / "composer.py"
    script.write_text(textwrap.dedent(COMPOSER), encoding="utf-8")
    studio.seat_assign("composer", "agent", runner="script", model=str(script))
    studio.task_add("composer", "Write the hook")
    out = floor.Floor(song).run(poll=0.05)
    studio.attention_resolve(out["needs_you"][0]["id"], "accept")  # the user auditions: no Producer agent
    scorecard.remember(song)

    [row] = scorecard.crew_scorecard()["rows"]
    assert (row["accepted"], row["tokens"], row["metered"], row["priced"]) == (1, 42000, 1, 0)
    assert row["tokens_per_take"] == 42000 and "usd_per_take" not in row  # tokens only: the runner gave no dollars
    table = scorecard.as_markdown(scorecard.crew_scorecard())
    assert "Cost per |" in table and "| 42k tok/take |" in table and "42k tokens." in table

    [pick] = scorecard.crew_pick()["picks"]
    assert (pick["seat"], pick["runner"], pick["model"], pick["same"]) == ("composer", "script", str(script), True)
    assert pick["why"].startswith("1 of 1 takes accepted, 0 returned over 1 session")

    studio.seat_assign("composer", "agent", runner="kilo", model="other")
    [pick] = scorecard.crew_pick()["picks"]
    assert pick["same"] is False
    assert studio.read()["seats"]["composer"]["occupant"]["model"] == "other"  # proposing changes nothing
    studio.seat_assign(pick["seat"], "agent", runner=pick["runner"], model=pick["model"], effort=pick["effort"])
    assert studio.read()["seats"]["composer"]["occupant"]["model"] == str(script)


def test_more_accepted_and_fewer_returned_wins_and_one_lucky_take_does_not():
    _history(_row("kilo · lucky", "kilo", "lucky", 1, 1, 0),
             _row("codex · steady", "codex", "steady", 10, 9, 1),
             _row("claude · sloppy", "claude", "sloppy", 10, 9, 6))
    [pick] = scorecard.crew_pick()["picks"]
    assert pick["model"] == "steady" and "(best of 3)" in pick["why"]


def test_cost_per_accepted_take_breaks_a_tie():
    _history(_row("codex · dear", "codex", "dear", 4, 4, 0, priced=4, metered=4, cost_usd=2.0, tokens=400000),
             _row("claude · cheap", "claude", "cheap", 4, 4, 0, priced=4, metered=4, cost_usd=0.4, tokens=900000))
    [pick] = scorecard.crew_pick()["picks"]
    assert pick["model"] == "cheap" and "$0.10 per take" in pick["why"]  # dollars first, where both report them
    [h1, h2] = scorecard.crew_history("composer")["history"]
    assert {h1["usd_per_take"], h2["usd_per_take"]} == {0.5, 0.1}


def test_a_model_whose_runs_reported_no_usage_is_not_marked_down():
    _history(_row("kilo · silent", "kilo", "silent", 4, 4, 0, runs=6),
             _row("claude · metered", "claude", "metered", 4, 4, 0, priced=4, metered=4, cost_usd=0.4, tokens=9000))
    [pick] = scorecard.crew_pick()["picks"]
    assert pick["model"] == "silent"  # level on takes, cost unknown on one side: more runs behind it decides


def test_old_history_rows_are_read_back_from_their_label_and_people_are_never_proposed():
    rows = [_row("kilo · free-a · high", None, None, 2, 2, 0), _row("you", None, None, 5, 5, 0)]
    for r in rows:
        del r["runner"], r["model"], r["effort"]
    _history(*rows)
    [pick] = scorecard.crew_pick()["picks"]
    assert (pick["runner"], pick["model"], pick["effort"]) == ("kilo", "free-a", "high")


def test_the_review_shows_no_cost_column_when_no_run_reported_usage(song):
    studio.log("run", seat="composer", task="T1", what="start", agent="kilo · free-a")
    studio.log("run", seat="composer", task="T1", what="end", ok=True, tools=0)
    table = scorecard.as_markdown(scorecard.crew_scorecard())
    assert "Cost per" not in table and "Usage reported" not in table
    assert scorecard.crew_pick()["picks"] == []  # nothing remembered yet


def test_rejected_takes_are_counted_and_cost_names_its_unit(song):
    studio.log("run", seat="composer", task="T1", what="start", agent="kilo · free-a")
    studio.log("take", id="tk1", state="submitted", seat="composer")
    studio.log("take", id="tk1", state="accepted", seat="composer")
    studio.log("take", id="tk2", state="rejected", seat="composer", reason="empty")  # never submitted
    studio.log("run", seat="composer", task="T1", what="end", ok=True, cost_usd=0.5)
    studio.log("run", seat="producer", task="T2", what="start", agent="kilo · free-b")
    studio.log("task", id="T2", seat="producer", state="done", by="producer")
    studio.log("run", seat="producer", task="T2", what="end", ok=True, cost_usd=0.2)
    card = scorecard.crew_scorecard()
    comp = next(r for r in card["rows"] if r["seat"] == "composer")
    assert (comp["handed_in"], comp["accepted"], comp["rejected"]) == (2, 1, 1)
    table = scorecard.as_markdown(card)
    assert "1 of 2 takes accepted (1 first time, 1 rejected)" in table
    assert "| $0.50/take |" in table and "| $0.20/task |" in table
