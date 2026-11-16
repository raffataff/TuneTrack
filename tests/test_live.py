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


ENGINE_RUNS = pytest.mark.skipif(not live.engine_runs(),
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
    assert a.key(sfz) == b.key(sfz)  # the worker renders SFZ either way: one cache file for both


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
