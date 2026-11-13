"""The studio board (seats, tasks, messages, Needs You) and takes (working copies merged back on acceptance)."""
import json
import threading
from pathlib import Path

import pytest

from tunetrack import api, engine, roles, studio, takes
from tunetrack.model import Project


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
    api.write("Piano", "D4:q F4 A4 D5 | C4:q E4 G4 C5 | Bb3:q D4 F4 Bb4 | A3:q C#4 E4 A4")
    api.track_add("Bass", "synth:pad")
    api.write("Bass", "D2:w | C2:w | Bb1:w | A1:w")
    api.track_add("Hall", kind="bus")
    api.harmony("Dm C Bb A", at="1:1")
    return str(tmp_path / "song")


def _doc(path) -> dict:
    return json.loads((Path(path) / "project.json").read_text(encoding="utf-8"))


def _notes(path, track) -> list:
    return api.notes_read(track, project=str(path), format="list")["tracks"][0]["notes"] if False else \
        sorted((n[0], round(c["start"] + n[1], 4)) for t in _doc(path)["tracks"] if t["name"] == track
               for c in t["clips"] for n in c.get("notes", []))


def _in_take(seat, fn, **kw):
    """Run edits the way a seated agent would: cut a take, edit it, submit it, step back out."""
    studio.seat_join(seat)
    t = takes.take_new(**kw)
    fn()
    takes.take_submit("done")
    studio.seat_join(None)
    api.set_default_project(None)
    return t["take"]


# ---- board ---------------------------------------------------------------------------------------------------------

def test_tasks_wait_for_their_dependencies(song):
    studio.seat_assign("composer", "agent", runner="claude", model="claude-opus-5-5")
    a = studio.task_add("composer", "Write the A theme", scope={"tracks": ["Piano"], "bars": "1-8"})
    b = studio.task_add("mix", "Balance the A section", depends=[a["id"]])
    assert a["ready"] and b["ready"] is False and b["waiting_for"] == [a["id"]]
    assert "empty" in b["warning"]  # nobody sits at the mix desk yet
    assert [t["id"] for t in studio.ready_tasks()] == [a["id"]]
    studio.task_update(a["id"], state="working")
    done = studio.task_update(a["id"], state="done", result="16 bars, hook in bar 5")
    assert done["now_ready"] == [b["id"]]
    assert [t["id"] for t in studio.ready_tasks()] == [b["id"]]
    board = studio.studio_status()
    assert board["warnings"] == ["mix has open tasks but nobody in the seat"]
    assert {s["seat"]: s["occupant"] for s in board["seats"]}["composer"] == "claude · claude-opus-5-5"
    assert "spotting" not in {s["seat"] for s in board["seats"]}  # optional and unused: not shown


def test_a_seat_only_moves_its_own_tasks(song):
    t = studio.task_add("mix", "Tame the low end")
    studio.seat_join("composer")
    with pytest.raises(PermissionError):
        studio.task_update(t["id"], state="done")
    with pytest.raises(PermissionError):
        studio.seat_assign("mix", "human")
    studio.seat_join("mix")
    assert studio.task_update(t["id"], state="working")["state"] == "working"


def test_needs_you_round_trip(song):
    t = studio.task_add("composer", "Write the theme")
    later = studio.task_add("rhythm", "Groove for the theme", depends=[t["id"]])
    studio.seat_join("composer")
    studio.task_update(t["id"], state="working")
    ask = studio.ask_human("Which theme should I develop?", "Two directions fit the brief.",
                           options=[{"label": "Sparse", "take": "tk1"}, "Strong hook"], task=t["id"])
    assert ask["options"] == ["a", "b"]
    assert studio.task_list("composer")["tasks"][0]["state"] == "needs_you"
    with pytest.raises(PermissionError):
        studio.attention_resolve(ask["needs_you"], "b")  # the composer can't answer for the user

    studio.seat_join(None)
    assert studio.studio_status()["needs_you"][0]["title"] == "Which theme should I develop?"
    with pytest.raises(KeyError):
        studio.attention_resolve(ask["needs_you"], "loud")
    res = studio.attention_resolve(ask["needs_you"], "strong hook", "More restraint in the second phrase")
    assert res["requeued"] == [t["id"]] and res["answer"]["option"] == "b"
    assert studio.attention_list()["open"] == 0
    assert later["id"] not in [x["id"] for x in studio.ready_tasks()]

    studio.seat_join("composer")
    box = studio.inbox()
    assert "chose Strong hook" in box["messages"][0]["text"] and "restraint" in box["messages"][0]["text"]
    assert box["messages"][0]["task"] == t["id"] and box["tasks"][0]["state"] == "queued"
    assert studio.inbox()["messages"] == []  # read once


