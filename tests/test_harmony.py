"""The chord track, generators that follow it, the score view and the harmony check."""
from __future__ import annotations

import asyncio
import json

import pytest

from tunetrack import api, cli, harmony, theory


def _notes(track):
    return sorted((n.pitch, round(c.start + n.start, 4), round(n.dur, 4)) for c in api.load().track(track).clips
                  for n in c.notes)


def _chords(entries):
    return [(e["at"], e["chord"]) for e in entries]


@pytest.fixture()
def song(tmp_path):
    """8 bars of D minor 4/4 with markers A (1:1) and B (5:1)."""
    api.project_new("Harmony Song", folder=str(tmp_path / "song"), tempo=100, key="D minor")
    api.markers(add=[{"at": "1:1", "name": "A"}, {"at": "5:1", "name": "B"}])
    api.project_set(loop="1:1-9:1")
    api.track_add("Pad", "synth:pad", role="pad")
    api.track_add("Bass", "synth:analog_bass", role="bass")
    api.track_add("Lead", "synth:lead", role="lead")
    return str(tmp_path / "song")


# ---- chord track storage -------------------------------------------------------------------------------------

def test_chord_track_set_by_section_resolves_romans_and_persists(song):
    res = api.harmony("i VI", section="A")  # 2 chords repeat to fill the 4-bar section
    assert res["set"] == "1:1 - 5:1" and res["repeated"].startswith("2x")
    assert _chords(res["chord_track"]) == [("1:1", "i"), ("2:1", "VI"), ("3:1", "i"), ("4:1", "VI")]
    assert res["chord_track"][1]["is"] == "Bb"
    api.harmony("Am7 F/A", section="B")  # symbols are stored as written
    saved = json.loads((api._path(song) / "project.json").read_text(encoding="utf-8"))["chord_track"]
    assert [(e["at"], e["len"], e["chord"]) for e in saved][-2:] == [(24.0, 4.0, "Am7"), (28.0, 4.0, "F/A")]
    read = api.harmony(at="5:1")["chord_track"]
    assert _chords(read) == [("5:1", "Am7"), ("6:1", "F/A"), ("7:1", "Am7"), ("8:1", "F/A")]
    assert api.project_info()["chord_track"] == "bars 1-8, 8 chords"


def test_roman_entries_follow_a_key_change_and_their_own_key(song):
    api.harmony("i iv:2 V7:2", at="1:1")
    api.harmony("I V", at="3:1", key="F major")  # a modulation keeps its own key
    api.project_set(key="E minor")
    got = {e["at"]: e.get("is", e["chord"]) for e in api.harmony()["chord_track"]}
    assert got == {"1:1": "Em", "2:1": "Am", "2:3": "B7", "3:1": "F", "4:1": "C"}


def test_replace_a_range_clear_and_gaps(song):
    api.harmony("i VI III VII", at="1:1", end="9:1")
    res = api.harmony("iv:2 V:2", at="2:3", end="3:3")  # replaces half of bar 2 and half of bar 3
    assert _chords(res["chord_track"])[:5] == [("1:1", "i"), ("2:1", "VI"), ("2:3", "iv"), ("3:1", "V"), ("3:3", "III")]
    res = api.harmony("i % N.C. V", at="5:1")  # % repeats, N.C. leaves a gap
    assert _chords(api.harmony(at="5:1", end="9:1")["chord_track"]) == [("5:1", "i"), ("6:1", "i"), ("8:1", "V")]
    api.harmony(clear=True, at="5:1", end="7:1")
    assert [e["at"] for e in api.harmony()["chord_track"]][-2:] == ["4:1", "8:1"]
    api.harmony(clear=True)
    assert api.harmony()["chord_track"] == [] and "hint" in api.harmony()


def test_a_progression_longer_than_the_range_is_cut_with_a_warning(song):
    res = api.harmony("i VI III VII i VI", section="A")
    assert "cut" in res["warnings"][0] and len(res["chord_track"]) == 4


def test_older_projects_without_a_chord_track_load_empty(song):
    f = api._path(song) / "project.json"
    d = json.loads(f.read_text(encoding="utf-8"))
    d.pop("chord_track", None)
    f.write_text(json.dumps(d), encoding="utf-8")
    p = api.load()
    assert p.chord_track == []
    api.harmony("i", at="1:1")  # and saving writes the new field
    assert json.loads(f.read_text(encoding="utf-8"))["chord_track"][0]["chord"] == "i"


