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
    other["picture"] = {"file": "picture/b.mp4", "start": 0.0}
    assert takes.merge(base, take, other)[1]  # two pictures: someone decides


def test_hits_merge_one_by_one_and_only_the_same_frame_collides(proj):
    base = Project.load(proj).to_json()
    base["hits"] = [{"t": 2.0, "name": "door"}, {"t": 5.0, "name": "glass"}]
    copy = lambda: json.loads(json.dumps(base))  # noqa: E731
    take, session = copy(), copy()
    take["hits"] = base["hits"] + [{"t": 3.0, "name": "step"}]           # adds one
    session["hits"] = [{"t": 2.0, "name": "door"}, {"t": 9.0, "name": "boom"}]  # removes glass, adds boom
    merged, conflicts, _ = takes.merge(base, take, session)
    assert not conflicts and [h["name"] for h in merged["hits"]] == ["door", "step", "boom"]
    assert [c["what"] for c in takes.changes(base, take) if c["where"] == "hits"] == ["added 'step' at 3.00 s"]

    # moving a hit is the old frame gone and a new one there: it merges with an edit elsewhere
    take = copy()
    take["hits"] = [{"t": 2.4, "name": "door"}, {"t": 5.0, "name": "glass"}]
    session = copy()
    session["hits"] = [{"t": 2.0, "name": "door"}, {"t": 5.0, "name": "glass", "soft": True}]
    merged, conflicts, _ = takes.merge(base, take, session)
    assert not conflicts and merged["hits"] == [{"t": 2.4, "name": "door"}, {"t": 5.0, "name": "glass", "soft": True}]

    # the same hit changed two ways, or changed on one side and removed on the other: someone decides
    take, session = copy(), copy()
    take["hits"][0]["name"], session["hits"][0]["name"] = "door slams", "door opens"
    merged, conflicts, _ = takes.merge(base, take, session)
    assert len(conflicts) == 1 and "both changed" in conflicts[0]["why"] and merged["hits"][0]["name"] == "door opens"
    assert takes.merge(base, take, session, prefer="src")[0]["hits"][0]["name"] == "door slams"
    session["hits"] = session["hits"][1:]
    assert "removed on one side" in takes.merge(base, take, session)[1][0]["why"]
    # within half a frame is the same frame: two seats marking one moment is one decision, not two hits
    take, session = copy(), copy()
    take["hits"].append({"t": 7.0, "name": "a"})
    session["hits"].append({"t": 7.01, "name": "b"})
    assert takes.merge(base, take, session)[1][0]["why"] == "both added a hit at this frame"
    # everything removed on both sides leaves no empty list behind
    take, session = copy(), copy()
    take["hits"] = session["hits"] = []
    assert "hits" not in takes.merge(base, take, session)[0]


def test_drop_frame_timecode_keeps_up_with_the_clock():
    fps = 29.97
    assert pic.timecode(1799 / fps, fps, df=True) == "00:00:59;29"   # the last label of the first minute
    assert pic.timecode(1800 / fps, fps, df=True) == "00:01:00;02"  # ;00 and ;01 are skipped
    assert pic.timecode(17982 / fps, fps, df=True) == "00:10:00;00"  # but not at every tenth minute
    assert pic.timecode(1800 / fps, fps) == "00:01:00:00"  # non-drop counts every frame number
    assert pic.timecode(3600.0, fps, df=True) == "01:00:00;00"  # an hour of drop-frame is an hour of clock
    for t in (0.0, 59.9, 60.1, 599.9, 600.1, 1234.567, 3599.0):
        assert abs(pic.parse_time(pic.timecode(t, fps, df=True), fps, df=True) - t) <= 0.5 / fps
    assert pic.parse_time("00:01:00;02", fps) == pytest.approx(1800 / fps)  # ";" says drop-frame by itself
    assert pic.parse_time("01:00:01;00", fps, tc_start="01:00:00:00", df=True) == pytest.approx(30 / fps)
    assert pic.timecode(1.0, fps, "01:00:00:00", df=True) == "01:00:01;00"
    assert pic.timecode(3600 / 59.94, 59.94, df=True) == "00:01:00;04" and pic.timecode(62.0, 25, df=True) == "00:01:02:00"


