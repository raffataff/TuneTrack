"""The live engine (engine/, tunetrack/live.py): the spec it loads, and its sound against the offline renderer."""
from pathlib import Path

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


@pytest.mark.skipif(not live.engine_runs(), reason="the live engine is not built, or Windows will not start it (engine/README.md)")
def test_engine_renders_the_same_mix_as_the_offline_renderer(proj, tmp_path):
    res = live.null_test(proj, per_sample=True, keep=tmp_path / "engine.wav")
    assert not res["warnings"]
    assert res["ref_rms_db"] > -40
    assert res["residual_db"] < -60, res


@pytest.mark.skipif(not live.engine_runs(), reason="the live engine is not built, or Windows will not start it (engine/README.md)")
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


@pytest.mark.skipif(not live.engine_runs(), reason="the live engine is not built, or Windows will not start it (engine/README.md)")
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
        api.fx_add("master", "builtin:gain")
        tk = takes.take_new("try", use=False, session=str(proj.path))["take"]
        st = server.live_play(beat=0.0, take=tk)           # the Desk opens a take: same engine, other project
        assert st["view"].replace("\\", "/").endswith("takes/" + tk)
        with pytest.raises(live.EngineError, match="no window"):
            server.live_window("Harp", 0)                  # built-in devices have none
        with pytest.raises(live.EngineError, match="master/fx/0 has no window"):
            server.live_window("master", 0, take=tk)       # a master insert is found by the engine, not "nothing loaded"
        with pytest.raises(KeyError):
            server.live_window("Nobody")
        assert server.live_stop()["playing"] is False
    finally:
        assert server.live_off()["on"] is False


def test_a_master_insert_window_names_the_engines_master_slot(proj, monkeypatch):
    """The engine loads the master chain as "master/fx/<n>" and leaves bypassed effects out, like a track's."""
    from tunetrack import livehost
    api.fx_add("master", "builtin:gain")
    api.fx_add("master", "builtin:width")
    api.fx_add("master", "builtin:eq")
    api.fx_update("master", 0, bypass=True)
    calls = []

    class Fake:
        project = api.load()
        engine = type("E", (), {"call": lambda self, op, **kw: calls.append((op, kw))})()

    monkeypatch.setattr(livehost, "_ensure", lambda path: Fake)
    assert livehost.window(proj.path, "master", 2) == {"opened": "master/fx/1"}
    assert livehost.window(proj.path, "Master", "1") == {"opened": "master/fx/0"}
    assert calls == [("editor", {"ref": "master/fx/1"}), ("editor", {"ref": "master/fx/0"})]
    with pytest.raises(RuntimeError, match="bypassed"):
        livehost.window(proj.path, "master", 0)
    with pytest.raises(KeyError):
        livehost.window(proj.path, "master", 3)


ENGINE_RUNS =pytest.mark.skipif(not live.engine_runs(),
                                 reason="the live engine is not built, or Windows will not start it (engine/README.md)")


def _per_sample(monkeypatch, on: bool) -> None:
    from tunetrack import config
    real = config.load_config
    monkeypatch.setattr(config, "load_config", lambda: {**real(), "synth_per_sample": on})


def test_offline_backend_falls_back_when_the_engine_cannot_run(proj, monkeypatch):
    from tunetrack import config
    monkeypatch.setattr(live, "engine_runs", lambda: False)
    assert engine.offline_backend() == "dawdreamer"
    monkeypatch.setattr(config, "load_config", lambda: {**config.DEFAULT_CONFIG, "offline_engine": "engine"})
    with pytest.raises(RuntimeError, match="Smart App Control"):
        engine.offline_backend()
    monkeypatch.setattr(config, "load_config", lambda: {**config.DEFAULT_CONFIG, "offline_engine": "dawdreamer"})
    assert engine.Renderer(proj).backend == "dawdreamer"


