"""A seat's bench: the read-only snapshot of the work in front of a seat."""
import json
import os
import time
from pathlib import Path

import pytest

from tunetrack import api, bench, studio, takes
from tunetrack.bench import seat_bench


@pytest.fixture(autouse=True)
def _as_user():
    studio.seat_join(None)
    api.set_default_project(None)
    yield
    studio.seat_join(None)
    api.set_default_project(None)


@pytest.fixture
def song(tmp_path):
    api.project_new("Night Drive", folder=str(tmp_path / "song"), tempo=96, key="D minor")
    api.track_add("Piano", "synth:pad")
    api.write("Piano", "D4:q F4 A4 D5 | C4:q E4 G4 C5")
    api.track_add("Bass", "synth:pad")
    api.write("Bass", "D2:w | C2:w")
    return str(tmp_path / "song")


def _cut(seat, fn, submit=True):
    studio.seat_join(seat)
    t = takes.take_new()
    fn()
    if submit:
        takes.take_submit("done")
    studio.seat_join(None)
    api.set_default_project(None)
    return t["take"]


def _track(b, name):
    return next(t for t in b["tracks"] if t["name"] == name)


def _compose():
    api.write("Piano", "E4:q", at="1:1")
    api.track_add("Lead", "synth:lead")
    api.write("Lead", "A4:h D5")


def _bump(path, ns=10_000_000_000):
    f = Path(path) / "project.json"
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + ns))


def test_without_a_take_the_bench_shows_the_session(song):
    b = seat_bench("composer")
    assert b["source"]["take"] is None and b["source"]["state"] is None and b["takes"] == []
    assert "changes" not in b and "removed" not in b
    assert all("was" not in t and "new" not in t for t in b["tracks"])
    notes = [n for t in b["tracks"] for n in t["notes"]]
    assert len(notes) == 10 and {n[4] for n in notes} == {0}


def test_a_composer_take_flags_added_notes_and_new_tracks(song):
    tid = _cut("composer", _compose, submit=False)
    b = seat_bench("composer")
    assert b["source"]["take"] == tid and b["source"]["state"] == "open" and b["takes"] == [tid]
    piano = _track(b, "Piano")
    flagged = {(n[0], n[1]): n[4] for n in piano["notes"]}
    assert flagged[(64, 0.0)] == 1 and flagged[(65, 1.0)] == 0 and flagged[(60, 4.0)] == 0
    assert sum(n[4] for n in piano["notes"]) == 1
    assert piano["gone"] == [[62, 0.0, 1.0]]
    lead = _track(b, "Lead")
    assert lead["new"] is True and [n[4] for n in lead["notes"]] == [1, 1]
    assert "new" not in _track(b, "Bass") and all(n[4] == 0 for n in _track(b, "Bass")["notes"])
    assert b["changes"] > 0 and b["removed"] == []


def test_a_submitted_take_shows_until_an_open_one_wins(song):
    first = _cut("composer", _compose)
    b = seat_bench("composer")
    assert b["source"]["take"] == first and b["source"]["state"] == "submitted"
    second = _cut("composer", lambda: api.write("Bass", "F2:w"), submit=False)
    b = seat_bench("composer")
    assert b["source"]["take"] == second and b["source"]["state"] == "open"
    assert b["takes"] == [first, second]


def test_a_mix_take_shows_where_each_moved_thing_was(song):
    def mix():
        api.track_update("Piano", volume_db=-6, pan=-0.3, mute=True)
        api.fx_add("Bass", "builtin:reverb")
        api.project_set(target_lufs=-14)
    doc = json.loads((Path(song) / "project.json").read_text(encoding="utf-8"))
    base_lufs = (doc.get("master") or {}).get("target_lufs")
    _cut("mix", mix)
    b = seat_bench("mix")
    assert b["view"] == "mix" and b["source"]["state"] == "submitted"
    piano, bass = _track(b, "Piano"), _track(b, "Bass")
    assert piano["was"] == {"volume_db": 0.0, "pan": 0.0, "mute": False}
    assert piano["volume_db"] == -6.0 and piano["mute"] is True
    assert bass["was"] == {"fx": []} and [f["label"] for f in bass["fx"]] == ["reverb"]
    assert all("notes" not in t for t in b["tracks"])
    assert b["master"]["target_lufs"] == -14 and b["master"]["was"]["target_lufs"] == base_lufs


