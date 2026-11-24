"""A one-shot rings out: the render stops 1 s after the sound has died away (engine.TAIL_*), not at a fixed tail."""
import numpy as np
import pytest
import soundfile as sf

from tunetrack import api, engine, model


@pytest.fixture
def proj(tmp_path):
    api.project_new("Tail Song", folder=str(tmp_path / "song"), tempo=120, key="C major")
    api.track_add("Lead", "synth:lead")
    api.write("Lead", "C4:w | D4:w")
    return str(tmp_path / "song")


def _export(p, master, tmp_path):
    r = engine.Renderer(p)
    r.sr, r.n = 48000, master.shape[1]
    r.master, r.pre, r.post = master, {}, {}
    r.report = {"warnings": [], "tracks": {}}
    rep = r.export(tmp_path, "song", "wav", 32, "off", normalize=False)
    return rep, sf.read(rep["files"][0])[0]


def test_a_long_reverb_rings_out_then_one_second(proj, tmp_path):
    p = api.load()
    sr, end = 48000, p.tempo_map().sec(p.end)
    n = int((end + 20) * sr)
    t = np.arange(n) / sr
    # full level to the end, then a 12 s decay to 80 dB under the peak, then silence
    sig = np.where(t < end, 0.5, 0.5 * 10 ** (-80 / 20 * (t - end) / 12)).astype(np.float32)
    sig[t >= end + 12.2] = 0
    rep, audio = _export(p, np.stack([sig, sig]), tmp_path)
    secs = audio.shape[0] / sr
    assert end + 12 + 0.8 < secs < end + 12 + 1.4, (end, secs)  # past the old 4 s cap, 1 s after the decay
    assert abs(audio[-1]).max() < 1e-6  # faded, no click
    assert not any("tail was still sounding" in w for w in rep["warnings"])


def test_a_tail_cut_by_the_limit_is_reported(proj, tmp_path):
    p = api.load()
    p.tail = 2.0
    sr, end = 48000, p.tempo_map().sec(p.end)
    sig = np.full(int((end + 10) * sr), 0.5, np.float32)
    rep, audio = _export(p, np.stack([sig, sig]), tmp_path)
    assert audio.shape[0] / sr == pytest.approx(end + 2, abs=0.01)
    assert any("tail was still sounding" in w for w in rep["warnings"])


def test_old_fixed_tail_reads_as_the_new_default():
    assert model._tail(4.0) == model.TAIL and model._tail(None) == model.TAIL and model._tail(12) == 12.0
