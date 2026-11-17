"""Stage 5: a video locked to the song, its hit points, and the tempo that puts the hits on beats."""
import asyncio
import json
import shutil
import subprocess

import pytest

from tunetrack import api, picture as pic, takes
from tunetrack.model import Project, TempoMap

needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not on PATH")


@pytest.fixture
def proj(tmp_path):
    path = str(tmp_path / "cue")
    api.project_new("cue", folder=path, tempo=100)
    return path


@pytest.fixture(scope="module")
def video(tmp_path_factory):
    """7.5 s, 30 fps: three shots (cuts at 2.0 and 5.0 s) and a click on each cut."""
    out = tmp_path_factory.mktemp("vid") / "clip.mp4"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=d=2:s=320x180:r=30",
                    "-f", "lavfi", "-i", "smptebars=d=3:s=320x180:r=30",
                    "-f", "lavfi", "-i", "mandelbrot=s=320x180:r=30",
                    "-f", "lavfi", "-i", "aevalsrc='0.6*sin(2*PI*880*t)*(between(t,2,2.08)+between(t,5,5.08))"
                                         "+0.001*sin(300*t)':d=7.5:s=48000",
                    "-filter_complex", "[2:v]trim=duration=2.5[c];[0:v][1:v][c]concat=n=3:v=1[v]",
                    "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-t", "7.5", str(out)], check=True)
    return str(out)


# ---- time ---------------------------------------------------------------------------------------------------------

def test_picture_times_are_read_as_seconds_minutes_or_timecode():
    assert pic.parse_time(12.4) == 12.4 and pic.parse_time("12.4") == 12.4 and pic.parse_time("12.4s") == 12.4
    assert pic.parse_time("1:02.5") == 62.5
    assert pic.parse_time("00:00:10:15", fps=30) == 10.5
    assert pic.parse_time("01:00:10:15", fps=30, tc_start="01:00:00:00") == 10.5  # from the file's own start
    assert pic.timecode(10.5, 30) == "00:00:10:15"
    assert pic.timecode(10.5, 30, "01:00:00:00") == "01:00:10:15"
    assert pic.timecode(pic.parse_time("00:01:02:07", 24), 24) == "00:01:02:07"
    with pytest.raises(ValueError, match="timecode"):
        pic.parse_time("soon")


# ---- hits ---------------------------------------------------------------------------------------------------------

def test_a_hit_belongs_to_the_picture_and_says_where_it_falls_in_the_music(proj):
    r = api.hits(add=[{"t": 6.0, "name": "door"}, {"t": "0:11.2", "name": "reveal"}, {"t": 3, "name": "glance", "soft": True}],
                 project=proj)["hits"]
    assert [h["name"] for h in r] == ["glance", "door", "reveal"]  # in picture order
    door, reveal = r[1], r[2]
    assert door["at"] == "3:3" and door["off_ms"] == 0 and door["on"] == "beat"  # 6 s at 100 bpm = beat 10
    assert reveal["off_ms"] == -200 and "on" not in reveal  # 11.2 s: 200 ms before beat 19
    assert r[0]["soft"] is True

    # the tempo changes: the hit keeps its second, and now sits on another beat
    api.project_set(project=proj, tempo=120)
    door = next(h for h in api.hits(project=proj)["hits"] if h["name"] == "door")
    assert door["t"] == 6.0 and door["at"] == "4:1" and door["on"] == "bar"
    assert api.project_info(proj, clips=False)["hits"][1] == {"name": "door", "at": "4:1", "off_ms": 0, "on": "bar"}

    # a hit named by a place in the music is the moment that is there now
    api.hits(add=[{"at": "2:1", "name": "bar two"}], project=proj)
    assert next(h for h in Project.load(proj).hits if h["name"] == "bar two")["t"] == 2.0
    api.hits(remove=["glance", 6.0], project=proj)
    assert [h["name"] for h in Project.load(proj).hits] == ["bar two", "reveal"]
    api.hits(clear=True, project=proj)
    assert "hits" not in json.loads((Project.load(proj).file).read_text(encoding="utf-8"))  # absent, not empty


def test_the_picture_start_moves_every_hit_with_it(proj):
    with api._Edit(proj) as p:
        p.picture = {"file": "picture/x.mp4", "start": 1.2, "fps": 25.0, "duration": 30.0}
        p.hits = [{"t": 4.8, "name": "impact"}]
    h = api.hits(project=proj)["hits"][0]
    assert h["at"] == "3:3" and h["on"] == "beat" and h["tc"] == "00:00:04:20"  # 4.8 + 1.2 = 6 s
    with api._Edit(proj) as p:
        p.picture["start"] = -6.0
    assert api.hits(project=proj)["hits"][0]["at"] == "before the music"


# ---- tempo --------------------------------------------------------------------------------------------------------

