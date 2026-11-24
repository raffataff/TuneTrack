"""Antigravity (agy) runner: detection, argv/workspace files, and the stream-json normaliser.

The stream lines are real output of agy 1.5.15 (gemini-3.8-flash-low) calling the tunetrack MCP server, shortened.
No model is called and nothing depends on agy being installed."""
import json
import os
import subprocess

import pytest

from tunetrack.crew import detect, runners
from tunetrack.crew.runners import Launch, build

MCP = {"tunetrack": {"command": r"C:\TT\.venv\Scripts\python.exe", "args": ["-m", "tunetrack.mcp_server"],
                     "env": {"TUNETRACK_SEAT": "mix"}}}

MODELS = """Fetching available models...
gemini-3.8-flash-high\tGemini 3.8 Flash (High)
gemini-3.8-flash-low\tGemini 3.8 Flash (Low)
claude-opus-4-6-thinking\tClaude Opus 4.6 (Thinking)
gpt-oss-120b-medium\tGPT-OSS 120B (Medium)
"""


@pytest.fixture
def agy_exe(monkeypatch, tmp_path):
    exe = str(tmp_path / "agy.exe")
    monkeypatch.setattr(detect, "find_exe", lambda r: exe if r == "agy" else None)
    return exe


def test_parse_agy_models():
    rows = detect.parse_agy_models(MODELS)
    assert [r["id"] for r in rows] == ["gemini-3.8-flash-high", "gemini-3.8-flash-low",
                                       "claude-opus-4-6-thinking", "gpt-oss-120b-medium"]
    assert rows[0]["label"] == "Gemini 3.8 Flash (High)"
    assert [r["tier"] for r in rows] == ["high", "fast", "top", "mid"]
    assert "reasoning" in rows[2]["info"]["can"] and rows[0]["cost"] == "paid"


def test_probe_agy(monkeypatch, tmp_path):
    exe = tmp_path / "agy.exe"
    exe.write_text("")
    monkeypatch.setattr(detect, "find_exe", lambda r: str(exe) if r == "agy" else None)

    def run(argv, **kw):
        out = {"--version": "1.5.15\n", "models": MODELS}.get(argv[1], "")
        return subprocess.CompletedProcess(argv, 0, out.encode(), b"")
    monkeypatch.setattr(subprocess, "run", run)
    rec = detect._probe("agy")
    assert rec["available"] and rec["version"] == "1.5.15" and rec["auth"] == "ok"
    assert len(rec["models"]) == 4 and rec["efforts"] == detect.AGY_EFFORTS and rec["name"] == "Antigravity"

    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, b"1.5.15\n" if argv[1] == "--version" else b"Fetching available models...\n", b""))
    assert detect._probe("agy")["auth"] == "missing"  # not signed in: nothing listed


def test_build_agy(agy_exe, tmp_path):
    ws = tmp_path / "work"
    ws.mkdir()
    prompt = 'Mix the "bridge" & keep it <8 bars>\nline two'
    cmd = build(Launch("agy", prompt, system="You are the mix engineer.", model="gemini-3.8-flash-high",
                       effort="low", cwd=str(ws), mcp=MCP, allow_tools=["mcp__tunetrack"]))
    try:
        a = cmd.argv
        assert a[0] == agy_exe and a[-1] == "-p="
        assert a[a.index("--output-format") + 1] == "stream-json" and a[a.index("--input-format") + 1] == "stream-json"
        assert a[a.index("--agent") + 1] == "tunetrack-crew" and "--conversation" not in a
        assert a[a.index("--model") + 1] == "gemini-3.8-flash-high" and a[a.index("--effort") + 1] == "low"
        assert prompt not in " ".join(a)
        assert json.loads(cmd.stdin) == {"event": "user", "message": {"role": "user", "content": prompt}}
        settings = json.load(open(a[a.index("--settings") + 1], encoding="utf-8"))
        assert settings["permissions"]["allow"] == ["mcp(tunetrack_tunetrack/*)"]
        assert "command(*)" in settings["permissions"]["deny"] and "write_file(*)" in settings["permissions"]["deny"]
        plug = ws / ".agents" / "plugins" / "tunetrack"
        assert json.loads((plug / "mcp_config.json").read_text())["mcpServers"]["tunetrack"] == MCP["tunetrack"]
        agent = (ws / ".agents" / "agents" / "tunetrack-crew" / "agent.md").read_text()
        assert "You are the mix engineer." in agent and "tools:\n  - read_file\n" in agent
        assert cmd.cwd == str(ws) and cmd.cleanup == [a[a.index("--settings") + 1]]
    finally:
        for p in cmd.cleanup:
            os.remove(p)