def test_chord_track_undo_redo(song):
    api.harmony("i VI", at="1:1")
    api.harmony("iv V", at="1:1")
    assert _chords(api.harmony()["chord_track"]) == [("1:1", "iv"), ("2:1", "V")]
    api.undo()
    assert _chords(api.harmony()["chord_track"]) == [("1:1", "i"), ("2:1", "VI")]
    api.redo()
    assert _chords(api.harmony()["chord_track"]) == [("1:1", "iv"), ("2:1", "V")]


def test_arrange_moves_the_chord_track_with_the_song(song):
    api.harmony("i VI III VII", at="1:1")
    api.arrange("insert", "2:1", "3:1")
    assert _chords(api.harmony()["chord_track"]) == [("1:1", "i"), ("3:1", "VI"), ("4:1", "III"), ("5:1", "VII")]
    api.arrange("delete", "2:1", "3:1")
    assert _chords(api.harmony()["chord_track"]) == [("1:1", "i"), ("2:1", "VI"), ("3:1", "III"), ("4:1", "VII")]
    api.arrange("copy", "1:1", "3:1", to="5:1")
    assert _chords(api.harmony(at="5:1")["chord_track"]) == [("5:1", "i"), ("6:1", "VI")]
    api.arrange("copy", "3:1", "5:1", to="5:1", tracks=["Pad"])  # a track-limited copy leaves it alone
    assert _chords(api.harmony(at="5:1")["chord_track"]) == [("5:1", "i"), ("6:1", "VI")]


# ---- generators follow the chord track ---------------------------------------------------------------------------

def test_chords_and_pattern_follow_the_chord_track(song, tmp_path):
    api.harmony("i VI III VII", section="A")
    api.harmony("iv:2 V:2 i", section="B")
    res = api.chords("Pad", section="A", voices=3)
    assert res["followed"] == "chord track" and res["chords"] == ["i", "VI", "III", "VII"]
    api.pattern("Bass", pattern="1 . 5- .", section="B", octave=2, rate=1)
    via_track = _notes("Pad"), _notes("Bass")
    # the same parts written with explicit progressions in a second project
    api.project_new("Explicit", folder=str(tmp_path / "explicit"), tempo=100, key="D minor")
    api.track_add("Pad", "synth:pad")
    api.track_add("Bass", "synth:analog_bass")
    api.chords("Pad", "i VI III VII", at="1:1", voices=3)
    api.pattern("Bass", "iv:2 V:2 i iv:2 V:2 i", "1 . 5- .", at="5:1", octave=2, rate=1)
    assert (_notes("Pad"), _notes("Bass")) == via_track


def test_generators_follow_at_end_and_leave_gaps_silent(song):
    api.harmony("i N.C. VI", at="1:1")
    api.chords("Pad", at="1:1", voices=3)  # end defaults to the end of the chord track
    starts = sorted({s for _, s, _ in _notes("Pad")})
    assert starts == [0.0, 8.0]
    api.pattern("Bass", pattern="1 1 1 1", at="3:1", end="3:3", octave=2, rate=1)
    assert [(p, s) for p, s, _ in _notes("Bass")] == [(46, 8.0), (46, 9.0)]  # Bb2 twice, only half a bar


def test_an_explicit_progression_overrides_the_chord_track(song):
    api.harmony("i VI III VII", section="A")
    res = api.chords("Pad", "iv V", at="1:1", voices=3)
    assert "followed" not in res and res["chords"] == ["iv", "V"]
    roots = {s: p % 12 for p, s, _ in _notes("Pad")}
    bass_pcs = {s: min(p for p, s2, _ in _notes("Pad") if s2 == s) % 12 for s in roots}
    assert len({s for _, s, _ in _notes("Pad")}) == 2  # two chords, not the four on the chord track
    assert theory.pitch_class("G") in {p % 12 for p, s, _ in _notes("Pad") if s == 0.0}
    assert bass_pcs  # voicings were written
    api.pattern("Bass", "i", "1", at="1:1", octave=2, rate=4)  # positional (track, progression, pattern) still works
    assert _notes("Bass") == [(38, 0.0, 3.6)]


def test_reharmonising_a_humanized_part_leaves_no_stray_notes(song):
    api.harmony("i VI III VII", section="A")
    api.pattern("Pad", pattern="1 5 8 5", section="A", octave=3, rate=1)
    api.notes_edit("Pad", "humanize", timing_ms=20, velocity=0, seed=2)  # nudges notes up to ~0.03 beat
    api.harmony("iv", at="2:1")
    api.pattern("Pad", pattern="1 5 8 5", at="2:1", end="3:1", octave=3, rate=1)
    near = lambda a, b: [p for p, s, _ in _notes("Pad") if a - 0.1 < s < b - 0.1]  # noqa: E731
    assert near(4, 8) == [55, 62, 62, 67]  # G D G D (sorted): only G minor, the old Bb downbeat is gone
    assert len(near(8, 9)) == 1  # bar 3's downbeat survived


