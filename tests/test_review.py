"""A person's review from the Review seat (review.py): filing it, handing its findings out, the second listen."""
import json
from pathlib import Path

import pytest

from tunetrack import api, review, studio


@pytest.fixture(autouse=True)
def _as_user():
    studio.seat_join(None)
    api.set_default_project(None)
    yield
    studio.seat_join(None)
    api.set_default_project(None)


@pytest.fixture
def song(tmp_path):
    api.project_new("Boss Loop", folder=str(tmp_path / "song"), tempo=150, key="D minor")
    api.track_add("Horns", "synth:pad")
    api.track_add("Strings", "synth:pad")
    for seat in ("producer", "composer", "mix"):
        studio.seat_assign(seat, "agent", runner="claude", model="claude-opus-5-5")
    studio.seat_assign("review", "human")
    return str(tmp_path / "song")


def _task(tid):
    return next(t for t in studio.read()["tasks"] if t["id"] == tid)


def _review_task():
    mix = studio.task_add("mix", "Balance and master")
    studio.task_update(mix["id"], state="done", result="mixed")
    return studio.task_add("review", "Review the loop", depends=[mix["id"]])["id"]


FINDINGS = [
    {"text": "The horn line is lost under the strings at the climax.", "bars": "24 - 17", "tracks": ["Horns"],
     "at": 41.2, "to": "mix"},
    {"text": "Bar 9 lands on a wrong note in the horns.", "bars": "bar 9", "tracks": ["Horns", "Horns"], "to": "composer"},
    {"text": "Too bright overall.", "to": "mix"},
    {"text": "", "bars": "", "tracks": []},  # an empty row the sheet left open
]


def test_a_review_goes_out_as_one_task_per_seat_and_closes_the_review_task(song):
    rt = _review_task()
    out = review.review_send("fix", FINDINGS, notes="Strong theme; the climax needs to hit.", task=rt)
    assert out["findings"] == 3 and out["closed"] == rt and out["producer"] is None
    seats = {t["seat"]: t for t in out["tasks"]}
    assert list(seats) == ["composer", "mix"]  # in the order the work flows
    comp, mix = _task(seats["composer"]["id"]), _task(seats["mix"]["id"])
    assert seats["mix"]["fixes"] == 2 and mix["depends"] == [comp["id"]]  # mix waits for the notes to change
    assert comp["depends"] == [] and comp["from_review"] == out["report"]
    assert mix["scope"] == {"bars": "17-24", "tracks": ["Horns"]}
    assert "1. Bars 17-24 (Horns): The horn line" in mix["detail"] and "2. Too bright overall." in mix["detail"]
    assert "overall note: Strong theme" in mix["detail"]  # no Producer task carries it, so the seats get it
    assert "Bar 9 (Horns): Bar 9 lands" in comp["detail"]
    # the second listen waits on every fix and remembers what it follows up
    again = _task(out["again"])
    assert again["seat"] == "review" and set(again["depends"]) == {comp["id"], mix["id"]}
    assert again["recheck"] == out["report"]
    # the review task is closed with what became of it
    done = _task(rt)
    assert done["state"] == "done" and done["report"] == out["report"]
    assert done["result"].startswith("Needs work: 3 findings in review") and f"Second listen: {again['id']}" in done["result"]
    # filed as a report people read, and kept as data for the second listen
    rep = studio.report_read(out["report"])
    assert rep["kind"] == "review" and rep["by"] == "review" and rep["title"].startswith("Review of rev")
    assert "at 0:41" in rep["body"] and f"Mix, {mix['id']}" in rep["body"] and "once the others are in" in rep["body"]
    kept = review.stored(out["report"])
    assert [f["task"] for f in kept["findings"]] == [mix["id"], comp["id"], mix["id"]]


def test_findings_left_to_the_producer_and_what_it_hands_out_is_waited_for(song):
    rt = _review_task()
    out = review.review_send("fix", [{"text": "The B section never builds.", "bars": "33-48", "to": "producer"},
                                     {"text": "Kick and bass fight at 60 Hz.", "to": "mix"}],
                             notes="Close.", task=rt)
    prod, mix, again = _task(out["producer"]), _task(out["tasks"][0]["id"]), _task(out["again"])
    assert prod["seat"] == "producer" and prod["from_review"] == out["report"]
    assert "Left to you (1):" in prod["detail"] and "1. Bars 33-48: The B section" in prod["detail"]
    assert f"Already handed out by the user: {mix['id']} mix (1)." in prod["detail"]
    assert "overall note: Close." in prod["detail"] and "overall note" not in mix["detail"]
    assert mix["depends"] == [prod["id"]]
    # the Producer works on it and hands a finding to the Composer: Mix and the second listen wait for that too
    studio.task_update(prod["id"], state="working")
    studio.seat_join("producer")
    added = studio.task_add("composer", "Build the B section", detail="bars 33-48")
    assert sorted(added["waited_for_by"]) == sorted([mix["id"], again["id"]])
    # no cycle: a task the Producer puts after Mix's fixes is not something Mix waits for
    later = studio.task_add("composer", "Touch up after the mix", depends=[mix["id"]])
    assert later.get("waited_for_by") == [again["id"]]
    studio.seat_join(None)
    assert added["id"] in _task(mix["id"])["depends"] and later["id"] not in _task(mix["id"])["depends"]
    # the link is only while the Producer is on the review: after it, tasks are its own business again
    studio.task_update(prod["id"], state="done", result="handed out")
    studio.seat_join("producer")
    assert "waited_for_by" not in studio.task_add("composer", "Something else")
    studio.seat_join(None)


