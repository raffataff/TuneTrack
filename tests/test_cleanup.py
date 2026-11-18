"""cleanup: pruning a session's render cache down to what live projects (session, open takes) would read."""
import os
import time
from pathlib import Path

import pytest

from tunetrack import api, cleanup, studio, takes
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
    api.project_new("Clean", folder=str(tmp_path / "song"), tempo=100)
    api.track_add("Lead", "synth:pad")
    api.write("Lead", "C4:q E4 G4 C5")
    api.track_add("Hall", kind="bus")
    return tmp_path / "song"


def _old(f: Path, data=b"x" * 100_000) -> Path:
    f.write_bytes(data)
    t = time.time() - 3600
    os.utime(f, (t, t))
    return f


def test_dead_renders_go_live_and_fresh_ones_stay(song):
    cache = song / "cache"
    live, _, buses = cleanup.live_keys(Project.load(song))
    assert live
    k = next(iter(live))
    keep = [_old(cache / f"{k}.wav"), _old(cache / f"{k}.mid")]
    dead = [_old(cache / "deadbeefdeadbeefdead.wav"), _old(cache / "deadbeefdeadbeefdead.mid"),
            _old(cache / "stretch-0123456789abcdef0123.wav"), _old(cache / "busnope-abcdef0123456789.wav")]
    bus = next(iter(buses))
    old_master = [_old(cache / f"master-{i:016x}.wav") for i in range(3)]
    new_master = cache / "master-ffffffffffffffff.wav"
    _old(new_master)
    os.utime(new_master, (time.time() - 1200, time.time() - 1200))
    bus_fx = _old(cache / f"bus{bus}-aaaaaaaaaaaaaaaa.wav")
    fresh = cache / "feedfacefeedfacefeed.wav"
    fresh.write_bytes(b"y")  # being written right now
    locked = _old(cache / "cafecafecafecafecafe.part.wav")
    (cache / "cafecafecafecafecafe.lock").write_bytes(b"")

    dry = api.cleanup(str(song), dry_run=True)
    assert dry["removed"] == len(dead) + len(old_master) and dry["freed_mb"] > 0
    assert all(f.exists() for f in dead + old_master)  # dry run deletes nothing

    res = api.cleanup(str(song))
    assert res["removed"] == dry["removed"]
    assert not any(f.exists() for f in dead + old_master)
    assert all(f.exists() for f in keep + [new_master, bus_fx, fresh, locked])


def test_a_takes_renders_live_while_it_is_open(song):
    studio.seat_join("composer")
    t = takes.take_new()
    api.write("Lead", "D4:q F4 A4 D5")
    studio.seat_join(None)
    api.set_default_project(None)
    tk = song / "takes" / t["take"]
    session_keys = cleanup.live_keys(Project.load(song))[0]
    take_only = cleanup.live_keys(Project.load(tk))[0] - session_keys
    assert take_only
    f = _old(song / "cache" / f"{next(iter(take_only))}.wav")
    assert api.cleanup(str(song))["removed"] == 0 and f.exists()
    takes.take_discard(t["take"], session=str(song))
    assert api.cleanup(str(song))["removed"] == 1 and not f.exists()
    assert (tk / "take.json").exists()  # the take's record is take_cleanup's business
