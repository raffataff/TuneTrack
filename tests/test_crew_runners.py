"""tunetrack.crew.runners: argv building per CLI, event normalisation from recorded lines, script runner end-to-end.

No real model is called and nothing depends on which CLIs are installed (detect.find_exe is monkeypatched)."""
import json
import os
import sys
import textwrap
import time

import pytest

from tunetrack.crew import detect, runners
from tunetrack.crew.runners import Launch, build, start

MCP = {"tunetrack": {"command": r"C:\TT\.venv\Scripts\tunetrack-mcp.exe", "args": ["--project", "x y"],
                     "env": {"TT_ROLE": "mix"}}}


@pytest.fixture
def fake_exes(monkeypatch, tmp_path):
    paths = {r: str(tmp_path / f"{r}.exe") for r in ("claude", "codex", "kilo")}
    monkeypatch.setattr(detect, "find_exe", lambda r: paths.get(r))
    return paths


def _cleanup(cmd):
    for p in cmd.cleanup:
        if os.path.exists(p):
            os.remove(p)


# --------------------------------------------------------------------------- build: claude

def test_build_claude_full(fake_exes, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "host-session")
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", r"C:\Git\bin\bash.exe")
    prompt = 'Write the "bridge" & keep it <8 bars>\nline two'
    cmd = build(Launch("claude", prompt, system="You are the mix engineer.", model="claude-haiku-5-5",
                       effort="low", cwd="C:/proj", env={"FOO": "bar"}, mcp=MCP,
                       allow_tools=["mcp__tunetrack", "Read"], resume="sess-1"))
    try:
        a = cmd.argv
        assert a[0] == fake_exes["claude"]
        assert a[1:5] == ["-p", "--output-format", "stream-json", "--verbose"]
        assert a[a.index("--permission-mode") + 1] == "dontAsk"
        assert a[a.index("--permission-prompts") + 1] == "none"
        assert "--strict-mcp-config" in a
        assert a[a.index("--allowedTools") + 1] == "mcp__tunetrack,Read"
        assert a[a.index("--model") + 1] == "claude-haiku-5-5"
        assert a[a.index("--effort") + 1] == "low"
        assert a[a.index("--resume") + 1] == "sess-1"
        assert cmd.stdin == prompt and prompt not in a  # prompt goes through stdin only
        cfg = json.load(open(a[a.index("--mcp-config") + 1], encoding="utf-8"))
        assert cfg == {"mcpServers": {"tunetrack": {"type": "stdio", "command": MCP["tunetrack"]["command"],
                                                    "args": ["--project", "x y"], "env": {"TT_ROLE": "mix"}}}}
        sysfile = a[a.index("--append-system-prompt-file") + 1]
        assert open(sysfile, encoding="utf-8").read() == "You are the mix engineer."
        assert set(cmd.cleanup) == {sysfile, a[a.index("--mcp-config") + 1]}
        assert cmd.cwd == "C:/proj" and cmd.env["FOO"] == "bar"
        # the host Claude Code session's variables don't leak into the child; user settings do
        assert "CLAUDECODE" not in cmd.env and "CLAUDE_CODE_SESSION_ID" not in cmd.env
        assert cmd.env["CLAUDE_CODE_GIT_BASH_PATH"] == r"C:\Git\bin\bash.exe"
    finally:
        _cleanup(cmd)


def test_build_claude_minimal_is_still_strict(fake_exes):
    cmd = build(Launch("claude", "hi"))
    assert "--strict-mcp-config" in cmd.argv and "--mcp-config" not in cmd.argv
    assert "--append-system-prompt-file" not in cmd.argv and "--resume" not in cmd.argv
    assert cmd.cleanup == []


def test_missing_cli_raises(monkeypatch):
    monkeypatch.setattr(detect, "find_exe", lambda r: None)
    for r in ("claude", "codex", "kilo"):
        with pytest.raises(runners.RunnerUnavailable):
            build(Launch(r, "hi"))
    with pytest.raises(ValueError):
        build(Launch("gpt", "hi"))


