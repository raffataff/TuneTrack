"""`tunetrack app`: the editor in a window of its own. Edge or Chrome opens it with --app (no tabs, no address bar)
on a profile kept in the library, and a Start menu shortcut can launch it without a console."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import config

SHORTCUT_NAME = "TuneTrack Studio.lnk"
ICON = Path(__file__).parent / "web" / "icon.ico"  # the shortcut's icon; the page uses icon.svg beside it


def find_browser() -> str | None:
    """Path of a Chromium browser that takes --app: Microsoft Edge first, then Google Chrome. None if neither."""
    env = os.environ.get
    places = [
        (env("ProgramFiles(x86)"), r"Microsoft\Edge\Application\msedge.exe"),
        (env("ProgramFiles"), r"Microsoft\Edge\Application\msedge.exe"),
        (env("LOCALAPPDATA"), r"Google\Chrome\Application\chrome.exe"),
        (env("ProgramFiles"), r"Google\Chrome\Application\chrome.exe"),
        (env("ProgramFiles(x86)"), r"Google\Chrome\Application\chrome.exe"),
    ]
    for base, rel in places:
        if base and Path(base, rel).exists():
            return str(Path(base, rel))
    for name in ("msedge", "chrome", "google-chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return None


def open_window(url: str, browser: str | None = None) -> dict:
    """Open url as an app window. Without Edge or Chrome it opens in the default browser as a normal tab."""
    exe = browser or find_browser()
    if not exe:
        webbrowser.open(url)
        return {"url": url, "browser": None,
                "note": "Edge or Chrome not found: opened in the default browser as a normal tab"}
    cmd = [exe, f"--app={url}", f"--user-data-dir={config.HOME / 'app-profile'}", "--no-first-run",
           "--no-default-browser-check", "--window-size=1500,920"]
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=flags, close_fds=True)
    return {"url": url, "browser": exe}


def _ps(s: str) -> str:
    """A PowerShell single-quoted string literal."""
    return "'" + str(s).replace("'", "''") + "'"


def make_shortcut(folder: str | Path | None = None) -> Path:
    """Write "TuneTrack Studio.lnk" (runs `tunetrack app` without a console) into folder, by default the user's
    Start menu programs."""
    if folder is None:
        folder = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) \
            / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    folder = Path(folder)
    lnk = folder / SHORTCUT_NAME
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    target = windowless if windowless.exists() else exe
    script = (f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut({_ps(lnk)}); "
              f"$s.TargetPath = {_ps(target)}; "
              f"$s.Arguments = {_ps('-m tunetrack.cli app')}; "
              f"$s.WorkingDirectory = {_ps(config.ROOT)}; "
              f"$s.Description = {_ps('TuneTrack Studio')}; "
              f"$s.IconLocation = {_ps(str(ICON) + ',0')}; "
              "$s.Save()")
    folder.mkdir(parents=True, exist_ok=True)
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, capture_output=True, text=True)
    return lnk