def test_an_untouched_track_has_no_was(song):
    _cut("mix", lambda: api.track_update("Piano", volume_db=-3))
    assert "was" not in _track(seat_bench("mix"), "Bass")


def test_a_sound_take_shows_the_old_instrument(song):
    _cut("sound", lambda: api.track_update("Piano", instrument="synth:lead"))
    b = seat_bench("sound")
    piano = _track(b, "Piano")
    assert b["view"] == "sound" and piano["instrument"] == "synth:lead"
    assert piano["was"] == {"instrument": "synth:pad"}


def test_each_seat_reads_the_work_its_own_way(song):
    views = {s: seat_bench(s)["view"] for s in ("producer", "composer", "mix", "sound", "review")}
    assert views == {"producer": "plan", "composer": "notes", "mix": "mix", "sound": "sound", "review": "report"}
    assert all("notes" not in t for t in seat_bench("producer")["tracks"])


def test_have_returns_unchanged_until_the_project_moves(song):
    b = seat_bench("composer")
    assert seat_bench("composer", have=b["version"]) == {"unchanged": True, "version": b["version"]}
    api.write("Bass", "E2:w", project=song)
    _bump(song)
    again = seat_bench("composer", have=b["version"])
    assert "unchanged" not in again and again["version"] != b["version"] and again["tracks"]


def test_take_names_any_take_and_never_a_path(song):
    tid = _cut("composer", _compose)
    b = seat_bench("mix", take=tid)
    assert b["source"]["take"] == tid and b["seat"] == "mix" and b["takes"] == []
    assert _track(b, "Lead")["new"] is True
    assert seat_bench("mix", take=tid.upper())["source"]["take"] == tid
    for bad in ("../x", "C:/somewhere", song, "tk1/../..", "tk99"):
        with pytest.raises(KeyError):
            seat_bench("mix", take=bad)
    with pytest.raises(KeyError):
        seat_bench("nobody")


def test_form_resolves_chords_and_sorts_markers(song):
    api.harmony("i VI", at="1:1")
    api.markers(add=[{"at": "2:1", "name": "B"}, {"at": "1:1", "name": "A"}])
    f = seat_bench("producer")["form"]
    assert [c["name"] for c in f["chords"]] == ["Dm", "Bb"]
    assert f["chords"][0]["at"] < f["chords"][1]["at"]
    assert f["bpb"] == 4.0 and f["time_sig"] == [4, 4] and f["key"] == "D minor"
    assert f["end"] >= 8.0
    assert [m["name"] for m in f["markers"]] == ["A", "B"]


def test_render_reads_last_json_and_knows_when_it_is_stale(song):
    assert seat_bench("mix")["render"] is None
    last = Path(song) / "renders" / "last.json"
    last.parent.mkdir(exist_ok=True)
    last.write_text(json.dumps({"mix": {"lufs": -16.2, "bands_pct": {"sub": 10}}, "warnings": ["x"],
                                "tracks": {"Piano": {"peak_dbfs": -3.0, "rms_dbfs": -18.0}}}), encoding="utf-8")
    future = time.time_ns() + 5_000_000_000
    os.utime(last, ns=(future, future))
    b = seat_bench("mix")
    r = b["render"]
    assert r["lufs"] == -16.2 and r["bands_pct"] == {"sub": 10} and r["warnings"] == ["x"]
    assert r["stale"] is False
    assert _track(b, "Piano")["level"] == {"peak_dbfs": -3.0, "rms_dbfs": -18.0}
    assert "level" not in _track(b, "Bass")
    api.track_update("Piano", volume_db=-2, project=song)
    _bump(song, 20_000_000_000)
    assert seat_bench("mix")["render"]["stale"] is True


def test_max_notes_truncates_the_bench(song, monkeypatch):
    monkeypatch.setattr(bench, "MAX_NOTES", 6)
    b = seat_bench("composer")
    assert b["truncated"] is True
    assert sum(len(t["notes"]) for t in b["tracks"]) <= 6
    assert "truncated" not in seat_bench("mix")
