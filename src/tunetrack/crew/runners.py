"""Launch an agent CLI headless and turn its output stream into normalised events.

Runners (flags checked against each CLI's ``--help`` on Windows, 2026-11):

* ``claude`` - Claude Code 2.1: ``claude -p --output-format stream-json --verbose --permission-mode dontAsk
  --permission-prompts none --strict-mcp-config [--mcp-config <tmp.json>] [--allowedTools a,b]
  [--model M] [--effort E] [--append-system-prompt-file <tmp.md>] [--resume ID]``, prompt on stdin.
  ``dontAsk`` + ``--permission-prompts none`` deny anything not allowed instead of waiting for a prompt.
  Variables of a host Claude Code session (CLAUDECODE, CLAUDE_CODE_SESSION_ID, ...) are removed from the
  child's environment so a crew agent launched from inside Claude Code / Claude Desktop starts as its own session.
* ``codex`` - Codex CLI 0.167 (npm ``codex.cmd`` shim, started as ``node codex.js`` to keep cmd.exe out of the
  quoting, or the codex.exe bundled in the Codex desktop app): ``codex exec [resume ID] --json
  --skip-git-repo-check --ignore-user-config -c approval_policy="never" -c sandbox_mode="read-only"
  [-C cwd] [-m M] [-c model_reasoning_effort=E] [-c developer_instructions=...]
  -c mcp_servers.<name>.command=... -c mcp_servers.<name>.args=[...] [-c mcp_servers.<name>.env={...}]
  -c mcp_servers.<name>.default_tools_approval_mode="approve" -``, prompt on stdin (``-``).
  ``--ignore-user-config`` skips ~/.codex/config.toml so only the given MCP servers load (auth still works).
* ``kilo`` - Kilo CLI 7.8 (opencode based): ``kilo run --format json [--dir cwd] [-m provider/model]
  [--variant E] [-s ID]``, prompt (with the role text prefixed in a ``<role>`` block) on stdin. MCP servers and
  tool permissions go in through the ``KILO_CONFIG_CONTENT`` env var, which Kilo merges over the user's config
  without touching it. Servers in the user's own kilo.jsonc still load (there is no strict flag). ``--auto`` is
  not used: only the allowed tools are pre-approved.
* ``script`` - ``python -u <script>`` with the prompt on stdin; the script prints normalised event JSON lines.
  Used to test the studio without spending tokens.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Iterator

from . import detect

IS_WINDOWS = sys.platform == "win32"
RUNNERS = ("claude", "codex", "kilo", "script")
MAX_ARG_SYSTEM = 16000  # longer role text is prefixed to the prompt (Windows command lines cap at 32k chars)


class RunnerUnavailable(RuntimeError):
    pass


@dataclass
class Launch:
    runner: str                 # "claude" | "codex" | "kilo" | "script"
    prompt: str                 # the task message
    system: str = ""            # role instructions
    model: str | None = None
    effort: str | None = None
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)       # extra env for the agent process
    mcp: dict[str, dict] = field(default_factory=dict)      # name -> {"command": str, "args": [str], "env": {}}
    allow_tools: list[str] = field(default_factory=list)    # e.g. ["mcp__tunetrack"]
    resume: str | None = None   # runner session id to continue
    log: str | None = None      # file to append the raw stream to
    script: str | None = None   # runner == "script": python file to run instead of a model
    # What the agent's own shell/file tools may do where a CLI has them (codex): crew agents work through MCP
    # tools only, so the default is to let those tools read and nothing else.
    sandbox: str = "read-only"


@dataclass
class Command:
    argv: list[str]
    env: dict[str, str]
    stdin: str | None = None
    cleanup: list[str] = field(default_factory=list)
    cwd: str | None = None


# --------------------------------------------------------------------------- env / small helpers

# user-level Claude Code settings that are fine (or needed) to pass on to a child session
_CLAUDE_KEEP = {
    "CLAUDE_CODE_GIT_BASH_PATH", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "CLAUDE_CODE_API_KEY_HELPER_TTL_MS",
}


def claude_env(env: dict[str, str]) -> dict[str, str]:
    """Drop the variables a host Claude Code session sets for its own children (session ids, host auth refresh,
    SDK switches). Without this a nested `claude -p` believes it is the host's child session."""
    if "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env:
        return env
    out = {}
    for k, v in env.items():
        ku = k.upper()
        if ku == "CLAUDECODE" or ku in ("CLAUDE_PID", "CLAUDE_EFFORT") or ku.startswith(("CLAUDE_AGENT_SDK_",
                                                                                        "CLAUDE_PREVIEW_")):
            continue
        if ku.startswith("CLAUDE_CODE_") and ku not in _CLAUDE_KEEP:
            continue
        out[k] = v
    return out