def test_cmd_shim_goes_through_node(tmp_path, monkeypatch):
    npm = tmp_path / "npm"
    js = npm / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
    js.parent.mkdir(parents=True)
    js.write_text("// stub")
    (npm / "node.exe").write_text("")
    shim = npm / "codex.cmd"
    shim.write_text('@ECHO off\r\n"%_prog%"  "%dp0%\\node_modules\\@openai\\codex\\bin\\codex.js" %*\r\n')
    assert detect.launcher_argv(str(shim)) == [str(npm / "node.exe"), str(js)]
    other = tmp_path / "tool.cmd"
    other.write_text("@echo off\r\necho hi\r\n")
    assert detect.launcher_argv(str(other)) == ["cmd", "/c", str(other)]
    assert detect.launcher_argv(r"C:\x\kilo.exe") == [r"C:\x\kilo.exe"]


# --------------------------------------------------------------------------- build: codex

def test_build_codex(fake_exes):
    cmd = build(Launch("codex", "the task", system='Role: "mixer"\nbe brief', model="gpt-5.6-luna", effort="low",
                       cwd=r"C:\proj", mcp=MCP))
    a = cmd.argv
    assert a[:3] == [fake_exes["codex"], "exec", "--json"]
    assert "--skip-git-repo-check" in a and "--ignore-user-config" in a
    cfgs = [a[i + 1] for i, x in enumerate(a) if x == "-c"]
    assert 'approval_policy="never"' in cfgs and 'sandbox_mode="read-only"' in cfgs
    assert 'model_reasoning_effort="low"' in cfgs
    assert 'developer_instructions="Role: \\"mixer\\"\\nbe brief"' in cfgs
    assert 'mcp_servers.tunetrack.command="C:\\\\TT\\\\.venv\\\\Scripts\\\\tunetrack-mcp.exe"' in cfgs
    assert 'mcp_servers.tunetrack.args=["--project", "x y"]' in cfgs
    assert 'mcp_servers.tunetrack.env={"TT_ROLE" = "mix"}' in cfgs
    assert 'mcp_servers.tunetrack.default_tools_approval_mode="approve"' in cfgs
    assert a[a.index("-C") + 1] == r"C:\proj" and a[a.index("-m") + 1] == "gpt-5.6-luna"
    assert a[-1] == "-" and cmd.stdin == "the task"


def test_build_codex_resume_and_long_system(fake_exes):
    long_role = "x" * (runners.MAX_ARG_SYSTEM + 1)
    cmd = build(Launch("codex", "go on", system=long_role, cwd="C:/p", resume="0199-abc"))
    a = cmd.argv
    assert a[1:3] == ["exec", "resume"]
    assert "-C" not in a  # `exec resume` has no --cd; Popen cwd is used instead
    assert a[-2:] == ["0199-abc", "-"] and cmd.cwd == "C:/p"
    assert not any(x.startswith("developer_instructions=") for x in a)
    assert cmd.stdin.startswith("<role>\n" + long_role) and cmd.stdin.endswith("go on")


def test_toml_str_edge_cases():
    import tomllib
    for s in ['plain', 'quote " and \\ backslash', "tab\tnew\nline", "emoji \U0001F3B5 é", "del\x7f ctl\x01"]:
        assert tomllib.loads(f"v = {runners.toml_str(s)}")["v"] == s


# --------------------------------------------------------------------------- build: kilo

