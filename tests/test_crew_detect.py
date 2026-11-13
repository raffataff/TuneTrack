"""tunetrack.crew.detect with every subprocess call and file location faked (no real CLI is run)."""
import json
import os
import subprocess
import time

import pytest

from tunetrack import config
from tunetrack.crew import detect

KILO_BANNER = (
    "INFO  2026-11-12T22:41:13 +20ms service=default version=7.8.27 command=--version run_id=x\n"
    "\x1b[90m██  ██ ████   ██\x1b[0m\n~~  ~~ ~~~~~~  ~~~~~\n"
)


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    """Fake HOME/APPDATA so no real auth file, model cache or app folder is seen; reset the cache + memo."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    for v in ("CODEX_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
              "CODEX_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(detect, "_exe_memo", {})
    monkeypatch.setattr(detect, "_appx_location", lambda name: None)
    cache = detect._cache_path()
    if cache.exists():
        cache.unlink()
    yield home
    if cache.exists():
        cache.unlink()


def _exes(monkeypatch, tmp_path, which):
    paths = {}
    for r in which:
        p = tmp_path / f"{r}.exe"
        p.write_text("")
        paths[r] = str(p)
    monkeypatch.setattr(detect, "find_exe", lambda r: paths.get(r))
    return paths


def fake_run(table):
    """table: (exe-name, first-arg) -> (rc, stdout, stderr) or an exception to raise."""
    calls = []

    def run(argv, capture_output=True, stdin=None, timeout=None, env=None, creationflags=0):
        calls.append((list(argv), timeout))
        assert timeout is not None and timeout <= 20
        key = (os.path.basename(argv[0]).split(".")[0], argv[1] if len(argv) > 1 else "")
        res = table.get(key, (1, "", "unknown command"))
        if isinstance(res, BaseException):
            raise res
        rc, out, err = res
        return subprocess.CompletedProcess(argv, rc, out.encode("utf-8"), err.encode("utf-8"))

    run.calls = calls
    return run


def _runner(data, rid):
    return next(r for r in data["runners"] if r["id"] == rid)


def test_all_missing(sandbox, monkeypatch):
    monkeypatch.setattr(detect, "find_exe", lambda r: None)
    data = detect.detect(refresh=True)
    assert [r["id"] for r in data["runners"]] == ["claude", "codex", "kilo"]
    for r in data["runners"]:
        assert r["available"] is False and r["exe"] is None
        assert any("not found" in n for n in r["notes"])
    assert _runner(data, "codex")["verified"] is False
    assert len(_runner(data, "claude")["models"]) == 4  # static list still offered
    assert {a["id"] for a in data["apps"]} == {"claude-desktop", "codex-desktop", "vscode-kilo"}
    assert not any(a["installed"] for a in data["apps"])
    json.dumps(data)
    assert isinstance(data["scanned_at"], int)


def test_full_scan(sandbox, monkeypatch, tmp_path):
    _exes(monkeypatch, tmp_path, ["claude", "codex", "kilo"])
    models = "kilo/~anthropic/claude-opus-latest\nkilo/openrouter/free\nINFO 2026 noise\nkilo/x/fast-mini:free\n" \
             "kilo/y/big-model\nnot a model line\n"
    monkeypatch.setattr(subprocess, "run", fake_run({
        ("claude", "--version"): (0, "2.1.295 (Claude Code)\n", ""),
        ("claude", "auth"): (0, json.dumps({"loggedIn": True, "email": "someone@example.com"}), ""),
        ("codex", "--version"): (0, "codex-cli 0.167.2\n", ""),
        ("kilo", "--version"): (0, "7.8.27\n", KILO_BANNER),
        ("kilo", "models"): (0, KILO_BANNER + models, ""),
    }))
    codex_home = sandbox / ".codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}")
    (codex_home / "models_cache.json").write_text(json.dumps({
        "identity": "do-not-copy", "etag": "x",
        "models": [
            {"slug": "gpt-b", "display_name": "GPT B", "visibility": "list", "priority": 2,
             "supported_reasoning_levels": [{"effort": "low", "description": "."}, {"effort": "high"}]},
            {"slug": "gpt-hidden", "display_name": "Hidden", "visibility": "hide", "priority": 0},
            {"slug": "gpt-a", "display_name": "GPT A", "visibility": "list", "priority": 1,
             "supported_reasoning_levels": [{"effort": "medium"}]},
        ]}))
    data = detect.detect(refresh=True)

    c = _runner(data, "claude")
    assert c["available"] and c["version"] == "2.1.295" and c["auth"] == "ok" and c["verified"]
    assert [m["id"] for m in c["models"]] == ["claude-fable-5-2", "claude-opus-5-5", "claude-sonnet-5-5",
                                              "claude-haiku-5-5"]
    assert [m["tier"] for m in c["models"]] == ["top", "high", "mid", "fast"]
    assert "high" in c["efforts"]
    assert "someone@example.com" not in json.dumps(data)  # account details are never kept

    x = _runner(data, "codex")
    assert x["version"] == "0.167.2" and x["auth"] == "ok" and x["verified"] is True
    assert [m["id"] for m in x["models"]] == ["gpt-a", "gpt-b"]
    assert x["efforts"] == ["medium", "low", "high"]
    assert "do-not-copy" not in json.dumps(data)

    k = _runner(data, "kilo")
    assert k["version"] == "7.8.27" and k["auth"] == "unknown"
    ids = [m["id"] for m in k["models"]]
    assert ids == ["kilo/openrouter/free", "kilo/x/fast-mini:free", "kilo/~anthropic/claude-opus-latest",
                   "kilo/y/big-model"]
    assert [m["cost"] for m in k["models"]] == ["free", "free", "paid", "paid"]
    assert k["models"][1]["tier"] == "fast" and k["models"][2]["tier"] == "top"

    cached = json.loads((config.HOME / "agents.json").read_text(encoding="utf-8"))
    assert cached == data


def test_timeouts_and_garbage_never_raise(sandbox, monkeypatch, tmp_path):
    _exes(monkeypatch, tmp_path, ["claude", "codex", "kilo"])
    monkeypatch.setattr(subprocess, "run", fake_run({
        ("claude", "--version"): subprocess.TimeoutExpired(["claude"], 20),
        ("claude", "auth"): (0, "\x00\xff garbage {not json", ""),
        ("codex", "--version"): (0, "\x1b[31m??? no digits here ???\x1b[0m", ""),
        ("kilo", "--version"): OSError("access denied"),
        ("kilo", "models"): subprocess.TimeoutExpired(["kilo"], 20),
    }))
    (sandbox / ".codex").mkdir()
    (sandbox / ".codex" / "models_cache.json").write_text("{this is not json")
    data = detect.detect(refresh=True)
    c, x, k = (_runner(data, r) for r in ("claude", "codex", "kilo"))
    assert c["version"] is None and any("timed out" in n for n in c["notes"]) and c["auth"] == "unknown"
    assert x["version"] is None and x["auth"] == "missing" and x["models"] == []
    assert any("model cache" in n for n in x["notes"])
    assert any("access denied" in n for n in k["notes"]) and any("models" in n for n in k["notes"])
    assert k["models"] == []
    json.dumps(data)


def test_probe_exception_lands_in_notes(sandbox, monkeypatch, tmp_path):
    _exes(monkeypatch, tmp_path, ["kilo"])

    def boom(*a, **k):
        raise RuntimeError("weird failure")

    monkeypatch.setattr(detect, "_version", boom)
    k = _runner(detect.detect(refresh=True), "kilo")
    assert any("weird failure" in n for n in k["notes"])


def test_cache_used_then_expires(sandbox, monkeypatch):
    monkeypatch.setattr(detect, "find_exe", lambda r: None)
    first = detect.detect(refresh=True)
    calls = []
    monkeypatch.setattr(detect, "_probe", lambda r: calls.append(r) or {"id": r, "available": False})
    assert detect.detect() == first and calls == []          # fresh cache: no rescan
    stale = dict(first, scanned_at=int(time.time()) - 25 * 3600)
    detect._cache_path().write_text(json.dumps(stale), encoding="utf-8")
    detect.detect()
    assert calls == ["claude", "codex", "kilo"]                # older than 24 h: rescanned
    calls.clear()
    detect.detect(refresh=True)
    assert calls == ["claude", "codex", "kilo"]


def test_cache_rescans_when_exe_vanished(sandbox, monkeypatch, tmp_path):
    data = {"runners": [{"id": "kilo", "available": True, "exe": str(tmp_path / "gone" / "kilo.exe")}],
            "apps": [], "scanned_at": int(time.time())}
    detect._cache_path().parent.mkdir(parents=True, exist_ok=True)
    detect._cache_path().write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(detect, "find_exe", lambda r: None)
    assert len(detect.detect()["runners"]) == 3


def test_parse_version():
    assert detect.parse_version(KILO_BANNER + "7.8.27\n") == "7.8.27"
    assert detect.parse_version(KILO_BANNER) == "7.8.27"  # only the log line carries it
    assert detect.parse_version("codex-cli 0.167.0-alpha.4.1") == "0.167.0-alpha.4.1"
    assert detect.parse_version("2.1.295 (Claude Code)") == "2.1.295"
    assert detect.parse_version("nothing") is None


def test_parse_kilo_models_cap_and_free_first():
    lines = "\n".join([f"kilo/p/m{i}" for i in range(100)] + ["kilo/p/z:free"])
    rows = detect.parse_kilo_models(lines, cap=10)
    assert len(rows) == 10 and rows[0]["id"] == "kilo/p/z:free" and rows[0]["cost"] == "free"


def test_find_exe_kilo_extension_newest(sandbox, monkeypatch):
    monkeypatch.setattr(detect.shutil, "which", lambda name: None)
    ext = sandbox / ".vscode" / "extensions"
    for v in ("7.8.9", "7.8.27", "7.10.1"):
        b = ext / f"kilocode.kilo-code-{v}-win32-x64" / "bin"
        b.mkdir(parents=True)
        (b / ("kilo.exe" if detect.IS_WINDOWS else "kilo")).write_text("")
    found = detect.find_exe("kilo")
    assert found and "kilo-code-7.10.1" in found
    assert detect.find_exe("claude") is None


def test_find_exe_codex_from_app_package(sandbox, monkeypatch, tmp_path):
    monkeypatch.setattr(detect.shutil, "which", lambda name: None)
    pkg = tmp_path / "WindowsApps" / "OpenAI.Codex_1.0_x64"
    (pkg / "app" / "resources").mkdir(parents=True)
    (pkg / "app" / "resources" / "codex.exe").write_text("")
    monkeypatch.setattr(detect, "_appx_location", lambda name: str(pkg) if name == "OpenAI.Codex" else None)
    assert detect.find_exe("codex") == str(pkg / "app" / "resources" / "codex.exe")


def test_find_exe_prefers_path(sandbox, monkeypatch, tmp_path):
    p = tmp_path / "claude.exe"
    p.write_text("")
    monkeypatch.setattr(detect.shutil, "which", lambda name: str(p) if name == "claude" else None)
    assert detect.find_exe("claude") == str(p)


def test_apps_detected(sandbox, monkeypatch, tmp_path):
    local = tmp_path / "local"
    (local / "Packages" / "Claude_abc" / "LocalCache" / "Roaming" / "Claude").mkdir(parents=True)
    (local / "Microsoft" / "WindowsApps" / "OpenAI.Codex_xyz").mkdir(parents=True)
    (sandbox / ".vscode" / "extensions" / "kilocode.kilo-code-1.0.0").mkdir(parents=True)
    apps = {a["id"]: a for a in detect._apps()}
    assert apps["claude-desktop"]["installed"]
    assert apps["claude-desktop"]["mcp_config"].endswith(os.path.join("Claude", "claude_desktop_config.json"))
    assert apps["codex-desktop"]["installed"] and apps["codex-desktop"]["mcp_config"].endswith("config.toml")
    assert apps["vscode-kilo"]["installed"] and apps["vscode-kilo"]["mcp_config"].endswith("kilo.jsonc")