def test_messages_reach_the_right_seats(song):
    studio.message_send("all", "Tempo is now 100")
    studio.message_send("mix", "Bass is too loud in bars 9-12")
    studio.seat_join("mix")
    assert [m["text"] for m in studio.inbox(mark_read=False)["messages"]] == ["Tempo is now 100", "Bass is too loud in bars 9-12"]
    studio.message_send("you", "Mix v2 is ready")
    studio.seat_join("composer")
    assert [m["text"] for m in studio.inbox()["messages"]] == ["Tempo is now 100"]
    studio.seat_join(None)
    assert [m["text"] for m in studio.inbox()["messages"]] == ["Mix v2 is ready"]  # "all" is the crew, not the user
    with pytest.raises(KeyError):
        studio.message_send("drummer", "hi")


def test_board_survives_many_writers_at_once(song):
    errors = []

    def add(i):
        try:
            studio.task_add("composer", f"task {i}", session=song)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=add, args=(i,)) for i in range(24)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    tasks = studio.task_list(state="all")["tasks"]
    assert len(tasks) == 24 and len({t["id"] for t in tasks}) == 24
    ev = studio.events()
    assert [e["seq"] for e in ev["events"]] == list(range(1, 25)) and ev["last"] == 24
    assert studio.events(since=20)["events"][0]["seq"] == 21


# ---- takes ---------------------------------------------------------------------------------------------------------

def test_a_take_is_private_until_accepted(song):
    before = _doc(song)
    studio.seat_join("composer")
    t = takes.take_new("Lift the last bar")
    api.write("Piano", "A4:q C#5 E5 A5", at="4:1")
    assert _doc(song)["tracks"] == before["tracks"]  # the session hasn't moved
    assert Project.load(t["project"]).root == Path(song)  # the take shares the session's audio + cache
    rep = takes.take_diff()
    assert [(c["kind"], c["where"]) for c in rep["changes"]] == [("notes", "Piano")]
    assert "bars 4 " in rep["changes"][0]["what"] and "outside_seat" not in rep
    with pytest.raises(PermissionError):
        takes.take_accept(t["take"])  # a composer doesn't accept its own take
    takes.take_submit("Last bar an octave up")

    studio.seat_join(None)
    api.set_default_project(None)
    res = takes.take_accept(t["take"])
    assert res["accepted"] and (81, 15.0) in _notes(song, "Piano")
    api.undo(song)
    assert _doc(song)["tracks"] == before["tracks"]  # accepting is one undo step


def test_notes_and_mix_on_one_track_merge(song):
    studio.task_add("composer", "Rewrite bar 2")
    tk = _in_take("composer", lambda: api.write("Piano", "C5:h G4:h", at="2:1"), task="T1")
    api.track_update("Piano", project=song, volume_db=-6.0, sends=[{"to": "Hall", "db": -14}])
    api.fx_add("Piano", "builtin:eq", project=song)
    rep = takes.take_diff(tk)
    assert rep["session_moved"]["conflicts"] == []
    res = takes.take_accept(tk)
    assert res["accepted"] and res["task_done"] == "T1" and "conflicts_resolved" not in res
    piano = next(t for t in _doc(song)["tracks"] if t["name"] == "Piano")
    assert piano["volume_db"] == -6.0 and piano["fx"][0]["name"] == "eq" and piano["sends"][0]["db"] == -14
    assert (72, 4.0) in _notes(song, "Piano") and (60, 4.0) not in _notes(song, "Piano")
    assert studio.task_list(state="done")["tasks"][0]["id"] == "T1"


def test_two_takes_in_different_bars_of_one_clip_merge(song):
    a = _in_take("composer", lambda: api.write("Piano", "D5:w", at="1:1"))
    b = _in_take("arranger", lambda: api.write("Piano", "A5:w", at="4:1"))
    assert takes.take_accept(a)["accepted"]
    res = takes.take_accept(b)
    assert res["accepted"] and "conflicts_resolved" not in res
    notes = _notes(song, "Piano")
    assert (74, 0.0) in notes and (81, 12.0) in notes
    assert (62, 0.0) not in notes and (57, 12.0) not in notes  # both replacements took effect
    assert (60, 4.0) in notes and (58, 8.0) in notes  # the bars nobody touched are intact


