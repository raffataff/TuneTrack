"""Audio clips that follow the project's tempo (bpm) and transpose (pitch): engine.audio_track_input."""

import numpy as np
import pytest
import soundfile as sf

from tunetrack import api, engine, live

SR = 48000


def _clicks(path, bpm, beats):
    """A click on every beat at `bpm` (2 ms 3 kHz bursts)."""
    n = int(round(beats * 60 / bpm * SR))
    x = np.zeros(n, np.float32)
    burst = (0.8 * np.sin(2 * np.pi * 3000 * np.arange(96) / SR)).astype(np.float32)
    for k in range(beats):
        i = int(round(k * 60 / bpm * SR))
        x[i:i + 96] += burst[: n - i]
    sf.write(str(path), x, SR, subtype="FLOAT")


def _onsets(y, at_secs, win=0.04):
    """Measured onset (s) near each expected time: first sample over half the window's peak."""
    e = np.abs(y[0])
    got = []
    for s in at_secs:
        c = int(round(s * SR))
        lo, hi = max(0, c - int(win * SR)), min(len(e), c + int(win * SR))
        w = e[lo:hi]
        got.append((lo + int(np.argmax(w > 0.5 * w.max()))) / SR)
    return np.array(got)


def _song(tmp_path, tempo=120):
    api.project_new("Stretch", folder=str(tmp_path / "song"), tempo=tempo)
    api.track_add("Loop", kind="audio")


def _input(p=None):
    p = p or api.load()
    t = p.track("Loop")
    return engine.audio_track_input(p, t, int(round(p.tempo_map().sec(p.end + 4) * p.sample_rate)))


def _track_render(p):
    r = engine.Renderer(p)
    t = p.track("Loop")
    try:
        return r.render_track(t, r.track_spec(t))[0]
    finally:
        r.close()


def test_import_with_bpm_sets_the_length_in_its_own_beats(tmp_path):
    _clicks(tmp_path / "c.wav", 100, 8)  # 4.8 s
    _song(tmp_path)
    got = api.audio_import("Loop", str(tmp_path / "c.wav"), bpm=100)
    assert got["length_beats"] == pytest.approx(8.0)
    got = api.audio_import("Loop", str(tmp_path / "c.wav"), at=16)
    assert got["length_beats"] == pytest.approx(4.8 * 2)  # plain: its seconds at 120 bpm
    with pytest.raises(ValueError, match="20-400"):
        api.audio_import("Loop", str(tmp_path / "c.wav"), bpm=500)


def test_a_100_bpm_loop_lands_on_a_120_bpm_projects_beats(tmp_path):
    _clicks(tmp_path / "c.wav", 100, 16)
    _song(tmp_path)
    api.audio_import("Loop", str(tmp_path / "c.wav"), bpm=100)
    p = api.load()
    tm = p.tempo_map()
    want = np.array([tm.sec(k) for k in range(16)])
    for y in (_input(p), _track_render(p)):  # the shared input, and the configured offline backend
        err = _onsets(y, want) - want
        assert np.abs(err).max() < 0.003, err
        # nothing past the clip's song span (8 s at 120 bpm)
        assert np.abs(y[:, int(tm.sec(16) * SR) + 200:]).max() < 1e-3


def test_a_tempo_change_inside_the_clip_is_followed(tmp_path):
    _clicks(tmp_path / "c.wav", 100, 16)
    _song(tmp_path)
    api.project_set(tempo_changes=[[6, 80], [11, 140]])
    api.audio_import("Loop", str(tmp_path / "c.wav"), at=2, bpm=100)
    p = api.load()
    tm = p.tempo_map()
    want = np.array([tm.sec(2 + k) for k in range(16)])
    err = _onsets(_input(p), want) - want
    assert np.abs(err).max() < 0.003, err


