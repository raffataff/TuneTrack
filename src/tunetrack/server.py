"""Local web editor server (127.0.0.1 only). Every edit goes through tunetrack.api, same as the agent's tools."""
from __future__ import annotations

import inspect
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from . import api, config

PORT = 7373
WEB = Path(__file__).parent / "web"
_BLOCKED = {"load", "parse_device"}
CALLABLE = {n for n, f in inspect.getmembers(api, inspect.isfunction)
            if not n.startswith("_") and f.__module__ == api.__name__ and n not in _BLOCKED}


async def index(_req: Request):
    return FileResponse(WEB / "index.html")


async def static(req: Request):
    f = (WEB / req.path_params["name"]).resolve()
    if WEB.resolve() not in f.parents or not f.is_file():
        return Response(status_code=404)
    return FileResponse(f)


async def ping(_req: Request):
    return JSONResponse({"tunetrack": True})


def _current() -> Path | None:
    cur = api._state().get("current")
    return Path(cur) if cur and (Path(cur) / "project.json").exists() else None


async def version(_req: Request):
    cur = _current()
    v = (cur / "project.json").stat().st_mtime_ns if cur else 0
    return JSONResponse({"project": str(cur) if cur else None, "version": v})


async def state(_req: Request):
    cur = _current()
    if not cur:
        return JSONResponse({"project": None, "recent": api._state().get("recent", [])})
    text = (cur / "project.json").read_text(encoding="utf-8")
    last = cur / "renders" / "last.json"
    return JSONResponse({"project": str(cur), "version": (cur / "project.json").stat().st_mtime_ns,
                         "doc": json.loads(text),
                         "last_render": json.loads(last.read_text(encoding="utf-8")) if last.exists() else None})


async def call(req: Request):
    body = await req.json()
    fn = body.get("fn")
    if fn not in CALLABLE:
        return JSONResponse({"error": f"unknown function {fn!r}"}, status_code=400)
    try:
        res = await run_in_threadpool(getattr(api, fn), **(body.get("args") or {}))
        return JSONResponse({"result": res})
    except Exception as e:  # noqa: BLE001 - surface the message in the UI
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)


async def audio(req: Request):
    f = Path(req.query_params.get("file", "")).resolve()
    roots = [config.HOME.resolve(), config.PROJECTS_DIR.resolve()]
    cur = _current()
    if cur:
        roots.append(cur.resolve())
    if not f.is_file() or not any(r == f or r in f.parents for r in roots):
        return Response(status_code=404)
    return FileResponse(f, headers={"Cache-Control": "no-store"})


app = Starlette(routes=[
    Route("/", index), Route("/web/{name}", static), Route("/api/ping", ping), Route("/api/version", version),
    Route("/api/state", state), Route("/api/call", call, methods=["POST"]), Route("/api/audio", audio),
])


def url() -> str:
    return f"http://127.0.0.1:{PORT}/"


def is_running() -> bool:
    try:
        with urllib.request.urlopen(url() + "api/ping", timeout=1) as r:
            return b"tunetrack" in r.read()
    except OSError:
        return False


def ensure_running() -> str:
    """Start the editor server in the background if it isn't up yet."""
    if not is_running():
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) \
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        log = open(config.HOME / "server.log", "a", encoding="utf-8")  # noqa: SIM115 - handed to the child
        subprocess.Popen([sys.executable, "-m", "tunetrack.server"], stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        for _ in range(40):
            if is_running():
                break
            time.sleep(0.25)
    return url()


def main(port: int = PORT) -> None:
    import uvicorn
    config.ensure_dirs()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