def test_two_takes_in_the_same_bars_conflict(song):
    a = _in_take("composer", lambda: api.write("Piano", "D5:w", at="1:1"))
    b = _in_take("arranger", lambda: api.write("Piano", "F5:w", at="1:1"))
    takes.take_accept(a)
    before = _doc(song)
    res = takes.take_accept(b)
    assert res["accepted"] is False and "bars 1" in res["conflicts"][0]["where"] and res["conflicts"][0]["kind"] == "notes"
    assert _doc(song) == before  # nothing applied
    kept = takes.take_accept(b, on_conflict="take")
    assert kept["accepted"] and kept["conflicts_resolved"]["winner"] == "take"
    assert (77, 0.0) in _notes(song, "Piano") and (74, 0.0) not in _notes(song, "Piano")


def test_tracks_added_on_both_sides_keep_apart(song):
    def strings():
        api.track_add("Strings", "synth:pad")
        api.track_add("Plate", kind="bus")
        api.track_update("Strings", sends=[{"to": "Plate", "db": -10}])
        api.write("Strings", "(D4 F4 A4):w")

    tk = _in_take("arranger", strings)
    api.track_add("Drums", "synth:drums", project=song)  # takes the id the take's Strings track has
    api.write("Drums", "C2:q C2 C2 C2", project=song)
    res = takes.take_accept(tk)
    assert res["accepted"]
    doc = _doc(song)
    names = {t["name"]: t for t in doc["tracks"]}
    assert set(names) == {"Piano", "Bass", "Hall", "Drums", "Strings", "Plate"}
    ids = [t["id"] for t in doc["tracks"]] + [c["id"] for t in doc["tracks"] for c in t["clips"]]
    assert len(ids) == len(set(ids))
    assert names["Strings"]["sends"][0]["to"] == names["Plate"]["id"]  # the send followed its bus's new id
    assert doc["next_id"] > max(int(i[1:]) for i in ids)
    api.track_add("Flute", "synth:pad", project=song)  # and new ids keep working afterwards
    assert len({t["id"] for t in _doc(song)["tracks"]}) == 7


def test_removing_a_track_someone_else_changed_is_a_conflict(song):
    tk = _in_take("producer", lambda: api.track_remove("Hall"))
    assert takes.take_accept(tk)["accepted"]  # untouched in the session: just removed
    assert "Hall" not in {t["name"] for t in _doc(song)["tracks"]}

    api.track_add("Room", kind="bus", project=song)
    api.track_update("Bass", project=song, output="Room")
    tk = _in_take("producer", lambda: api.track_remove("Bass"))
    api.track_update("Bass", project=song, volume_db=-3.0)
    res = takes.take_accept(tk)
    assert res["accepted"] is False and res["conflicts"][0]["where"] == "track Bass"
    assert takes.take_accept(tk, on_conflict="session")["accepted"]
    assert "Bass" in {t["name"] for t in _doc(song)["tracks"]}


def test_chord_track_merges_per_bar(song):
    a = _in_take("composer", lambda: api.harmony("Gm", at="3:1", end="4:1"))
    api.harmony("F", at="2:1", end="3:1", project=song)
    assert takes.take_accept(a)["accepted"]
    assert [e["chord"] for e in _doc(song)["chord_track"]] == ["Dm", "F", "Gm", "A"]
    b = _in_take("composer", lambda: api.harmony("Eb", at="3:1", end="4:1"))
    api.harmony("Bb", at="3:1", end="4:1", project=song)
    res = takes.take_accept(b)
    assert res["accepted"] is False and res["conflicts"][0]["where"] == "chord track, bars 3"


def test_sync_brings_accepted_work_into_an_open_take(song):
    studio.seat_join("mix")
    mine = takes.take_new("Mix pass")
    api.track_update("Bass", volume_db=-4.0)
    studio.seat_join(None)
    api.set_default_project(None)
    other = _in_take("composer", lambda: api.write("Piano", "D5:w", at="1:1"))
    takes.take_accept(other)

    studio.seat_join("mix")
    takes.take_use(mine["take"])
    assert (74, 0.0) not in _notes(mine["project"], "Piano")
    res = takes.take_sync()
    assert res["synced"] and any("Piano" in line for line in res["brought_in"])
    assert (74, 0.0) in _notes(mine["project"], "Piano")
    rep = takes.take_diff()
    assert [(c["kind"], c["where"]) for c in rep["changes"]] == [("mix", "Bass")] and "session_moved" not in rep
    assert takes.take_sync()["note"] == "Already up to date"