def test_build_kilo(fake_exes, monkeypatch):
    monkeypatch.delenv("KILO_CONFIG_CONTENT", raising=False)
    cmd = build(Launch("kilo", "the task", system="You are the composer.", model="kilo/kilo-auto/free",
                       effort="high", cwd=r"C:\proj", mcp=MCP, allow_tools=["mcp__tunetrack", "mcp__x__y", "read"],
                       resume="ses_1"))
    try:
        a = cmd.argv
        assert a[:4] == [fake_exes["kilo"], "run", "--format", "json"]
        assert a[a.index("--dir") + 1] == r"C:\proj" and a[a.index("-m") + 1] == "kilo/kilo-auto/free"
        assert a[a.index("--variant") + 1] == "high" and a[a.index("-s") + 1] == "ses_1"
        assert "--auto" not in a
        assert cmd.stdin == "<role>\nYou are the composer.\n</role>\n\nthe task"
        cfg = json.loads(cmd.env["KILO_CONFIG_CONTENT"])
        assert cfg["mcp"] == {"tunetrack": {"type": "local", "enabled": True, "environment": {"TT_ROLE": "mix"},
                                            "command": [MCP["tunetrack"]["command"], "--project", "x y"]}}
        assert cfg["permission"] == {"*": "deny", "bash": "deny", "edit": "deny", "webfetch": "deny",
                                     "tunetrack_*": "allow", "x_y": "allow", "read": "allow"}
        assert list(cfg["permission"])[0] == "*"  # the last matching rule wins, so the blanket deny goes first
        assert "instructions" not in cfg and cmd.cleanup == []
    finally:
        _cleanup(cmd)


def test_build_kilo_merges_caller_config(fake_exes):
    cmd = build(Launch("kilo", "t", mcp=MCP, env={"KILO_CONFIG_CONTENT": json.dumps(
        {"mcp": {"other": {"type": "local", "command": ["x"]}}, "model": "kilo/a"})}))
    cfg = json.loads(cmd.env["KILO_CONFIG_CONTENT"])
    assert set(cfg["mcp"]) == {"other", "tunetrack"} and cfg["model"] == "kilo/a"


# --------------------------------------------------------------------------- normalisation (recorded lines)

# Real lines from `claude -p --output-format stream-json --verbose` (2.1.295), ids/paths replaced. That call hit an
# expired login, so the tool_use/tool_result pair below is written in the same documented message shape.
CLAUDE_LINES = [
    '{"type":"system","subtype":"init","cwd":"<CWD>","session_id":"<SID>","tools":["Read","mcp__tunetrack__status"],'
    '"mcp_servers":[{"name":"tunetrack","status":"connected","source":"dynamic"}],"model":"claude-haiku-5-5",'
    '"permissionMode":"dontAsk","apiKeySource":"none"}',
    '{"type":"assistant","message":{"id":"m1","role":"assistant","content":[{"type":"text","text":"Checking."},'
    '{"type":"tool_use","id":"toolu_1","name":"mcp__tunetrack__status","input":{}}]},"session_id":"<SID>"}',
    '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_1",'
    '"content":"{}","is_error":false}]},"session_id":"<SID>"}',
    '{"type":"stream_event","event":{"type":"content_block_delta"},"session_id":"<SID>"}',
    '{"type":"assistant","message":{"id":"m2","role":"assistant","content":[{"type":"text","text":"OK"}]},'
    '"session_id":"<SID>"}',
    '{"type":"result","subtype":"success","is_error":false,"result":"OK","session_id":"<SID>",'
    '"total_cost_usd":0.0123,"usage":{"input_tokens":10,"cache_creation_input_tokens":5,'
    '"cache_read_input_tokens":100,"output_tokens":3}}',
]
CLAUDE_AUTH_FAIL = [
    CLAUDE_LINES[0],
    '{"type":"assistant","message":{"model":"<synthetic>","role":"assistant","content":[{"type":"text",'
    '"text":"Failed to authenticate: OAuth session expired and could not be refreshed"}]},"session_id":"<SID>",'
    '"error":"authentication_failed","is_api_error_message":true}',
    '{"type":"result","subtype":"success","is_error":true,"result":"Failed to authenticate: OAuth session expired '
    'and could not be refreshed","session_id":"<SID>","total_cost_usd":0,"terminal_reason":"api_error",'
    '"usage":{"input_tokens":0,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":0}}',
]

