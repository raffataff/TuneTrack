"""Tracking (stage 4): recording through the live engine, passes, comping, the MIDI keyboard.

The engine tests run without an audio device: the `pull` op plays a file as the interface's input and a list of
events as the keyboard, so nothing sounds and nothing depends on the room."""
import numpy as np
import pytest
import soundfile as sf

from tunetrack import api, live, tracking
from tunetrack.model import Clip, Project, Track

ENGINE_RUNS = pytest.mark.skipif(not live.engine_runs(),
                                 reason="the live engine is not built, or Windows will not start it (engine/README.md)")
SR = 48000
BEAT = SR // 2  # 120 bpm


# ---- passes and latency -----------------------------------------------------------------------------------------

def test_one_pass_is_moved_earlier_by_the_latency():
    # recording started with the engine at song sample 1000; everything is 300 samples late
    assert tracking.passes([0, 1000], 5000, 300) == [{"k": 0, "at": 0, "pos": 700, "n": 5000}]
    # nothing before the punch is kept: the count-in, and the latency's worth before it
    assert tracking.passes([0, 1000], 5000, 300, floor=1200) == [{"k": 0, "at": 500, "pos": 1200, "n": 4500}]


def test_each_time_round_the_loop_is_a_pass():
    # loop 1000..3000; the transport wrapped after 2000 and 4000 samples of recording
    got = tracking.passes([0, 1000, 2000, 1000, 4000, 1000], 5000, 100)
    assert got == [{"k": 0, "at": 0, "pos": 900, "n": 2100},      # runs to the loop's end as it was heard
                   {"k": 1, "at": 2100, "pos": 1000, "n": 2000},
                   {"k": 2, "at": 4100, "pos": 1000, "n": 900}]
    assert tracking.locate(2050, [0, 1000, 2000, 1000], 100) == (0, 2950)   # still the end of the first pass
    assert tracking.locate(2100, [0, 1000, 2000, 1000], 100) == (1, 1000)


# ---- comping ------------------------------------------------------------------------------------------------

def _two_passes() -> tuple[Project, Track]:
    p = Project(name="c", tempo=120)
    t = Track(id="t1", name="Gtr", kind="audio")
    t.clips = [Clip(id="a", kind="audio", file="audio/x.wav", start=0, length=16, offset=0.0, lane=1, muted=True),
               Clip(id="b", kind="audio", file="audio/x.wav", start=0, length=16, offset=8.0, lane=2)]
    p.tracks.append(t)
    p.next_id = 10
    return p, t


def _heard(t: Track) -> list:
    return sorted((c.start, c.end, c.lane) for c in t.clips if not c.muted)


def test_comp_cuts_at_the_edges_and_swaps_what_is_heard():
    p, t = _two_passes()
    res = tracking.comp(p, t, 4, 8, 1)
    assert _heard(t) == [(0, 4, 2), (4, 8, 1), (8, 16, 2)]
    assert res["changed"] == 2 and len(t.clips) == 6
    mid = next(c for c in t.clips if c.lane == 1 and not c.muted)
    assert mid.offset == pytest.approx(2.0) and mid.fade_in == mid.fade_out == tracking.DECLICK
    assert next(c for c in t.clips if c.lane == 2 and c.start == 8).offset == pytest.approx(12.0)


def test_comping_back_puts_the_clips_together_again():
    p, t = _two_passes()
    tracking.comp(p, t, 4, 8, 1)
    tracking.comp(p, t, 4, 8, 2)
    assert _heard(t) == [(0, 16, 2)] and len(t.clips) == 2
    assert all(c.fade_in == 0 and c.fade_out == 0 for c in t.clips)


def test_comp_names_the_passes_when_asked_for_one_that_is_not_there():
    p, t = _two_passes()
    with pytest.raises(ValueError, match="no pass 5.*passes there: 1, 2"):
        tracking.comp(p, t, 0, 4, 5)


