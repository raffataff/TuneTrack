"""Automation of device settings (built-in effects and synths, VST3 parameters) and the built-in delay on the
tempo map. Rendered by the live engine, which is what plays them."""
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from tunetrack import api, engine, live, plugins

SR = 48000
NOVA = Path(r"C:\Program Files\Common Files\VST3") / "TDR Nova.vst3"

pytestmark = pytest.mark.skipif(not live.engine_runs(),
                                reason="the live engine is not built, or Windows will not start it (engine/README.md)")


@pytest.fixture
def song(tmp_path):
    """120 bpm (a bar is 2 s): an audio track playing a steady 1 kHz tone for 10 s."""
    t = np.arange(SR * 10) / SR
    sf.write(str(tmp_path / "tone.wav"), (0.3 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32), SR)
    api.project_new("Auto", folder=str(tmp_path / "song"), tempo=120)
    api.track_add("Tone", kind="audio")
    api.audio_import("Tone", str(tmp_path / "tone.wav"))
    return tmp_path


def _track(name: str):
    p = api.load()
    r = engine.Renderer(p)
    t = next(x for x in p.tracks if x.name == name)
    audio, info = r.render_track(t, r.track_spec(t))
    return audio, info


def _db(audio: np.ndarray, a: float, b: float) -> float:
    seg = audio[0, int(a * SR):int(b * SR)]
    return float(20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-12))


TONE_DB = 20 * np.log10(0.3 / np.sqrt(2))


def test_an_effect_setting_follows_its_lane(song):
    api.fx_add("Tone", {"type": "builtin", "name": "gain", "params": {"gain_db": -6}})
    res = api.automation("Tone", "gain_db", [["2:1", -40], ["3:1", 0]], device=0)
    assert res["device"] == "builtin:gain ~gain_db" and res["gain_db"] == [["2:1", -40.0], ["3:1", 0.0]]
    audio, info = _track("Tone")
    assert not info["warnings"]
    # held at the first point before it (the static -6 dB is ignored), a straight line in dB, held after
    for (a, b), want in [((0.5, 1.5), -40), ((2.95, 3.05), -20), ((3.45, 3.55), -10), ((5.0, 6.0), 0)]:
        assert abs(_db(audio, a, b) - (TONE_DB + want)) < 0.3, (a, want, _db(audio, a, b))


def test_a_changed_lane_renders_again_and_a_cleared_one_brings_the_setting_back(song):
    api.fx_add("Tone", {"type": "builtin", "name": "gain", "params": {"gain_db": -6}})
    api.automation("Tone", "gain_db", [["1:1", -30]], device=0)
    assert abs(_db(_track("Tone")[0], 1, 2) - (TONE_DB - 30)) < 0.3
    api.automation("Tone", "gain_db", [["1:1", -20]], device=0, clear=True)
    audio, info = _track("Tone")
    assert not info["cached"] and abs(_db(audio, 1, 2) - (TONE_DB - 20)) < 0.3
    api.automation("Tone", "gain_db", clear=True, device=0)
    assert "automation" not in api.load().tracks[0].fx[0].to_json()
    assert abs(_db(_track("Tone")[0], 1, 2) - (TONE_DB - 6)) < 0.3


def test_a_running_engine_follows_new_lanes_and_lets_go_of_old_ones(song, tmp_path):
    api.fx_add("Tone", {"type": "builtin", "name": "gain", "params": {"gain_db": -6}})
    api.automation("Tone", "gain_db", [["1:1", -30]], device=0)
    player = live.Player(api.load().path, no_device=True)

    def level() -> float:
        p = api.load()
        player.engine.render(tmp_path / "m.wav", int(4 * SR))
        got, _ = sf.read(str(tmp_path / "m.wav"), dtype="float32", always_2d=True)
        return _db(got.T, 1, 2)

    try:
        nodes = player.loaded["nodes"]
        assert abs(level() - (TONE_DB - 30)) < 0.3
        api.automation("Tone", "gain_db", [["1:1", -12]], device=0, clear=True)
        assert player.sync()["nodes"] == nodes  # a new lane reloads nothing
        assert abs(level() - (TONE_DB - 12)) < 0.3
        api.automation("Tone", "gain_db", clear=True, device=0)
        player.sync()
        assert abs(level() - (TONE_DB - 6)) < 0.3  # back to its setting
    finally:
        player.close()


def test_a_built_in_synth_setting_follows_its_lane(tmp_path):
    api.project_new("Synth", folder=str(tmp_path / "song"), tempo=120)
    api.track_add("Square", "synth:chip_square")
    api.write("Square", "A4:w | A4:w | A4:w")
    api.automation("Square", "level", [["2:1", 0.4], ["2:3", 0.1]], device="instrument")
    assert api.project_info()["tracks"][0]["instrument_automation"] == ["level"]
    audio, info = _track("Square")
    assert not info["warnings"]
    drop = _db(audio, 3.2, 3.8) - _db(audio, 0.5, 1.5)
    assert abs(drop - 20 * np.log10(0.1 / 0.4)) < 0.5, drop