@pytest.mark.skipif(live.engine_exe() is None, reason="the live engine is not built (engine/README.md)")
def test_engine_renders_are_cached_apart_from_the_workers(proj):
    t = next(x for x in proj.tracks if x.name == "Harp")
    a, b = engine.Renderer(proj, backend="engine"), engine.Renderer(proj, backend="dawdreamer")
    spec = a.track_spec(t)
    assert a.key(spec) != b.key(spec) and b.key(spec) == engine._fingerprint(spec)
    sfz = {**spec, "instrument": {"type": "sfz", "path": "x.sfz"}, "fx": []}
    if a.sfz_in_engine:  # sfizz plays it in the engine, TuneTrack's own player in the worker
        assert a.key(sfz) != b.key(sfz)
        a.sfz_in_engine = False
        assert a.key({**sfz, "fx": spec["fx"]}) != engine.Renderer(proj, backend="engine").key({**sfz, "fx": spec["fx"]})
    assert a.key(sfz) == b.key(sfz)  # without sfizz the worker renders SFZ either way: one cache file for both


@ENGINE_RUNS
def test_the_engine_renders_offline_what_the_worker_did(proj, monkeypatch):
    """Every track before its fader, the buses (delay keyed by the kick, reverb) and the master, rendered by the
    engine, against the same session rendered by DawDreamer (whose synths free voices as per-sample mode does)."""
    _per_sample(monkeypatch, True)
    ref = engine.Renderer(proj, backend="dawdreamer")
    ref.run()
    got = engine.Renderer(proj, backend="engine")
    rep = got.run()
    assert not [w for w in rep["warnings"] if "failed" in w], rep["warnings"]
    assert set(got.pre) == set(ref.pre)
    for tid in ref.pre:
        assert live.compare(ref.pre[tid], got.pre[tid], ref.sr)["residual_db"] < -60, tid
    for tid in ref.post:
        assert live.compare(ref.post[tid], got.post[tid], ref.sr)["residual_db"] < -60, tid
    assert live.compare(ref.master, got.master, ref.sr)["residual_db"] < -60
    again = engine.Renderer(proj, backend="engine")
    rep2 = again.run()  # nothing changed: every track read from the cache
    assert all(v["render"] == "cached" for v in rep2["tracks"].values())
    assert live.compare(got.master, again.master, ref.sr)["residual_db"] < -100


@ENGINE_RUNS
def test_an_engine_export_writes_stems_through_the_buses(proj, tmp_path):
    rep = engine.render_project(proj, str(tmp_path / "out"), stems=True)
    assert rep["renderer"].backend == "engine"
    names = sorted(Path(f).name for f in rep["files"])
    assert any("_stem_Harp" in n for n in names) and any("_stem_Kick" in n for n in names)


@ENGINE_RUNS
def test_a_track_that_fails_in_the_engine_leaves_nothing_in_the_cache(proj, monkeypatch):
    real = engine.Renderer._engine_render

    def broken(self, spec, taps, names, eng):
        if any(s["name"] == "Harp" for s in spec["tracks"]):
            for f in taps.values():
                Path(f).write_bytes(b"half a file")
            return {"error": "the engine stopped (crashed in some.dll+1234)"}
        return real(self, spec, taps, names, eng)

    monkeypatch.setattr(engine.Renderer, "_engine_render", broken)
    monkeypatch.setattr(engine.config, "load_config", lambda: {**engine.config.DEFAULT_CONFIG, "render_workers": 1})
    r = engine.Renderer(proj, backend="engine")
    rep = r.run()
    assert any("Harp" in w and "crashed in some.dll" in w for w in rep["warnings"])
    t = next(x for x in proj.tracks if x.name == "Harp")
    assert not (r.cache / f"{r.key(r.track_spec(t))}.wav").exists()


HALION = Path(r"C:\Program Files\Common Files\VST3\Steinberg\HALion Sonic.vst3")
HALION_STATE = Path(__file__).resolve().parents[1] / "library" / "patches" / "halion-sonic-bass-1.state"


