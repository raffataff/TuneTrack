import json
import struct
from pathlib import Path

import numpy as np
import pytest

from tunetrack import api, engine, generators, notation, plugins, theory


# ---- notation ---------------------------------------------------------------------------------------------

def test_parse_basic_durations_and_sticky_values():
    notes, length, warnings = notation.parse("C4:q!mf D4 E4:h | (C4 E4 G4):w")
    assert [(n.pitch, n.start, n.dur, n.vel) for n in notes[:3]] == [(60, 0, 1, 80), (62, 1, 1, 80), (64, 2, 2, 80)]
    assert [n.pitch for n in notes[3:]] == [60, 64, 67] and all(n.start == 4 and n.dur == 4 for n in notes[3:])
    assert length == 8 and warnings == []


def test_parse_dotted_triplets_ties_rests_staccato_accent():
    notes, _length, _ = notation.parse("C4:q. D4:e r:h | E4:e3 F4 G4 A4:h~ A4:q B4:q'>")
    assert notes[0].dur == 1.5 and notes[1].start == 1.5
    assert notes[2].start == 4 and abs(notes[2].dur - 1 / 3) < 1e-9
    tied = [n for n in notes if n.pitch == 69]
    assert len(tied) == 1 and tied[0].dur == 3
    b = notes[-1]
    assert b.pitch == 71 and b.dur == 0.5 and b.vel == 90 + 16


def test_bar_check_warns():
    _, _, warnings = notation.parse("C4:q D4 E4 | F4:w |", 4)
    assert warnings and "bar 1 has 3 beats" in warnings[0]


def test_reposition_cursor_for_voices():
    notes, _, _ = notation.parse("C5:w @0 E4:h G4:h")
    assert [(n.pitch, n.start) for n in notes] == [(72, 0), (64, 0), (67, 2)]


@pytest.mark.parametrize("text", [
    "D4:q!80 F4 A4:h | (D3 A3 F4):w",
    "C4:e3 D4 E4 F4:q. G4:e | r:h A4:h",
    "C5:w @0 E4:h G4:h | B3:q r:q D4:h",
    "(C3 G3):w @0 E5:q D5 C5:h",
])
def test_format_round_trip(text):
    notes, _, _ = notation.parse(text)
    again, _, _ = notation.parse(notation.format_notes(notes))

    def key(ns):
        return sorted((n.pitch, round(n.start, 6), round(n.dur, 6), n.vel) for n in ns)
    assert key(notes) == key(again)


# ---- theory ----------------------------------------------------------------------------------------------

def test_note_numbers():
    assert theory.note_number("C4") == 60 and theory.note_number("A4") == 69 and theory.note_number("Bb3") == 58
    assert theory.note_name(61) == "C#4" and theory.note_name(61, flats=True) == "Db4"


def test_roman_numerals_follow_the_key():
    k = theory.Key("D minor")
    assert theory.parse_roman("VI", k).root == theory.pitch_class("Bb")
    assert theory.parse_roman("i", k).intervals == [0, 3, 7]
    v7 = theory.parse_roman("V7", k)
    assert v7.root == theory.pitch_class("A") and v7.intervals == [0, 4, 7, 10]
    vv = theory.parse_roman("V/V", theory.Key("C major"))
    assert vv.root == theory.pitch_class("D") and vv.intervals == [0, 4, 7]
    assert theory.parse_roman("bVII", theory.Key("C major")).root == theory.pitch_class("Bb")


def test_chord_symbols_and_tones():
    c = theory.parse_chord("F#m7b5/C")
    assert c.root == 6 and c.bass == 0 and c.intervals == [0, 3, 6, 10]
    sus = theory.parse_chord("Dsus4")
    assert sus.tone(3) == 5 and sus.tone(8) == 12 and theory.parse_chord("C").tone(10) == 16


def test_voicing_in_range_with_required_tones():
    k = theory.Key("C major")
    prev = None
    for chord, _ in theory.parse_progression("I vi IV V7", k):
        v = theory.voice_chord(chord, 4, 52, 76, prev)
        assert all(52 <= p <= 76 for p in v)
        pcs = {p % 12 for p in v}
        assert chord.root in pcs
        assert any((p - chord.root) % 12 in (3, 4) for p in pcs)
        if prev:
            assert sum(abs(a - b) for a, b in zip(sorted(prev), v)) <= 12  # smooth voice leading
        prev = v


def test_progression_durations():
    prog = theory.parse_progression("i:2 iv:2 V:4", theory.Key("A minor"))
    assert [length for _, length in prog] == [2, 2, 4]


