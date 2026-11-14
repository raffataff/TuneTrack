"""The live engine (engine/, tunetrack/live.py): the spec it loads, and its sound against the offline renderer."""
import numpy as np
import pytest

from tunetrack import api, engine, live

DUCK = {"threshold": -40, "ratio": 20, "attack": 0.001, "release": 0.08, "makeup": 0}


@pytest.fixture
def proj(tmp_path):
    import soundfile as sf
    sr = 48000
    t = np.arange(sr * 2) / sr
    sf.write(str(tmp_path / "tone.wav"), (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), sr)
    api.project_new("Live", folder=str(tmp_path / "song"), tempo=96, key="D minor")
    # Karplus-Strong plucks: every note rings out, so both engines hand out synth voices the same way
    api.track_add("Harp", "synth:pluck", fx=[{"type": "builtin", "name": "saturator", "params": {"drive": 9}},
                                             {"type": "builtin", "name": "filter", "params": {"cutoff": 1800}}])
    api.write("Harp", "D4:e F4 A4 D5 C5 A4 F4 E4 | D4:q A3 (D4 F4 A4):h | r:w | r:w")
    api.track_add("Audio", kind="audio", fx=["builtin:width"])
    api.audio_import("Audio", str(tmp_path / "tone.wav"), at="2:1", fade_in=0.05, fade_out=0.2)
    api.track_add("Kick", {"type": "synth", "name": "drums", "params": {"kdecay": 0.06}})
    api.write("Kick", "C2:q C2 C2 C2 | C2:q C2 C2 C2", vel=120)
    api.track_add("Echo", kind="bus", fx=["builtin:delay"])
    api.fx_add("Echo", {"type": "builtin", "name": "compressor", "params": DUCK}, sidechain="Kick")
    api.track_add("Room", kind="bus", fx=["builtin:reverb"])
    api.track_update("Harp", sends=[{"to": "Echo", "db": -6}], pan=-0.3, volume_db=-2)
    api.track_update("Audio", output="Room", pan=0.4)
    api.track_update("Echo", sends=[{"to": "Room", "db": -9}], volume_db=-3)
    api.automation("Harp", "volume_db", [["1:1", -12], ["2:1", 0], ["3:1", -6]])
    api.automation("Audio", "pan", [["2:1", -1], ["3:1", 1]])
    api.project_set(loop="1:1-3:1")
    return api.load()


def test_midi_events_land_where_the_midi_file_puts_them(proj, tmp_path):
    import mido
    p = proj
    p.tempo_changes = [[4.0, 133.0], [6.5, 71.0]]
    t = next(x for x in p.tracks if x.name == "Harp")
    engine.write_midi(p, t, tmp_path / "h.mid")
    want, now = [], 0.0
    for msg in mido.MidiFile(str(tmp_path / "h.mid")):
        now += msg.time
        if msg.type in ("note_on", "note_off"):
            want.append((int(now * p.sample_rate), msg.note, msg.type == "note_on"))
    flat = live._events(p, t, p.tempo_map(), p.sample_rate, 0)
    got = [(flat[i], flat[i + 2], flat[i + 1] & 0xF0 == 0x90) for i in range(0, len(flat), 4)]
    assert len(got) == len(want) > 20
    assert [(n, on) for _s, n, on in got] == [(n, on) for _s, n, on in want]
    assert max(abs(a[0] - b[0]) for a, b in zip(got, want)) <= 1


def test_spec_describes_the_whole_session(proj):
    spec, warnings = live.build_spec(proj)
    assert not warnings
    by = {t["name"]: t for t in spec["tracks"]}
    ids = {t["name"]: t["id"] for t in spec["tracks"]}
    assert by["Harp"]["source"]["type"] == "synth" and by["Harp"]["source"]["template"] == "ks"
    assert [d["name"] for d in by["Harp"]["fx"]] == ["saturator", "filter"]
    assert by["Harp"]["fx"][0]["params"]["drive"] == 9 and "mix" in by["Harp"]["fx"][0]["params"]
    assert by["Audio"]["source"]["type"] == "stem" and by["Audio"]["out"] == ids["Room"]
    assert by["Harp"]["sends"] == [[ids["Echo"], -6.0]] and by["Echo"]["sends"] == [[ids["Room"], -9.0]]
    assert by["Echo"]["fx"][0]["params"]["delay_sec"] == pytest.approx(0.75 * 60 / 96)
    assert by["Echo"]["fx"][1]["key"] == ids["Kick"]
    sr = spec["sr"]
    bar = int(round(4 * 60 / 96 * sr))
    assert spec["loop"] == [0, 2 * bar]
    assert by["Harp"]["vol_auto"] == [[0, -12.0], [bar, 0.0], [2 * bar, -6.0]]
    assert spec["tempo"] == [[0, 0.0, 96.0]] and spec["end"] > 4 * bar


