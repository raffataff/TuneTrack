"""Command line: tunetrack <command>. `tunetrack call <fn> '<json>'` reaches every API operation."""
from __future__ import annotations

import argparse
import inspect
import json
import sys

from . import api, config


def _print(obj) -> None:
    print(json.dumps(obj, indent=1, default=str))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="tunetrack", description="AI-controllable music workstation for game soundtracks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup", help="create the library folders and scan VST3 plugins")
    s = sub.add_parser("scan", help="scan VST3 plugins")
    s.add_argument("--force", action="store_true")
    s.add_argument("--retry", action="store_true", help="retry plugins that failed before")
    sub.add_parser("status", help="library + current project overview")
    u = sub.add_parser("ui", help="open the editor in the browser")
    u.add_argument("project", nargs="?")
    sub.add_parser("serve", help="run the editor server in the foreground")
    sub.add_parser("mcp", help="run the MCP server (stdio) for AI agents")
    r = sub.add_parser("render", help="render the current (or given) project")
    r.add_argument("project", nargs="?")
    r.add_argument("--out")
    r.add_argument("--format", default="wav")
    r.add_argument("--bits", type=int, default=24)
    r.add_argument("--loop", default="auto", choices=["auto", "off", "loop", "intro+loop"])
    r.add_argument("--no-normalize", action="store_true")
    r.add_argument("--stems", action="store_true")
    r.add_argument("--layers", action="store_true")
    r.add_argument("--midi", action="store_true")
    c = sub.add_parser("capture", help="open a plugin window and save the sound you pick as a patch")
    c.add_argument("plugin")
    c.add_argument("name")
    c.add_argument("--tags", default="")
    c.add_argument("--description", default="")
    c.add_argument("--role")
    c.add_argument("--dynamics-cc", type=int)
    k = sub.add_parser("call", help="call any API function with JSON args, e.g. call project_info '{}'")
    k.add_argument("fn")
    k.add_argument("args", nargs="?", default="{}")
    sub.add_parser("functions", help="list API functions for `call`")
    w = sub.add_parser("crew", help="the studio crew: `crew detect` (which agent apps and models are installed), "
                                    "`crew status`, `crew run [--brief \"...\"]` (let the seated agents work)")
    w.add_argument("action", choices=["detect", "status", "run"])
    w.add_argument("--brief", help="what to make: the Producer plans from it")
    w.add_argument("--project")
    w.add_argument("--parallel", type=int, default=3, help="agents working at once")
    w.add_argument("--max-runs", type=int, default=40, help="agent runs before the crew stops and asks you")
    w.add_argument("--refresh", action="store_true", help="detect: rescan instead of using the cached result")
    sub.add_parser("install-claude", help="install the TuneTrack skill and register the MCP server with Claude Code")
    a = ap.parse_args(argv)

    if a.cmd == "setup":
        config.ensure_dirs()
        print(f"Library: {config.HOME}\nProjects: {config.PROJECTS_DIR}\nScanning VST3 plugins (each in its own process)...")
        from . import plugins
        _print(plugins.scan(retry_failed=True, progress=lambda m: print("  " + m)))
    elif a.cmd == "scan":
        from . import plugins
        _print(plugins.scan(force=a.force, retry_failed=a.retry, progress=lambda m: print("  " + m)))
    elif a.cmd == "status":
        out = api.library_status()
        try:
            out["current_project"] = api.project_info(clips=False)
        except Exception as e:  # noqa: BLE001
            out["current_project"] = str(e)
        _print(out)
    elif a.cmd == "ui":
        import webbrowser
        from .server import ensure_running
        if a.project:
            api.project_open(a.project)
        url = ensure_running()
        webbrowser.open(url)
        print(url)
    elif a.cmd == "serve":
        from .server import main as serve
        serve()
    elif a.cmd == "mcp":
        from .mcp_server import main as run_mcp
        run_mcp()
    elif a.cmd == "render":
        _print(api.render(a.project, a.out, a.format, a.bits, a.loop, not a.no_normalize, a.stems, a.layers, a.midi))
    elif a.cmd == "capture":
        tags = [t.strip() for t in a.tags.split(",") if t.strip()]
        print(f"Opening {a.plugin}... pick a sound, then close the plugin window.")
        _print(api.patch_capture(a.plugin, a.name, tags, a.description, role=a.role, dynamics_cc=a.dynamics_cc))
    elif a.cmd == "install-claude":
        install_claude()
    elif a.cmd == "crew":
        from . import studio
        from .crew import detect, floor
        if a.action == "detect":
            _print(detect.detect(refresh=a.refresh))
            return
        if a.project:
            api.project_open(a.project)
        if a.action == "status":
            _print({**studio.studio_status(), **floor.crew()})
            return
        if a.brief:
            floor.kickoff(a.brief)

        def show(seat, ev):
            if ev.get("type") == "tool":
                print(f"  {seat:9} {str(ev.get('name', '')).replace('mcp__tunetrack__', '')}", flush=True)
            elif ev.get("type") in ("text", "error") and (ev.get("text") or ev.get("message")):
                print(f"  {seat:9} {'! ' if ev['type'] == 'error' else ''}"
                      f"{(ev.get('text') or ev.get('message')).strip()[:300]}", flush=True)

        if hasattr(sys.stdout, "reconfigure"):  # agents write arrows and curly quotes; a cp1252 console can't
            sys.stdout.reconfigure(errors="replace")
        problems = floor.crew().get("problems")
        if problems:
            print("Before the crew can work:\n  " + "\n  ".join(problems))
        _print(floor.Floor(max_parallel=a.parallel, max_launches=a.max_runs, on_event=show).run())
    elif a.cmd == "functions":
        for mod in _modules():
            for name, fn in inspect.getmembers(mod, inspect.isfunction):
                if not name.startswith("_") and fn.__module__ == mod.__name__:
                    print(f"{name}{inspect.signature(fn)}")
    elif a.cmd == "call":
        fn = None if a.fn.startswith("_") else next(
            (f for f in (getattr(m, a.fn, None) for m in _modules())
             if inspect.isfunction(f) and f.__module__.startswith("tunetrack.")), None)
        if fn is None:
            sys.exit(f"unknown function {a.fn!r} (see `tunetrack functions`)")
        _print(fn(**json.loads(a.args)))


def _modules() -> list:
    """Where `call` and `functions` look: every project operation, then the studio board and takes."""
    from . import studio, takes
    return [api, studio, takes]


def install_claude() -> None:
    """Copy skill/SKILL.md to ~/.claude/skills/tunetrack and register the MCP server at user scope."""
    import shutil
    import subprocess
    from pathlib import Path
    dest = Path.home() / ".claude" / "skills" / "tunetrack"
    dest.mkdir(parents=True, exist_ok=True)
    for f in (config.ROOT / "skill").iterdir():
        shutil.copy2(f, dest / f.name)
    print(f"skill installed: {dest}")
    exe = Path(sys.executable).parent / ("tunetrack-mcp.exe" if sys.platform == "win32" else "tunetrack-mcp")
    claude = shutil.which("claude")
    if not claude:
        print(f'claude CLI not found. Register manually:\n  claude mcp add --scope user tunetrack -- "{exe}"')
        return
    subprocess.run([claude, "mcp", "remove", "--scope", "user", "tunetrack"], capture_output=True)
    res = subprocess.run([claude, "mcp", "add", "--scope", "user", "tunetrack", "--", str(exe)], capture_output=True, text=True)
    print((res.stdout or res.stderr).strip())


if __name__ == "__main__":
    main()