def test_build_agy_resume_and_temp_workspace(agy_exe):
    cmd = build(Launch("agy", "hi", resume="conv-1"))
    try:
        a = cmd.argv
        assert a[a.index("--conversation") + 1] == "conv-1" and "--agent" not in a
        assert os.path.isdir(cmd.cwd) and cmd.cwd in cmd.cleanup  # no cwd given: a temp workspace, removed after
    finally:
        runners.Run._cleanup(type("R", (), {"_cleaned": False, "command": cmd})())
    assert not os.path.exists(cmd.cwd)


def test_agy_permission():
    assert runners.agy_permission("mcp__tunetrack") == "mcp(tunetrack_tunetrack/*)"
    assert runners.agy_permission("mcp__tunetrack__render") == "mcp(tunetrack_tunetrack/render)"


C = '"conversation_id":"<C>"'
AGY_LINES = [
    '{"event":"init","conversation_id":"<C>","init":{"model":"gemini-3.8-flash-low","agent":"tunetrack-crew",'
    '"tools":["call_mcp_tool","read_file"],"permission_mode":"request-review"}}',
    '{"event":"step_update","step_update":{' + C + ',"step_index":0,"state":"DONE","step_type":"user_input"}}',
    '{"event":"step_update","step_update":{' + C + ',"step_index":1,"state":"DONE","step_type":"agent_response",'
    '"usage":{"input_tokens":10394,"output_tokens":38}}}',
    '{"event":"step_update","step_update":{' + C + ',"step_index":2,"state":"ACTIVE","step_type":"tool",'
    '"tool_name":"call_mcp_tool","tool_info":{"name":"call_mcp_tool","parameters":{"Arguments":{"limit":5},'
    '"ServerName":"tunetrack_tunetrack","ToolName":"project_list"}}}}',
    '{"event":"step_update","step_update":{' + C + ',"step_index":2,"state":"DONE","step_type":"tool",'
    '"tool_name":"call_mcp_tool","tool_info":{"name":"call_mcp_tool","parameters":{"Arguments":{"limit":5},'
    '"ServerName":"tunetrack_tunetrack","ToolName":"project_list"}}}}',
    '{"event":"step_update","step_update":{' + C + ',"step_index":4,"state":"ERROR","step_type":"tool",'
    '"tool_name":"run_command","tool_info":{"name":"run_command","parameters":{"CommandLine":"echo hi"},'
    '"error":{"type":"TOOL_ERROR","message":"Permission denied"}}}}',
    '{"event":"step_update","step_update":{' + C + ',"step_index":5,"state":"ACTIVE","step_type":"agent_response",'
    '"text_delta":"There are "}}',
    '{"event":"step_update","step_update":{' + C + ',"step_index":5,"state":"DONE","step_type":"agent_response",'
    '"text_delta":"20 projects.\\n","usage":{"input_tokens":2219,"output_tokens":88}}}',
    '{"event":"result","result":{' + C + ',"status":"SUCCESS","response":"There are 20 projects.\\n",'
    '"num_turns":1,"usage":{"input_tokens":10604,"output_tokens":156,"thinking_tokens":4,"cache_read_tokens":4116,'
    '"total_tokens":10760}}}',
]


def _feed(lines):
    n = runners.normaliser("agy", ("tunetrack",))
    out = []
    for ln in lines + ["not json", '{"event":"something_new"}']:
        obj = runners.parse_line(ln)
        if obj is not None:
            out += n.feed(obj)
    return out + n.finish(), n


def test_normalise_agy():
    ev, n = _feed(AGY_LINES)
    assert ev == [
        {"type": "session", "id": "<C>"},
        {"type": "tool", "name": "mcp__tunetrack__project_list", "input": {"limit": 5}, "id": 2},
        {"type": "tool_result", "name": "mcp__tunetrack__project_list", "ok": True, "id": 2},
        {"type": "tool", "name": "run_command", "input": {"CommandLine": "echo hi"}, "id": 4},
        {"type": "tool_result", "name": "run_command", "ok": False, "id": 4},
        {"type": "text", "text": "There are 20 projects."},
        {"type": "usage", "input_tokens": 14720, "output_tokens": 160, "cached_tokens": 4116, "cost_usd": None},
    ]
    assert n.ok is True and n.result == "There are 20 projects." and n.session == "<C>"


def test_normalise_agy_error():
    ev, n = _feed(['{"event":"result","result":{"conversation_id":"<C>","status":"ERROR","response":"",'
                   '"error":"tool \\"x\\" not found in registry","usage":{}}}'])
    assert n.ok is False and ev[-1] == {"type": "error", "message": 'tool "x" not found in registry'}