# Real lines from `codex exec --json` (codex-cli 0.167) calling the tunetrack MCP server; ids/output shortened.
CODEX_LINES = [
    '{"type":"thread.started","thread_id":"<THREAD>"}',
    '{"type":"turn.started"}',
    '{"type":"item.started","item":{"id":"item_0","type":"mcp_tool_call","server":"tunetrack","tool":"status",'
    '"arguments":{},"result":null,"error":null,"status":"in_progress"}}',
    '{"type":"item.completed","item":{"id":"item_0","type":"mcp_tool_call","server":"tunetrack","tool":"status",'
    '"arguments":{},"result":{"content":[{"type":"text","text":"{...}"}]},"error":null,"status":"completed"}}',
    '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"OK"}}',
    '{"type":"turn.completed","usage":{"input_tokens":58649,"cached_input_tokens":45952,'
    '"cache_write_input_tokens":0,"output_tokens":118,"reasoning_output_tokens":45}}',
]

# Real lines from `kilo run --format json` (7.8.27, free model) calling the tunetrack MCP server; ids shortened.
KILO_LINES = [
    '{"type":"step_start","timestamp":1,"sessionID":"<SES>","part":{"id":"p1","sessionID":"<SES>",'
    '"messageID":"msg_1","type":"step-start"}}',
    '{"type":"tool_use","timestamp":2,"sessionID":"<SES>","part":{"id":"p2","sessionID":"<SES>","messageID":"msg_1",'
    '"type":"tool","callID":"call_1","tool":"tunetrack_status","state":{"status":"completed","input":{},'
    '"output":"{...}","title":"","metadata":{"truncated":false},"time":{"start":1,"end":2}}}}',
    '{"type":"text","timestamp":3,"sessionID":"<SES>","part":{"id":"p3","sessionID":"<SES>","messageID":"msg_1",'
    '"type":"text","text":"\\n\\n\\n","time":{"start":1,"end":2}}}',
    '{"type":"step_finish","timestamp":4,"sessionID":"<SES>","part":{"id":"p4","sessionID":"<SES>",'
    '"messageID":"msg_1","type":"step-finish","reason":"tool-calls","model":{"providerID":"kilo",'
    '"modelID":"dots-studio/dots-3-note-preview:free"},"cost":0,"tokens":{"total":42000,"input":40000,"output":5,'
    '"reasoning":30,"cache":{"read":2000,"write":0}}}}',
    '{"type":"step_start","timestamp":5,"sessionID":"<SES>","part":{"id":"p5","sessionID":"<SES>",'
    '"messageID":"msg_2","type":"step-start"}}',
    '{"type":"text","timestamp":6,"sessionID":"<SES>","part":{"id":"p6","sessionID":"<SES>","messageID":"msg_2",'
    '"type":"text","text":"\\n\\nOK","time":{"start":5,"end":6}}}',
    '{"type":"step_finish","timestamp":7,"sessionID":"<SES>","part":{"id":"p7","sessionID":"<SES>",'
    '"messageID":"msg_2","type":"step-finish","reason":"stop","cost":0,"tokens":{"total":42691,"input":40602,'
    '"output":2,"reasoning":39,"cache":{"read":2048,"write":0}}}}',
]


def _feed(runner, lines, mcp=("tunetrack",)):
    n = runners.normaliser(runner, mcp)
    out = []
    for ln in lines + ["not json at all", "{broken json", '["a list"]', '{"type":"something_new","x":1}']:
        obj = runners.parse_line(ln)
        if obj is not None:
            out += n.feed(obj)
    return out + n.finish(), n


def test_normalise_claude():
    ev, n = _feed("claude", CLAUDE_LINES)
    assert ev == [
        {"type": "session", "id": "<SID>"},
        {"type": "text", "text": "Checking."},
        {"type": "tool", "name": "mcp__tunetrack__status", "input": {}, "id": "toolu_1"},
        {"type": "tool_result", "name": "mcp__tunetrack__status", "ok": True, "id": "toolu_1"},
        {"type": "text", "text": "OK"},
        {"type": "usage", "input_tokens": 115, "output_tokens": 3, "cached_tokens": 105, "cost_usd": 0.0123},
    ]
    assert n.ok is True and n.result == "OK"


