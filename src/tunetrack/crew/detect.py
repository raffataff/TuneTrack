"""Find the agent CLIs (Claude Code, Codex, Kilo) and the desktop apps that can host TuneTrack's MCP server.

``detect()`` is read-only: it runs ``--version``/``models``/``auth status`` style commands with short timeouts,
checks that auth files *exist* (never reads them) and reads Codex's model metadata cache. Results are cached
in ``config.HOME / "agents.json"`` for 24 hours. Nothing in here raises: problems land in each runner's ``notes``.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .. import config

CACHE_NAME = "agents.json"
CACHE_TTL = 24 * 3600
TIMEOUT = 20  # seconds, every subprocess call
KILO_MODEL_CAP = 40

IS_WINDOWS = sys.platform == "win32"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-+.][0-9A-Za-z.\-]+)?")
_LOG_PREFIX = re.compile(r"^\s*(INFO|WARN|WARNING|DEBUG|ERROR|TRACE)\b")

CLAUDE_MODELS = [
    {"id": "claude-fable-5-2", "label": "Fable 5.2", "tier": "top", "cost": "paid"},
    {"id": "claude-opus-5-5", "label": "Opus 5.5", "tier": "high", "cost": "paid"},
    {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5", "tier": "mid", "cost": "paid"},
    {"id": "claude-haiku-5-5", "label": "Haiku 5.5", "tier": "fast", "cost": "paid"},
]
CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]  # from `claude --help` (--effort)

_NAMES = {"claude": "Claude Code", "codex": "Codex", "kilo": "Kilo"}
_HINTS = {
    "claude": "Install Claude Code (https://claude.com/claude-code) so `claude` is on PATH, then run `claude auth login`.",
    "codex": "Install the Codex desktop app or the Codex CLI (`npm i -g @openai/codex`) and sign in once.",
    "kilo": "Install the Kilo Code VS Code extension (it bundles kilo.exe) or the Kilo CLI so `kilo` is on PATH.",
}

_exe_memo: dict[str, str | None] = {}


# --------------------------------------------------------------------------- helpers

def _home() -> Path:
    return Path(os.path.expanduser("~"))


_SHIM_JS = re.compile(r'"%(?:dp0|~dp0)%\\?([^"%]+?\.(?:js|cjs|mjs))"', re.I)


def launcher_argv(exe: str) -> list[str]:
    """argv prefix that starts ``exe``. npm ``.cmd`` shims are resolved to ``node <script.js>`` so arguments
    never pass through cmd.exe's quoting (which mangles quotes, ``&`` and ``^``); other shims get ``cmd /c``."""
    if not exe.lower().endswith((".cmd", ".bat")):
        return [exe]
    try:
        text = Path(exe).read_text(encoding="utf-8", errors="replace")[:20000]
        m = _SHIM_JS.search(text)
        if m:
            base = os.path.dirname(exe)
            js = os.path.normpath(os.path.join(base, m.group(1).lstrip("\\/")))
            local_node = os.path.join(base, "node.exe")
            node = local_node if os.path.isfile(local_node) else shutil.which("node")
            if node and os.path.isfile(js):
                return [node, js]
    except OSError:
        pass
    return ["cmd", "/c", exe]


def _run(argv: list[str], timeout: float = TIMEOUT, env: dict | None = None) -> tuple[int | None, str, str]:
    """Run a short command. Returns (returncode, stdout, stderr); returncode None on timeout/failure to start."""
    if argv:
        argv = launcher_argv(argv[0]) + list(argv[1:])
    try:
        p = subprocess.run(argv, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout, env=env,
                           creationflags=_NO_WINDOW if IS_WINDOWS else 0)
    except subprocess.TimeoutExpired:
        return None, "", f"timed out after {timeout:g}s"
    except (OSError, ValueError) as e:
        return None, "", str(e)
    dec = lambda b: (b or b"").decode("utf-8", errors="replace")  # noqa: E731
    return p.returncode, dec(p.stdout), dec(p.stderr)


def _clean(text: str) -> list[str]:
    return [_ANSI.sub("", ln).strip() for ln in (text or "").splitlines()]


def parse_version(text: str) -> str | None:
    """First version-looking token, preferring lines that are not log lines (kilo prints an INFO line + banner)."""
    lines = [ln for ln in _clean(text) if ln]
    for ln in [x for x in lines if not _LOG_PREFIX.match(x)] + lines:
        m = _VERSION.search(ln)
        if m:
            return m.group(0)
    return None