@ENGINE_RUNS
@pytest.mark.skipif(not (HALION.exists() and HALION_STATE.exists()), reason="HALion Sonic and its Bass 1 patch")
def test_a_sampler_is_heard_settling_before_the_render(tmp_path):
    """HALion loads its samples from message-thread callbacks: the probe plays it one of its own notes until two
    rounds agree, and only then renders. With no wait at all beforehand it must still come out right."""
    import os
    import soundfile as sf
    sr, n = 48000, 48000 * 3
    notes = []
    for i, pitch in enumerate((40, 43, 45, 47)):
        notes += [int(i * 0.5 * sr), 0x90, pitch, 100, int((i * 0.5 + 0.4) * sr), 0x80, pitch, 0]
    spec = {"sr": sr, "block": 512, "ts": [4, 4], "end": n, "tempo": [[0, 0.0, 120.0]], "loop": None,
            "tracks": [{"id": "t1", "name": "Bass", "kind": "midi", "on": False, "out": "master", "sends": [],
                        "db": 0.0, "pan": 0.0, "vol_auto": [], "pan_auto": [], "fx": [],
                        "source": {"type": "vst3", "path": str(HALION), "shell": None, "state": str(HALION_STATE),
                                   "params": {}, "name": "HALion Sonic", "events": notes}}],
            "master": {"db": 0.0, "fx": []}}
    os.environ["TT_SETTLE_MS"], os.environ["TT_SETTLE_EACH_MS"] = "0", "0"  # read by the engine at start
    try:
        eng = live.Engine(no_device=True)
    finally:
        del os.environ["TT_SETTLE_MS"], os.environ["TT_SETTLE_EACH_MS"]
    try:
        eng.load_spec(spec, tmp_path / "spec.json")
        res = eng.render(None, n, retries=0, taps={"t1": str(tmp_path / "bass.wav")})
    finally:
        eng.close()
    assert not res.get("unready") and not res.get("silent"), res
    assert res["probe_ms"] > 0
    audio, _ = sf.read(str(tmp_path / "bass.wav"), dtype="float32", always_2d=True)
    assert audio.shape == (n, 2)
    for i in range(4):  # every note sounds, the first one too
        assert np.abs(audio[int(i * 0.5 * sr):int((i * 0.5 + 0.3) * sr)]).max() > 1e-3, i


@ENGINE_RUNS
def test_synth_releases_ring_out_unless_per_sample_is_asked_for(tmp_path, monkeypatch):
    """The owner's choice (2026-11-16): a plucked string's release (0.35 s) is heard after its note-off. Per-sample
    mode, DawDreamer's way, frees a releasing voice at its first near-silent sample, which for a single string is
    the first zero crossing: the note stops dead. (The pad and FM synths checked sound the same either way.)"""
    api.project_new("Tails", folder=str(tmp_path / "tails"), tempo=120)
    api.track_add("Pluck", "synth:pluck")
    api.write("Pluck", "C4:q r:q r:h | r:w")  # note-off at 0.5 s
    p = api.load()
    sr = p.sample_rate
    tails = {}
    for on in (False, True):
        _per_sample(monkeypatch, on)
        r = engine.Renderer(api.load(), backend="engine")
        r.run()
        audio = r.pre[p.tracks[0].id]
        tails[on] = float(np.abs(audio[:, int(0.6 * sr):int(0.75 * sr)]).max())
    assert tails[False] > 1e-3, tails
    assert tails[True] < 1e-6, tails