def test_pitch_12_doubles_the_frequency_and_keeps_the_length(tmp_path):
    t = np.arange(SR * 2) / SR
    sf.write(str(tmp_path / "tone.wav"), (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), SR)
    _song(tmp_path)
    c = api.audio_import("Loop", str(tmp_path / "tone.wav"), at=1)["clip"]
    api.clip_edit(c, "stretch", pitch=12)
    p = api.load()
    y = _input(p)
    a, b = int(p.tempo_map().sec(1) * SR), int(p.tempo_map().sec(5) * SR)  # 2 s at 120 = 4 beats
    mid = y[0, a + SR // 2: a + SR // 2 + SR]
    spec = np.abs(np.fft.rfft(mid * np.hanning(len(mid))))
    assert np.fft.rfftfreq(len(mid), 1 / SR)[np.argmax(spec)] == pytest.approx(440, abs=2)
    assert np.abs(y[0, a:b]).max() > 0.2
    assert np.abs(y[:, :a]).max() == 0 and np.abs(y[:, b:]).max() == 0
    assert np.sqrt(np.mean(y[0, b - SR // 4:b] ** 2)) > 0.15  # sounds to the end of its span


def test_split_halves_play_the_whole_clip(tmp_path):
    _clicks(tmp_path / "c.wav", 100, 16)
    _song(tmp_path)
    c = api.audio_import("Loop", str(tmp_path / "c.wav"), bpm=100, pitch=3)["clip"]
    p = api.load()
    tm = p.tempo_map()
    whole = _input(p)
    right = api.clip_edit(c, "split", at=6.5)["created"]
    p = api.load()
    _, rc = p.clip(right)
    assert rc.bpm == 100 and rc.pitch == 3
    assert rc.offset == pytest.approx(6.5 * 60 / 100)
    halves = _input(p)
    want = np.array([tm.sec(k) for k in range(16)])
    assert np.abs(_onsets(halves, want) - _onsets(whole, want)).max() < 0.002
    # the same sound away from the cut (10 ms loudness frames; the stretcher's phases restart at the cut)
    fr = SR // 100

    def env(y):
        m = y.shape[1] // fr * fr
        return np.sqrt(np.mean(y[0, :m].reshape(-1, fr) ** 2, axis=1))
    a, b = env(whole), env(halves)
    keep = np.ones(len(a), bool)
    cut = int(tm.sec(6.5) * 100)
    keep[cut - 10: cut + 10] = False
    diff = np.abs(a[keep] - b[keep]).sum() / a[keep].sum()
    assert diff < 0.15, diff


def test_unstretched_clips_render_exactly_as_before(tmp_path):
    rng = np.random.default_rng(1)
    sf.write(str(tmp_path / "n.wav"), (0.2 * rng.standard_normal((SR * 3, 2))).astype(np.float32), SR,
             subtype="FLOAT")
    _song(tmp_path)
    api.project_set(tempo_changes=[[3, 90]])
    c = api.audio_import("Loop", str(tmp_path / "n.wav"), at=1, gain_db=-3, fade_in=0.05, fade_out=0.3)["clip"]
    api.clip_edit(c, "resize", length=4)
    with api._Edit(None) as p:
        p.clip(c)[1].offset = 0.25
    p = api.load()
    tm, cl = p.tempo_map(), p.clip(c)[1]
    data, _ = sf.read(str(tmp_path / "n.wav"), dtype="float32", always_2d=True)
    seg = data.T[:, int(round(0.25 * SR)):][:, :int(round((tm.sec(cl.end) - tm.sec(cl.start)) * SR))].copy()
    seg *= 10 ** (-3 / 20)
    k = int(0.05 * SR)
    seg[:, :k] *= np.linspace(0, 1, k)[None, :]
    k = int(0.3 * SR)
    seg[:, -k:] *= np.linspace(1, 0, k)[None, :]
    y = _input(p)
    ref = np.zeros_like(y)
    s = int(round(tm.sec(1) * SR))
    ref[:, s:s + seg.shape[1]] += seg
    assert np.array_equal(y, ref)
    assert "bpm" not in cl.to_json() and "pitch" not in cl.to_json()
    # following the tempo at the project's own tempo with no transpose is the same audio, untouched
    api.project_set(tempo_changes=[])
    plain = _input()
    api.clip_edit(c, "stretch", bpm=120)
    assert np.array_equal(_input(), plain)
    assert not list((tmp_path / "song" / "cache").glob("stretch-*"))


def test_the_stretch_cache_is_reused_and_follows_the_tempo(tmp_path, monkeypatch):
    _clicks(tmp_path / "c.wav", 100, 8)
    _song(tmp_path)
    api.audio_import("Loop", str(tmp_path / "c.wav"), bpm=100)
    calls = []
    real = engine._time_stretch
    monkeypatch.setattr(engine, "_time_stretch", lambda *a: calls.append(1) or real(*a))
    cache = tmp_path / "song" / "cache"
    first = _input()
    assert len(calls) == 1 and len(list(cache.glob("stretch-*.wav"))) == 1
    assert np.allclose(_input(), first, atol=1e-7) and len(calls) == 1
    api.track_update("Loop", volume_db=-6)  # nothing the stretch depends on
    _input()
    assert len(calls) == 1
    api.project_set(tempo_changes=[[4, 90]])
    _input()
    assert len(calls) == 2 and len(list(cache.glob("stretch-*.wav"))) == 2
    assert not list(cache.glob("*.part.wav"))


def test_live_stems_follow_the_tempo_map_stretched_or_not(tmp_path):
    _clicks(tmp_path / "c.wav", 100, 8)
    _song(tmp_path)
    c = api.audio_import("Loop", str(tmp_path / "c.wav"), at=8)["clip"]
    for bpm in (None, 100):
        if bpm:
            api.clip_edit(c, "stretch", bpm=bpm)
        p = api.load()
        r = engine.Renderer(p, backend="dawdreamer")
        t = p.track("Loop")
        spec = r.track_spec(t)
        a = live._audio_stem(r, p, t, spec)
        r.tm.bpms = [121.0]  # same clips and length, another tempo: the clip lands elsewhere
        assert live._audio_stem(r, p, t, spec) != a


def test_stretch_op_checks_its_arguments(tmp_path):
    _clicks(tmp_path / "c.wav", 100, 8)
    _song(tmp_path)
    c = api.audio_import("Loop", str(tmp_path / "c.wav"))["clip"]
    with pytest.raises(ValueError, match="bpm"):
        api.clip_edit(c, "stretch")
    with pytest.raises(ValueError, match="20-400"):
        api.clip_edit(c, "stretch", bpm=10)
    with pytest.raises(ValueError, match="-24 and 24"):
        api.clip_edit(c, "stretch", pitch=30)
    got = api.clip_edit(c, "stretch", bpm=100)
    assert got["length_beats"] == pytest.approx(8.0)  # the same 4.8 s of audio, now in its own beats
    got = api.clip_edit(c, "stretch", bpm=0)
    assert got["bpm"] is None and got["length_beats"] == pytest.approx(9.6)
    api.track_add("Keys", "synth:pluck")
    api.write("Keys", "C4:q")
    mc = api.load().track("Keys").clips[0].id
    with pytest.raises(ValueError, match="audio clips"):
        api.clip_edit(mc, "stretch", pitch=2)