def _ver_key(path: str) -> tuple:
    m = re.search(r"-(\d+(?:\.\d+)+)", os.path.basename(os.path.dirname(os.path.dirname(path))) or "")
    if not m:
        m = re.search(r"(\d+(?:\.\d+)+)", path)
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def _newest(paths: list[str]) -> str | None:
    paths = [p for p in paths if os.path.isfile(p)]
    return max(paths, key=_ver_key) if paths else None


def _appx_location(name: str) -> str | None:
    if not IS_WINDOWS:
        return None
    rc, out, _ = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                       f"(Get-AppxPackage -Name '{name}' | Select-Object -First 1).InstallLocation"], timeout=15)
    loc = out.strip() if rc == 0 else ""
    return loc if loc and os.path.isdir(loc) else None


# --------------------------------------------------------------------------- executables

def _candidates(runner: str) -> list[str]:
    home = _home()
    appdata = os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))
    out: list[str] = []
    if runner == "claude":
        out += [str(home / ".local" / "bin" / ("claude.exe" if IS_WINDOWS else "claude")),
                str(home / ".claude" / "local" / ("claude.exe" if IS_WINDOWS else "claude")),
                os.path.join(appdata, "npm", "claude.cmd")]
    elif runner == "kilo":
        exe = "kilo.exe" if IS_WINDOWS else "kilo"
        found = []
        for root in (".vscode", ".vscode-insiders", ".cursor", ".windsurf"):
            found += glob.glob(str(home / root / "extensions" / "kilocode.kilo-code-*" / "bin" / exe))
        newest = _newest(found)
        if newest:
            out.append(newest)
        out += [os.path.join(appdata, "npm", "kilo.cmd"), str(home / ".local" / "bin" / exe)]
    elif runner == "codex":
        out += [os.path.join(appdata, "npm", "codex.cmd"), str(home / ".local" / "bin" / "codex.exe")]
    return out


def find_exe(runner: str) -> str | None:
    """Absolute path of the runner's CLI ("claude" | "codex" | "kilo"), or None. PATH first, then known locations."""
    try:
        memo = _exe_memo.get(runner)
        if memo and os.path.isfile(memo):
            return memo
        path = shutil.which(runner)
        if not path:
            path = next((c for c in _candidates(runner) if os.path.isfile(c)), None)
        if not path and runner == "codex":
            # The Codex desktop app (MSIX) ships the CLI; WindowsApps can't be listed, so ask Appx for the folder.
            loc = _appx_location("OpenAI.Codex")
            if loc:
                c = os.path.join(loc, "app", "resources", "codex.exe")
                path = c if os.path.isfile(c) else None
        path = os.path.abspath(path) if path else None
        _exe_memo[runner] = path
        return path
    except Exception:  # noqa: BLE001 - never raise out of detection
        return None


# --------------------------------------------------------------------------- per-runner probes

def _version(exe: str, notes: list[str]) -> str | None:
    rc, out, err = _run([exe, "--version"])
    if rc is None:
        notes.append(f"`--version` failed: {err}")
        return None
    v = parse_version(out) or parse_version(err)
    if not v:
        notes.append("could not parse the version output")
    return v


def _claude_auth(exe: str, notes: list[str]) -> str:
    from .runners import claude_env  # same env the runner uses (host-session variables removed)
    rc, out, err = _run([exe, "auth", "status", "--json"], env=claude_env(dict(os.environ)))
    try:
        data = json.loads(out[out.find("{"):]) if "{" in out else None
    except ValueError:
        data = None
    if isinstance(data, dict) and "loggedIn" in data:  # only this flag is kept: the payload has account details
        if data.get("loggedIn"):
            notes.append("`claude auth status` reports logged in; an expired OAuth token only shows up on the "
                         "first call (run `claude` once interactively to refresh it).")
            return "ok"
        notes.append("the standalone CLI is not signed in (a Claude Desktop / IDE session's login does not carry "
                     "over to headless runs): run `claude auth login` once in a terminal")
        return "missing"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "ok"
    if rc is None:
        notes.append(f"auth check failed: {err}")
    return "unknown"


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (_home() / ".codex"))


def _codex_auth() -> str:
    if (_codex_home() / "auth.json").exists() or os.environ.get("OPENAI_API_KEY") or os.environ.get("CODEX_API_KEY"):
        return "ok"
    return "missing"


def _tier_codex(i: int, slug: str) -> str:
    s = slug.lower()
    if any(w in s for w in ("luna", "mini", "nano", "lite")):
        return "fast"
    return "top" if i == 0 else "high" if i <= 2 else "mid"