def test_normalise_claude_auth_failure():
    ev, n = _feed("claude", CLAUDE_AUTH_FAIL)
    errors = [e for e in ev if e["type"] == "error"]
    assert len(errors) == 1 and "Failed to authenticate" in errors[0]["message"]
    assert not any(e["type"] == "text" for e in ev)
    assert n.ok is False


def test_normalise_codex():
    ev, n = _feed("codex", CODEX_LINES)
    assert ev == [
        {"type": "session", "id": "<THREAD>"},
        {"type": "tool", "name": "mcp__tunetrack__status", "input": {}, "id": "item_0"},
        {"type": "tool_result", "name": "mcp__tunetrack__status", "ok": True, "id": "item_0"},
        {"type": "text", "text": "OK"},
        {"type": "usage", "input_tokens": 58649, "output_tokens": 118, "cached_tokens": 45952, "cost_usd": None},
    ]
    assert n.ok is True and n.result == "OK"


def test_normalise_codex_failure_and_shell():
    lines = ['{"type":"item.started","item":{"id":"i","type":"command_execution","command":"dir","status":"in_progress"}}',
             '{"type":"item.completed","item":{"id":"i","type":"command_execution","command":"dir","exit_code":1,'
             '"status":"failed"}}',
             '{"type":"turn.failed","error":{"message":"usage limit reached"}}']
    ev, n = _feed("codex", lines)
    assert ev[0]["name"] == "shell" and ev[1] == {"type": "tool_result", "name": "shell", "ok": False, "id": "i"}
    assert ev[2] == {"type": "error", "message": "usage limit reached"} and n.ok is False


def test_normalise_kilo():
    ev, n = _feed("kilo", KILO_LINES)
    assert ev == [
        {"type": "session", "id": "<SES>"},
        {"type": "tool", "name": "mcp__tunetrack__status", "input": {}, "id": "call_1"},
        {"type": "tool_result", "name": "mcp__tunetrack__status", "ok": True, "id": "call_1"},
        {"type": "text", "text": "OK"},
        {"type": "usage", "input_tokens": 84650, "output_tokens": 76, "cached_tokens": 4048, "cost_usd": 0.0},
    ]
    assert n.ok is True and n.result == "OK"


def test_normalise_kilo_error_and_unknown_tool():
    lines = ['{"type":"tool_use","sessionID":"s","part":{"tool":"bash","callID":"c","state":{"status":"error",'
             '"input":{"command":"x"}}}}',
             '{"type":"error","sessionID":"s","error":{"name":"APIError","data":{"message":"rate limited"}}}']
    ev, n = _feed("kilo", lines)
    assert ev[1] == {"type": "tool", "name": "bash", "input": {"command": "x"}, "id": "c"}
    assert ev[2]["ok"] is False and ev[-1] == {"type": "error", "message": "rate limited"} and n.ok is False


# --------------------------------------------------------------------------- script runner end to end

def _script(tmp_path, body, name="agent.py"):
    p = tmp_path / name
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(p)


