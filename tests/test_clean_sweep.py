"""tunetrack clean: one sweep across every project, auditions and check runs; dry run by default."""
import os
import time
from pathlib import Path

import pytest

from tunetrack import api, cleanup, cli, config, studio


def _age(p: Path, days: float) -> Path:
    t = time.time() - days * 86400
    for f in [p, *(p.rglob("*") if p.is_dir() else [])]:
        os.utime(f, (t, t))
    return p


@pytest.fixture
def world(tmp_path, monkeypatch):
    studio.seat_join(None)
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(config, "HOME", tmp_path / "library")
    monkeypatch.setattr(config, "ROOT", tmp_path)
    api.project_new("Song", folder=str(tmp_path / "projects" / "song"), tempo=100)
    api.track_add("Lead", "synth:pad")
    api.write("Lead", "C4:q E4 G4 C5")
    song = tmp_path / "projects" / "song"
    dead = song / "cache" / "deadbeefdeadbeefdead.wav"
    dead.parent.mkdir(exist_ok=True)
    dead.write_bytes(b"x" * 200_000)
    _age(dead, 1)
    aud = tmp_path / "library" / "auditions"
    aud.mkdir(parents=True)
    (aud / "old.wav").write_bytes(b"x" * 100_000)
    _age(aud / "old.wav", 30)
    (aud / "new.wav").write_bytes(b"x" * 100_000)
    prev = tmp_path / "output" / "_preview"
    (prev / "old-check").mkdir(parents=True)
    (prev / "old-check" / "a.png").write_bytes(b"x" * 300_000)
    _age(prev / "old-check", 30)
    (prev / "busy-check").mkdir()
    (prev / "busy-check" / "a.png").write_bytes(b"x")
    _age(prev / "busy-check", 30)
    (prev / "busy-check" / "b.png").write_bytes(b"x")  # written today: the run may still be going
    (tmp_path / ".claude" / "worktrees" / "agent-1").mkdir(parents=True)
    (tmp_path / ".claude" / "worktrees" / "agent-1" / "f").write_bytes(b"x" * 50_000)
    yield tmp_path, song, dead
    api.set_default_project(None)


def test_dry_run_reports_and_deletes_nothing(world):
    root, song, dead = world
    r = cleanup.sweep(7, dry_run=True)
    assert r["renders"] == {"song": 0.2}
    assert r["auditions"]["removed"] == 1 and r["previews"]["removed"] == 1
    assert r["worktrees"] == {"agent-1": 0.1}
    assert r["total_mb"] == pytest.approx(0.6, abs=0.05)
    assert dead.exists() and (root / "output" / "_preview" / "old-check").exists()


def test_yes_deletes_only_what_is_old_and_unused(world):
    root, song, dead = world
    cleanup.sweep(7, dry_run=False)
    assert not dead.exists()
    assert not (root / "library" / "auditions" / "old.wav").exists()
    assert (root / "library" / "auditions" / "new.wav").exists()
    assert not (root / "output" / "_preview" / "old-check").exists()
    assert (root / "output" / "_preview" / "busy-check").exists()
    assert (root / ".claude" / "worktrees" / "agent-1").exists()  # only reported
    assert (song / "project.json").exists()


def test_cli_prints_a_summary(world, capsys):
    cli.main(["clean"])
    out = capsys.readouterr().out
    assert out.startswith("Would free") and "Nothing deleted" in out and "agent-1" in out