# ---- generators ------------------------------------------------------------------------------------------

def test_pattern_follows_chords():
    prog = theory.parse_progression("C G", None, 4)
    notes = generators.pattern_notes(prog, "1 3 5 8", rate=1, octave=4)
    assert [n.pitch for n in notes] == [60, 64, 67, 72, 67, 71, 74, 79]


def test_pattern_holds_and_rests():
    prog = theory.parse_progression("C", None, 4)
    notes = generators.pattern_notes(prog, "1 - . 5-", rate=1, octave=3)
    assert [(n.pitch, n.start, n.dur) for n in notes] == [(48, 0, 2), (43, 3, 0.9)]


def test_drum_grid_bars_cycle():
    notes = generators.drum_grid({"kick": "x...|x.x."}, steps_per_beat=1, bars=2)
    assert [n.start for n in notes] == [0, 4, 6]


# ---- api (projects in a temp library) ----------------------------------------------------------------------

@pytest.fixture()
def proj(tmp_path):
    api.project_new("Test Song", folder=str(tmp_path / "song"), tempo=100, key="D minor")
    api.track_add("Lead", "synth:lead", role="violin")
    api.track_add("Pad", "synth:pad")
    return str(tmp_path / "song")


def _starts(track):
    return sorted(c.start + n.start for c in api.load().track(track).clips for n in c.notes)


def test_write_and_read_back(proj):
    res = api.write("Lead", "D5:q!mf F5 A5:h | G5:w", at="2:1")
    assert res["added"] == 4 and "warnings" not in res
    clips = api.notes_read("Lead")["clips"]
    assert clips[0]["start"] == "2:1"
    assert clips[0]["ttn"].startswith("D5:q!80 F5 A5:h | G5:w")


def test_chords_and_mode_replace(proj):
    api.chords("Pad", "i VI III VII", voices=4)
    assert sum(len(c.notes) for c in api.load().track("Pad").clips) == 16
    api.chords("Pad", "i VI III VII", voices=3)  # replace mode overwrites the span
    assert sum(len(c.notes) for c in api.load().track("Pad").clips) == 12


def test_arrange_copy_insert_delete(proj):
    api.write("Lead", "D5:w | F5:w", at=0)
    api.arrange("copy", "1:1", "3:1", to="5:1")
    assert _starts("Lead") == [0, 4, 16, 20]
    api.arrange("insert", "3:1", "5:1")
    assert _starts("Lead") == [0, 4, 24, 28]
    api.arrange("delete", "3:1", "5:1")
    assert _starts("Lead") == [0, 4, 16, 20]


def test_notes_edit_and_undo_redo(proj):
    api.write("Lead", "D5:q E5 F5 G5")
    api.notes_edit("Lead", "transpose", steps=1)
    pitches = lambda: [n.pitch for c in api.load().track("Lead").clips for n in c.notes]  # noqa: E731
    assert pitches() == [76, 77, 79, 81]
    api.undo()
    assert pitches() == [74, 76, 77, 79]
    api.redo()
    assert pitches() == [76, 77, 79, 81]


def test_clip_split(proj):
    api.write("Lead", "D5:h E5:h | F5:w")
    clip = api.load().track("Lead").clips[0].id
    api.clip_edit(clip, "split", at="2:1")
    clips = sorted(api.load().track("Lead").clips, key=lambda c: c.start)
    assert len(clips) == 2 and [n.pitch for n in clips[1].notes] == [77] and clips[1].notes[0].start == 0


def test_notes_report_flags_range(proj):
    api.write("Lead", "C2:w")  # far below a violin
    rep = api.analyze(images=False)
    assert any("outside the playable violin range" in i for i in rep["tracks"]["Lead"]["issues"])


def test_cc_ramp(proj):
    api.write("Pad", "(D4 F4 A4):w")
    res = api.cc("Pad", 1, "1:1", "2:1", 20, 100, shape="linear", resolution=1)
    assert res["points"] == 5


# ---- engine helpers ------------------------------------------------------------------------------------------

def test_loop_wrap_folds_tail():
    audio = np.zeros((2, 40), dtype=np.float32)
    audio[:, 30:33] = 1.0  # tail after the loop end at 30
    out = engine.loop_wrap(audio, 10, 30, keep_intro=False)
    assert out.shape[1] == 20 and out[0, :3].tolist() == [1, 1, 1]


def test_limiter_has_no_overs():
    rng = np.random.default_rng(0)
    audio = (rng.standard_normal((2, 48000)) * 0.5).astype(np.float32)
    out, gr = engine.limiter(audio, 48000, -1.0)
    assert np.abs(out).max() <= 10 ** (-1 / 20) + 1e-6 and gr > 0