def test_following_an_empty_chord_track_explains_what_to_do(song):
    with pytest.raises(ValueError, match="chord track is empty"):
        api.chords("Pad", section="A")
    api.harmony("i", at="1:1")
    with pytest.raises(ValueError, match="no chords in"):
        api.pattern("Bass", pattern="1", at="5:1", end="6:1")


# ---- score view -------------------------------------------------------------------------------------------------

def test_score_text_for_a_small_project(song):
    api.harmony("i VI", at="1:1")
    api.chords("Pad", at="1:1", voices=3, low="F3", high="D4")
    api.write("Lead", "D5:q E5:e F5 A5:h | Bb5:h~ Bb5:q A5:q", at="1:1")
    api.write("Bass", "D2:w | Bb1:w", at="1:1")
    api.write("Pad", "(F3 A3 D4):w | (F3 A3 D4):w", at="3:1")  # no chord-track entry here: inferred
    res = api.score(end="5:1")
    lines = res["score"].splitlines()
    assert res["bars"] == "1-4" and lines[0].startswith("Harmony Song: D minor, 4/4, 100 bpm, bars 1-4")
    bar1 = lines[lines.index("bar 1 [A] loop start") + 1:lines.index("bar 2")]
    assert bar1[0].split() == ["chord", "Dm(i)", "-", "-", "-"]  # parts run high to low, like a score
    assert bar1[1].split() == ["Lead", "D5:q", "E5:e", "F5", "A5:h"]
    assert bar1[2].split() == ["Pad", "(F3", "A3", "D4):w"]
    assert bar1[3].split() == ["Bass", "D2:w"]
    bar2 = lines[lines.index("bar 2") + 1:lines.index("bar 3")]
    assert bar2[0].split()[:2] == ["chord", "Bb(VI)"] and bar2[1].split() == ["Lead", "Bb5:h.", "A5:q"]
    bar3 = lines[lines.index("bar 3") + 1:lines.index("bar 4")]
    assert bar3[0].split()[:2] == ["chord", "Dm/F*"] and len(bar3) == 2  # inferred; no bass there, so Dm/F
    assert lines[lines.index("bar 4") + 2].split() == ["Pad", "%"]
    # columns: the token that starts on beat 3 lines up with the chord row's beat-3 cell
    col = bar1[0].index("-", bar1[0].index("-") + 1)
    assert bar1[1].index("A5") == col
    assert res["warnings"] == []


def test_score_ties_track_subset_and_leaves_drums_out(song):
    api.track_add("Drums", "synth:drums", role="drums")
    api.drums("Drums", {"kick": "x...x...x...x..."}, at="1:1", bars=2)
    api.write("Lead", "D5:h. F5:h A5:q", at="1:1")  # the F5 crosses the bar line
    txt = api.score()["score"]
    assert "not shown: Drums" in txt.splitlines()[0] and "\n  Drums" not in txt
    assert "F5:q~" in txt and "\n  Lead   F5:q" in txt.replace("Lead    ", "Lead   ")
    only = api.score(tracks=["Drums"])["score"]
    assert "\n  Drums" in only and "Lead" not in only.split("\n", 3)[3]


def test_score_shows_the_chord_track_before_any_notes(song):
    api.harmony("i VI III VII", section="A")
    res = api.score()
    assert res["bars"] == "1-8"  # the loop region sets the song length
    assert "Bb(VI)" in res["score"] and "bar 5-8: no notes" in res["score"]


# ---- harmony check ------------------------------------------------------------------------------------------------

def _warnings(**kw):
    return api.score(**kw)["warnings"]


def test_clean_voice_leading_stays_quiet(tmp_path):
    api.project_new("Clean", folder=str(tmp_path / "clean"), tempo=100, key="C major")
    api.track_add("Pad", "synth:pad", role="pad")
    api.track_add("Bass", "synth:analog_bass", role="bass")
    api.track_add("Melody", "synth:lead", role="lead")
    api.harmony("I vi IV V", at="1:1")
    api.chords("Pad", voices=3, low="C3", high="G4")
    api.pattern("Bass", pattern="1 - - - 5- - 1 -", octave=2, rate=0.5)
    api.write("Melody", "E5:h D5:q C5 | C5:h B4:q A4 | A4:h C5:h | B4:h D5:h", at="1:1")
    assert _warnings() == []
    assert "harmony_warnings" not in api.analyze(images=False)


