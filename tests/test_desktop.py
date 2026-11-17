"""`tunetrack app`: finding the browser, the window's command line, the Start menu shortcut, and app-mode idle quit.
No real browser is opened and no real shortcut is written."""
from __future__ import annotations

import sys
from pathlib import Path

from tunetrack import config, desktop, server


def _no_browsers(monkeypatch, tmp_path, present=()):
    for var, sub in (("ProgramFiles(x86)", "pf86"), ("ProgramFiles", "pf"), ("LOCALAPPDATA", "local")):
        monkeypatch.setenv(var, str(tmp_path / sub))
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    for rel in present:
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("")


def test_find_browser_prefers_edge(monkeypatch, tmp_path):
    _no_browsers(monkeypatch, tmp_path, ["local/Google/Chrome/Application/chrome.exe",
                                         "pf/Microsoft/Edge/Application/msedge.exe"])
    assert desktop.find_browser() == str(tmp_path / "pf" / "Microsoft" / "Edge" / "Application" / "msedge.exe")


def test_find_browser_chrome_then_path(monkeypatch, tmp_path):
    _no_browsers(monkeypatch, tmp_path, ["local/Google/Chrome/Application/chrome.exe"])
    assert desktop.find_browser().endswith("chrome.exe")
    _no_browsers(monkeypatch, tmp_path / "empty")
    monkeypatch.setattr(desktop.shutil, "which", lambda name: "/usr/bin/chromium" if name == "chromium" else None)
    assert desktop.find_browser() == "/usr/bin/chromium"


def test_find_browser_none(monkeypatch, tmp_path):
    _no_browsers(monkeypatch, tmp_path)
    assert desktop.find_browser() is None


def test_open_window_command(monkeypatch):
    seen = {}
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda cmd, **kw: seen.update(cmd=cmd, kw=kw))
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: (_ for _ in ()).throw(AssertionError("tab")))
    res = desktop.open_window("http://127.0.0.1:7373/", browser="C:/Edge/msedge.exe")
    assert seen["cmd"] == ["C:/Edge/msedge.exe", "--app=http://127.0.0.1:7373/",
                           f"--user-data-dir={config.HOME / 'app-profile'}", "--no-first-run",
                           "--no-default-browser-check", "--window-size=1500,920"]
    assert res == {"url": "http://127.0.0.1:7373/", "browser": "C:/Edge/msedge.exe"}
    if sys.platform == "win32":
        assert seen["kw"]["creationflags"] & desktop.subprocess.DETACHED_PROCESS


def test_open_window_fallback(monkeypatch):
    opened = []
    monkeypatch.setattr(desktop, "find_browser", lambda: None)
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
    res = desktop.open_window("http://127.0.0.1:7373/")
    assert opened == ["http://127.0.0.1:7373/"]
    assert res["browser"] is None and "normal tab" in res["note"]


def test_make_shortcut_command(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(desktop.subprocess, "run", lambda cmd, **kw: seen.update(cmd=cmd))
    folder = tmp_path / "it's here"
    lnk = desktop.make_shortcut(folder)
    assert lnk == folder / "TuneTrack Studio.lnk"
    assert seen["cmd"][:3] == ["powershell", "-NoProfile", "-Command"]
    script = seen["cmd"][3]
    assert "WScript.Shell" in script and "$s.Save()" in script
    assert "CreateShortcut('" + str(lnk).replace("'", "''") + "')" in script
    assert "$s.Arguments = '-m tunetrack.cli app'" in script
    assert f"$s.WorkingDirectory = '{config.ROOT}'" in script
    exe = Path(sys.executable)
    target = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
    assert f"$s.TargetPath = '{target}'" in script
    assert not lnk.exists()


def test_should_quit():
    q = server._should_quit
    # a page is open: never, however long ago it came
    assert not q(1000.0, 1, 0.0, 0.0, False)
    # the last page went 30 s ago / 100 s ago
    assert not q(130.0, 0, 100.0, 0.0, False)
    assert q(200.0, 0, 100.0, 0.0, False)
    # no page ever came: wait FIRST_PAGE_S from start
    assert not q(100.0, 0, None, 0.0, False)
    assert q(121.0, 0, None, 0.0, False)
    # a crew running or a recording keeps it up
    assert not q(1000.0, 0, 0.0, 0.0, True)
    assert not q(1000.0, 0, None, 0.0, True)


def test_busy_recording(monkeypatch):
    monkeypatch.setattr(server.livehost, "_rec", {"path": "x"})
    assert server._busy()
    monkeypatch.setattr(server.livehost, "_rec", None)
    monkeypatch.setattr(server.host, "_hosts", {})
    assert not server._busy()


def test_busy_crew(monkeypatch):
    class Alive:
        def is_alive(self):
            return True
    monkeypatch.setattr(server.livehost, "_rec", None)
    monkeypatch.setattr(server.host, "_hosts", {"s": {"thread": Alive()}})
    assert server._busy()


def test_page_counter(monkeypatch):
    monkeypatch.setattr(server, "_pages", 0)
    monkeypatch.setattr(server, "_last_page", None)
    server._page_opened()
    server._page_opened()
    assert server._pages == 2 and server._last_page is not None
    server._page_closed()
    assert server._pages == 1
    server._page_closed()
    server._page_closed()  # never below zero
    assert server._pages == 0


def test_events_stream_counts_page(monkeypatch):
    import asyncio
    monkeypatch.setattr(server, "_pages", 0)
    monkeypatch.setattr(server, "_last_page", None)
    counts = []

    class Req:
        query_params = {}

        async def is_disconnected(self):
            counts.append(server._pages)
            return True  # the page went as soon as it came

    async def drain():
        resp = await server.events(Req())
        async for _ in resp.body_iterator:
            pass
    asyncio.run(drain())
    assert counts == [1] and server._pages == 0 and server._last_page is not None


def test_idle_watch_stops_server(monkeypatch):
    class Srv:
        should_exit = False
    srv = Srv()
    monkeypatch.setattr(server, "_pages", 0)
    monkeypatch.setattr(server, "_last_page", None)
    monkeypatch.setattr(server, "_busy", lambda: False)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    server._idle_watch(srv, started=server.time.monotonic() - 1000)
    assert srv.should_exit
