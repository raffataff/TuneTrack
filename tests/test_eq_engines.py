"""EQ engines: builtin:eq's settings on SSL Native Channel Strip 2 / TDR Nova, and the display-text matcher."""
import json
from pathlib import Path

import numpy as np
import pytest
from scipy.signal import csd, welch

from tunetrack import api, engine, eqmap, plugins, worker
from tunetrack.model import Device, Project

VST3 = Path(r"C:\Program Files\Common Files\VST3")
SSL = VST3 / "SSL Native Channel Strip 2.vst3"
NOVA = VST3 / "TDR Nova.vst3"
FAKE = {"SSL Native Channel Strip 2": {"name": "SSL Native Channel Strip 2", "path": "C:/ssl.vst3", "kind": "effect",
                                       "host": "dawdreamer"},
        "TDR Nova": {"name": "TDR Nova", "path": "C:/nova.vst3", "kind": "effect", "host": "dawdreamer"}}


@pytest.fixture
def proj(tmp_path):
    api.project_new("EQ", folder=str(tmp_path / "song"), tempo=120, key="C major")
    api.track_add("Pad", "synth:pad")
    api.write("Pad", "(C3 E3 G3 C4):w")
    return str(tmp_path / "song")


@pytest.fixture
def installed(monkeypatch):
    """Both plugins in the registry (fake entries: nothing here loads them)."""
    monkeypatch.setattr(eqmap.plugins, "resolve", lambda name, kind=None: FAKE[name])


# ---- translation ---------------------------------------------------------------------------------------------

def test_nova_gets_the_same_settings_in_its_own_terms():
    params, notes = eqmap.translate("nova", {"hpf": 150, "low_freq": 120, "low_gain": 3, "mid_freq": 400,
                                             "mid_gain": -6, "mid_q": 1.0, "high_freq": 8000, "high_gain": 2})
    assert notes == []
    assert params == {
        "EQ Auto Gain": "Off",  # on by default, it would undo the cuts in the level readings
        "HP Active": "On", "HP Type": "12dB/oct", "HP Frequency": "150",
        "Band 1 Type": "Low S", "Band 1 Q": "0.71", "Band 1 Frequency": "120", "Band 1 Gain": "3.0",
        "Band 2 Frequency": "400", "Band 2 Gain": "-6.0", "Band 2 Q": "1.40",
        "Band 4 Q": "0.71", "Band 4 Frequency": "8000", "Band 4 Gain": "2.0",
    }


def test_untouched_bands_and_filters_are_left_alone():
    assert eqmap.translate("nova", {"hpf": 20, "lpf": 20000, "mid_gain": 0})[0] == {"EQ Auto Gain": "Off"}
    assert eqmap.translate("ssl", {})[0] == {"Dynamics In": "Out"}


def test_ssl_writes_khz_and_goes_through_its_dial_law():
    params, notes = eqmap.translate("ssl", {"hpf": 150, "mid_freq": 400, "mid_gain": -6, "lpf": 9000,
                                            "low_freq": 200, "low_gain": 2, "high_freq": 5000, "high_gain": 3})
    assert notes == []
    assert params["Dynamics In"] == "Out"  # the strip's compressor/gate stay out of an EQ
    assert params["High Pass Filter"] == "150.0" and params["Low Pass Filter"] == "9.00"  # LPF in kHz
    assert params["LMF Frequency"] == "0.40" and params["LMF Q"] == "2.50"  # kHz; mid_q 1 = SSL's narrowest
    assert params["LMF Gain"] == "-8.1"  # dial -8.1 measures -6 dB (the dial reads high at moderate settings)
    assert params["LF Frequency"] == f"{200 / eqmap.SSL_LF_HALF:.1f}"  # its half-gain point lands at 200 Hz
    assert params["HF Freq"] == f"{5000 / eqmap.SSL_HF_HALF / 1000:.2f}"
    assert params["HF Gain"] == "3.0"


def test_ssl_picks_the_band_for_the_mid_frequency():
    assert "HMF Freq" in eqmap.translate("ssl", {"mid_freq": 3000, "mid_gain": 3})[0]
    low_bell = eqmap.translate("ssl", {"mid_freq": 120, "mid_gain": -4})[0]  # under LMF: the free LF band
    assert low_bell["LF Type"] == "Bell" and low_bell["LF Frequency"] == "120.0"
    high_bell = eqmap.translate("ssl", {"mid_freq": 10000, "mid_gain": 2})[0]
    assert high_bell["HF Type"] == "Bell" and high_bell["HF Freq"] == "10.00"


def test_settings_a_plugin_cant_reach_are_clamped_with_a_note():
    params, notes = eqmap.translate("ssl", {"mid_freq": 9000, "mid_gain": -3, "high_gain": 2, "high_freq": 12000})
    assert params["HMF Freq"] == "7.00"  # the HF band is taken by the shelf
    assert any(n.startswith("mid_freq 9000") for n in notes)
    assert any("high_freq 12000" in n and "SSL's HF shelf" in n for n in notes)
    params, notes = eqmap.translate("nova", {"mid_gain": -30})
    assert params["Band 2 Gain"] == "-18.0" and "mid_gain -30 dB is outside -18-18 dB" in notes[0]


