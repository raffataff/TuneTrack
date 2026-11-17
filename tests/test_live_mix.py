"""Faders, pans and sends moved while LIVE plays: the engine's `mix` op (heard at once, no reload) and
track_update on an automated fader (the curve moves, so letting go keeps what was heard)."""
from pathlib import Path

import numpy as np
import pytest

from tunetrack import api, live

ENGINE_RUNS = pytest.mark.skipif(not live.engine_runs(),
                                 reason="the live engine is not built, or Windows will not start it (engine/README.md)")
SR = 48000
TONE = 0.3 / np.sqrt(2)  # RMS of the 0.3 sine


def _spec(path: Path, vol_auto=None, sends=None) -> dict:
    import soundfile as sf
    t = np.arange(SR * 2) / SR
    sf.write(str(path), np.stack([0.3 * np.sin(2 * np.pi * 500 * t)] * 2, axis=1).astype(np.float32), SR)
    strip = {"db": 0.0, "pan": 0.0, "vol_auto": [], "pan_auto": [], "fx": [], "out": "master", "on": True}
    tracks = [{**strip, "id": "t1", "name": "Tone", "kind": "audio", "sends": sends or [],
               "vol_auto": vol_auto or [], "source": {"type": "stem", "file": str(path)}}]
    if sends:
        tracks.append({**strip, "id": "b1", "name": "Bus", "kind": "bus", "sends": [], "source": None})
    return {"sr": SR, "block": 512, "ts": [4, 4], "end": len(t), "tempo": [[0, 0.0, 120.0]], "loop": None,
            "tracks": tracks, "master": {"db": 0.0, "fx": []}}


def _level(eng: live.Engine, tmp: Path) -> float:
    """dB of what a device would hear, against the tone as written."""
    import soundfile as sf
    out = tmp / "heard.wav"
    eng.call("pull", timeout=120, device_sr=SR, block=480, length=SR // 2, out=str(out))
    x = sf.read(str(out), dtype="float64", always_2d=True)[0][4800:, 0]
    return 20 * np.log10(np.sqrt(np.mean(x ** 2)) / TONE)


@ENGINE_RUNS
def test_mix_moves_a_fader_the_master_and_a_send_without_a_reload(tmp_path):
    eng = live.Engine(no_device=True)
    try:
        eng.load_spec(_spec(tmp_path / "tone.wav", sends=[["b1", -60.0]]), tmp_path / "spec.json")
        assert _level(eng, tmp_path) == pytest.approx(0.0, abs=0.05)
        assert not eng.call("mix", set=[{"id": "t1", "db": -12.0}])["problems"]
        assert _level(eng, tmp_path) == pytest.approx(-12.0, abs=0.05)
        eng.call("mix", set=[{"id": "master", "db": -6.0}])
        assert _level(eng, tmp_path) == pytest.approx(-18.0, abs=0.05)
        # the send brings the same tone in again through the bus: twice the amplitude, +6 dB
        eng.call("mix", set=[{"id": "t1", "db": 0.0}, {"id": "master", "db": 0.0}, {"id": "t1", "to": "b1", "db": 0.0}])
        assert _level(eng, tmp_path) == pytest.approx(20 * np.log10(2), abs=0.05)
        res = eng.call("mix", set=[{"id": "nope", "db": 0.0}, {"id": "t1", "to": "nowhere", "db": 0.0}])
        assert "nope" in res["problems"] and "nowhere" in res["problems"]
    finally:
        eng.close()


@ENGINE_RUNS
def test_mix_on_an_automated_fader_offsets_the_curve(tmp_path):
    """The curve holds -6 dB; the fader's own level is 0. Moving the fader to +3 plays the curve 3 dB higher."""
    eng = live.Engine(no_device=True)
    try:
        spec = _spec(tmp_path / "tone.wav", vol_auto=[[0, -6.0], [SR * 4, -6.0]])
        eng.load_spec(spec, tmp_path / "spec.json")
        assert _level(eng, tmp_path) == pytest.approx(-6.0, abs=0.05)
        eng.call("mix", set=[{"id": "t1", "db": 3.0}])
        assert _level(eng, tmp_path) == pytest.approx(-3.0, abs=0.05)
        eng.load_spec(spec, tmp_path / "spec.json")
        assert _level(eng, tmp_path) == pytest.approx(-6.0, abs=0.05)  # a reload is the project again
    finally:
        eng.close()


@ENGINE_RUNS
def test_the_desk_moves_a_fader_on_the_engine_it_plays_and_nowhere_else(tmp_path, monkeypatch):
    import soundfile as sf
    from tunetrack import livehost, server, takes
    monkeypatch.setattr(livehost, "NO_DEVICE", True)
    t = np.arange(SR * 4) / SR
    sf.write(str(tmp_path / "tone.wav"), np.stack([0.3 * np.sin(2 * np.pi * 500 * t)] * 2, axis=1).astype(np.float32), SR)
    api.project_new("Desk", folder=str(tmp_path / "song"), tempo=120)
    api.track_add("Tone", kind="audio")
    api.audio_import("Tone", str(tmp_path / "tone.wav"), at="1:1")
    tid = api.load().track("Tone").id
    try:
        assert server.live_mix([{"id": tid, "db": -12}])["live"] is False  # no engine yet: nothing is started
        server.live_play(beat=0.0, loop=False)
        server.live_stop()
        before = _level(livehost._player.engine, tmp_path)
        assert server.live_mix([{"id": tid, "db": -12}]) == {"live": True}
        assert _level(livehost._player.engine, tmp_path) == pytest.approx(before - 12, abs=0.05)
        tk = takes.take_new("try", use=False, session=str(tmp_path / "song"))["take"]
        assert server.live_mix([{"id": tid, "db": 0}], take=tk)["live"] is False  # the engine plays the session
        assert _level(livehost._player.engine, tmp_path) == pytest.approx(before - 12, abs=0.05)
    finally:
        server.live_off()


def test_a_new_level_on_an_automated_fader_moves_its_curve(tmp_path):
    api.project_new("Auto", folder=str(tmp_path / "song"), tempo=120)
    api.track_add("Pad", "synth:pad")
    api.track_update("Pad", volume_db=-6)
    api.automation("Pad", "volume_db", [["1:1", -10], ["2:1", -4]])
    api.automation("Pad", "pan", [["1:1", 0.9]])
    res = api.track_update("Pad", volume_db=-3, pan=-0.5)
    t = api.load().track("Pad")
    assert t.volume_db == -3 and res["moved"] == {"volume_db": 3.0, "pan": -0.5}
    assert t.automation["volume_db"] == [[0.0, -7.0], [4.0, -1.0]]
    assert t.automation["pan"] == [[0.0, 0.4]]
    # no automation, or no change: nothing moves and the answer says nothing
    api.track_add("Bass", "synth:pad")
    assert "moved" not in api.track_update("Bass", volume_db=-2)
    assert "moved" not in api.track_update("Pad", volume_db=-3)