@ENGINE_RUNS
def test_strips_computed_side_by_side_sound_the_same_as_on_one_thread(proj, tmp_path):
    """Source strips that no insert keys into are computed in lanes, on several threads at once; a strip whose
    insert listens to another stays in the session's graph. Either way every tap and the master come out the same,
    whatever the number of threads, and the mix still matches the offline renderer."""
    import soundfile as sf
    for i, line in enumerate(["D5:e F5 A5 D6 C6 A5 F5 E5 | D5:q A4 D5:h", "A3:h C4 | D4:w", "F2:q F2 C3 F2 | D2:w"]):
        api.track_add(f"Pluck {i}", "synth:pluck")
        api.write(f"Pluck {i}", line)
    api.fx_add("Pluck 1", {"type": "builtin", "name": "compressor", "params": DUCK}, sidechain="Kick")
    p = api.load()
    spec, warnings = live.build_spec(p)
    assert not warnings
    n = spec["end"]
    taps = {t["id"]: t["name"] for t in spec["tracks"] if t["source"]}
    got = {}
    for threads in (1, 4):
        eng = live.Engine(no_device=True, per_sample=True, threads=threads)
        try:
            res = eng.load_spec(spec, tmp_path / "spec.json")
            assert res["threads"] == threads and not res["warnings"]
            assert res["lanes"] == len(taps) - 1  # Pluck 1 listens to the kick
            out = eng.render(tmp_path / f"master{threads}.wav", n, retries=0,
                             taps={tid: str(tmp_path / f"{tid}-{threads}.wav") for tid in taps})
            assert not out.get("silent"), out
        finally:
            eng.close()
        got[threads] = {k: sf.read(str(tmp_path / f"{k}-{threads}.wav"), dtype="float32")[0] for k in taps}
        got[threads]["master"] = sf.read(str(tmp_path / f"master{threads}.wav"), dtype="float32")[0]
    for k in got[1]:
        assert np.abs(got[1][k]).max() > 1e-3, k
        assert np.array_equal(got[1][k], got[4][k]), k
    ref = engine.Renderer(p, backend="dawdreamer")
    ref.run()
    assert live.compare(ref.master, got[4]["master"].T, ref.sr)["residual_db"] < -60


def _tone_spec(path: Path, sr: int, hz: float, seconds: float) -> dict:
    import soundfile as sf
    t = np.arange(int(sr * seconds)) / sr
    sf.write(str(path), np.stack([0.3 * np.sin(2 * np.pi * hz * t)] * 2, axis=1).astype(np.float32), sr)
    return {"sr": sr, "block": 512, "ts": [4, 4], "end": len(t), "tempo": [[0, 0.0, 120.0]], "loop": None,
            "tracks": [{"id": "t1", "name": "Tone", "kind": "audio", "on": True, "out": "master", "sends": [],
                        "db": 0.0, "pan": 0.0, "vol_auto": [], "pan_auto": [], "fx": [],
                        "source": {"type": "stem", "file": str(path)}}],
            "master": {"db": 0.0, "fx": []}}


@ENGINE_RUNS
@pytest.mark.parametrize("project_sr, device_sr", [(48000, 44100), (44100, 48000)])
def test_a_device_at_another_rate_hears_the_project_at_its_own_pitch(tmp_path, project_sr, device_sr):
    """The session runs at the project's rate; a device that cannot is fed through a converter. Pulled through the
    device path without a device: a 1 kHz tone stays 1 kHz at the device's rate, clean, at its level."""
    import soundfile as sf
    eng = live.Engine(no_device=True)
    try:
        eng.load_spec(_tone_spec(tmp_path / "tone.wav", project_sr, 1000.0, 2.0), tmp_path / "spec.json")
        eng.call("pull", device_sr=device_sr, block=441 if device_sr == 44100 else 480, length=device_sr,
                 out=str(tmp_path / "heard.wav"))
    finally:
        eng.close()
    heard, sr = sf.read(str(tmp_path / "heard.wav"), dtype="float64")
    assert sr == device_sr and len(heard) == device_sr
    x = heard[device_sr // 10:, 0]  # past the converter's start
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x)))) ** 2
    freqs = np.fft.rfftfreq(len(x), 1 / device_sr)
    assert abs(freqs[np.argmax(spec)] - 1000.0) < 2.0
    near = spec[np.abs(freqs - 1000.0) < 30].sum()
    assert 10 * np.log10(spec.sum() - near + 1e-30) - 10 * np.log10(near) < -70  # no clicks, no aliasing
    assert np.sqrt(np.mean(x ** 2)) == pytest.approx(0.3 / np.sqrt(2), rel=0.002)