def test_unknown_eq_params_are_refused(proj):
    with pytest.raises(KeyError, match="no parameter 'freq'"):
        eqmap.translate("ssl", {"freq": 100})
    with pytest.raises(KeyError):
        api.fx_add("Pad", {"type": "builtin", "name": "eq", "params": {"gain": 3}})
    assert api.project_info()["tracks"][0].get("fx") is None


# ---- devices, project setting, fallback --------------------------------------------------------------------------

def test_eq_engine_device_strings():
    d = api.parse_device("eq:ssl", instrument=False)
    assert (d.type, d.name, d.engine, d.label()) == ("builtin", "eq", "ssl", "eq:ssl")
    assert api.parse_device("eq:TDR Nova", instrument=False).engine == "nova"
    assert api.parse_device("builtin:eq", instrument=False).engine is None  # follows the project
    d = api.parse_device({"type": "eq", "engine": "Channel Strip", "params": {"hpf": 90}}, instrument=False)
    assert (d.name, d.engine, d.params) == ("eq", "ssl", {"hpf": 90})
    assert Device.from_json(d.to_json()).engine == "ssl"
    with pytest.raises(ValueError, match="Unknown EQ engine"):
        api.parse_device("eq:pultec", instrument=False)
    with pytest.raises(ValueError, match="only to the EQ"):
        api.parse_device({"type": "builtin", "name": "reverb", "engine": "ssl"}, instrument=False)


def test_project_eq_engine_is_saved_and_reported(proj, installed):
    assert api.project_info()["eq_engine"] == "builtin"
    assert "eq_engine" not in json.loads((Path(proj) / "project.json").read_text())  # no churn for old projects
    res = api.project_set(eq_engine="nova")
    assert res["eq_engine"] == "nova" and "notes" not in res
    assert json.loads((Path(proj) / "project.json").read_text())["eq_engine"] == "nova"
    with pytest.raises(ValueError):
        api.project_set(eq_engine="neve")


def test_new_projects_take_the_configured_engine(tmp_path, monkeypatch, installed):
    monkeypatch.setattr(api.config, "load_config", lambda: {"eq_engine": "ssl"})
    assert api.project_new("A", folder=str(tmp_path / "a"))["eq_engine"] == "ssl"
    assert api.project_new("B", folder=str(tmp_path / "b"), eq_engine="builtin")["eq_engine"] == "builtin"


def test_eq_inserts_run_on_the_projects_engine_unless_they_pick_one(proj, installed):
    api.project_set(eq_engine="nova")
    api.fx_add("Pad", {"type": "builtin", "name": "eq", "params": {"hpf": 150}})
    api.fx_add("Pad", "eq:ssl")
    api.fx_add("Pad", "eq:builtin")
    p = api.load()
    nova, ssl, own = (engine.resolve_device(d, p, False) for d in p.track("Pad").fx)
    assert nova["type"] == "vst3" and nova["name"] == "TDR Nova" and nova["params"]["HP Frequency"] == "150"
    assert ssl["name"] == "SSL Native Channel Strip 2"
    assert own["type"] == "faust"
    assert api.project_info()["tracks"][0]["fx"] == ["builtin:eq (TDR Nova)", "eq:ssl (SSL Native Channel Strip 2)",
                                                    "eq:builtin"]


def test_a_missing_plugin_falls_back_to_the_builtin_eq_with_a_warning(proj):
    # the test library's registry is empty: neither plugin is "installed"
    res = api.project_set(eq_engine="ssl")
    assert "isn't in the plugin registry" in res["notes"][0]
    res = api.fx_add("Pad", {"type": "builtin", "name": "eq", "params": {"hpf": 200}})
    assert "isn't installed" in res["notes"][0]
    p = api.load()
    assert engine.resolve_device(p.track("Pad").fx[0], p, False)["type"] == "faust"
    assert "SSL Native Channel Strip 2 isn't in the plugin registry" in eqmap.warnings(p)[0]
    assert res["fx"] == ["builtin:eq (built-in: SSL Native Channel Strip 2 not installed)"]
    rep = engine.Renderer(p).run()
    assert any("use" in w and "built-in EQ" in w for w in rep["warnings"])
    assert rep["tracks"]["Pad"]["rms_dbfs"] > -60


def test_fx_add_reports_clamped_eq_settings(proj, installed):
    res = api.fx_add("Pad", {"type": "eq", "engine": "nova", "params": {"mid_gain": -24}})
    assert "mid_gain -24 dB is outside" in res["notes"][0]
    res = api.fx_update("Pad", 0, params={"mid_gain": -3})
    assert "notes" not in res


# ---- display-text matching -----------------------------------------------------------------------------------