def test_work_outside_a_seats_department_is_flagged(song):
    tk = _in_take("mix", lambda: (api.track_update("Bass", volume_db=-4.0), api.write("Piano", "D5:w", at="1:1")))
    rep = takes.take_diff(tk)
    assert len(rep["outside_seat"]) == 1 and "notes is not mix's department" in rep["outside_seat"][0]


def test_returning_a_take_requeues_its_task(song):
    studio.task_add("composer", "Write the theme")
    tk = _in_take("composer", lambda: api.write("Piano", "D5:w", at="1:1"), task="T1")
    assert studio.task_list()["tasks"][0]["state"] == "submitted"
    res = takes.take_reject(tk, "Bar 1 loses the motif: keep the D-F-A rise")
    assert res == {"returned": tk, "to": "composer", "task": "T1"}
    assert studio.task_list()["tasks"][0]["state"] == "queued"
    studio.seat_join("composer")
    assert "keep the D-F-A rise" in studio.inbox()["messages"][0]["text"]
    assert takes.take_list()["takes"][0]["state"] == "open"  # still there to continue


def test_an_empty_take_cannot_be_submitted(song):
    studio.seat_join("composer")
    takes.take_new()
    with pytest.raises(ValueError, match="changes nothing"):
        takes.take_submit("nothing")


def test_merge_keeps_the_session_when_only_it_changed():
    base = {"tempo": 96, "next_id": 3, "tracks": [{"id": "t1", "name": "A", "volume_db": 0, "clips": [
        {"id": "c2", "start": 0, "length": 4, "notes": [[60, 0, 1, 90]]}]}]}
    dst = json.loads(json.dumps(base))
    dst["tempo"] = 100
    dst["tracks"][0]["clips"][0]["notes"].append([64, 1, 1, 90])
    merged, conflicts, notes = takes.merge(base, base, dst)
    assert {k: merged[k] for k in dst} == dst and not conflicts and not notes


def test_a_clip_moved_and_extended_still_merges_by_absolute_time():
    clip = {"id": "c2", "start": 4, "length": 4, "notes": [[60, 0, 1, 90], [62, 2, 1, 90]]}
    base = {"next_id": 3, "tracks": [{"id": "t1", "name": "A", "clips": [clip]}]}
    src = json.loads(json.dumps(base))  # the take wrote an earlier bar: the clip now starts at 0
    src["tracks"][0]["clips"][0] = {"id": "c2", "start": 0, "length": 8,
                                    "notes": [[55, 0, 2, 90], [60, 4, 1, 90], [62, 6, 1, 90]]}
    dst = json.loads(json.dumps(base))  # the session added a bar after it
    dst["tracks"][0]["clips"][0] = {"id": "c2", "start": 4, "length": 8,
                                    "notes": [[60, 0, 1, 90], [62, 2, 1, 90], [67, 4, 4, 90]]}
    merged, conflicts, _ = takes.merge(base, src, dst)
    c = merged["tracks"][0]["clips"][0]
    assert not conflicts and (c["start"], c["length"]) == (0, 12)
    assert c["notes"] == [[55, 0, 2, 90], [60, 4, 1, 90], [62, 6, 1, 90], [67, 8, 4, 90]]


# ---- shared render cache -------------------------------------------------------------------------------------------

def test_cache_entry_is_written_once_and_whole(tmp_path):
    out = tmp_path / "abc.wav"
    with engine._cache_entry(out) as part:
        assert part is not None and part.name == "abc.part.wav"
        part.write_bytes(b"audio")
        assert not out.exists()  # not visible under its real name until complete
    assert out.read_bytes() == b"audio"
    with engine._cache_entry(out) as part:
        assert part is None  # someone already rendered it
    with pytest.raises(RuntimeError), engine._cache_entry(tmp_path / "bad.wav") as part:
        part.write_bytes(b"half")
        raise RuntimeError("worker died")
    assert not (tmp_path / "bad.wav").exists() and not (tmp_path / "bad.part.wav").exists()