def test_the_picture_says_its_timecode_kind_and_can_be_switched(proj, monkeypatch):
    monkeypatch.setattr(pic, "scan", lambda p, redo=False: {})  # no video file behind this picture
    with api._Edit(proj) as p:
        p.picture = {"file": "picture/x.mp4", "start": 0.0, "fps": 29.97, "duration": 120.0, "tc": "01:00:00:00"}
        p.hits = [{"t": 60.06, "name": "minute"}]
    assert api.hits(project=proj)["hits"][0]["tc"] == "01:01:00:00"  # non-drop: an hour's labels run 3.6 s behind the clock
    api.picture(drop_frame=True, project=proj)
    assert api.hits(project=proj)["hits"][0]["tc"] == "01:01:00;02"
    assert api.project_info(proj, clips=False)["picture"]["drop_frame"] is True
    api.hits(add=[{"t": "01:01:30;00", "name": "typed"}], project=proj)
    assert next(h for h in api.hits(project=proj)["hits"] if h["name"] == "typed")["tc"] == "01:01:30;00"
    with api._Edit(proj) as p:
        p.picture = {**p.picture, "fps": 25.0, "df": False}
    with pytest.raises(ValueError, match="29.97"):
        api.picture(drop_frame=True, project=proj)


def test_a_free_start_moves_where_the_music_enters_to_catch_the_hits(proj):
    # hits every 2.5 s but 0.13 s late against the music: no steady tempo near 120 catches them from 0
    with api._Edit(proj) as p:
        p.picture = {"file": "picture/x.mp4", "start": 0.0, "fps": 25.0, "duration": 30.0}
    api.hits(add=[{"t": 2.5 * i + 0.13, "name": f"h{i}"} for i in range(1, 6)], project=proj)
    fixed = api.tempo_fit(low=115, high=125, project=proj)["candidates"][0]
    free = api.tempo_fit(low=115, high=125, free_start=True, project=proj)["candidates"][0]
    assert fixed["rms_ms"] > 15 and free["rms_ms"] < 1 and round(free["tempo"]) == 120
    assert free["start"] == pytest.approx(0.37, abs=0.002)  # a beat is 0.5 s: the picture goes 0.37 s later, not 0.13 s before 0
    r = api.tempo_fit(tempo=120, free_start=True, apply=True, project=proj)
    assert Project.load(proj).picture["start"] == pytest.approx(0.37, abs=0.002) and r["start"] == Project.load(proj).picture["start"]
    assert all(h["on"] in ("bar", "beat") for h in r["hits"])
    # a picture that already enters before the music may move either way: the nearest place wins
    with api._Edit(proj) as p:
        p.picture["start"] = -1.0
    assert api.tempo_fit(low=118, high=122, free_start=True, project=proj)["candidates"][0]["shift_ms"] == pytest.approx(-130, abs=2)
    with api._Edit(proj) as p:
        p.picture = None
    with pytest.raises(ValueError, match="set a picture"):
        api.tempo_fit(free_start=True, project=proj)


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


# ---- the guide: the picture's own sound under the music on LIVE ---------------------------------------------------

@needs_ffmpeg
@pytest.mark.skipif(not __import__("tunetrack.live", fromlist=["x"]).engine_runs(), reason="the live engine is not built")
def test_the_engine_plays_the_pictures_sound_past_the_master_and_only_when_asked(proj, video, tmp_path):
    import numpy as np
    import soundfile as sf
    from tunetrack import live
    api.picture(file=video, start=1.0, project=proj)       # the clicks at 2 s and 5 s of the picture: 3 s and 6 s of the song
    api.project_set(project=proj, master_volume_db=-40.0)  # the guide does not go through the master
    p, sr = Project.load(proj), 48000
    stem = pic.guide_stem(p, sr, p.root / "cache")
    ref = sf.read(str(stem), dtype="float32", always_2d=True)[0]
    onset = lambda x: int(np.argmax(np.abs(x[:, 0]) > 0.1))  # noqa: E731 - where the first click begins (80 ms long)
    assert ref.shape[1] == 2 and abs(onset(ref) / sr - 3.0) < 0.03  # placed by the start
    assert pic.guide_stem(p, sr, p.root / "cache") == stem  # made once

    def pull(player) -> np.ndarray:
        out = tmp_path / "heard.wav"
        player.engine.call("pull", timeout=120, device_sr=sr, block=480, length=4 * sr, out=str(out))
        return sf.read(str(out), dtype="float32", always_2d=True)[0]

    spec = live.build_spec(p, guide=-6.0)[0]
    g = next(t for t in spec["tracks"] if t["id"] == live.GUIDE)
    assert g["out"] == "direct" and g["db"] == -6.0 and g["source"]["file"] == str(stem)
    assert not any(t["id"] == live.GUIDE for t in live.build_spec(p)[0]["tracks"])  # renders never ask for it
    player = live.Player(proj, no_device=True, guide=-6.0)
    try:
        heard = pull(player)
        at = onset(heard * 2)  # heard 6 dB down
        assert abs(at - onset(ref)) <= 1  # in time with the stem: nothing in its way
        assert np.abs(heard[at - 2000:at + 2000]).max() / np.abs(ref[at - 2000:at + 2000]).max() == pytest.approx(10 ** (-6 / 20), rel=0.03)
        assert player.set_guide(-6.0) is None  # the same level: no reload
        player.set_guide(None)
        assert np.abs(pull(player)).max() == 0
    finally:
        player.close()
