"""Studio crew: launch AI agent CLIs (Claude Code, Codex, Kilo) headless as TuneTrack roles.

``crew.detect`` stays the submodule (``crew.detect.detect()`` scans); ``crew.scan`` is the same function.
"""
from . import detect, runners
from .detect import find_exe
from .runners import Command, Launch, Run, RunnerUnavailable, build, start

scan = detect.detect

__all__ = ["Command", "Launch", "Run", "RunnerUnavailable", "build", "detect", "find_exe", "runners", "scan",
           "start"]