def test_a_new_recording_is_heard_over_the_old_one_where_it_plays():
    p, t = _two_passes()
    rec = {"sr": SR, "marks": [0, 4 * BEAT], "samples": 4 * BEAT, "comp": {}}
    made = tracking.place_audio(p, t, "audio/rec/new.wav", rec, punch=4)
    assert [c.lane for c in made] == [3]
    assert _heard(t) == [(0, 4, 2), (4, 8, 3), (8, 16, 2)]


def test_keyboard_events_become_notes_as_played():
    p = Project(name="k", tempo=120)
    t = Track(id="t1", name="Keys")
    p.tracks.append(t)
    ev = [BEAT, 0x90, 60, 100, 2 * BEAT, 0x80, 60, 0,                  # C4, a beat long, on beat 2 of the bar
          2 * BEAT + 100, 0xB0, 64, 127, 3 * BEAT, 0x90, 64, 80]       # the pedal, and a note held past the stop
    rec = {"sr": SR, "marks": [0, 4 * BEAT], "samples": 4 * BEAT, "midi": ev, "comp": {"out": 240, "graph": 0, "in": 999}}
    [c] = tracking.place_midi(p, t, rec, punch=4)
    late = 240 / BEAT  # the way out only: a keyboard has no way in to wait for
    assert c.start == 4 and c.length == 4 and c.lane == 1 and not c.muted
    assert [(n.pitch, n.vel) for n in c.notes] == [(60, 100), (64, 80)]
    assert c.notes[0].start == pytest.approx(1 - late) and c.notes[0].dur == pytest.approx(1)
    assert c.notes[1].start + c.notes[1].dur == pytest.approx(4 - late)   # cut where the recording stopped
    assert c.cc["64"][0][1] == 127


def test_clip_edit_split_keeps_the_pass(tmp_path):
    api.project_new("S", folder=str(tmp_path / "s"), tempo=120)
    api.track_add("Keys", "synth:pluck")
    api.write("Keys", "C4:q D4 E4 F4 | G4:q A4 B4 C5")
    p = api.load()
    cid = p.tracks[0].clips[0].id
    with api._Edit(None) as q:
        q.tracks[0].clips[0].lane = 2
    right = api.clip_edit(cid, "split", at="2:1")["created"]
    assert api.load().clip(right)[1].lane == 2
    assert api.project_info()["tracks"][0]["clips"][0]["pass"] == 2


def test_only_audio_tracks_take_an_input(tmp_path):
    api.project_new("I", folder=str(tmp_path / "i"), tempo=120)
    api.track_add("Keys", "synth:pluck")
    api.track_add("Gtr", kind="audio")
    api.track_update("Gtr", input=[1, 2])
    assert api.load().track("Gtr").input == [1, 2]
    api.track_update("Gtr", input=0)
    assert api.load().track("Gtr").input is None
    with pytest.raises(ValueError, match="not an audio track"):
        api.track_update("Keys", input=1)


# ---- through the engine -----------------------------------------------------------------------------------------

@pytest.fixture
def session(tmp_path):
    api.project_new("Track", folder=str(tmp_path / "song"), tempo=120)
    api.track_add("Gtr", kind="audio")
    api.track_update("Gtr", input=2)
    api.track_add("Keys", "synth:pluck")
    rng = np.random.default_rng(7)
    played = (0.2 * rng.standard_normal((8 * SR, 2))).astype(np.float32)  # what comes in: left and right differ
    sf.write(str(tmp_path / "in.wav"), played, SR, subtype="FLOAT")
    return api.load(), tmp_path, played


def _pull(eng: live.Engine, tmp, n: int, **more) -> np.ndarray:
    eng.call("pull", timeout=120, device_sr=SR, block=256, length=n, out=str(tmp / "out.wav"), **more)
    return sf.read(str(tmp / "out.wav"), dtype="float32", always_2d=True)[0]