def test_a_strong_beat_clash_is_flagged_but_an_appoggiatura_is_not(song):
    api.harmony("i iv", at="1:1")
    api.chords("Pad", voices=3, low="F3", high="D4")
    api.write("Bass", "D2:w | G2:w", at="1:1")
    api.write("Lead", "Eb5:h D5:h | Bb4:w", at="1:1")  # Eb over Dm resolves down by step: fine
    assert _warnings() == []
    api.write("Lead", "Eb5:h A5:h | Bb4:w", at="1:1")  # Eb leaps away: a clash
    w = _warnings()
    assert len(w) == 1 and w[0].startswith("1:1 Lead Eb5 clashes with Dm (i, chord track)")
    assert "a semitone above D" in w[0] and "use D5 or F5" in w[0]
    assert any("Lead Eb5 clashes" in x for x in api.analyze(images=False)["harmony_warnings"])


def test_inferred_chords_catch_a_wrong_third(song):
    api.write("Pad", "(D4 F4 A4):w | (D4 F4 A4):w", at="1:1")
    api.write("Bass", "D2:w | D2:w", at="1:1")
    api.write("Lead", "A5:w | F#5:h E5:h", at="1:1")  # F# on beat 1 against the pad's F, moving on by step...
    assert _warnings() == []
    api.write("Lead", "A5:w | F#5:h A5:h", at="1:1")  # ...and leaping away
    w = _warnings()
    assert len(w) == 1 and "2:1 Lead Gb5 clashes with Dm (implied by the parts)" in w[0]  # names use the key's flats


def test_bass_that_disagrees_with_the_chord_track(tmp_path):
    api.project_new("Bass Check", folder=str(tmp_path / "b"), tempo=100, key="C major")
    api.track_add("Pad", "synth:pad")
    api.track_add("Bass", "synth:analog_bass", role="bass")
    api.harmony("vi IV", at="1:1")
    api.chords("Pad", voices=3, low="C4", high="G4")
    api.write("Bass", "F2:w | F2:w", at="1:1")
    w = _warnings()
    assert len(w) == 1
    assert w[0].startswith("1:1 Bass plays F2 under Am (vi, chord track): play A2 (root) or C/E")
    assert "change the chord track there" in w[0]


def test_a_pedal_point_is_not_flagged(song):
    api.harmony("i VI III VII", at="1:1")
    api.chords("Pad", voices=3, low="F3", high="D5")
    api.write("Bass", "D2:w | D2:w | D2:w | D2:w", at="1:1")
    assert not [w for w in _warnings() if "Bass" in w]


def test_parallel_fifths_between_the_outer_voices(tmp_path):
    api.project_new("Fifths", folder=str(tmp_path / "f"), tempo=100, key="C major")
    api.track_add("Pad", "synth:pad")
    api.track_add("Bass", "synth:analog_bass", role="bass")
    api.track_add("Lead", "synth:lead")
    api.harmony("I ii", at="1:1")
    api.write("Pad", "(E3 G3 C4):w | (F3 A3 D4):w", at="1:1")
    api.write("Bass", "C2:w | D2:w", at="1:1")
    api.write("Lead", "G4:w | A4:w", at="1:1")
    w = _warnings()
    assert w == [("2:1 parallel fifths between Bass and Lead (D2/A4): e.g. Bass F2 there (a 3rd under A4), or "
                  "contrary motion")]
    api.write("Lead", "G4:w | F4:w", at="1:1")  # contrary motion
    assert _warnings() == []


def test_a_long_octave_doubling_is_deliberate(tmp_path):
    api.project_new("Unison", folder=str(tmp_path / "u"), tempo=100, key="C major")
    api.track_add("Bass", "synth:analog_bass", role="bass")
    api.track_add("Lead", "synth:lead")
    api.write("Bass", "C2:q D2 E2 F2 | G2:q F2 E2 D2 | C2:w", at="1:1")
    api.write("Lead", "C5:q D5 E5 F5 | G5:q F5 E5 D5 | C5:w", at="1:1")
    assert not [w for w in _warnings() if "parallel" in w]


def test_voice_crossings_by_role(song):
    api.harmony("i", at="1:1", end="9:1")
    api.write("Pad", " | ".join(["(D4 F4 A4):w"] * 8), at="1:1")
    api.write("Bass", " | ".join(["D3:q D3 A2 D3"] * 4 + ["F4:q F4 F4 F4"] + ["D3:q D3 A2 D3"] * 3), at="1:1")
    api.write("Lead", " | ".join(["D5:h F5:h"] * 6 + ["E4:w", "D5:w"]), at="1:1")
    w = _warnings()
    assert any(x.startswith("5:1-5:4 Bass F4 crosses above Pad (D4): drop Bass an octave there (F3)") for x in w)
    assert any(x.startswith("7:1 Lead E4 dips under Pad's top note A4") for x in w)


