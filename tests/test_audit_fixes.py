"""Regression tests for the reliability/security audit (concurrency, clip bounds, editor server, SFZ expression,
section edits, audio imports, layer routing, bus solo, pattern tokens)."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

from tunetrack import api, engine, generators, sfz, theory
from tunetrack.model import Note


@pytest.fixture
def proj(tmp_path):
    api.project_new("Audit Song", folder=str(tmp_path / "song"), tempo=120, key="C major")
    api.track_add("Lead", "synth:lead")
    api.track_add("Pad", "synth:pad")
    return str(tmp_path / "song")


def _starts(track):
    return sorted(c.start + n.start for c in api.load().track(track).clips for n in c.notes)


# ---- 1. concurrent edits ----------------------------------------------------------------------------------------

def test_concurrent_thread_edits_are_all_kept(proj):
    errors = []

    def worker(i):
        try:
            api.markers(add=[{"at": i, "name": f"m{i}"}])
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(api.load().markers) == 16


def test_concurrent_process_edits_are_all_kept(proj):
    code = ("import sys; from tunetrack import api\n"
            "for i in range(8):\n"
            "    api.markers(project=sys.argv[1], add=[{'at': int(sys.argv[2]) * 100 + i, 'name': 'x'}])\n")
    env = {**os.environ}
    procs = [subprocess.Popen([sys.executable, "-c", code, proj, str(k)], env=env) for k in range(3)]
    for p in procs:
        assert p.wait(timeout=120) == 0
    assert len(api.load(proj).markers) == 24


def test_revision_increments_and_stale_edit_is_refused(proj):
    rev = api.load().rev
    api.write("Lead", "C4:q D4")
    assert api.load().rev == rev + 1
    tok = api.expected_rev.set(rev)  # an editor still looking at the old revision
    try:
        with pytest.raises(api.ConflictError):
            api.write("Lead", "E4:q")
    finally:
        api.expected_rev.reset(tok)
    assert _starts("Lead") == [0, 1]
    api.undo()
    assert api.load().rev > rev + 1  # undo is a change too: editors must reload


def test_clip_set_refuses_stale_note_list(proj):
    api.write("Lead", "C4:q D4 E4 F4")
    c = api.load().track("Lead").clips[0]
    seen = [n.to_json() for n in c.notes]
    api.write("Lead", "G5:e", at=2.5, mode="merge")  # the agent adds a note to the same clip meanwhile
    with pytest.raises(api.ConflictError):
        api.clip_set(c.id, notes=seen[:2], expect_notes=seen)
    assert len(api.load().track("Lead").clips[0].notes) == 5
    now = [n.to_json() for n in api.load().track("Lead").clips[0].notes]
    api.clip_set(c.id, notes=now[:2], expect_notes=now)
    assert len(api.load().track("Lead").clips[0].notes) == 2


# ---- 2. edits that move notes keep them audible -------------------------------------------------------------------

def _midi_count(track):
    p = api.load()
    return engine._track_events(p, p.track(track), 960)[1]


def test_shifting_notes_past_the_clip_end_extends_the_clip(proj):
    api.write("Lead", "C4:q D4 E4 F4")
    assert _midi_count("Lead") == 4
    api.notes_edit("Lead", "shift", beats=8)
    assert _starts("Lead") == [8, 9, 10, 11]
    assert _midi_count("Lead") == 4
    api.notes_edit("Lead", "stretch", factor=4)
    assert _midi_count("Lead") == 4


def test_shifting_notes_before_the_clip_start_moves_the_clip(proj):
    api.write("Lead", "C4:q D4", at="3:1")
    api.notes_edit("Lead", "shift", beats=-6)
    assert _starts("Lead") == [2, 3]
    assert _midi_count("Lead") == 2


def test_notes_hidden_by_a_trimmed_clip_stay_hidden(proj):
    api.write("Lead", "C4:w | D4:w")
    c = api.load().track("Lead").clips[0]
    api.clip_edit(c.id, "resize", length="1 bar")
    assert _midi_count("Lead") == 1
    api.notes_edit("Lead", "transpose", semitones=2)
    assert _midi_count("Lead") == 1


# ---- 3. editor server -------------------------------------------------------------------------------------------

def _asgi(app, method, path, headers=None, body=b"", port=7373):
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": b"", "headers": hdrs,
             "server": ("127.0.0.1", port), "client": ("127.0.0.1", 5555), "root_path": ""}
    sent = []
    done = False

    async def receive():
        nonlocal done
        if done:
            return {"type": "http.disconnect"}
        done = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(app(scope, receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, data


def test_server_requires_token_origin_host_and_allowlist(proj):
    from tunetrack import server
    good = {"host": "127.0.0.1:7373", "content-type": "application/json", "x-tunetrack-token": server.TOKEN,
            "origin": "http://127.0.0.1:7373"}
    body = json.dumps({"fn": "project_list", "args": {}}).encode()
    assert _asgi(server.app, "POST", "/api/call", good, body)[0] == 200
    assert _asgi(server.app, "POST", "/api/call", {**good, "x-tunetrack-token": "nope"}, body)[0] == 403
    assert _asgi(server.app, "POST", "/api/call", {**good, "origin": "http://evil.example"}, body)[0] == 403
    assert _asgi(server.app, "POST", "/api/call", {**good, "content-type": "text/plain"}, body)[0] == 415
    assert _asgi(server.app, "POST", "/api/call", {**good, "host": "evil.example:7373"}, body)[0] == 421
    assert _asgi(server.app, "GET", "/api/state", {"host": "rebind.example:7373"})[0] == 421
    for fn in ("audio_import", "patch_delete", "play", "midi_import", "patch_import_folder", "ui_open", "load"):
        b = json.dumps({"fn": fn, "args": {}}).encode()
        assert _asgi(server.app, "POST", "/api/call", good, b)[0] == 400, fn
    status, html = _asgi(server.app, "GET", "/", {"host": "localhost:7373"})
    assert status == 200 and server.TOKEN.encode() in html


def test_server_reports_conflicts(proj):
    from tunetrack import server
    api.write("Lead", "C4:q")
    rev = api.load().rev
    api.write("Lead", "D4:q", at=4, mode="merge")
    good = {"host": "127.0.0.1:7373", "content-type": "application/json", "x-tunetrack-token": server.TOKEN}
    body = json.dumps({"fn": "track_update", "args": {"track": "Lead", "volume_db": -3}, "rev": rev}).encode()
    status, data = _asgi(server.app, "POST", "/api/call", good, body)
    assert status == 409 and json.loads(data)["conflict"]


# ---- 4. SFZ expression ------------------------------------------------------------------------------------------

def _sfz(tmp_path, body):
    f = tmp_path / "t.sfz"
    f.write_text(body)
    return sfz.Instrument(f)


def test_sfz_cc_follows_a_held_note(tmp_path):
    inst = _sfz(tmp_path, "<region> sample=*sine key=60 loop_mode=loop_continuous loop_start=0 loop_end=47999 "
                          "amplitude_oncc1=100 ampeg_release=0.01\n")
    note = ("note", 0.0, 2.0, 60, 100)
    flat = inst.render([note, ("cc", 0.0, 1, 0)], 2.2)
    swell = inst.render([note, ("cc", 0.0, 1, 0)] + [("cc", 0.1 * i, 1, min(127, 7 * i)) for i in range(1, 20)], 2.2)
    sr = 48000
    assert np.abs(flat).max() < 1e-4
    assert np.abs(swell[:, int(1.8 * sr):int(1.9 * sr)]).max() > 0.3
    assert np.abs(swell[:, int(0.05 * sr):int(0.09 * sr)]).max() < 1e-3


def test_sfz_cc_crossfade_layers_follow_a_held_note(tmp_path):
    inst = _sfz(tmp_path, "<region> sample=*sine key=60 loop_mode=loop_continuous loop_start=0 loop_end=47999 "
                          "xfin_locc1=0 xfin_hicc1=127\n")
    note = ("note", 0.0, 1.0, 60, 100)
    held = inst.render([note, ("cc", 0.0, 1, 0), ("cc", 0.5, 1, 127)], 1.0)
    assert np.abs(held[:, 24000 + 2000:]).max() > 0.3
    assert np.abs(held[:, :20000]).max() < 1e-3


def test_sfz_pitch_bend_changes_pitch(tmp_path):
    inst = _sfz(tmp_path, "<region> sample=*sine key=60 loop_mode=loop_continuous loop_start=0 loop_end=47999 "
                          "bend_up=1200\n")
    note = ("note", 0.0, 0.5, 60, 127)

    def freq(sig):
        x = sig[0, 4800:19200]
        return np.count_nonzero(np.diff(np.signbit(x))) / 2 / (len(x) / 48000)

    plain = freq(inst.render([note], 0.5))
    bent = freq(inst.render([note, ("bend", 0.0, 8191)], 0.5))
    assert abs(plain - 261.6) < 5 and abs(bent - 523.2) < 10


def test_worker_midi_events_keep_pitch_bend(tmp_path):
    import mido

    from tunetrack import worker
    mf = mido.MidiFile(ticks_per_beat=480)
    tr = mido.MidiTrack()
    mf.tracks.append(tr)
    tr += [mido.Message("note_on", note=60, velocity=90, time=0), mido.Message("pitchwheel", pitch=4096, time=240),
           mido.Message("note_off", note=60, velocity=0, time=240)]
    mf.save(str(tmp_path / "b.mid"))
    bends = []
    notes, ccs = worker._midi_events(str(tmp_path / "b.mid"), bends)
    assert len(notes) == 1 and bends and bends[0][1] == 4096


# ---- 5. section edits -------------------------------------------------------------------------------------------

def test_insert_moves_automation_tempo_loop_and_length(proj):
    api.write("Lead", "C4:w | D4:w | E4:w")
    api.automation("Lead", points=[["1:1", -6], ["3:1", 0]])
    api.project_set(tempo_changes=[["3:1", 90]], loop="1:1-4:1")
    api.arrange("insert", "2:1", "4:1")
    p = api.load()
    assert p.tempo_changes == [[16.0, 90.0]]
    assert p.loop == {"start": 0.0, "end": 20.0}
    lane = dict((b, v) for b, v in p.track("Lead").automation["volume_db"])
    assert lane[0] == -6 and lane[16] == 0
    assert abs(lane[4] - lane[12]) < 1e-6  # value held across the gap
    assert _starts("Lead") == [0, 12, 16]


def test_delete_moves_automation_tempo_loop(proj):
    api.write("Lead", "C4:w | D4:w | E4:w | F4:w")
    api.automation("Lead", points=[["4:1", -12]])
    api.project_set(tempo_changes=[["2:1", 90], ["4:1", 140]], loop="1:1-5:1")
    api.arrange("delete", "2:1", "3:1")
    p = api.load()
    assert p.track("Lead").automation["volume_db"] == [[8.0, -12.0]]
    assert p.tempo_map().bpm_at(4) == 90 and p.tempo_map().bpm_at(8) == 140
    assert p.loop == {"start": 0.0, "end": 12.0}


def test_arrange_copy_replace_and_merge(proj):
    api.write("Lead", "C4:w")
    api.write("Lead", "G4:w", at="3:1")
    api.arrange("copy", "1:1", "2:1", to="3:1")
    pitches = sorted(n.pitch for c in api.load().track("Lead").clips for n in c.notes if c.start + n.start >= 8)
    assert pitches == [60]
    api.write("Lead", "G4:w", at="5:1")
    api.arrange("copy", "1:1", "2:1", to="5:1", mode="merge")
    pitches = sorted(n.pitch for c in api.load().track("Lead").clips for n in c.notes if c.start + n.start >= 16)
    assert pitches == [60, 67]


# ---- 6. audio imports ------------------------------------------------------------------------------------------

def test_same_named_audio_files_get_their_own_copy(proj, tmp_path):
    import soundfile as sf
    a, b = tmp_path / "a" / "tone.wav", tmp_path / "b" / "tone.wav"
    for f, hz in ((a, 220), (b, 440)):
        f.parent.mkdir()
        sf.write(str(f), (0.3 * np.sin(2 * np.pi * hz * np.arange(4800) / 48000)).astype(np.float32), 48000)
    api.track_add("Audio", kind="audio")
    api.audio_import("Audio", str(a))
    api.audio_import("Audio", str(b), at=8)
    api.audio_import("Audio", str(a), at=16)
    files = [c.file for c in api.load().track("Audio").clips]
    assert files == ["audio/tone.wav", "audio/tone-2.wav", "audio/tone.wav"]
    copied = sf.read(str(Path(proj) / "audio" / "tone-2.wav"))[0]
    assert np.allclose(copied, sf.read(str(b))[0])


# ---- 7/8. routing: layers through buses + master, bus solo --------------------------------------------------------

def _fake_renderer(monkeypatch, p, sources: dict[str, float]):
    """A Renderer whose tracks are constant DC signals and whose FX just double their input (to detect them)."""
    r = engine.Renderer(p)
    r.n = 4800
    r.sr = 48000

    def fake_fx(self, audio, devices, tag, *_owner_warnings):
        return audio * 2 if devices else audio

    monkeypatch.setattr(engine.Renderer, "render_fx", fake_fx)
    return r, {tid: np.full((2, r.n), v, dtype=np.float32) for tid, v in sources.items()}


def test_layers_include_bus_and_master_processing(monkeypatch, proj, tmp_path):
    import soundfile as sf
    api.track_add("Verb", kind="bus", fx=["builtin:reverb"])
    api.track_update("Lead", output="Verb", layer="melody")
    api.track_update("Pad", layer="base")
    p = api.load()
    lead, pad = p.track("Lead").id, p.track("Pad").id
    r, pre = _fake_renderer(monkeypatch, p, {lead: 0.1, pad: 0.1})
    r.pre = pre
    r.master, r.post = r.mix(pre, {"warnings": []})
    r.report = {"warnings": [], "tracks": {}}
    rep = r.export(tmp_path, "song", "wav", 32, "off", normalize=False, layers=True, stems=True)
    melody = sf.read(next(f for f in rep["files"] if f.endswith("_layer_melody.wav")))[0]
    base = sf.read(next(f for f in rep["files"] if f.endswith("_layer_base.wav")))[0]
    assert abs(melody[1000, 0] - 0.2) < 1e-4  # went through the bus FX (x2)
    assert abs(base[1000, 0] - 0.1) < 1e-4
    assert abs(r.master[0, 1000] - 0.3) < 1e-4


def test_layers_are_limited_not_clipped(monkeypatch, proj, tmp_path):
    import soundfile as sf
    p = api.load()
    r, pre = _fake_renderer(monkeypatch, p, {p.track("Lead").id: 0.9})
    pre[p.track("Lead").id][:, 2000:2100] = 3.0
    r.pre = pre
    r.master, r.post = r.mix(pre, {"warnings": []})
    r.report = {"warnings": [], "tracks": {}}
    rep = r.export(tmp_path, "song", "wav", 32, "off", normalize=False, layers=True)
    lay = sf.read(next(f for f in rep["files"] if "_layer_" in f))[0]
    assert np.abs(lay).max() <= 10 ** (-1 / 20) + 1e-4
    assert rep.get("stem_limiter_reduction_db")


def test_soloing_a_bus_plays_its_sources(proj):
    api.track_add("Drums", "synth:pad")
    api.track_add("Grp", kind="bus")
    api.track_add("Verb", kind="bus")
    api.track_update("Lead", output="Grp")
    api.track_update("Pad", sends=[{"to": "Verb", "db": -6}])
    api.track_update("Grp", output="Verb")
    api.track_update("Grp", solo=True)
    p = api.load()
    ids = {t.name: t.id for t in p.tracks}
    assert engine.soloed_sources(p) == {ids["Lead"]}
    api.track_update("Grp", solo=False)
    api.track_update("Verb", solo=True)
    assert engine.soloed_sources(api.load()) == {ids["Lead"], ids["Pad"]}
    api.track_update("Verb", solo=False)
    assert engine.soloed_sources(api.load()) == {ids["Lead"], ids["Pad"], ids["Drums"]}


# ---- 9. pattern tokens -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("tok,semis", [("b7", 10), ("#4", 6), ("b3", 3), ("b9", 13), ("7b", 10), ("1", 0)])
def test_documented_accidental_tokens(tok, semis):
    prog = [(theory.parse_chord("C7" if tok == "7b" else "C"), 1.0)]
    if tok == "7b":
        semis = 9  # the chord's own (flat) 7th, lowered again
    notes = generators.pattern_notes(prog, tok, rate=1.0, octave=4)
    assert [n.pitch for n in notes] == [60 + semis]


def test_double_accidental_token_is_rejected():
    with pytest.raises(ValueError):
        generators.pattern_notes([(theory.parse_chord("C"), 1.0)], "b7b", rate=1.0)