def codex_models(notes: list[str]) -> tuple[list[dict], list[str]]:
    """Model ids from ~/.codex/models_cache.json (model metadata only; account fields are never touched)."""
    path = _codex_home() / "models_cache.json"
    if not path.exists():
        notes.append("no ~/.codex/models_cache.json yet (open Codex once); pass a model id explicitly")
        return [], []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows = data.get("models") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError("no 'models' list")
    except Exception as e:  # noqa: BLE001
        notes.append(f"could not read the Codex model cache: {e}")
        return [], []
    listed = [m for m in rows if isinstance(m, dict) and isinstance(m.get("slug"), str)
              and m.get("visibility", "list") == "list"]
    listed.sort(key=lambda m: m.get("priority") if isinstance(m.get("priority"), int) else 999)
    models, efforts = [], []
    for i, m in enumerate(listed):
        levels = []
        for lv in m.get("supported_reasoning_levels") or []:
            e = lv.get("effort") if isinstance(lv, dict) else lv if isinstance(lv, str) else None
            if isinstance(e, str):
                levels.append(e)
                if e not in efforts:
                    efforts.append(e)
        models.append({"id": m["slug"], "label": str(m.get("display_name") or m["slug"]),
                       "tier": _tier_codex(i, m["slug"]), "cost": "paid", "efforts": levels,
                       "default_effort": m.get("default_reasoning_level")})
    return models, efforts


def _tier_generic(model_id: str) -> str:
    s = model_id.lower()
    if any(w in s for w in ("fable", "opus", "ultra")):
        return "top"
    if any(w in s for w in ("haiku", "flash", "mini", "nano", "lite", "small", "lightning", "1.7b", "fast")):
        return "fast"
    if any(w in s for w in ("sonnet", "-pro", "gpt-6", "max", "large")):
        return "high"
    return "mid"


def parse_kilo_models(text: str, cap: int = KILO_MODEL_CAP) -> list[dict]:
    """`kilo models` prints one provider/model per line (plus log lines/banners). Free models first, capped."""
    seen, free, paid = set(), [], []
    for ln in _clean(text):
        if not ln or _LOG_PREFIX.match(ln) or " " in ln or "/" not in ln:
            continue
        if not re.fullmatch(r"[A-Za-z0-9._~\-]+(/[A-Za-z0-9._~:@+\-]+)+", ln) or ln in seen:
            continue
        seen.add(ln)
        is_free = "free" in ln.lower()
        provider = ln.split("/", 1)[0]
        row = {"id": ln, "label": ln.split("/", 1)[1], "tier": _tier_generic(ln),
               "cost": "free" if is_free else ("paid" if provider == "kilo" else "unknown")}
        (free if is_free else paid).append(row)
    return (free + paid)[:cap]


def _kilo_auth() -> str:
    data = Path(os.environ.get("XDG_DATA_HOME") or (_home() / ".local" / "share")) / "kilo" / "auth.json"
    return "ok" if data.exists() else "unknown"


def _probe(runner: str) -> dict:
    notes: list[str] = []
    rec = {"id": runner, "name": _NAMES[runner], "available": False, "exe": None, "version": None,
           "auth": "unknown", "models": [], "efforts": [], "verified": runner != "codex", "notes": notes}
    try:
        exe = find_exe(runner)
        if not exe:
            notes.append(f"{_NAMES[runner]} CLI not found. {_HINTS[runner]}")
            if runner == "codex":
                rec["models"], rec["efforts"] = codex_models([])
            elif runner == "claude":
                rec["models"], rec["efforts"] = [dict(m) for m in CLAUDE_MODELS], list(CLAUDE_EFFORTS)
            return rec
        rec["exe"] = exe
        rec["version"] = _version(exe, notes)
        rec["available"] = rec["version"] is not None or os.path.isfile(exe)
        if runner == "claude":
            rec["auth"] = _claude_auth(exe, notes)
            rec["models"], rec["efforts"] = [dict(m) for m in CLAUDE_MODELS], list(CLAUDE_EFFORTS)
        elif runner == "codex":
            rec["auth"] = _codex_auth()
            rec["models"], rec["efforts"] = codex_models(notes)
            rec["verified"] = True  # `codex exec --json` checked against this CLI (help + live call)
            if "WindowsApps" in exe:
                notes.append("using the CLI bundled in the Codex desktop app; its path changes when the app updates")
        elif runner == "kilo":
            rec["auth"] = _kilo_auth()
            rc, out, err = _run([exe, "models"])
            if rc is None:
                notes.append(f"`kilo models` failed: {err}")
            else:
                rec["models"] = parse_kilo_models(out)
                if not rec["models"]:
                    notes.append("`kilo models` listed no models")
            notes.append("effort maps to `kilo run --variant` (provider-specific: e.g. minimal, low, high, max)")
    except Exception as e:  # noqa: BLE001
        notes.append(f"probe failed: {e}")
    return rec