@ENGINE_RUNS
def test_going_down_a_rate_does_not_fold_what_the_device_cannot_play(tmp_path):
    """A 96 kHz project on a 48 kHz device: a 30 kHz tone would fold back to 18 kHz; it is filtered out first."""
    import soundfile as sf
    eng = live.Engine(no_device=True)
    try:
        eng.load_spec(_tone_spec(tmp_path / "tone.wav", 96000, 30000.0, 1.0), tmp_path / "spec.json")
        eng.call("pull", device_sr=48000, block=480, length=24000, out=str(tmp_path / "heard.wav"))
    finally:
        eng.close()
    heard, _ = sf.read(str(tmp_path / "heard.wav"), dtype="float64")
    assert 20 * np.log10(np.abs(heard[4800:]).max() / 0.3) < -60


SFIZZ = pytest.mark.skipif(not live.engine_plays_sfz(), reason="the engine is built without sfizz (engine/README.md)")


def _sfz(folder: Path, round_robin: bool = False) -> Path:
    """A one-sample SFZ instrument (a decaying 440 Hz tone at A4, stereo), or with round_robin a second sample at
    660 Hz that sfizz picks at random (lorand/hirand) half of the time."""
    import soundfile as sf
    sr = 48000
    t = np.arange(int(sr * 1.5)) / sr
    folder.mkdir(parents=True, exist_ok=True)
    for name, hz in (("a.wav", 440.0), ("b.wav", 660.0)):
        x = 0.4 * np.sin(2 * np.pi * hz * t) * np.exp(-1.5 * t)
        sf.write(str(folder / name), np.stack([x, x], axis=1).astype(np.float32), sr)
    body = ("<group> pitch_keycenter=69 ampeg_release=0.05\n<region> sample=a.wav lorand=0 hirand=0.5\n"
            "<region> sample=b.wav lorand=0.5 hirand=1\n") if round_robin else \
        "<region> sample=a.wav pitch_keycenter=69 ampeg_release=0.05\n"
    path = folder / ("rr.sfz" if round_robin else "tone.sfz")
    path.write_text(body, encoding="utf-8")
    return path


def _peak_hz(x: np.ndarray, sr: int) -> float:
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / sr)[np.argmax(spec)])


@ENGINE_RUNS
@SFIZZ
def test_sfizz_plays_an_sfz_track_in_the_engine_in_tune_and_at_the_players_level(tmp_path, monkeypatch):
    """With sfizz built in, an SFZ track is an instrument of the engine (no stem). Against TuneTrack's own SFZ
    player (the worker): every note at the same pitch and level (the same velocity curve). `sfz_player:
    "tunetrack"` brings the stem back."""
    from tunetrack import config
    api.project_new("Sampler", folder=str(tmp_path / "song"), tempo=120)
    api.track_add("Keys", f"sfz:{_sfz(tmp_path / 'inst')}")
    api.write("Keys", "A4:q!f E5 A3:h | r:w")  # 0, 0.5, 1 s; the A3 plays the sample an octave down
    p = api.load()
    spec, warnings = live.build_spec(p)
    assert not warnings and spec["tracks"][0]["source"]["type"] == "sfz"
    got = engine.Renderer(p, backend="engine")
    rep = got.run()
    assert not rep["warnings"], rep["warnings"]
    ref = engine.Renderer(p, backend="dawdreamer")
    ref.run()
    tid, sr = p.tracks[0].id, p.sample_rate
    a, b = got.pre[tid][0], ref.pre[tid][0]
    for start, hz in ((0.0, 440.0), (0.5, 659.26), (1.0, 220.0)):
        x, y = a[int((start + 0.05) * sr):int((start + 0.45) * sr)], b[int((start + 0.05) * sr):int((start + 0.45) * sr)]
        assert abs(_peak_hz(x, sr) - hz) < 3 and abs(_peak_hz(y, sr) - hz) < 3, start
        level = 20 * np.log10(np.sqrt(np.mean(x ** 2)) / np.sqrt(np.mean(y ** 2)))
        assert abs(level) < 0.5, (start, level)
    real = config.load_config
    monkeypatch.setattr(config, "load_config", lambda: {**real(), "sfz_player": "tunetrack"})
    assert live.build_spec(api.load())[0]["tracks"][0]["source"]["type"] == "stem"