def test_wav_loop_chunk(tmp_path):
    import soundfile as sf
    f = tmp_path / "x.wav"
    engine.write_audio(f, np.zeros((2, 4800), dtype=np.float32), 48000, "wav", 16, loop=(100, 4800))
    data, sr = sf.read(str(f))
    assert sr == 48000 and len(data) == 4800
    raw = f.read_bytes()
    i = raw.index(b"smpl")
    start, end = struct.unpack("<II", raw[i + 8 + 36 + 8: i + 8 + 36 + 16])
    assert (start, end) == (100, 4799)


# ---- patches (worker stubbed) -------------------------------------------------------------------------------

def test_capture_and_import_do_not_send_registry_param_count(monkeypatch):
    entry = {"name": "Fake Synth", "path": "C:/fake.vst3", "kind": "instrument", "host": "dawdreamer", "params": 42}
    monkeypatch.setattr(plugins, "load_registry", lambda: {"plugins": [entry]})
    specs = []

    def fake_worker(cmd, arg, timeout=300):
        specs.append(json.loads(Path(arg).read_text(encoding="utf-8")))
        return {"ok": True, "params": 42}

    monkeypatch.setattr(plugins, "run_worker", fake_worker)
    meta = plugins.capture("Fake Synth", "Test Sound")
    plugins.import_preset("Fake Synth", "C:/fake.vstpreset", "Test Preset")
    assert [("params" in s["plugin"]) for s in specs] == [False, False]
    assert meta["param_count"] == 42


def test_patch_channel_reaches_the_rendered_midi(monkeypatch, proj, tmp_path):
    import mido
    entry = {"name": "Fake Multi", "path": "C:/fake.vst3", "kind": "instrument", "host": "dawdreamer", "params": 9}
    monkeypatch.setattr(plugins, "load_registry", lambda: {"plugins": [entry]})
    plugins.save_patch({"id": "fake-multi-cello", "name": "Cello", "plugin": "Fake Multi", "kind": "instrument",
                        "tags": [], "state": "fake-multi.state", "channel": 6})
    with pytest.raises(ValueError):
        api.patch_update("fake-multi-cello", channel=17)
    dev = api.parse_device("patch:fake-multi-cello")
    spec = engine.resolve_device(dev, api.load(), True)
    assert spec["channel"] == 6
    api.write("Lead", "C4:q D4 E4 F4")
    engine.write_midi(api.load(), api.load().track("Lead"), tmp_path / "t.mid", spec["channel"] - 1)
    chans = {m.channel for m in mido.MidiFile(str(tmp_path / "t.mid")) if m.type == "note_on"}
    assert chans == {5}


# ---- import / export / render (spawns worker processes) ---------------------------------------------------

def test_midi_export_import_round_trip(proj, tmp_path):
    api.write("Lead", "D5:q E5 F5 G5 | A5:w")
    api.chords("Pad", "i VI", at="1:1")
    mid = api.midi_export(str(tmp_path / "out.mid"))["file"]
    made = api.midi_import(mid, at="5:1")["tracks"]
    assert {m["notes"] for m in made} == {5, 8}
    lead2 = next(m for m in made if m["notes"] == 5)
    assert _starts(lead2["track"]) == [16, 17, 18, 19, 20]


def test_render_audio_and_synth_tracks(proj, tmp_path):
    import soundfile as sf
    sr = 48000
    t = np.arange(sr) / sr
    sf.write(str(tmp_path / "tone.wav"), (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32), sr)
    api.track_add("Audio", kind="audio")
    api.audio_import("Audio", str(tmp_path / "tone.wav"), at="1:1", fade_out=0.1)
    api.write("Lead", "A4:h C5:h | E5:w")
    api.track_add("Verb", kind="bus", fx=["builtin:reverb"])
    api.track_update("Lead", sends=[{"to": "Verb", "db": -6}])
    api.project_set(loop="1:1-3:1")
    rep = api.render(out=str(tmp_path / "mix.ogg"), stems=True)
    assert rep["loop_mode"] == "loop" and not [w for w in rep["warnings"] if "SILENT" in w or "failed" in w]
    assert {"Lead", "Audio"} <= set(rep["tracks"])
    info = sf.info(rep["files"][0])
    assert abs(info.duration - 2 * 4 * 60 / 100) < 0.05  # exactly two bars at 100 bpm
    assert any(f.endswith("_stem_Audio.ogg") for f in rep["files"])