# --------------------------------------------------------------------------- apps

def _apps() -> list[dict]:
    home = _home()
    appdata = os.environ.get("APPDATA", str(home / "AppData" / "Roaming"))
    local = os.environ.get("LOCALAPPDATA", str(home / "AppData" / "Local"))
    aliases = os.path.join(local, "Microsoft", "WindowsApps")
    apps = []
    try:
        # Claude Desktop: the MSIX build may see a virtualised copy of %APPDATA%\Claude under its package folder.
        pkg = glob.glob(os.path.join(local, "Packages", "Claude_*"))
        installed = bool(pkg) or os.path.isfile(os.path.join(aliases, "claude-desktop.exe")) \
            or os.path.isdir(os.path.join(local, "AnthropicClaude"))
        cands = [os.path.join(p, "LocalCache", "Roaming", "Claude", "claude_desktop_config.json") for p in pkg]
        default = os.path.join(appdata, "Claude", "claude_desktop_config.json")
        cfg = next((c for c in cands + [default] if os.path.isfile(c)), default if installed else None)
        apps.append({"id": "claude-desktop", "name": "Claude Desktop", "installed": installed, "mcp_config": cfg,
                     "mcp_config_exists": bool(cfg and os.path.isfile(cfg)), "format": "json"})
    except Exception:  # noqa: BLE001
        apps.append({"id": "claude-desktop", "name": "Claude Desktop", "installed": False, "mcp_config": None})
    try:
        installed = bool(glob.glob(os.path.join(aliases, "OpenAI.Codex_*"))
                         or glob.glob(os.path.join(local, "Packages", "OpenAI.Codex_*")))
        cfg = str(_codex_home() / "config.toml")
        apps.append({"id": "codex-desktop", "name": "Codex", "installed": installed,
                     "mcp_config": cfg if (installed or os.path.isfile(cfg)) else None,
                     "mcp_config_exists": os.path.isfile(cfg), "format": "toml"})
    except Exception:  # noqa: BLE001
        apps.append({"id": "codex-desktop", "name": "Codex", "installed": False, "mcp_config": None})
    try:
        ext = []
        for root in (".vscode", ".vscode-insiders"):
            ext += glob.glob(str(home / root / "extensions" / "kilocode.kilo-code-*"))
        cfg_dir = Path(os.environ.get("XDG_CONFIG_HOME") or (home / ".config")) / "kilo"
        cfg = next((str(cfg_dir / n) for n in ("kilo.jsonc", "kilo.json", "opencode.json") if (cfg_dir / n).exists()),
                   str(cfg_dir / "kilo.jsonc"))
        apps.append({"id": "vscode-kilo", "name": "Kilo Code (VS Code)", "installed": bool(ext),
                     "mcp_config": cfg if ext or os.path.isfile(cfg) else None,
                     "mcp_config_exists": os.path.isfile(cfg), "format": "jsonc"})
    except Exception:  # noqa: BLE001
        apps.append({"id": "vscode-kilo", "name": "Kilo Code (VS Code)", "installed": False, "mcp_config": None})
    return apps


# --------------------------------------------------------------------------- public

def _cache_path() -> Path:
    return Path(config.HOME) / CACHE_NAME


def _cache_ok(data) -> bool:
    if not isinstance(data, dict) or not isinstance(data.get("runners"), list):
        return False
    if time.time() - float(data.get("scanned_at") or 0) > CACHE_TTL:
        return False
    # an app update moves the exe (Codex/Kilo versioned folders): rescan when a cached exe vanished
    return all(not r.get("available") or (r.get("exe") and os.path.isfile(r["exe"])) for r in data["runners"])


def detect(refresh: bool = False) -> dict:
    """Runners (claude, codex, kilo) and apps, cached for 24 h in config.HOME/agents.json. Never raises."""
    path = _cache_path()
    if not refresh:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if _cache_ok(data):
                return data
        except Exception:  # noqa: BLE001
            pass
    if refresh:
        _exe_memo.clear()
    data = {"runners": [_probe(r) for r in ("claude", "codex", "kilo")], "apps": _apps(),
            "scanned_at": int(time.time())}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass
    return data