def test_muted_and_soloed_tracks_stay_loaded_but_silent(proj):
    api.track_update("Kick", mute=True)   # still keys the compressor on Echo
    api.track_update("Audio", solo=True)
    spec, _ = live.build_spec(api.load())
    by = {t["name"]: t for t in spec["tracks"]}
    assert by["Kick"]["on"] is False and by["Kick"]["source"] is not None
    assert by["Audio"]["on"] is True and by["Harp"]["on"] is False and by["Harp"]["source"] is None


@pytest.mark.skipif(live.engine_exe() is None, reason="the live engine is not built (engine/README.md)")
def test_engine_renders_the_same_mix_as_the_offline_renderer(proj, tmp_path):
    res = live.null_test(proj, per_sample=True, keep=tmp_path / "engine.wav")
    assert not res["warnings"]
    assert res["ref_rms_db"] > -40
    assert res["residual_db"] < -60, res


@pytest.mark.skipif(live.engine_exe() is None, reason="the live engine is not built (engine/README.md)")
def test_edits_reach_a_running_engine_without_rebuilding_it(proj, tmp_path):
    import soundfile as sf

    def against_offline(name):
        p = api.load()
        r = engine.Renderer(p)
        r.run()
        player.engine.render(tmp_path / name, r.n)
        got, _ = sf.read(str(tmp_path / name), dtype="float32", always_2d=True)
        return live.compare(r.master, got.T, r.sr)["residual_db"]

    player = live.Player(proj.path, no_device=True)
    try:
        nodes = player.loaded["nodes"]
        assert against_offline("a.wav") < -60
        assert against_offline("again.wav") < -60          # a second render starts as clean as the first
        assert player.sync() is None                        # nothing changed: nothing reloaded
        api.track_update("Harp", volume_db=-9, pan=0.5)     # a fader move,
        api.write("Harp", "A4:q C5 E5 A5", at="3:1")        # new notes,
        api.fx_update("Harp", 0, params={"drive": 3})       # and a setting on an insert
        res = player.sync()
        assert res is not None and res["nodes"] == nodes and not res["warnings"]
        assert against_offline("b.wav") < -60
        api.fx_add("Audio", "builtin:bitcrush")             # a new device joins the graph
        assert player.sync()["nodes"] == nodes + 1
        assert against_offline("c.wav") < -60
    finally:
        player.close()


@pytest.mark.skipif(live.engine_exe() is None, reason="the live engine is not built (engine/README.md)")
def test_the_editor_server_hosts_one_engine_that_follows_the_desk(proj, monkeypatch):
    import time
    from tunetrack import livehost, model, server, takes
    monkeypatch.setattr(livehost, "NO_DEVICE", True)
    try:
        assert server.live_state()["on"] is False and server.live_state()["available"] is True
        st = server.live_play(beat=4.0, loop=False)
        assert st["on"] and not st["error"] and not st["warnings"] and st["view"] == str(proj.path)
        api.track_update("Harp", volume_db=-20)            # an edit from anywhere reaches the engine by itself
        for _ in range(40):
            if server.live_state().get("rev") == model.read_rev(proj.path):
                break
            time.sleep(0.1)
        assert server.live_state()["rev"] == model.read_rev(proj.path)
        tk = takes.take_new("try", use=False, session=str(proj.path))["take"]
        st = server.live_play(beat=0.0, take=tk)           # the Desk opens a take: same engine, other project
        assert st["view"].replace("\\", "/").endswith("takes/" + tk)
        with pytest.raises(live.EngineError, match="no window"):
            server.live_window("Harp", 0)                  # built-in devices have none
        with pytest.raises(KeyError):
            server.live_window("Nobody")
        assert server.live_stop()["playing"] is False
    finally:
        assert server.live_off()["on"] is False