def test_a_steady_tempo_is_found_that_catches_the_hits(proj):
    # hits every 2.5 s: on the beat at 96 (4 beats) and 120 (5 beats), nowhere near at 100
    api.hits(add=[{"t": 2.5 * i, "name": f"h{i}"} for i in range(1, 6)], project=proj)
    c = api.tempo_fit(low=90, high=130, project=proj)["candidates"]
    assert {round(c[0]["tempo"]), round(c[1]["tempo"])} == {96, 120}
    assert c[0]["rms_ms"] < 1 and c[0]["worst_ms"] == 0 and c[2]["rms_ms"] > 20
    assert Project.load(proj).tempo == 100  # nothing written without apply
    r = api.tempo_fit(low=110, high=130, apply=True, project=proj)
    assert r["applied"] and Project.load(proj).tempo == 120.0
    assert all(h["on"] in ("bar", "beat") for h in r["hits"])
    with pytest.raises(ValueError, match="no hit points"):
        api.hits(clear=True, project=proj)
        api.tempo_fit(project=proj)


def test_a_tempo_map_lands_every_hard_hit_exactly_and_leaves_soft_ones(proj):
    api.hits(add=[{"t": 6.3, "name": "a"}, {"t": 11.9, "name": "b"}, {"t": 14.0, "name": "soft", "soft": True},
                  {"t": 19.25, "name": "c"}], project=proj)
    before = api.tempo_fit(mode="map", tempo=100, project=proj)
    assert Project.load(proj).tempo_changes == []
    assert before["missed"] == []
    fit = api.tempo_fit(mode="map", tempo=100, apply=True, project=proj)
    p = Project.load(proj)
    tm = p.tempo_map()
    for t in (6.3, 11.9, 19.25):
        b = tm.beat(t)
        assert abs(b - round(b)) < 1e-4  # on a beat, to well under a sample
    assert all(92 <= bpm <= 108 for bpm in tm.bpms)  # within the 8% bend
    assert tm.bpms[-1] == 100  # after the last hit it is back at the tempo asked for
    rows = {h["name"]: h for h in fit["hits"]}
    assert rows["a"]["off_ms"] == 0 and rows["c"]["on"] in ("bar", "beat") and "on" not in rows["soft"]

    bars = api.tempo_fit(mode="map", tempo=100, on="bar", bend=25, project=proj)
    tm = TempoMap(bars["tempo"], bars["tempo_changes"])
    hard = [t for t in (6.3, 11.9, 19.25) if f"{t}" not in bars["missed"]]
    assert all(abs(tm.beat(t) / 4 - round(tm.beat(t) / 4)) < 1e-4 for t in hard)

    # a hit no tempo within the bend can reach is said, not forced
    api.hits(clear=True, project=proj)
    api.hits(add=[{"t": 0.9, "name": "too soon"}, {"t": 6.0, "name": "fine"}], project=proj)
    assert api.tempo_fit(mode="map", tempo=100, bend=5, project=proj)["missed"] == ["too soon"]


# ---- takes --------------------------------------------------------------------------------------------------------

def test_hits_and_the_picture_travel_through_a_take_as_form(proj):
    base = Project.load(proj).to_json()
    take = json.loads(json.dumps(base))
    take["hits"] = [{"t": 3.0, "name": "x"}]
    take["picture"] = {"file": "picture/a.mp4", "start": 0.0}
    session = json.loads(json.dumps(base))
    session["tempo"] = 90
    merged, conflicts, _ = takes.merge(base, take, session)
    assert not conflicts and merged["hits"] == take["hits"] and merged["picture"]["file"] == "picture/a.mp4"
    assert merged["tempo"] == 90
    kinds = {(c["kind"], c["where"]) for c in takes.changes(base, take)}
    assert ("form", "hits") in kinds and ("form", "picture") in kinds
    other = json.loads(json.dumps(base))
    other["hits"] = [{"t": 9.0, "name": "y"}]
    assert takes.merge(base, take, other)[1]  # both wrote the cue sheet: someone decides


def test_spotting_has_the_picture_tools_and_a_writing_seat_does_not():
    from tunetrack import roles
    assert roles.PICTURE <= roles.tools_for("spotting")
    assert roles.LOOK <= roles.tools_for("producer") and "tempo_fit" not in roles.tools_for("producer")
    assert not roles.PICTURE & roles.tools_for("composer")


# ---- the video itself ---------------------------------------------------------------------------------------------

