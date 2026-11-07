"""Sidechain keying: an effect's detector driven by another track's pre-fader sound."""
from pathlib import Path

import numpy as np
import pytest

from tunetrack import api, engine, plugins

BUS_COMP = Path(r"C:\Program Files\Common Files\VST3\SSL Native Bus Compressor 2.vst3")
DUCK = {"threshold": -40, "ratio": 20, "attack": 0.001, "release": 0.08, "makeup": 0}


@pytest.fixture
def proj(tmp_path):
    api.project_new("Pump", folder=str(tmp_path / "song"), tempo=120, key="A minor")
    api.track_add("Pad", "synth:pad")
    api.write("Pad", "(A3 C4 E4):w | (A3 C4 E4):w")
    # GM 36 = kick on every beat. A short decay, so the key lets go between beats: with the default 0.45 s tail a
    # 120 bpm kick never drops below threshold and the pad stays ducked flat instead of pumping.
    api.track_add("Kick", {"type": "synth", "name": "drums", "params": {"kdecay": 0.06}})
    api.write("Kick", "C2:q C2 C2 C2 | C2:q C2 C2 C2", vel=120)
    return str(tmp_path / "song")


def _render():
    r = engine.Renderer(api.load())
    rep = r.run()
    return r, rep


def _beat_dip_db(audio: np.ndarray, sr: int, bpm: float = 120, beats=(2, 3, 5, 6, 7)) -> float:
    """Mean level in the 15-75 ms after each beat against the 60 ms before it (negative = ducked)."""
    spb = 60 / bpm
    win = int(0.06 * sr)
    dips = []
    for b in beats:
        s = int(b * spb * sr)
        hit = np.sqrt(np.mean(audio[:, s + int(0.015 * sr):s + int(0.015 * sr) + win] ** 2))
        pre = np.sqrt(np.mean(audio[:, s - win:s] ** 2))
        dips.append(20 * np.log10((hit + 1e-12) / (pre + 1e-12)))
    return float(np.mean(dips))


def _pad(r):
    return r.pre[api.load().track("Pad").id]


def test_builtin_compressor_ducks_on_the_key_and_stops_when_disconnected(proj):
    api.fx_add("Pad", {"type": "builtin", "name": "compressor", "params": DUCK}, sidechain="Kick")
    r, rep = _render()
    keyed = _beat_dip_db(_pad(r), r.sr)
    assert keyed < -6, keyed
    assert rep["sidechains"] == ["track 'Pad': builtin:compressor <- Kick"]
    assert api.project_info()["tracks"][0]["fx"] == ["builtin:compressor <- sidechain Kick"]

    api.fx_update("Pad", 0, sidechain="")
    r, rep = _render()
    assert abs(_beat_dip_db(_pad(r), r.sr)) < 1.5  # the pad's own steady level barely moves the detector
    assert "sidechains" not in rep


def test_a_muted_key_still_drives_the_sidechain_but_isnt_heard(proj):
    api.fx_add("Pad", {"type": "builtin", "name": "compressor", "params": DUCK}, sidechain="Kick")
    api.track_update("Kick", mute=True)
    r, rep = _render()
    assert "Kick" not in rep["tracks"] and api.load().track("Kick").id not in r.pre
    assert _beat_dip_db(_pad(r), r.sr) < -6


def test_soloing_the_keyed_track_keeps_the_ducking(proj):
    api.fx_add("Pad", {"type": "builtin", "name": "compressor", "params": DUCK}, sidechain="Kick")
    api.track_update("Pad", solo=True)
    r, rep = _render()
    assert set(rep["tracks"]) == {"Pad"} and _beat_dip_db(_pad(r), r.sr) < -6


def test_editing_the_key_rerenders_the_keyed_track(proj):
    api.fx_add("Pad", {"type": "builtin", "name": "compressor", "params": DUCK}, sidechain="Kick")
    _render()
    r, rep = _render()
    assert rep["tracks"]["Pad"]["render"] == "cached"
    api.write("Kick", "C2:h C2:h | C2:h C2:h", vel=120)
    r, rep = _render()
    assert rep["tracks"]["Pad"]["render"] != "cached"


def test_bus_effects_can_be_keyed(proj):
    api.track_add("Music", kind="bus", fx=[{"type": "builtin", "name": "compressor", "params": DUCK}])
    api.track_update("Pad", output="Music")
    api.fx_update("Music", 0, sidechain="Kick")
    r, rep = _render()
    bus = r.post[api.load().track("Music").id]
    assert _beat_dip_db(bus, r.sr) < -6
    assert rep["sidechains"] == ["bus 'Music': builtin:compressor <- Kick"]