@ENGINE_RUNS
@SFIZZ
def test_an_sfz_tracks_round_robins_depend_on_nothing_but_its_own_notes(tmp_path):
    """sfizz draws random round robins from one generator per process; the engine gives every SFZ instrument its
    own. A track comes out bit for bit the same alone or beside another SFZ track, on one thread or four, and on a
    second render in the same engine; and both of its samples are heard."""
    import soundfile as sf
    sr, n = 48000, 48000 * 5
    path = str(_sfz(tmp_path / "inst", round_robin=True))
    strip = {"kind": "midi", "on": False, "out": "master", "sends": [], "db": 0.0, "pan": 0.0, "vol_auto": [],
             "pan_auto": [], "fx": []}

    def notes(step: float) -> list[int]:
        ev = []
        for i in range(int(4.5 / step)):
            ev += [int(i * step * sr), 0x90, 69, 100, int((i * step + step * 0.8) * sr), 0x80, 69, 0]
        return ev

    tracks = [{**strip, "id": "one", "name": "One", "source": {"type": "sfz", "path": path, "events": notes(0.25)}},
              {**strip, "id": "two", "name": "Two", "source": {"type": "sfz", "path": path, "events": notes(0.15)}}]
    got = []
    for threads, which in ((1, tracks), (4, tracks), (1, tracks[:1])):
        spec = {"sr": sr, "block": 512, "ts": [4, 4], "end": n, "tempo": [[0, 0.0, 120.0]], "loop": None,
                "tracks": which, "master": {"db": 0.0, "fx": []}}
        eng = live.Engine(no_device=True, threads=threads)
        try:
            res = eng.load_spec(spec, tmp_path / "spec.json")
            assert not res["warnings"], res["warnings"]
            for k in range(2 if threads == 4 else 1):
                eng.render(None, n, retries=0, taps={"one": str(tmp_path / f"one-{len(got)}.wav")})
                got.append(sf.read(str(tmp_path / f"one-{len(got)}.wav"), dtype="float32")[0])
        finally:
            eng.close()
    for g in got[1:]:
        assert np.array_equal(got[0], g)
    picks = {round(_peak_hz(got[0][int((i * 0.25 + 0.02) * sr):int((i * 0.25 + 0.18) * sr), 0], sr) / 220)
             for i in range(18)}
    assert picks == {2, 3}, picks  # 440 Hz and 660 Hz notes