@needs_ffmpeg
def test_a_video_becomes_the_picture_and_is_read_for_cuts_and_sound(proj, video):
    r = api.picture(file=video, project=proj)
    p = Project.load(proj)
    assert p.picture["file"] == "picture/clip.mp4" and (p.root / p.picture["file"]).is_file()  # copied in
    assert p.picture["duration"] == 7.5 and p.picture["fps"] == 30.0 and p.picture["audio"] and "proxy" not in p.picture
    assert r["picture"]["ends_tc"] == "00:00:07:15" and r["picture"]["ends_at"] == "4:1.5"
    assert [c[0] for c in r["cuts"]] == [2.0, 5.0] and r["cuts"][1][1] == "3:1.33"
    assert [round(t, 1) for t, _ in r["sound"]["peaks"]] == [2.0, 5.0]  # the clicks: likely hit points
    view = pic.view(p)
    assert view["cuts"] == [2.0, 5.0] and view["strip"]["n"] == 8 and pic._side(p, ".strip.jpg").is_file()

    # found once: a second look reads the file beside the video
    marked = pic._side(p, ".scan.json").stat().st_mtime_ns
    api.picture(project=proj)
    assert pic._side(p, ".scan.json").stat().st_mtime_ns == marked

    moved = api.picture(start=1.0, project=proj)  # the first frame a second into the music
    assert moved["picture"]["start_s"] == 1.0 and moved["cuts"][0][1] == "2:2"  # 3 s at 100 bpm = beat 5
    assert api.project_info(proj, clips=False)["picture"]["duration_s"] == 7.5
    api.picture(clear=True, project=proj)
    assert Project.load(proj).picture is None and api.picture(project=proj) == {"picture": None}
    with pytest.raises(ValueError, match="not a video"):
        api.picture(file=__file__, project=proj)


@needs_ffmpeg
def test_frames_come_back_as_one_labelled_sheet_and_as_an_image_over_mcp(proj, video, monkeypatch):
    from PIL import Image

    from tunetrack import mcp_server
    api.picture(file=video, project=proj)
    api.hits(add=[{"t": 5.0, "name": "bars end"}], project=proj)
    r = api.picture_frames(at="cuts", project=proj)
    assert [f["n"] for f in r["frames"]] == [1, 2, 3] and r["frames"][1]["t"] == pytest.approx(2.07, abs=0.01)
    with Image.open(r["file"]) as im:
        assert im.size == (3 * 320, 180 + 16)
    assert len(api.picture_frames(count=6, project=proj)["frames"]) == 6
    assert api.picture_frames(at="hits", project=proj)["frames"] == [{"n": 1, "t": 5.0, "at": "3:1.33"}]
    with pytest.raises(ValueError, match="no frames"):
        api.picture_frames(at=[99], project=proj)

    api.picture(file=r["file"], note="the cave, torches", project=proj)  # an image is a screenshot, not the picture
    p = Project.load(proj)
    assert p.picture["file"] == "picture/clip.mp4" and p.brief["stills"][0]["note"] == "the cave, torches"
    assert api.picture_frames(at="stills", project=proj)["stills"][0]["n"] == 1

    api.set_default_project(proj)
    try:
        out = asyncio.run(mcp_server.mcp.call_tool("picture_frames", {"at": "cuts"}))
        content = out.content if hasattr(out, "content") else out[0] if isinstance(out, tuple) else out
        kinds = [c.type for c in content]
        assert kinds == ["text", "image"] and content[1].mime_type == "image/jpeg"
        assert "file" in json.loads(content[0].text)
        monkeypatch.setenv("TUNETRACK_SEAT", "spotting")  # a launched agent cannot open a path: it gets the image only
        out = asyncio.run(mcp_server.mcp.call_tool("picture_frames", {"at": "cuts"}))
        content = out.content if hasattr(out, "content") else out[0] if isinstance(out, tuple) else out
        assert "file" not in json.loads(content[0].text) and content[1].type == "image"
    finally:
        api.set_default_project(None)


@needs_ffmpeg
def test_the_mix_is_exported_under_the_picture(proj, video):
    api.picture(file=video, start=1.0, project=proj)
    api.track_add("Lead", instrument="synth:pluck", project=proj)
    api.write("Lead", "C4:q D4 E4 G4 | C5:w", project=proj)
    rep = api.render(project=proj, out="cue.mp4")
    info = pic.probe(rep["files"][0])
    assert rep["files"][0].endswith("cue.mp4") and rep["files"][1].endswith(".wav")
    assert info["codec"] == "h264" and info["audio"] and abs(info["duration"] - 7.5) < 0.2  # as long as the picture
    api.picture(clear=True, project=proj)
    with pytest.raises(ValueError, match="no picture"):
        api.render(project=proj, out="cue.mp4")


@needs_ffmpeg
def test_the_desk_gets_the_video_in_ranges_and_the_strip(proj, video):
    from starlette.testclient import TestClient

    from tunetrack import server
    api.project_open(proj)
    api.picture(file=video, project=proj)
    c = TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")
    st = c.get("/api/state").json()
    assert st["picture"]["cuts"] == [2.0, 5.0] and st["doc"]["picture"]["fps"] == 30.0
    r = c.get("/api/picture?kind=video", headers={"Range": "bytes=100-199"})
    assert r.status_code == 206 and len(r.content) == 100  # seeking in the page depends on this
    assert c.get("/api/picture?kind=strip").headers["content-type"] == "image/jpeg"
    for fn in ("hits", "picture", "tempo_fit"):
        assert fn in server.CALLABLE
    api.picture(clear=True, project=proj)
    assert c.get("/api/picture?kind=video").status_code == 404