def test_script_runner_passthrough(tmp_path):
    script = _script(tmp_path, """
        import json, os, sys
        prompt = sys.stdin.read()
        def emit(**e): print(json.dumps(e), flush=True)
        emit(type="session", id="script-1")
        print("plain log line, ignored")
        sys.stderr.write("some warning on stderr\\n")
        emit(type="text", text="got: " + prompt)
        emit(type="text", text="role=" + os.environ["TUNETRACK_CREW_SYSTEM"] + " foo=" + os.environ["FOO"])
        emit(type="tool", name="mcp__tunetrack__status", input={})
        emit(type="tool_result", name="mcp__tunetrack__status", ok=True)
        emit(type="usage", input_tokens=1, output_tokens=2, cost_usd=None)
    """)
    log = tmp_path / "run.log"
    prompt = 'Compose "the hook" — ünïcödé & <stuff>\n' + "long " * 5000
    run = start(Launch("script", prompt, system="composer", env={"FOO": "bar"}, script=script, log=str(log)))
    assert run.pid > 0
    ev = list(run.events())
    assert [e["type"] for e in ev] == ["session", "text", "text", "tool", "tool_result", "usage", "done"]
    assert ev[1]["text"] == "got: " + prompt
    assert ev[2]["text"] == "role=composer foo=bar"
    assert ev[-1] == {"type": "done", "ok": True, "returncode": 0, "result": ""}
    assert run.returncode == 0 and run.session == "script-1"
    text = log.read_text(encoding="utf-8")
    assert "plain log line, ignored" in text and "[stderr] some warning on stderr" in text
    with pytest.raises(RuntimeError):
        list(run.events())


def test_script_runner_own_done_is_single_and_last(tmp_path):
    script = _script(tmp_path, """
        import json
        print(json.dumps({"type": "done", "ok": True, "returncode": 0, "result": "all good"}))
        print(json.dumps({"type": "text", "text": "after done"}))
    """)
    ev = list(start(Launch("script", "", script=script)).events())
    assert [e["type"] for e in ev] == ["text", "done"]
    assert ev[-1] == {"type": "done", "ok": True, "returncode": 0, "result": "all good"}


def test_script_runner_nonzero_exit(tmp_path):
    script = _script(tmp_path, """
        import json, sys
        print(json.dumps({"type": "text", "text": "partial"}))
        sys.stderr.write("Traceback: boom\\n")
        sys.exit(3)
    """)
    ev = start(Launch("script", "x", script=script)).wait()
    assert ev == {"type": "done", "ok": False, "returncode": 3, "result": ""}
    evs = list(start(Launch("script", "x", script=script)).events())
    assert {"type": "error", "message": "Traceback: boom"} in evs


def test_script_runner_stop_kills_tree(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    script = _script(tmp_path, f"""
        import json, subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(child_pid_file)!r}, "w").write(str(child.pid))
        print(json.dumps({{"type": "text", "text": "started"}}), flush=True)
        time.sleep(60)
    """)
    run = start(Launch("script", "", script=script))
    it = run.events()
    assert next(it) == {"type": "text", "text": "started"}
    t0 = time.time()
    run.stop(timeout=5)
    rest = list(it)
    assert time.time() - t0 < 15
    assert rest[-1]["type"] == "done" and rest[-1]["ok"] is False and rest[-1].get("stopped") is True
    assert run.returncode is not None
    child = int(child_pid_file.read_text())
    deadline = time.time() + 10
    while _alive(child) and time.time() < deadline:
        time.sleep(0.2)
    assert not _alive(child), "grandchild survived stop()"


def _alive(pid):
    if sys.platform == "win32":
        import subprocess
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def test_stop_before_events_and_temp_cleanup(tmp_path, monkeypatch):
    script = _script(tmp_path, "import time; time.sleep(60)\n")
    run = start(Launch("script", "", script=script))
    run.stop()
    assert run.returncode is not None
    # temp files listed in Command.cleanup are removed when the run ends
    tmpf = tmp_path / "cfg.json"
    tmpf.write_text("{}")
    cmd = runners.Command([sys.executable, "-c", "print('{\"type\": \"text\", \"text\": \"hi\"}')"],
                          dict(os.environ), None, [str(tmpf)])
    r = runners.Run(Launch("script", ""), cmd)
    assert [e["type"] for e in r.events()] == ["text", "done"]
    assert not tmpf.exists()


def test_script_runner_requires_script(tmp_path):
    with pytest.raises(ValueError):
        build(Launch("script", "x"))
    with pytest.raises(FileNotFoundError):
        build(Launch("script", "x", script=str(tmp_path / "missing.py")))