def test_automation_is_checked_where_it_can_be(song, tmp_path):
    api.fx_add("Tone", "builtin:filter")
    with pytest.raises(ValueError, match="no setting 'freq'.*cutoff"):
        api.automation("Tone", "freq", [[0, 1]], device=0)
    with pytest.raises(IndexError, match="no effect at index 3"):
        api.automation("Tone", "cutoff", [[0, 1]], device=3)
    with pytest.raises(ValueError, match="volume_db or pan"):
        api.automation("Tone", "cutoff", [[0, 1]])
    api.track_add("Piano", "sfz:" + str(tmp_path / "piano.sfz"))
    with pytest.raises(ValueError, match="cc"):
        api.automation("Piano", "volume", [[0, 1]], device="instrument")
    api.fx_add("master", "builtin:width")
    assert api.automation("master", "width", [["1:1", 1], ["2:1", 2]], device=0)["track"] == "master"


def test_moving_bars_moves_device_lanes(song):
    api.fx_add("Tone", "builtin:filter")
    api.fx_add("master", "builtin:width")
    api.automation("Tone", "cutoff", [["3:1", 500], ["5:1", 5000]], device=0)
    api.automation("master", "width", [["3:1", 1], ["5:1", 2]], device=0)
    api.arrange("insert", "1:1", "2:1")
    p = api.load()
    assert p.tracks[0].fx[0].automation["cutoff"] == [[12.0, 500.0], [20.0, 5000.0]]
    assert p.master["fx"][0].automation["width"] == [[12.0, 1.0], [20.0, 2.0]]


def test_the_built_in_delay_keeps_its_time_in_beats_across_a_tempo_change(tmp_path):
    click = np.zeros(SR, dtype=np.float32)
    click[0] = 0.5
    sf.write(str(tmp_path / "click.wav"), click, SR)
    api.project_new("Echo", folder=str(tmp_path / "song"), tempo=120)
    api.track_add("Clicks", kind="audio", fx=[{"type": "builtin", "name": "delay",
                                               "params": {"time": 1, "feedback": 0, "mix": 1, "spread": 0}}])
    api.audio_import("Clicks", str(tmp_path / "click.wav"), at=2)    # 1.0 s
    api.audio_import("Clicks", str(tmp_path / "click.wav"), at=10)   # 4.0 s to beat 8, then 2 beats at 60: 6.0 s
    p = api.load()
    p.tempo_changes = [[8.0, 60.0]]
    p.save()
    audio, info = _track("Clicks")
    assert not info["warnings"]
    hits = np.flatnonzero(np.abs(audio[0]) > 0.1)
    hits = hits[np.insert(np.diff(hits) > 100, 0, True)] / SR
    assert np.allclose(hits, [1.0, 1.5, 6.0, 7.0], atol=2 / SR), hits  # the echo is a beat late: 0.5 s, then 1 s


def test_the_dawdreamer_worker_says_it_holds_automated_settings(song):
    api.fx_add("Tone", "builtin:gain")
    api.automation("Tone", "gain_db", [["1:1", -20]], device=0)
    p = api.load()
    r = engine.Renderer(p, backend="dawdreamer")
    t = p.tracks[0]
    _audio, info = r.render_track(t, r.track_spec(t))
    assert any("plays in the live engine only" in w for w in info["warnings"]), info["warnings"]


@pytest.mark.skipif(not NOVA.exists(), reason="TDR Nova not installed")
def test_a_plugin_parameter_follows_its_lane_through_a_built_in_eq_on_nova(song, tmp_path):
    reg = plugins.load_registry()
    reg["plugins"] = [e for e in reg["plugins"] if e["name"] != NOVA.stem] + plugins.run_worker("probe", str(NOVA))["plugins"]
    plugins.save_registry(reg)
    api.fx_add("Tone", {"type": "builtin", "name": "eq", "engine": "nova", "params": {"mid_freq": 1000, "mid_q": 1}})
    # from flat (no band 2 in Nova's settings) up a bell at the tone: the band stays in along the lane
    api.automation("Tone", "mid_gain", [["2:1", 0], ["3:1", 12]], device=0)
    audio, info = _track("Tone")
    assert not info["warnings"], info["warnings"]
    for (a, b), want in [((0.5, 1.5), 0), ((5.0, 6.0), 12)]:
        assert abs(_db(audio, a, b) - (TONE_DB + want)) < 0.5, (a, want, _db(audio, a, b))
    mid = _db(audio, 2.95, 3.05) - TONE_DB
    assert 2 < mid < 10, mid  # on its way up (Nova's gain is linear in dB: about 6)
    # a VST3 parameter by its own name and shown text, straight on the plugin
    api.fx_remove("Tone", 0)
    api.fx_add("Tone", {"type": "vst3", "name": "TDR Nova", "params": {"EQ Auto Gain": "Off"}})
    api.automation("Tone", "Output Gain", [["2:1", "0.0"], ["3:1", "-12.0"]], device=0)
    audio, info = _track("Tone")
    assert not info["warnings"], info["warnings"]
    assert abs(_db(audio, 0.5, 1.5) - TONE_DB) < 0.3
    assert abs(_db(audio, 5.0, 6.0) - (TONE_DB - 12)) < 0.3
