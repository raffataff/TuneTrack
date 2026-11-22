"""A writing seat grows the cast: an accepted take that adds a part gives Sound its instrument task."""
import pytest

from tunetrack import api, studio, takes
from tunetrack.crew import briefs, craft


@pytest.fixture(autouse=True)
def _as_user():
    studio.seat_join(None)
    api.set_default_project(None)
    yield
    studio.seat_join(None)
    api.set_default_project(None)


@pytest.fixture
def song(tmp_path):
    api.project_new("Grow", folder=str(tmp_path / "song"), tempo=96, key="D minor")
    api.track_add("Piano", "synth:pad")
    api.write("Piano", "D4:q F4 A4 D5")
    return str(tmp_path / "song")


def _counterline(seat="arranger"):
    studio.seat_join(seat)
    t = takes.take_new()
    api.track_add("Counter", "synth:pad", role="violin")
    api.track_add("Plate", kind="bus")  # a bus is Mix's, not an instrument choice
    api.write("Counter", "r:h A4:q G4")
    takes.take_submit("Counterline in bar 1")
    studio.seat_join(None)
    api.set_default_project(None)
    return t["take"]


def _task(song, tid):
    return next(t for t in studio.studio_status(session=song)["tasks"] if t["id"] == tid)


def test_a_new_part_joins_the_sound_task_that_has_not_started(song):
    studio.seat_assign("sound", "human")
    planned = studio.task_add("sound", "Instruments for every part", "Piano: warm.", scope={"tracks": ["Piano"]})
    res = takes.take_accept(_counterline())
    assert res["new_parts"] == {"task": planned["id"], "added_to": True, "tracks": ["Counter"]}
    with studio._Board(song) as b:
        t = b.task(planned["id"])
        assert "Counter (violin, stand-in synth:pad)" in t["detail"] and "Plate" not in t["detail"]
        assert t["scope"]["tracks"] == ["Piano", "Counter"]


def test_without_one_sound_gets_a_new_task_and_mix_waits_for_it(song):
    studio.seat_assign("sound", "human")
    mix = studio.task_add("mix", "Mix it")
    res = takes.take_accept(_counterline())
    new = res["new_parts"]
    assert new["tracks"] == ["Counter"] and new["mix_waits"] == [mix["id"]]
    assert _task(song, new["task"])["seat"] == "sound"
    assert new["task"] in _task(song, mix["id"])["waiting_for"]


def test_an_empty_sound_seat_keeps_the_stand_in(song):
    res = takes.take_accept(_counterline())
    assert "Sound is empty" in res["new_parts"]["note"]
    assert not [t for t in studio.studio_status(session=song)["tasks"] if t["seat"] == "sound"]


def test_only_writing_seats_trigger_it(song):
    studio.seat_assign("sound", "human")
    assert "new_parts" not in takes.take_accept(_counterline("mix"))


def test_the_crew_is_told_the_cast_can_grow():
    assert "Growing the arrangement" in craft.craft("arranger") and "Growing" not in craft.craft("mix")
    assert "arrangement map" in briefs.PRODUCER and "brief.size" in briefs.PRODUCER