class FakeParam:
    def __init__(self, show):
        self.v, self.show = 0.0, show

    def set(self, v):
        self.v = v

    def text(self):
        return self.show(self.v)


def _match(show, target, name):
    p = FakeParam(show)
    v, note = worker._value_for_text(p.text, p.set, target, name)
    p.set(v)
    return p.text(), note


def test_hz_targets_land_on_khz_displays():
    khz = lambda v: f"{0.2 + 1.8 * v:.2f}"  # SSL LMF: 0.20-2.00, kHz without a unit
    assert _match(khz, "400 Hz", "LMF Frequency") == ("0.40", None)
    assert _match(khz, "400", "LMF Frequency") == ("0.40", None)
    assert _match(khz, "1.2 kHz", "LMF Frequency") == ("1.20", None)
    assert _match(khz, "0.8", "LMF Frequency") == ("0.80", None)
    hpf = lambda v: "OUT" if v < 0.01 else f"{20 + 480 * v:.1f}"
    assert _match(hpf, "150", "High Pass Filter")[1] is None
    hz = lambda v: f"{10 * 4000 ** v:.0f}"  # Nova: 10-40000 Hz
    assert _match(hz, "3 kHz", "Band 3 Frequency")[0] in ("2999", "3000", "3001")


def test_values_a_plugin_cant_show_get_a_note_not_a_silent_guess():
    gain = lambda v: f"{-20 + 40 * v:.1f}"
    shown, note = _match(gain, "-30", "LMF Gain")  # not a frequency: no kHz rescaling
    assert shown == "-20.0" and "LMF Gain can't show '-30'" in note and "'-20.0' to '20.0'" in note
    ratio = lambda v: f"{1 + 9 * v:.1f}:1"
    shown, note = _match(ratio, "400", "Ratio")
    assert shown == "10.0:1" and note
    khz = lambda v: f"{0.2 + 1.8 * v:.2f}"
    shown, note = _match(khz, "9 kHz", "LMF Frequency")  # beyond the band even in kHz
    assert shown == "2.00" and "can't show '9 kHz'" in note


# ---- real plugins --------------------------------------------------------------------------------------------

def _register(path: Path):
    reg = plugins.load_registry()
    reg["plugins"] = [e for e in reg["plugins"] if e["name"] != path.stem] + plugins.run_worker("probe", str(path))["plugins"]
    plugins.save_registry(reg)


def _response_db(spec, hz, folder: Path):
    """The effect's gain at `hz`, measured with noise through a render worker (plugins load out of process)."""
    import soundfile as sf
    sr, dur = 48000, 4.0
    x = (np.random.default_rng(3).standard_normal((2, int(sr * dur))) * 0.05).astype(np.float32)
    sf.write(str(folder / "in.wav"), x.T, sr, subtype="FLOAT")
    job = folder / "job.json"
    job.write_text(json.dumps({"sr": sr, "bpm": 120, "duration": dur, "fx": [spec], "audio_in": str(folder / "in.wav"),
                               "out": str(folder / "out.wav")}), encoding="utf-8")
    plugins.run_worker("render", str(job))
    y = sf.read(str(folder / "out.wav"), dtype="float32", always_2d=True)[0].T
    n = min(x.shape[1], y.shape[1])
    f, pxy = csd(x[0, :n], y[0, :n], sr, nperseg=8192)
    _, pxx = welch(x[0, :n], sr, nperseg=8192)
    return [float(np.interp(h, f, 20 * np.log10(np.abs(pxy / pxx) + 1e-12))) for h in hz]


@pytest.mark.parametrize("eng,path", [("nova", NOVA), ("ssl", SSL)])
def test_the_plugins_do_what_the_settings_say(eng, path, tmp_path):
    if not path.exists():
        pytest.skip(f"{path.stem} not installed")
    _register(path)
    p = Project("t")
    p.eq_engine = eng
    bell = engine.resolve_device(Device("builtin", "eq", params={"mid_freq": 1000, "mid_gain": -6}), p, False)
    assert bell["type"] == "vst3"
    at_centre, far = _response_db(bell, [1000, 18000], tmp_path)
    assert abs(at_centre - -6) < 1.0 and abs(far) < 0.5
    hp = engine.resolve_device(Device("builtin", "eq", params={"hpf": 200}), p, False)
    octave_below, above = _response_db(hp, [100, 1000], tmp_path)
    assert octave_below < -8 and abs(above) < 0.5


@pytest.mark.skipif(not NOVA.exists(), reason="TDR Nova not installed")
def test_render_reports_plugin_values_it_couldnt_set(proj):
    _register(NOVA)
    api.fx_add("Pad", {"type": "vst3", "name": "TDR Nova", "params": {"Band 2 Gain": "-30", "EQ Auto Gain": "Off"}})
    rep = engine.Renderer(api.load()).run()
    assert any("Band 2 Gain can't show '-30'" in w for w in rep["warnings"]), rep["warnings"]
