"""Spotting's bench: the picture, the cue sheet and what a take did to it. No ffmpeg: the scan file is written by hand."""
import json
from pathlib import Path

import pytest

from tunetrack import api, studio, takes
from tunetrack.bench import seat_bench


@pytest.fixture(autouse=True)
def _as_user():
    studio.seat_join(None)
    api.set_default_project(None)
    yield
    studio.seat_join(None)
    api.set_default_project(None)


@pytest.fixture
def cue(tmp_path):
    path = tmp_path / "cue"
    api.project_new("cue", folder=str(path), tempo=120)
    (path / "picture").mkdir()
    (path / "picture" / "clip.mp4").write_bytes(b"not really a video")
    (path / "picture" / "clip.strip.jpg").write_bytes(b"\xff\xd8\xff")
    level = [-60.0] * 6 + [-10.0] * 6 + [-60.0] * 8  # loud from 3 s to 6 s
    (path / "picture" / "clip.scan.json").write_text(json.dumps({
        "mark": [18, 1], "scores": [[2.0, 0.9], [2.1, 0.95], [4.0, 0.1], [5.0, 0.5]],
        "sound": {"level": level, "peaks": [[3.0, -8.0]]}, "strip": {"step": 1.0, "n": 10, "w": 128, "h": 72}}),
        encoding="utf-8")
    (path / "picture" / "stills").mkdir()
    (path / "picture" / "stills" / "cave.png").write_bytes(b"\x89PNG fake")
    with api._Edit(str(path)) as p:
        p.picture = {"file": "picture/clip.mp4", "start": 0.0, "fps": 25.0, "duration": 10.0, "size": [320, 180],
                     "audio": True, "codec": "h264"}
        p.hits = [{"t": 2.0, "name": "door"}, {"t": 4.1, "name": "glance", "soft": True}]
        p.brief["stills"] = [{"file": "picture/stills/cave.png", "note": "the cave, torches"}]
    return path


def test_spotting_opens_on_the_picture_and_its_cue_sheet(cue):
    b = seat_bench("spotting")
    assert b["view"] == "picture"
    P = b["picture"]
    assert P["picture"] == {"file": "clip.mp4", "duration": 10.0, "fps": 25.0, "size": [320, 180], "start": 0.0,
                            "tc": None, "audio": True, "mark": [18, 1]}
    assert P["cuts"] == [[2.0, "2:1"], [5.0, "3:3"]]  # 2.1 is the same cut; 4.0 scores under the threshold
    assert P["loud"] == [[3.0, 6.0, -10.0]] and P["peaks"] == [[3.0, -8.0]] and P["strip"]["n"] == 10
    door, glance = P["hits"]
    assert door["name"] == "door" and door["on"] == "bar" and door["at"] == "2:1" and door["tc"] == "00:00:02:00"
    assert glance["soft"] and glance["off_ms"] == 100 and "new" not in door
    assert P["bars"][:3] == [[0.0, 1], [2.0, 2], [4.0, 3]] and P["span"] == 10.0
    assert P["stills"] == [{"n": 0, "file": "cave.png", "note": "the cave, torches"}]
    assert seat_bench("producer")["view"] == "plan" and "picture" not in seat_bench("producer")


def test_without_a_video_hits_are_in_song_time(tmp_path):
    api.project_new("game", folder=str(tmp_path / "g"), tempo=60)
    api.hits(add=[{"t": 3.0, "name": "boss"}])
    P = seat_bench("spotting")["picture"]
    assert P["picture"] is None and P["cuts"] == [] and P["hits"][0]["at"] == "1:4" and P["span"] == 5.0


def test_a_take_marks_the_hits_it_added_and_the_ones_it_removed(cue):
    api.set_default_project(str(cue))
    studio.seat_join("spotting")
    t = takes.take_new()["take"]
    api.hits(remove=["door"], add=[{"t": 6.0, "name": "explosion"}])
    api.hits(remove=["glance"], add=[{"t": 4.1, "name": "a look"}])  # renamed: same moment, not new
    studio.seat_join(None)
    api.set_default_project(str(cue))
    b = seat_bench("spotting", take=t)
    assert b["source"]["take"] == t
    P = b["picture"]
    assert {h["name"]: bool(h.get("new")) for h in P["hits"]} == {"a look": False, "explosion": True}
    assert [h["name"] for h in P["gone"]] == ["door"]
    assert P["picture"]["file"] == "clip.mp4" and P["cuts"]  # the take reads the session's picture folder
    assert "gone" not in seat_bench("spotting", take="session")["picture"]


def test_the_picture_endpoint_refuses_bad_frames_and_stills(cue):
    from starlette.testclient import TestClient

    from tunetrack import server
    api.project_open(str(cue))
    c = TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")
    for qs in ("kind=frame", "kind=frame&t=abc", "kind=frame&t=nan", "kind=frame&t=-1", "kind=frame&t=99",
               "kind=frame&t=1&w=50", "kind=frame&t=1&w=2000", "kind=frame&t=1&w=x",
               "kind=still", "kind=still&n=x", "kind=still&n=1", "kind=still&n=-1", "kind=still&n=0&take=../x"):
        assert c.get("/api/picture?" + qs).status_code == 404, qs
    r = c.get("/api/picture?kind=still&n=0")
    assert r.status_code == 200 and r.content == b"\x89PNG fake"
    with api._Edit(str(cue)) as p:  # a still pointing outside the session is not served
        p.brief["stills"] = [{"file": "../../outside.png"}]
    (Path(cue).parent.parent / "outside.png").write_bytes(b"x")
    assert c.get("/api/picture?kind=still&n=0").status_code == 404
