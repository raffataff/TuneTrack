import json

from tunetrack.crew import usage

CLAUDE = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": 100,
          "unifiedWindows": {"five_hour": {"utilization": 0.17, "resetsAt": 100}, "seven_day": {"utilization": 0.5, "resetsAt": 200}}}}
CODEX = {"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
    "primary": {"used_percent": 2.0, "window_minutes": 300, "resets_at": 300},
    "secondary": {"used_percent": 74.0, "window_minutes": 10080, "resets_at": 400}}}}


def test_claude_line():
    got = usage.claude_from_line(json.dumps(CLAUDE))
    assert got["5h"] == {"used_pct": 17.0, "resets_at": 100} and got["week"]["used_pct"] == 50.0


def test_codex_line():
    got = usage.codex_from_line(json.dumps(CODEX))
    assert got["5h"]["used_pct"] == 2.0 and got["week"] == {"used_pct": 74.0, "resets_at": 400}


def test_junk_is_skipped():
    assert usage.claude_from_line('{"type":"rate_limit_event"') is None
    assert usage.codex_from_line('{"rate_limits": "x"}') is None
    assert usage.claude_from_line('{"type":"assistant"}') is None


def test_newest_record_wins(tmp_path, monkeypatch):
    logs = tmp_path / "p" / "studio" / "logs"; logs.mkdir(parents=True)
    old = dict(CLAUDE, rate_limit_info=dict(CLAUDE["rate_limit_info"], unifiedWindows={"five_hour": {"utilization": 0.1, "resetsAt": 1}}))
    (logs / "a.jsonl").write_text(json.dumps(old) + "\n" + json.dumps(CLAUDE) + "\n", encoding="utf-8")
    monkeypatch.setattr(usage.config, "PROJECTS_DIR", tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nocodex"))
    usage._cache["key"] = "reset"
    got = usage.usage()
    assert got["claude"]["5h"]["used_pct"] == 17.0 and got["codex"] is None and got["agy"] is None