@ENGINE_RUNS
def test_an_armed_track_is_heard_only_while_monitoring(session):
    p, tmp, played = session
    gtr = p.track("Gtr").id
    player = live.Player(p.path, no_device=True)
    try:
        assert player.track({gtr: {"arm": True, "monitor": False}}) is not None
        assert player.track({gtr: {"arm": True, "monitor": False}}) is None      # nothing changed: no reload
        spec = live.build_spec(p, tracking=player.tracking)[0]
        assert next(t for t in spec["tracks"] if t["id"] == gtr)["input"] == {"ch": [1], "monitor": False}
        quiet = _pull(player.engine, tmp, SR, input=str(tmp / "in.wav"))
        assert np.abs(quiet).max() == 0
        player.track({gtr: {"arm": True, "monitor": True}})
        heard = _pull(player.engine, tmp, SR, input=str(tmp / "in.wav"))
        # input 2 on both sides, through the strip at unity (the first block fades in)
        assert np.allclose(heard[512:, 0], played[512:SR, 1], atol=1e-6)
        assert np.allclose(heard[512:, 1], played[512:SR, 1], atol=1e-6)
    finally:
        player.close()


@ENGINE_RUNS
def test_recording_round_a_loop_through_the_server(session, monkeypatch):
    from tunetrack import livehost, server
    p, tmp, played = session
    monkeypatch.setattr(livehost, "NO_DEVICE", True)
    api.project_set(loop="2:1-3:1")   # beats 4..8
    try:
        with pytest.raises(RuntimeError, match="No track is armed"):
            server.live_record(beat=4.0)
        st = server.live_arm("Gtr", arm=True)
        assert st["armed"] == {p.track("Gtr").id: {"arm": True, "monitor": False}}
        st = server.live_record(beat=4.0, count_in=1, loop=True)
        assert st["recording"] and st["playing"]
        # a bar of count-in, then two and a half times round the loop
        _pull(livehost._player.engine, tmp, int(4 * BEAT + 2.5 * 4 * BEAT), input=str(tmp / "in.wav"), hold=True)
        st = server.live_stop()
        assert not st["recording"] and st["recorded"]["heard"] == {p.track("Gtr").id: [2]}
    finally:
        server.live_off()
    t = api.load().track("Gtr")
    clips = sorted(t.clips, key=lambda c: c.lane)
    assert [(c.lane, c.start, round(c.length, 3), c.muted) for c in clips] == \
        [(1, 4.0, 4.0, True), (2, 4.0, 4.0, False), (3, 4.0, 2.0, True)]          # the half pass stays to comp from
    assert [round(c.offset, 4) for c in clips] == [2.0, 4.0, 6.0]
    got, sr = sf.read(str(api.load().root / clips[0].file), dtype="float32", always_2d=True)
    assert sr == SR and got.shape == (14 * BEAT, 1)
    assert np.allclose(got[:, 0], played[:14 * BEAT, 1], atol=2e-7)               # input 2, as it came in (24 bit)
    assert api.undo()["undone"] and not api.load().track("Gtr").clips            # the whole recording is one step
    api.redo()
    api.comp("Gtr", "2:1", "2:3", use=1)                                          # the first two beats from pass 1
    assert _heard(api.load().track("Gtr")) == [(4.0, 6.0, 1), (6.0, 8.0, 2)]