def test_sidechain_needs_a_keyable_effect_and_a_real_source_track(proj):
    with pytest.raises(ValueError, match="no sidechain input"):
        api.fx_add("Pad", "builtin:reverb", sidechain="Kick")
    with pytest.raises(ValueError, match="own track"):
        api.fx_add("Pad", "builtin:compressor", sidechain="Pad")
    api.track_add("Verb", kind="bus")
    with pytest.raises(ValueError, match="is a bus"):
        api.fx_add("Pad", "builtin:compressor", sidechain="Verb")
    with pytest.raises(KeyError):
        api.fx_add("Pad", "builtin:compressor", sidechain="Nope")
    assert api.load().track("Pad").fx == []


def test_sidechain_loops_are_refused(proj):
    api.fx_add("Pad", "builtin:compressor", sidechain="Kick")
    with pytest.raises(ValueError, match="loop"):
        api.fx_add("Kick", "builtin:compressor", sidechain="Pad")
    api.track_add("Bass", "synth:analog_bass")
    api.fx_add("Kick", "builtin:compressor", sidechain="Bass")
    with pytest.raises(ValueError, match="loop"):
        api.fx_add("Bass", "builtin:compressor", sidechain="Pad")  # Bass <- Pad <- Kick <- Bass


def test_a_chain_of_keys_renders_in_order(proj):
    api.track_add("Bass", "synth:analog_bass")
    api.write("Bass", "A1:w | A1:w")
    api.fx_add("Bass", {"type": "builtin", "name": "compressor", "params": DUCK}, sidechain="Kick")
    api.fx_add("Pad", {"type": "builtin", "name": "compressor", "params": DUCK}, sidechain="Bass")
    r, rep = _render()
    assert not [w for w in rep["warnings"] if "failed" in w or "loop" in w], rep["warnings"]
    assert {"Pad", "Bass", "Kick"} <= set(rep["tracks"])


def test_removing_the_key_track_disconnects_and_warns(proj):
    api.fx_add("Pad", "builtin:compressor", sidechain="Kick")
    res = api.track_remove("Kick")
    assert api.load().track("Pad").fx[0].sidechain is None
    assert "lost its sidechain key 'Kick'" in res["warnings"][0]


def test_a_key_without_notes_feeds_silence_with_a_warning(proj):
    api.track_add("Ghost", "synth:drums")
    api.fx_add("Pad", {"type": "builtin", "name": "compressor", "params": DUCK}, sidechain="Ghost")
    r, rep = _render()
    assert any("sidechain key 'Ghost' has no audio" in w for w in rep["warnings"])
    assert abs(_beat_dip_db(_pad(r), r.sr)) < 1.5


def test_external_sidechain_switch_names():
    yes = ["External S/C", "Low Ext S/C", "EXT Sidechain", "Ext Key", "External Sidechain", "Ext. SC"]
    no = ["Sidechain HPF", "S/C Listen", "Sidechain Listen", "Filters to S/C", "EQ to S/C", "Threshold", "Ext S/C Listen"]
    assert all(plugins.is_ext_sidechain_switch(n) for n in yes)
    assert not any(plugins.is_ext_sidechain_switch(n) for n in no)


def test_engine_warns_when_a_switch_is_on_without_a_key(proj, monkeypatch):
    p = api.load()
    from tunetrack.model import Device
    p.track("Pad").fx.append(Device("vst3", "Some Comp", params={"External S/C": "On"}))
    assert "won't react" in engine.sidechain_switch_warnings(p)[0]


@pytest.mark.skipif(not BUS_COMP.exists(), reason="SSL Native Bus Compressor 2 not installed")
def test_ssl_bus_compressor_ducks_from_a_kick(proj):
    reg = plugins.load_registry()
    reg["plugins"] = [e for e in reg["plugins"] if e["name"] != BUS_COMP.stem] + \
        plugins.run_worker("probe", str(BUS_COMP))["plugins"]
    plugins.save_registry(reg)
    res = api.fx_add("Pad", {"type": "vst3", "name": BUS_COMP.stem,
                             "params": {"Threshold": "-20.0", "Ratio": "10:1", "Attack": "0.1", "Release": "0.1"}},
                     sidechain="Kick")
    assert res["params"]["External S/C"] == 1.0 and "External S/C" in res["notes"][0]
    r, rep = _render()
    assert _beat_dip_db(_pad(r), r.sr) < -6, rep["warnings"]

    res = api.fx_update("Pad", 0, sidechain="")  # disconnecting switches the plugin back to its own signal
    assert res["params"]["External S/C"] == 0.0
    r, _ = _render()
    assert abs(_beat_dip_db(_pad(r), r.sr)) < 1.5


def test_pedalboard_hosted_plugins_refuse_a_sidechain(proj):
    reg = plugins.load_registry()
    reg["plugins"].append({"name": "Shell Comp Stereo", "path": "C:/x.vst3", "kind": "effect", "host": "pedalboard",
                           "shell": "Shell Comp Stereo", "inputs": None, "outputs": None, "params": None})
    plugins.save_registry(reg)
    with pytest.raises(ValueError, match="pedalboard"):
        api.fx_add("Pad", "vst3:Shell Comp Stereo", sidechain="Kick")