def test_a_note_alone_goes_to_the_producer_and_ready_needs_no_one(song):
    out = review.review_send("fix", [], notes="The whole thing is too busy to play under dialogue.")
    assert out["producer"] and out["tasks"] == [] and "acts on the note" in studio.report_read(out["report"])["body"]
    rt = _review_task()
    ok = review.review_send("ready", [], notes="Does the job.", task=rt)
    assert ok["producer"] is None and ok["tasks"] == [] and ok["again"] is None
    assert _task(rt)["result"] == f"Ready (review {ok['report']}): Does the job."


def test_a_second_listen_ticks_off_the_earlier_findings(song):
    first = review.review_send("fix", FINDINGS[:2], task=_review_task())
    again = first["again"]
    seen = review.review_draft(again)
    assert seen["draft"] is None and [f["n"] for f in seen["earlier"]["findings"]] == [1, 2]
    out = review.review_send("ready", [], task=again, checked={"1": "fixed", "2": "still", "9": "fixed"})
    body = studio.report_read(out["report"])["body"]
    assert f"## Since review {first['report']}" in body and "- Fixed: 1. Bars 17-24" in body and "- Still off: 2." in body
    assert review.stored(out["report"])["checked"] == {"1": "fixed", "2": "still"}


def test_drafts_survive_and_go_once_the_review_is_sent(song):
    rt = _review_task()
    review.review_draft(rt, {"notes": "half way", "findings": [{"text": "x"}]})
    assert review.review_draft(rt)["draft"]["notes"] == "half way"
    review.review_draft(None, {"notes": "no task"})
    assert review.review_draft()["draft"]["notes"] == "no task"
    review.review_send("ready", [], task=rt)
    assert review.review_draft(rt)["draft"] is None
    with pytest.raises(KeyError):
        review.review_draft("../../evil", {"x": 1})  # the browser names a task, never a file


@pytest.mark.parametrize("findings,notes,msg", [
    ([{"text": "x", "to": "arranger"}], "", "nobody is in that seat"),
    ([{"text": "x", "to": "review"}], "", "changes nothing"),
    ([{"text": "x", "bars": "nine", "to": "mix"}], "", "17-24"),
    ([{"bars": "4", "to": "mix"}], "", "says where but not what"),
    ([], "", "Say what needs work"),
])
def test_a_review_that_cannot_be_acted_on_is_refused(song, findings, notes, msg):
    with pytest.raises(ValueError, match=msg):
        review.review_send("fix", findings, notes=notes)
    assert studio.read()["reports"] == []


def test_without_a_producer_every_finding_needs_a_seat(song):
    studio.seat_assign("producer", "none")
    with pytest.raises(ValueError, match="say which seat"):
        review.review_send("fix", [{"text": "x"}])
    with pytest.raises(ValueError, match="Nobody is in the Producer seat"):
        review.review_send("fix", [], notes="too busy")
    assert review.review_send("fix", [{"text": "x", "to": "mix"}])["tasks"][0]["seat"] == "mix"


def test_only_an_open_review_task_is_answered(song):
    other = studio.task_add("mix", "Balance")["id"]
    with pytest.raises(KeyError):
        review.review_send("ready", [], task=other)
    rt = _review_task()
    review.review_send("ready", [], task=rt)
    with pytest.raises(ValueError, match="already done"):
        review.review_send("ready", [], task=rt)


def test_the_editor_can_send_a_review():
    from tunetrack import server
    assert {"review_send", "review_draft"} <= set(server.STUDIO)


def test_the_bench_carries_the_tempo_map(song):
    from tunetrack import bench
    doc = json.loads((Path(song) / "project.json").read_text(encoding="utf-8"))
    doc["tempo_changes"] = [[64, 120]]
    (Path(song) / "project.json").write_text(json.dumps(doc), encoding="utf-8")
    assert bench.seat_bench("review")["form"]["tempo_changes"] == [[64, 120]]


# ---- IT'S READY: the numbers before, the final mix after -----------------------------------------------------------

def test_checks_find_silent_bars_a_falling_ending_and_measure_earlier_findings():
    bars = [-18.0] * 16 + [None] + [-17.0] * 7 + [-14.0] * 8 + [-20.0, -21.0, -22.0, -21.0]
    earlier = {"findings": [{"n": 1, "bars": "17", "text": "gap"}, {"n": 2, "bars": "33-36", "text": "tails off"}]}
    out = review._checks(bars, earlier)
    assert out[0] == "Silent: bar 17."
    assert out[1].startswith("The ending falls away: bars 33-36")
    assert out[2].startswith("Finding 1 (bar 17): silent") and out[3].startswith("Finding 2 (bars 33-36): -20.9 LUFS, around it -14.0")
    # a held ending and no gaps: nothing to say; a reverb tail after the last bar is not a silent bar
    assert review._checks([-18.0] * 12 + [-13.0] * 4 + [None], None) == []


def test_a_ready_verdict_with_nothing_left_renders_the_final_mix(song, monkeypatch):
    started = []
    monkeypatch.setattr(review, "START_FINAL", lambda session: started.append(session))
    assert review.review_send("fix", [{"text": "Too bare.", "bars": "1", "to": "composer"}])["final"] is False
    assert not started
    out = review.review_send("ready", [], notes="It does the job.")
    assert out["final"] is True and len(started) == 1


def test_finalize_renders_the_session_and_files_it(song):
    api.write("Horns", "D4:w!mf | F4:w | A4:w | D5:w", at="1:1")
    rec = review.finalize(song)
    assert rec["file"].endswith("song_final.wav") and Path(rec["file"]).is_file()
    side = json.loads((Path(song) / "renders" / "song_final.json").read_text(encoding="utf-8"))
    assert side["lufs"] is not None and side["loop"] == "off" and side["bands_pct"]
    r = studio.report_read(rec["report"])
    assert r["kind"] == "final" and r["body"].startswith("**Final mix** · rev")
