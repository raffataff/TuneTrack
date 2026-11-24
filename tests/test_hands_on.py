"""Going hands-on with the crew's work (signoff.your_version / hand_over / take_yours): your own version instead of
a seat's take, and handing a take of yours to the Producer or straight into the song. Scripted agents only."""
import json
from pathlib import Path

import pytest

from tunetrack import api, signoff, studio, takes
from test_signoff import COMPOSER, PRODUCER, _as_user, agent, open_items, piano, prompts, run, seat, song  # noqa: F401


def _meta(song, tk):
    return json.loads((Path(song) / "takes" / tk / "take.json").read_text(encoding="utf-8"))


def _stopped(song, tmp_path):
    seat("producer", agent(tmp_path, "producer", PRODUCER))
    seat("composer", agent(tmp_path, "composer", COMPOSER))
    signoff.seat_signoff("composer")
    studio.task_add("composer", "Write the hook")
    run(song)
    [item] = open_items()
    return item


def test_your_own_version_replaces_the_seats_take_and_goes_to_the_producer(song, tmp_path):
    item = _stopped(song, tmp_path)
    mine = signoff.your_version(item["id"])
    assert mine["replaces"] == "tk1" and not mine["continued"]
    assert signoff.your_version(item["id"]) == {**mine, "continued": True}  # picked up again, not a second one
    api.write("Piano", "F5:w", at="1:1", project=str(Path(song) / "takes" / mine["take"]))
    out = signoff.hand_over(mine["take"], "My own hook", check=["mix", "producer"])
    assert out["put_away"] == "tk1" and _meta(song, "tk1")["state"] == "rejected"
    assert open_items() == []  # their stop went with their take
    run(song)
    assert _meta(song, mine["take"])["state"] == "accepted" and (77, 0.0) in piano(song)
    told = next(p for p in prompts(song) if mine["take"] in p)
    assert "The user made this themself instead of the Composer's take tk1" in told and "Mix check it" in told
    [t1] = [t for t in studio.task_list(state="done")["tasks"] if t["seat"] == "composer"]
    assert t1["take"] == mine["take"]


def test_a_take_of_yours_can_go_straight_into_the_song(song):
    tk = takes.take_new("louder piano", use=False)["take"]
    api.track_update("Piano", volume_db=-3.0, project=str(Path(song) / "takes" / tk))
    out = signoff.hand_over(tk, to="song")
    assert out["accepted"] and _meta(song, tk)["state"] == "accepted"
    doc = json.loads((Path(song) / "project.json").read_text(encoding="utf-8"))
    assert next(t for t in doc["tracks"] if t["name"] == "Piano")["volume_db"] == -3.0


def test_handing_over_to_the_producer_needs_its_agent_and_no_stop_in_the_way(song, tmp_path):
    tk = takes.take_new(use=False)["take"]
    api.track_update("Piano", volume_db=-2.0, project=str(Path(song) / "takes" / tk))
    with pytest.raises(ValueError, match="No agent sits in the Producer seat"):
        signoff.hand_over(tk)
    item = _stopped(song, tmp_path)
    with pytest.raises(ValueError, match=f"answer it there"):
        signoff.hand_over(item["take"])


def test_co_producer_does_not_stop_you_for_your_own_take(song, tmp_path):
    seat("producer", agent(tmp_path, "producer", PRODUCER))
    signoff.seat_signoff("producer")
    tk = takes.take_new(use=False)["take"]
    api.track_update("Piano", volume_db=-5.0, project=str(Path(song) / "takes" / tk))
    signoff.hand_over(tk, "quieter")
    run(song)
    assert _meta(song, tk)["state"] == "accepted" and open_items() == []


def test_what_you_changed_after_a_hand_in_is_listed(song, tmp_path):
    item = _stopped(song, tmp_path)
    assert signoff.take_yours("tk1")["changes"] == []
    api.track_update("Piano", volume_db=-4.0, project=str(Path(song) / "takes" / "tk1"))
    [c] = signoff.take_yours("tk1")["changes"]
    assert c.startswith("Piano")