def _base_env(launch: Launch, scrub_claude: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    if scrub_claude:
        env = claude_env(env)
    env.update({str(k): str(v) for k, v in (launch.env or {}).items()})
    return env


def _tmp(suffix: str, text: str) -> str:
    fd, path = tempfile.mkstemp(prefix="tunetrack-crew-", suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def _require(runner: str) -> str:
    exe = detect.find_exe(runner)
    if not exe:
        raise RunnerUnavailable(f"{runner} CLI not found (run tunetrack.crew.detect(refresh=True) for hints)")
    return exe


def _exe_argv(exe: str) -> list[str]:
    return detect.launcher_argv(exe)


def _server(spec: dict) -> tuple[str, list[str], dict[str, str]]:
    return (str(spec.get("command") or ""), [str(a) for a in spec.get("args") or []],
            {str(k): str(v) for k, v in (spec.get("env") or {}).items()})


def _with_system(launch: Launch) -> str:
    if not launch.system:
        return launch.prompt
    return f"<role>\n{launch.system.strip()}\n</role>\n\n{launch.prompt}"


def toml_str(s: str) -> str:
    """A TOML basic string (JSON escapes are not all valid TOML: no surrogate pairs, DEL must be escaped)."""
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\r":
            out.append("\\r")
        elif o < 0x20 or o == 0x7F:
            out.append(f"\\u{o:04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toml_key(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name) or "server"


# --------------------------------------------------------------------------- build

def _build_claude(launch: Launch) -> Command:
    exe = _require("claude")
    cleanup: list[str] = []
    argv = _exe_argv(exe) + ["-p", "--output-format", "stream-json", "--verbose",
                             "--permission-mode", "dontAsk", "--permission-prompts", "none", "--strict-mcp-config"]
    if launch.mcp:
        servers = {}
        for name, spec in launch.mcp.items():
            cmd, args, env = _server(spec)
            servers[name] = {"type": "stdio", "command": cmd, "args": args, "env": env}
        path = _tmp(".json", json.dumps({"mcpServers": servers}, indent=2))
        cleanup.append(path)
        argv += ["--mcp-config", path]
    if launch.allow_tools:
        argv += ["--allowedTools", ",".join(launch.allow_tools)]
    if launch.model:
        argv += ["--model", launch.model]
    if launch.effort:
        argv += ["--effort", launch.effort]
    if launch.system:
        path = _tmp(".md", launch.system)
        cleanup.append(path)
        argv += ["--append-system-prompt-file", path]
    if launch.resume:
        argv += ["--resume", launch.resume]
    return Command(argv, _base_env(launch, scrub_claude=True), launch.prompt, cleanup, launch.cwd)


def _build_codex(launch: Launch) -> Command:
    exe = _require("codex")
    argv = _exe_argv(exe) + ["exec"]
    if launch.resume:
        argv.append("resume")  # `exec resume` has no -C/--sandbox: cwd comes from Popen, sandbox via -c
    argv += ["--json", "--skip-git-repo-check", "--ignore-user-config",
             "-c", 'approval_policy="never"', "-c", f'sandbox_mode="{launch.sandbox}"']
    if launch.cwd and not launch.resume:
        argv += ["-C", launch.cwd]
    if launch.model:
        argv += ["-m", launch.model]
    if launch.effort:
        argv += ["-c", f"model_reasoning_effort={toml_str(launch.effort)}"]
    prompt = launch.prompt
    if launch.system:
        if len(launch.system) <= MAX_ARG_SYSTEM:
            argv += ["-c", f"developer_instructions={toml_str(launch.system)}"]
        else:
            prompt = _with_system(launch)
    for name, spec in launch.mcp.items():
        cmd, args, env = _server(spec)
        key = f"mcp_servers.{_toml_key(name)}"
        argv += ["-c", f"{key}.command={toml_str(cmd)}",
                 "-c", f"{key}.args=[{', '.join(toml_str(a) for a in args)}]"]
        if env:
            argv += ["-c", f"{key}.env={{{', '.join(f'{toml_str(k)} = {toml_str(v)}' for k, v in env.items())}}}"]
        argv += ["-c", f'{key}.default_tools_approval_mode="approve"']
    if launch.resume:
        argv.append(launch.resume)
    argv.append("-")  # read the prompt from stdin
    return Command(argv, _base_env(launch), prompt, [], launch.cwd)


def kilo_permission_key(tool: str) -> str:
    """Claude-style tool names -> Kilo/opencode permission patterns (MCP tools are named <server>_<tool>)."""
    m = re.fullmatch(r"mcp__([^_].*?)(?:__(.+))?", tool)
    if m:
        return f"{m.group(1)}_{m.group(2)}" if m.group(2) else f"{m.group(1)}_*"
    return tool


def _build_kilo(launch: Launch) -> Command:
    exe = _require("kilo")
    cleanup: list[str] = []
    argv = _exe_argv(exe) + ["run", "--format", "json"]
    if launch.cwd:
        argv += ["--dir", launch.cwd]
    if launch.model:
        argv += ["-m", launch.model]
    if launch.effort:
        argv += ["--variant", launch.effort]
    if launch.resume:
        argv += ["-s", launch.resume]
    cfg: dict = {}
    if launch.mcp:
        cfg["mcp"] = {}
        for name, spec in launch.mcp.items():
            cmd, args, env = _server(spec)
            entry = {"type": "local", "command": [cmd, *args], "enabled": True}
            if env:
                entry["environment"] = env
            cfg["mcp"][name] = entry
    if launch.allow_tools:
        # Everything a crew agent does goes through the allowed (MCP) tools. "*": "deny" comes first (the last
        # matching rule wins) and takes Kilo's own tools off the table: shell, file edits, web, and its subagent
        # tool `task`, which a live Producer kept calling when it meant the studio's task_update.
        cfg["permission"] = {"*": "deny", "bash": "deny", "edit": "deny", "webfetch": "deny",
                             **{kilo_permission_key(t): "allow" for t in launch.allow_tools}}
    # Role text is prefixed to the prompt. (Kilo accepts `instructions: [file]` in KILO_CONFIG_CONTENT and
    # resolves it in `kilo debug config`, but a live run did not follow it, so the prompt is the reliable path.)
    env = _base_env(launch)
    if cfg:
        if env.get("KILO_CONFIG_CONTENT"):  # caller supplied one: merge ours on top (shallow per section)
            try:
                base = json.loads(env["KILO_CONFIG_CONTENT"])
                for k, v in cfg.items():
                    if isinstance(v, dict) and isinstance(base.get(k), dict):
                        base[k] = {**base[k], **v}
                    elif isinstance(v, list) and isinstance(base.get(k), list):
                        base[k] = base[k] + v
                    else:
                        base[k] = v
                cfg = base
            except ValueError:
                pass
        env["KILO_CONFIG_CONTENT"] = json.dumps(cfg)
    return Command(argv, env, _with_system(launch), cleanup, launch.cwd)


def _build_script(launch: Launch) -> Command:
    if not launch.script:
        raise ValueError("runner 'script' needs Launch.script")
    script = os.path.abspath(launch.script)
    if not os.path.isfile(script):
        raise FileNotFoundError(script)
    env = _base_env(launch)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["PYTHONUNBUFFERED"] = "1"
    env["TUNETRACK_CREW_SYSTEM"] = launch.system or ""
    if launch.model:
        env["TUNETRACK_CREW_MODEL"] = launch.model
    if launch.resume:
        env["TUNETRACK_CREW_RESUME"] = launch.resume
    if launch.mcp:
        env["TUNETRACK_CREW_MCP"] = json.dumps(launch.mcp)
    return Command([sys.executable, "-u", script], env, launch.prompt, [], launch.cwd)


_BUILDERS = {"claude": _build_claude, "codex": _build_codex, "kilo": _build_kilo, "script": _build_script}


def build(launch: Launch) -> Command:
    """The argv/env/stdin to run ``launch``. Temp files it creates are listed in ``Command.cleanup``."""
    try:
        builder = _BUILDERS[launch.runner]
    except KeyError:
        raise ValueError(f"unknown runner {launch.runner!r}; expected one of {RUNNERS}") from None
    return builder(launch)


# --------------------------------------------------------------------------- normalisers

def _mcp_name(server: str, tool: str) -> str:
    return f"mcp__{server}__{tool}"


class _Normaliser:
    """Turns one parsed JSON line into normalised events. ``ok``/``result`` feed the final done event."""

    def __init__(self, mcp_names=()):
        self.mcp_names = sorted(mcp_names, key=len, reverse=True)
        self.session: str | None = None
        self.ok: bool | None = None
        self.result = ""

    def _session(self, sid) -> list[dict]:
        if isinstance(sid, str) and sid and sid != self.session:
            self.session = sid
            return [{"type": "session", "id": sid}]
        return []

    def feed(self, obj: dict) -> list[dict]:  # pragma: no cover - overridden
        return []

    def finish(self) -> list[dict]:
        return []


def _int(x) -> int:
    return int(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else 0


class ClaudeNormaliser(_Normaliser):
    def __init__(self, mcp_names=()):
        super().__init__(mcp_names)
        self.tools: dict[str, str] = {}
        self.errored = False

    def feed(self, o: dict) -> list[dict]:
        t = o.get("type")
        ev = self._session(o.get("session_id"))
        msg = o.get("message") if isinstance(o.get("message"), dict) else {}
        content = msg.get("content") if isinstance(msg.get("content"), list) else []
        if t == "assistant":
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text" and c.get("text"):
                    if o.get("is_api_error_message") or o.get("error"):
                        self.errored = True
                        ev.append({"type": "error", "message": c["text"]})
                    else:
                        ev.append({"type": "text", "text": c["text"]})
                elif c.get("type") == "tool_use":
                    name = str(c.get("name") or "")
                    self.tools[str(c.get("id"))] = name
                    ev.append({"type": "tool", "name": name, "input": c.get("input") or {}, "id": c.get("id")})
        elif t == "user":
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    tid = str(c.get("tool_use_id"))
                    ev.append({"type": "tool_result", "name": self.tools.get(tid, ""), "ok": not c.get("is_error"),
                               "id": c.get("tool_use_id")})
        elif t == "result":
            self.result = o.get("result") if isinstance(o.get("result"), str) else ""
            self.ok = not o.get("is_error") and o.get("subtype", "success") == "success"
            u = o.get("usage") if isinstance(o.get("usage"), dict) else {}
            cached = _int(u.get("cache_read_input_tokens")) + _int(u.get("cache_creation_input_tokens"))
            cost = o.get("total_cost_usd")
            ev.append({"type": "usage", "input_tokens": _int(u.get("input_tokens")) + cached,
                       "output_tokens": _int(u.get("output_tokens")), "cached_tokens": cached,
                       "cost_usd": float(cost) if isinstance(cost, (int, float)) else None})
            if not self.ok and not self.errored:
                ev.append({"type": "error", "message": self.result or str(o.get("subtype") or "error")})
        return ev


class CodexNormaliser(_Normaliser):
    def feed(self, o: dict) -> list[dict]:
        t = o.get("type")
        if t == "thread.started":
            return self._session(o.get("thread_id"))
        if t == "turn.completed":
            u = o.get("usage") if isinstance(o.get("usage"), dict) else {}
            if self.ok is None:
                self.ok = True
            return [{"type": "usage", "input_tokens": _int(u.get("input_tokens")),
                     "output_tokens": _int(u.get("output_tokens")), "cached_tokens": _int(u.get("cached_input_tokens")),
                     "cost_usd": None}]
        if t in ("turn.failed", "error"):
            self.ok = False
            err = o.get("error")
            msg = err.get("message") if isinstance(err, dict) else o.get("message") or err
            return [{"type": "error", "message": str(msg or t)}]
        item = o.get("item") if isinstance(o.get("item"), dict) else None
        if not item or t not in ("item.started", "item.completed"):
            return []
        it = item.get("type")
        if it == "agent_message":
            if t == "item.completed" and item.get("text"):
                self.result = item["text"]
                return [{"type": "text", "text": item["text"]}]
            return []
        if it == "mcp_tool_call":
            name = _mcp_name(str(item.get("server") or ""), str(item.get("tool") or ""))
            if t == "item.started":
                return [{"type": "tool", "name": name, "input": item.get("arguments") or {}, "id": item.get("id")}]
            res = item.get("result") if isinstance(item.get("result"), dict) else {}
            ok = not item.get("error") and item.get("status") not in ("failed", "error") and not res.get("isError")
            return [{"type": "tool_result", "name": name, "ok": bool(ok), "id": item.get("id")}]
        if it == "command_execution":
            if t == "item.started":
                return [{"type": "tool", "name": "shell", "input": {"command": item.get("command")},
                         "id": item.get("id")}]
            code = item.get("exit_code")
            return [{"type": "tool_result", "name": "shell", "id": item.get("id"),
                     "ok": item.get("status") not in ("failed", "declined") and (code in (0, None))}]
        if it in ("file_change", "web_search") and t == "item.completed":
            return [{"type": "tool_result", "name": it, "id": item.get("id"),
                     "ok": item.get("status") not in ("failed", "declined")}]
        if it == "error" and t == "item.completed":
            return [{"type": "error", "message": str(item.get("message") or "error")}]
        return []


class KiloNormaliser(_Normaliser):
    def __init__(self, mcp_names=()):
        super().__init__(mcp_names)
        self.usage = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cost_usd": 0.0}
        self.steps = 0
        self.last_msg: str | None = None
        self.texts: list[str] = []

    def tool_name(self, tool: str) -> str:
        for server in self.mcp_names:
            if tool.startswith(server + "_"):
                return _mcp_name(server, tool[len(server) + 1:])
        return tool

    def feed(self, o: dict) -> list[dict]:
        t = o.get("type")
        ev = self._session(o.get("sessionID"))
        part = o.get("part") if isinstance(o.get("part"), dict) else {}
        if t == "text":
            text = part.get("text") if isinstance(part.get("text"), str) else ""
            if part.get("messageID") != self.last_msg:
                self.last_msg, self.texts = part.get("messageID"), []
            if text.strip():
                self.texts.append(text.strip())
                ev.append({"type": "text", "text": text.strip()})
        elif t == "tool_use":
            st = part.get("state") if isinstance(part.get("state"), dict) else {}
            name = self.tool_name(str(part.get("tool") or ""))
            ev.append({"type": "tool", "name": name, "input": st.get("input") or {}, "id": part.get("callID")})
            if st.get("status") in ("completed", "error"):
                ev.append({"type": "tool_result", "name": name, "ok": st.get("status") == "completed",
                           "id": part.get("callID")})
        elif t == "step_finish":
            self.steps += 1
            tok = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
            cached = _int(cache.get("read")) + _int(cache.get("write"))
            self.usage["input_tokens"] += _int(tok.get("input")) + cached
            self.usage["output_tokens"] += _int(tok.get("output")) + _int(tok.get("reasoning"))
            self.usage["cached_tokens"] += cached
            if isinstance(part.get("cost"), (int, float)):
                self.usage["cost_usd"] += float(part["cost"])
            if part.get("reason") in ("stop", "end_turn", "length") and self.ok is None:
                self.ok = True
        elif t == "error":
            self.ok = False
            err = o.get("error") if isinstance(o.get("error"), dict) else {}
            data = err.get("data") if isinstance(err.get("data"), dict) else {}
            ev.append({"type": "error",
                       "message": str(data.get("message") or err.get("message") or err.get("name") or "error")})
        return ev

    def finish(self) -> list[dict]:
        self.result = "\n\n".join(self.texts)
        return [{"type": "usage", **self.usage}] if self.steps else []


class ScriptNormaliser(_Normaliser):
    """Pass-through: the script already prints normalised events. Its ``done`` is held for the final event."""

    def __init__(self, mcp_names=()):
        super().__init__(mcp_names)
        self.done: dict | None = None

    def feed(self, o: dict) -> list[dict]:
        t = o.get("type")
        if not isinstance(t, str) or not t:
            return []
        if t == "done":
            self.done = o
            self.ok = bool(o.get("ok", True))
            self.result = o.get("result") if isinstance(o.get("result"), str) else ""
            return []
        if t == "session" and isinstance(o.get("id"), str):
            self.session = o["id"]
        return [o]


_NORMALISERS = {"claude": ClaudeNormaliser, "codex": CodexNormaliser, "kilo": KiloNormaliser,
                "script": ScriptNormaliser}


def normaliser(runner: str, mcp_names=()) -> _Normaliser:
    return _NORMALISERS[runner](mcp_names)


def parse_line(line: str):
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


# --------------------------------------------------------------------------- run

def _kill_tree(pid: int) -> None:
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass


class Run:
    """A running agent. Iterate ``events()`` (once) to drive it; ``stop()`` kills the whole process tree."""

    def __init__(self, launch: Launch, command: Command):
        self.launch = launch
        self.command = command
        self._norm = normaliser(launch.runner, launch.mcp.keys())
        self._log_lock = threading.Lock()
        self._log = open(launch.log, "a", encoding="utf-8", errors="replace") if launch.log else None
        self._stopped = False
        self._consumed = False
        self._cleaned = False
        self.stderr_tail: list[str] = []
        flags, extra = 0, {}
        if IS_WINDOWS:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            extra["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(
                command.argv, env=command.env, cwd=command.cwd or None,
                stdin=subprocess.PIPE if command.stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=flags, **extra)
        except BaseException:
            self._cleanup()
            if self._log:
                self._log.close()
            raise
        self.pid = self.proc.pid
        self._err_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._err_thread.start()
        self._in_thread = None
        if command.stdin is not None:
            self._in_thread = threading.Thread(target=self._feed_stdin, args=(command.stdin,), daemon=True)
            self._in_thread.start()

    # -- plumbing
    @property
    def returncode(self) -> int | None:
        return self.proc.poll()

    @property
    def session(self) -> str | None:
        return self._norm.session

    def _write_log(self, text: str) -> None:
        if self._log:
            with self._log_lock:
                try:
                    self._log.write(text if text.endswith("\n") else text + "\n")
                    self._log.flush()
                except (OSError, ValueError):
                    pass

    def _feed_stdin(self, text: str) -> None:
        try:
            self.proc.stdin.write(text.encode("utf-8"))
        except (OSError, ValueError):
            pass
        finally:
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass

    def _drain_stderr(self) -> None:
        try:
            for raw in iter(self.proc.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                self._write_log("[stderr] " + line)
                self.stderr_tail = (self.stderr_tail + [line])[-20:]
        except (OSError, ValueError):
            pass

    def _cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        for p in self.command.cleanup:
            try:
                os.remove(p)
            except OSError:
                pass

    # -- public
    def events(self) -> Iterator[dict]:
        """Blocks; yields normalised events until the process ends, then exactly one ``done``."""
        if self._consumed:
            raise RuntimeError("Run.events() can only be iterated once")
        self._consumed = True
        try:
            for raw in iter(self.proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                self._write_log(line)
                obj = parse_line(line)
                if obj is None:
                    continue
                try:
                    out = self._norm.feed(obj)
                except Exception as e:  # noqa: BLE001 - a weird line must never kill the stream
                    out = [{"type": "error", "message": f"could not parse an event: {e}"}]
                yield from out
        except (OSError, ValueError):
            pass
        rc = self.proc.wait()
        self._err_thread.join(timeout=5)
        if self._in_thread:
            self._in_thread.join(timeout=5)
        try:
            yield from self._norm.finish()
        except Exception:  # noqa: BLE001
            pass
        ok = bool(self._norm.ok if self._norm.ok is not None else True) and rc == 0 and not self._stopped
        if rc != 0 and self._norm.ok is None and not self._stopped and self.stderr_tail:
            yield {"type": "error", "message": next((ln for ln in reversed(self.stderr_tail) if ln.strip()), "")}
        done = {"type": "done", "ok": ok, "returncode": rc, "result": self._norm.result or ""}
        if self._stopped:
            done["stopped"] = True
        self._write_log(json.dumps({"tunetrack_crew": done}))
        self._close()
        yield done

    def wait(self) -> dict:
        """Consume the stream and return the final done event."""
        last = {}
        for ev in self.events():
            last = ev
        return last

    def stop(self, timeout: float = 5.0) -> None:
        """Terminate the process and all its children."""
        if self.proc.poll() is None:
            self._stopped = True
            _kill_tree(self.proc.pid)
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass
        if not self._consumed:
            self._close()

    def _close(self) -> None:
        self._cleanup()
        for s in (self.proc.stdout, self.proc.stderr):
            try:
                if s:
                    s.close()
            except OSError:
                pass
        if self._log:
            with self._log_lock:
                try:
                    self._log.close()
                except OSError:
                    pass
            self._log = None

    def __del__(self):  # best effort: never leave temp config files behind
        try:
            self._cleanup()
        except Exception:  # noqa: BLE001
            pass


def start(launch: Launch) -> Run:
    """Build and launch. Raises RunnerUnavailable when the CLI is missing."""
    return Run(launch, build(launch))