@ENGINE_RUNS
@SFIZZ
def test_an_sfz_note_plays_past_its_preloaded_start_offline(tmp_path):
    """sfizz keeps the first 8192 frames of each sample in memory and streams the rest. Offline the render runs far
    faster than the stream, so each block must wait for what its notes asked for (freewheeling); else a held note
    drops out 0.17 s in until the stream catches up, differently on every render."""
    import soundfile as sf
    sr, n = 48000, 48000 * 4
    t = np.arange(sr * 20) / sr
    (tmp_path / "inst").mkdir()
    sf.write(str(tmp_path / "inst" / "long.wav"), np.stack([0.3 * np.sin(2 * np.pi * 440 * t)] * 2, axis=1)
             .astype(np.float32), sr, subtype="FLOAT")
    (tmp_path / "inst" / "long.sfz").write_text("<region> sample=long.wav pitch_keycenter=69\n", encoding="utf-8")
    strip = {"kind": "midi", "on": False, "out": "master", "sends": [], "db": 0.0, "pan": 0.0, "vol_auto": [],
             "pan_auto": [], "fx": []}
    spec = {"sr": sr, "block": 512, "ts": [4, 4], "end": n, "tempo": [[0, 0.0, 120.0]], "loop": None,
            "tracks": [{**strip, "id": "t", "name": "Long", "source": {
                "type": "sfz", "path": str(tmp_path / "inst" / "long.sfz"), "events": [0, 0x90, 69, 127, 3 * sr, 0x80, 69, 0]}}],
            "master": {"db": 0.0, "fx": []}}
    eng = live.Engine(no_device=True)
    try:
        eng.load_spec(spec, tmp_path / "spec.json")
        eng.render(None, n, retries=0, taps={"t": str(tmp_path / "t.wav")})
    finally:
        eng.close()
    x = sf.read(str(tmp_path / "t.wav"), dtype="float64")[0][:, 0]
    win = sr // 20
    rms = np.sqrt(np.mean(x[int(0.05 * sr):int(2.95 * sr)][: (int(2.9 * sr) // win) * win].reshape(-1, win) ** 2, axis=1))
    assert rms.min() > 0.9 * rms.max(), (rms.min(), rms.max())  # no drop-out anywhere in the held note
    assert rms.max() == pytest.approx(0.3 / np.sqrt(2), rel=0.01)


NOVA = Path(r"C:\Program Files\Common Files\VST3\TDR Nova.vst3")


@ENGINE_RUNS
@pytest.mark.skipif(not NOVA.exists(), reason="TDR Nova (an effect with a mono sidechain input)")
def test_a_mono_sidechain_input_hears_both_sides_of_its_key(tmp_path):
    """TDR Nova's key input is one channel. It hears (L + R) / 2 of a stereo key, as the offline worker feeds it:
    a key on the right side only ducks exactly as the same key at half the level on both sides does."""
    import soundfile as sf
    sr, n = 48000, 48000 * 2
    t = np.arange(n) / sr
    tone = np.stack([0.3 * np.sin(2 * np.pi * 220 * t)] * 2, axis=1).astype(np.float32)
    burst = (0.8 * np.sin(2 * np.pi * 90 * t) * ((t % 0.5) < 0.25)).astype(np.float32)
    sf.write(str(tmp_path / "tone.wav"), tone, sr)
    zero = np.zeros_like(burst)
    keys = {"right": np.stack([zero, burst], axis=1), "both": np.stack([burst / 2, burst / 2], axis=1),
            "none": np.stack([zero, zero], axis=1)}
    nova = {"type": "vst3", "path": str(NOVA), "shell": None, "state": None, "name": "TDR Nova", "key": "k",
            "params": {"Wide Band Dyn": "On", "Wide Band Threshold": "-40", "Sidechain Mode": 0.5}}
    strip = {"on": True, "out": "master", "sends": [], "db": 0.0, "pan": 0.0, "vol_auto": [], "pan_auto": []}
    out = {}
    eng = live.Engine(no_device=True)
    try:
        for name, key in keys.items():
            sf.write(str(tmp_path / f"key-{name}.wav"), key.astype(np.float32), sr)
            spec = {"sr": sr, "block": 512, "ts": [4, 4], "end": n, "tempo": [[0, 0.0, 120.0]], "loop": None,
                    "tracks": [{**strip, "id": "t", "name": "Tone", "kind": "audio", "fx": [nova],
                                "source": {"type": "stem", "file": str(tmp_path / "tone.wav")}},
                               {**strip, "id": "k", "name": "Key", "kind": "audio", "fx": [], "on": False,
                                "source": {"type": "stem", "file": str(tmp_path / f"key-{name}.wav")}}],
                    "master": {"db": 0.0, "fx": []}}
            res = eng.load_spec(spec, tmp_path / "spec.json")
            assert not res["warnings"], res["warnings"]
            eng.render(None, n, retries=0, taps={"t": str(tmp_path / f"t-{name}.wav")})
            out[name] = sf.read(str(tmp_path / f"t-{name}.wav"), dtype="float64")[0]
    finally:
        eng.close()
    level = {k: float(np.sqrt(np.mean(v[sr // 2:] ** 2))) for k, v in out.items()}
    assert level["right"] < level["none"] * 0.9, level  # the key on the right is heard: Nova ducks
    assert live.compare(out["both"].T, out["right"].T, sr)["residual_db"] < -60, level
