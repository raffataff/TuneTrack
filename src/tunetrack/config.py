"""Locations of the TuneTrack library (plugins, patches, sample instruments) and user config."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HOME = Path(os.environ.get("TUNETRACK_HOME", ROOT / "library"))
PATCH_DIR = HOME / "patches"
INSTRUMENT_DIR = HOME / "instruments"
REGISTRY_FILE = HOME / "plugins.json"
CONFIG_FILE = HOME / "config.json"
PROJECTS_DIR = Path(os.environ.get("TUNETRACK_PROJECTS", ROOT / "projects"))

ENGINE_VERSION = "2"  # bump to invalidate every render cache

DEFAULT_CONFIG = {
    "vst3_dirs": [
        r"C:\Program Files\Common Files\VST3",
    ],
    "sample_dirs": [str(INSTRUMENT_DIR)],
    "skip_plugins": [],
    "render_workers": 4,
    "default_sample_rate": 48000,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    return cfg


def save_config(cfg: dict) -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def ensure_dirs() -> None:
    for d in (HOME, PATCH_DIR, INSTRUMENT_DIR, PROJECTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)


def python_exe() -> str:
    return sys.executable