@ENGINE_RUNS
def test_the_keyboard_plays_and_records_an_armed_instrument(session):
    p, tmp, _played = session
    keys = p.track("Keys").id
    player = live.Player(p.path, no_device=True)
    try:
        player.track({keys: {"arm": True, "monitor": False}})   # no notes yet: the instrument is loaded to be played
        eng = player.engine
        eng.call("record", **{"from": 0})
        events = [[BEAT, 0x90, 69, 110], [2 * BEAT, 0x80, 69, 0]]
        out = _pull(eng, tmp, 3 * BEAT, midi=events, hold=True)
        rec = eng.call("record_stop")
        assert np.abs(out[:BEAT - 256]).max() == 0 and np.abs(out[BEAT:2 * BEAT]).max() > 0.01
        assert rec["midi"] == [BEAT, 0x90, 69, 110, 2 * BEAT, 0x80, 69, 0] and rec["marks"] == [0, 0]
        res = tracking.commit(p.path, rec, {}, [keys])
        assert res["heard"] == {keys: [1]}
        [c] = api.load().track("Keys").clips
        assert [(n.pitch, n.start, n.dur, n.vel) for n in c.notes] == [(69, 1.0, 1.0, 110)]
        player.track({})                                         # disarmed: the keyboard is no longer heard
        player.sync(force=True)
        assert np.abs(_pull(eng, tmp, BEAT // 2, midi=[[100, 0x90, 72, 100]], hold=True)).max() == 0
    finally:
        player.close()


# ---- punch out, quantise, which keyboard, a device at another rate ------------------------------------------------

def test_a_punch_out_ends_the_pass_there():
    assert tracking.passes([0, 1000], 5000, 300, floor=1200, ceil=3000) == [{"k": 0, "at": 500, "pos": 1200, "n": 1800}]
    # round a loop: every pass is cut at both ends
    got = tracking.passes([0, 1000, 2000, 1000], 4000, 0, floor=1500, ceil=2500)
    assert [(g["at"], g["pos"], g["n"]) for g in got] == [(500, 1500, 1000), (2500, 1500, 1000)]


def test_a_recording_at_the_devices_rate_is_counted_in_project_samples():
    rec = {"sr": 48000, "file_sr": 44100, "marks": [0, 1000, 44100, 1000], "samples": 88200,
           "midi": [22050, 0x90, 60, 100], "comp": {"graph": 64, "out": 441, "in": 882}}
    got = tracking.at_project_rate(rec)
    assert got["marks"] == [0, 1000, 48000, 1000] and got["samples"] == 96000 and got["midi"][0] == 24000
    assert got["comp"] == {"graph": 64, "out": 480, "in": 960}
    assert tracking.at_project_rate(got) is got


def _keys_rec(ev, src=None, **more):
    return {"sr": SR, "marks": [0, 0], "samples": 8 * BEAT, "midi": ev, "midi_src": src or [], "comp": {}, **more}


def test_quantise_moves_starts_and_keeps_lengths():
    p = Project(name="q", tempo=120)
    t = Track(id="t1", name="Keys")
    p.tracks.append(t)
    late = int(0.11 * BEAT)
    ev = [BEAT + late, 0x90, 60, 100, 2 * BEAT + late, 0x80, 60, 0, 3 * BEAT - late, 0x90, 62, 90, 4 * BEAT, 0x80, 62, 0]
    [c] = tracking.place_midi(p, t, _keys_rec(ev), quantise=0.5)
    assert [(n.pitch, n.start) for n in c.notes] == [(60, 1.0), (62, 3.0)]
    assert c.notes[0].dur == pytest.approx(1.0) and c.notes[1].dur == pytest.approx(1.11)


def test_a_track_takes_its_own_keyboard_and_the_on_screen_keys():
    p = Project(name="k", tempo=120)
    a, b = Track(id="t1", name="A"), Track(id="t2", name="B")
    p.tracks += [a, b]
    ev = [0, 0x90, 60, 100, BEAT, 0x80, 60, 0,            # keyboard 1
          0, 0x90, 72, 100, BEAT, 0x80, 72, 0,            # keyboard 2
          2 * BEAT, 0x90, 48, 100, 3 * BEAT, 0x80, 48, 0]  # the on-screen keys
    rec = _keys_rec(ev, [1, 1, 2, 2, 0, 0])
    assert [n.pitch for n in tracking.place_midi(p, a, rec, src=1)[0].notes] == [60, 48]
    assert [n.pitch for n in tracking.place_midi(p, b, rec, src=2)[0].notes] == [72, 48]
    assert sorted(n.pitch for n in tracking.place_midi(p, a, rec)[0].notes) == [48, 60, 72]   # any keyboard


def test_notes_after_the_punch_out_are_dropped_and_held_ones_end_there():
    p = Project(name="o", tempo=120)
    t = Track(id="t1", name="Keys")
    p.tracks.append(t)
    ev = [BEAT, 0x90, 60, 100, 5 * BEAT, 0x80, 60, 0, 4 * BEAT + 10, 0x90, 64, 100, 5 * BEAT, 0x80, 64, 0]
    [c] = tracking.place_midi(p, t, _keys_rec(ev), until=4.0)
    assert [(n.pitch, n.start, n.dur) for n in c.notes] == [(60, 1.0, 3.0)] and c.length == 4


def test_comping_a_group_moves_the_tracks_recorded_together():
    p = Project(name="g", tempo=120)
    for i, name in enumerate(("Kick", "Snare", "Bass")):
        t = Track(id=f"t{i}", name=name, kind="audio")
        stamp = "20261117-120000" if name != "Bass" else "20261117-130000"
        first = 3 if name == "Snare" else 1   # the snare track had two passes before this recording
        t.clips = [Clip(id=f"{name}{k}", kind="audio", file=f"audio/rec/{stamp}-t{i}.wav", start=0, length=8,
                        offset=4.0 * k, lane=first + k, muted=k == 0) for k in range(2)]
        p.tracks.append(t)
    p.next_id = 50
    kick, snare, bass = p.tracks
    assert tracking.mates(p, kick, 0, 4, 1) == [(snare, 3)]
    for t, lane in [(kick, 1), *tracking.mates(p, kick, 0, 4, 1)]:
        tracking.comp(p, t, 0, 4, lane)
    assert _heard(kick) == [(0, 4, 1), (4, 8, 2)] and _heard(snare) == [(0, 4, 3), (4, 8, 4)]
    assert _heard(bass) == [(0, 8, 2)]


def test_peaks_are_the_loudest_sample_of_each_slice(tmp_path):
    x = np.zeros((SR, 2), np.float32)
    x[SR // 2:, 1] = -0.5
    sf.write(str(tmp_path / "x.wav"), x, SR, subtype="FLOAT")
    pk = tracking.peaks(tmp_path / "x.wav")
    got = np.frombuffer(pk["peaks"], np.uint8)
    assert pk["per_sec"] == 200 and len(got) == 200
    assert got[:100].max() == 0 and set(got[100:]) == {128}
    assert tracking.peaks(tmp_path / "x.wav") is pk


@ENGINE_RUNS
def test_a_punch_stops_by_itself_and_the_old_pass_is_silent_under_it(session, monkeypatch):
    import time
    from tunetrack import livehost, server
    p, tmp, played = session
    monkeypatch.setattr(livehost, "NO_DEVICE", True)
    api.audio_import("Gtr", str(tmp / "in.wav"))     # what was there before: 16 beats of it
    try:
        server.live_arm("Gtr", arm=True)
        st = server.live_record(beat=4.0, count_in=1, until=8.0, loop=True)
        assert st["recording"] and st["punch"] == [4.0, 8.0]
        out = _pull(livehost._player.engine, tmp, 12 * BEAT, input=str(tmp / "in.wav"), hold=True)
        for _ in range(100):
            if livehost.state()["done"]["n"]:
                break
            time.sleep(0.1)
        st = livehost.state()
        assert not st["recording"] and not st["playing"] and st["done"]["recorded"]["clips"]
    finally:
        server.live_off()
    # the pre-roll plays the old pass (and the click), the punch silences it, and it is back after the punch out
    assert np.abs(out[BEAT:4 * BEAT]).max() > 0.05
    assert np.abs(out[4 * BEAT + 256:8 * BEAT]).max() == 0
    assert np.abs(out[8 * BEAT + 256:8 * BEAT + SR // 2 - 256]).max() > 0.05
    assert np.abs(out[8 * BEAT + SR // 2 + 600:]).max() == 0      # stopped half a second after the punch out
    t = api.load().track("Gtr")
    assert _heard(t) == [(0.0, 4.0, 0), (4.0, 8.0, 1), (8.0, 16.0, 0)]
    new = next(c for c in t.clips if c.lane == 1)
    got = sf.read(str(api.load().root / new.file), dtype="float32", always_2d=True)[0]
    assert np.allclose(got[4 * BEAT:8 * BEAT, 0], played[4 * BEAT:8 * BEAT, 1], atol=2e-7) and new.offset == pytest.approx(2.0)


@ENGINE_RUNS
def test_recording_through_a_device_at_another_rate(session):
    p, tmp, _played = session
    api.project_set(loop="2:1-3:1")   # beats 4..8
    p = api.load()
    gtr, dev = p.track("Gtr").id, 44100
    rng = np.random.default_rng(3)
    came = (0.2 * rng.standard_normal((8 * dev, 2))).astype(np.float32)
    sf.write(str(tmp / "in441.wav"), came, dev, subtype="FLOAT")
    player = live.Player(p.path, no_device=True)
    try:
        player.track({gtr: {"arm": True, "monitor": False}})
        eng = player.engine
        rel = "audio/rec/x-gtr.wav"
        res = eng.call("record", audio={gtr: {"file": str(p.root / rel), "ch": [1]}}, device_sr=dev, loop=True,
                       **{"from": 4 * BEAT})
        assert res["file_sr"] == dev
        n = int(2.5 * 2 * dev)   # two and a half times round the two-second loop
        eng.call("pull", timeout=120, device_sr=dev, block=441, length=n, out=str(tmp / "out.wav"),
                 input=str(tmp / "in441.wav"), midi=[[dev, 0x90, 60, 100]], hold=True)
        rec = eng.call("record_stop")
    finally:
        player.close()
    assert rec["file_sr"] == dev and rec["samples"] == n and rec["midi"] == [dev, 0x90, 60, 100]
    got, fsr = sf.read(str(p.root / rel), dtype="float32", always_2d=True)
    assert fsr == dev and np.allclose(got[:, 0], came[:n, 1], atol=2e-7)        # as it came in, at the rate of the device
    marks = np.array(rec["marks"]).reshape(-1, 2)
    assert list(marks[:, 1]) == [4 * BEAT] * 3
    assert np.abs(marks[:, 0] - np.array([0, 2 * dev, 4 * dev])).max() <= 2     # heard two device-seconds apart
    made = tracking.commit(p.path, rec, {gtr: rel}, [])
    clips = sorted(api.load().track("Gtr").clips, key=lambda c: c.lane)
    assert [(c.lane, round(c.start, 2), round(c.length, 2), c.muted) for c in clips] == \
        [(1, 4.0, 4.0, True), (2, 4.0, 4.0, False), (3, 4.0, 2.0, True)]
    assert [round(c.offset, 3) for c in clips] == [0.0, 2.0, 4.0] and made["heard"] == {gtr: [2]}


@ENGINE_RUNS
def test_each_keyboard_plays_the_track_that_listens_to_it(session):
    p, tmp, _played = session
    keys = p.track("Keys").id
    player = live.Player(p.path, no_device=True)
    try:
        player.track({keys: {"arm": True, "monitor": False, "key": 2}})
        eng = player.engine
        other = _pull(eng, tmp, BEAT, midi=[[100, 0x90, 69, 110, 1], [BEAT // 2, 0x80, 69, 0, 1]], hold=True)
        assert np.abs(other).max() == 0                                    # keyboard 1 is not this track
        eng.call("record", **{"from": 0})
        mine = _pull(eng, tmp, BEAT, midi=[[100, 0x90, 69, 110, 2], [BEAT // 2, 0x80, 69, 0, 2]], hold=True)
        rec = eng.call("record_stop")
        assert np.abs(mine).max() > 0.01 and rec["midi_src"] == [2, 2]
    finally:
        player.close()