def test_a_part_that_does_not_follow_the_chord_track(song):
    api.harmony("i VI III VII", at="1:1")
    api.chords("Pad", voices=3, low="F3", high="D5")  # follows the chord track
    api.write("Bass", "D2:w | Bb1:w | F2:w | C2:w", at="1:1")
    assert _warnings() == []
    api.harmony("iv", at="2:1")  # Bb -> Gm shares two notes: the pad sounds like Gm7, the bass is its 3rd. Fine.
    assert _warnings() == []
    api.harmony("V", at="2:1")  # reharmonised bar 2 to A major, but the pad and bass weren't re-run
    w = _warnings()
    assert any(x.startswith("2:1-3:1 Pad doesn't follow the chord track (plays Bb/F where it says A(V))") for x in w)
    assert any(x.startswith("2:1 Bass plays Bb1 under A (V, chord track): play A1 (root)") for x in w)
    api.chords("Pad", at="2:1", end="3:1", voices=3, low="F3", high="D5")
    api.pattern("Bass", pattern="1", at="2:1", end="3:1", octave=1, rate=4, gate=1)
    assert _warnings() == []


def test_drum_tracks_are_left_out_of_the_check(song):
    api.track_add("Kit", "synth:drums")
    api.harmony("i", at="1:1")
    api.chords("Pad", voices=3, low="F3", high="D4")
    api.drums("Kit", {"kick": "x...", "snare": "..x.", "hat": "xxxx"}, at="1:1", bars=1, steps_per_beat=1)
    assert _warnings() == []


# ---- inference unit checks ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("notes,want", [
    ([(0, 4, 50), (0, 4, 53), (0, 4, 57)], "Dm"),
    ([(0, 4, 53), (0, 4, 57), (0, 4, 62)], "Dm/F"),  # pad voiced from F with no bass: an inversion, not F6
    ([(0, 4, 43), (0, 4, 59), (0, 4, 62), (0, 4, 65)], "G7"),
    ([(0, 4, 36), (0, 4, 55), (0, 4, 64), (0, 4, 69)], "C6"),
    ([(0, 4, 45), (0, 4, 55), (0, 4, 64), (0, 4, 72)], "Am7"),
    ([(0, 4, 38), (0, 4, 45)], "D5"),
])
def test_infer_names_the_chord(notes, want):
    (_, _, res), = harmony._infer([(a, b, p) for a, b, p in notes], 0, 4, 1.0, theory.Key("C major").pcs)
    assert harmony._chord(*res, flats=False).symbol == want


def test_infer_splits_a_bar_with_two_chords():
    notes = [(0, 2, 38), (0, 2, 62), (0, 2, 65), (0, 2, 69), (2, 4, 41), (2, 4, 65), (2, 4, 69), (2, 4, 72)]
    segs = harmony._infer(notes, 0, 4, 1.0, theory.Key("D minor").pcs)
    assert [(a, b, harmony._chord(*r, flats=True).symbol) for a, b, r in segs] == [(0, 2.0, "Dm"), (2.0, 4, "F")]


# ---- MCP and CLI ----------------------------------------------------------------------------------------------------

def test_mcp_tools_end_to_end(song):
    from tunetrack import mcp_server
    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"harmony", "score"} <= names and len(names) == 70

    def call(name, args):
        r = asyncio.run(mcp_server.mcp.call_tool(name, args))
        assert not r.is_error
        return json.loads(r.content[0].text)

    assert call("harmony", {"progression": "i VI", "section": "A"})["set"] == "1:1 - 5:1"
    assert call("chords", {"track": "Pad", "section": "A", "voices": 3})["followed"] == "chord track"
    assert call("pattern", {"track": "Bass", "pattern": "1 5-", "section": "A", "octave": 2, "rate": 2})["added"] == 8
    sc = call("score", {"start": "1:1", "end": "3:1"})
    assert sc["bars"] == "1-2" and "Bb(VI)" in sc["score"] and "warnings" in sc


def test_cli_call_score(song, capsys):
    api.harmony("i VI", at="1:1")
    cli.main(["call", "score", json.dumps({"start": "1:1", "end": "3:1"})])
    out = json.loads(capsys.readouterr().out)
    assert out["bars"] == "1-2" and "Dm(i)" in out["score"]