def test_a_take_renders_from_the_sessions_cache(song):
    first = api.render(song)
    assert first["tracks"]["Piano"]["render"] != "cached"
    studio.seat_join("mix")
    t = takes.take_new("Quieter bass")
    api.track_update("Bass", volume_db=-6.0)  # a fader move: no track needs re-rendering
    rep = api.render()
    assert {v["render"] for v in rep["tracks"].values()} == {"cached"}
    assert Path(rep["files"][0]).parent == Path(t["project"]) / "renders"  # the take's own mix, not the session's
    assert not (Path(t["project"]) / "cache").exists()


def test_roles_catalog_is_consistent():
    for r in roles.ROLES.values():
        assert not (r.tools & roles.HUMAN_ONLY), r.id
        assert all(h in roles.ROLES for h in r.hands) and (r.merges_into is None or r.merges_into in roles.ROLES)
        assert r.owns <= set(takes.CATEGORIES)
    assert "write" not in roles.get("review").tools and "write" in roles.get("composer").tools
    assert roles.tools_for("you") is None


# ---- how the board reaches agents and the editor -------------------------------------------------------------------

def test_a_seated_mcp_server_offers_only_its_seats_tools(song):
    from tunetrack import mcp_server
    assert set().union(*[r.tools for r in roles.ROLES.values()]) <= set(mcp_server.TOOLS)
    assert roles.HUMAN_ONLY <= set(mcp_server.TOOLS)
    removed = []
    studio.seat_join("review")
    try:
        mcp_server.mcp.remove_tool, real = removed.append, mcp_server.mcp.remove_tool
        assert mcp_server.apply_seat() == removed
    finally:
        mcp_server.mcp.remove_tool = real
    assert {"write", "fx_add", "take_accept", "patch_capture", "project_open", "seat_join"} <= set(removed)
    assert not {"listen", "render", "score", "task_add", "message_send", "ask_human"} & set(removed)
    studio.seat_join(None)
    assert mcp_server.apply_seat() == []  # the user's own assistant keeps everything
    assert "You are the Review" not in mcp_server.INSTRUCTIONS and "seat_join" in mcp_server.INSTRUCTIONS


def test_the_editor_feed_reports_file_changes_and_new_activity(song):
    from tunetrack import server
    msg, seq = server._pulse(Path(song), 0)
    assert msg["project"] == song and msg["events"] == [] and seq == 0
    studio.task_add("composer", "Write the theme")
    msg2, seq = server._pulse(Path(song), seq)
    assert [e["kind"] for e in msg2["events"]] == ["task"] and seq == 1 and msg2["version"] == msg["version"]
    api.track_update("Piano", project=song, volume_db=-3.0)
    msg3, seq = server._pulse(Path(song), seq)
    assert msg3["events"] == [] and msg3["version"] != msg["version"]
    assert server._pulse(None, 5) == ({"project": None, "version": 0, "events": []}, 0)
    assert {"take_accept", "attention_resolve", "seat_assign", "events"} <= set(server.STUDIO)
    assert not set(server.STUDIO) & server.CALLABLE


def test_track_refs_and_take_paths_as_agents_write_them(song):
    """From the first live session: agents wrote "t1 Pad" for a track and project="takes/tk1" for a take."""
    p = api.load(song)
    first = p.tracks[0]
    for ref in (f"{first.id} {first.name}", f"{first.id}:{first.name}", f"{first.id} ({first.name})", first.id):
        assert p.track(ref) is first
    with pytest.raises(KeyError):
        p.track(f"{first.id} Tuba")  # the id with another track's name is a mistake, not a match
    tk = takes.take_new("try", use=False)["take"]
    api.set_default_project(song)
    try:
        for ref in (tk, f"takes/{tk}", "takes\\" + tk):
            assert api._path(ref) == (Path(song) / "takes" / tk).resolve()
        assert api._path("session") == Path(song).resolve()
    finally:
        api.set_default_project(None)


def test_a_relative_render_path_lands_in_the_session(song, tmp_path, monkeypatch):
    """A live Mix agent passed out="renders/final": it must not land in whatever folder the process runs in."""
    monkeypatch.chdir(tmp_path)
    tk = takes.take_new("mix", use=False)["project"]
    rep = api.render(project=tk, out="renders/final")
    assert rep["files"] and all(Path(f).resolve().is_relative_to(Path(song).resolve() / "renders" / "final")
                                for f in rep["files"])
    assert not (tmp_path / "renders").exists()
