"""Local web editor server (127.0.0.1 only). Every edit goes through tunetrack.api, same as the agent's tools.

Hardening against other web pages in the user's browser reaching this local service:
- every request must carry a Host header naming this server (blocks DNS-rebinding),
- /api/call needs the per-process session token (embedded in the editor page, which other origins can't read),
  a same-origin Origin header and a JSON content type, and only runs an explicit allowlist of editor operations.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import api, config, studio, takes
from .crew import detect, host

PORT = 7373
WEB = Path(__file__).parent / "web"
# Only what the editor UI uses. Imports, patch deletion, opening files/apps etc. stay agent/CLI-only.
CALLABLE = {
    "audition", "clip_create", "clip_edit", "clip_set", "fx_add", "fx_remove", "fx_update", "library_status",
    "markers", "patch_capture", "plugin_params", "preview", "project_list", "project_new", "project_open",
    "project_set", "redo", "render", "sfz_list", "synths_list", "track_add", "track_duplicate", "track_move",
    "track_remove", "track_update", "undo",
}


def crew_detect(refresh: bool = False) -> dict:
    """The agent CLIs on this computer and the models each offers (what can sit in a seat)."""
    return detect.detect(refresh=bool(refresh))


def studio_view() -> dict:
    """The open session's board and crew state in one call (what the rack redraws from)."""
    cur = _current()
    if not cur:
        return {"board": None, "crew": None}
    return {"board": studio.board(str(cur)), "crew": host.crew_state(str(cur))}


# The studio board, as the user (this server never speaks for a seat). Read-only calls and the user's own
# decisions: assigning seats, handing out tasks, talkback, answering Needs You, accepting or returning takes,
# and starting or stopping the crew.
STUDIO = {f.__name__: f for f in (
    studio.studio_status, studio.board, studio.seat_assign, studio.task_add, studio.task_update, studio.task_list,
    studio.message_send, studio.inbox, studio.attention_list, studio.attention_resolve, studio.events,
    takes.take_list, takes.take_diff, takes.take_accept, takes.take_reject, takes.take_discard,
    takes.take_audition, host.crew_start, host.crew_stop, host.crew_state, host.seat_hold, crew_detect,
    studio_view,
)}
TOKEN = secrets.token_urlsafe(32)
_HOSTS = {"127.0.0.1", "localhost", "[::1]"}


def _port(scope) -> int:
    server = scope.get("server") or (None, PORT)
    return int(server[1] or PORT)


def _host_ok(scope) -> bool:
    host = dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1").lower()
    return host in {f"{h}:{_port(scope)}" for h in _HOSTS}


def _origin_ok(req: Request) -> bool:
    origin = req.headers.get("origin")
    if origin is None:  # non-browser clients (curl, scripts) send none; they still need the token
        return True
    from urllib.parse import urlsplit
    try:
        u = urlsplit(origin)
        return u.scheme == "http" and (u.hostname or "") in {"127.0.0.1", "localhost", "::1"} and             u.port == _port(req.scope)
    except ValueError:
        return False


class _HostGuard:
    """ASGI middleware: reject any request whose Host isn't this local server."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not _host_ok(scope):
            await Response("bad host", status_code=421)(scope, receive, send)
            return
        await self.inner(scope, receive, send)


async def index(_req: Request):
    html = (WEB / "index.html").read_text(encoding="utf-8")
    html = html.replace("</head>", f'<meta name="tt-token" content="{TOKEN}">\n</head>', 1)
    return Response(html, media_type="text/html", headers={"Cache-Control": "no-store"})


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


def _version(cur: Path) -> str:
    st = (cur / "project.json").stat()
    return f"{st.st_mtime_ns}:{st.st_size}"


async def version(_req: Request):
    cur = _current()
    return JSONResponse({"project": str(cur) if cur else None, "version": _version(cur) if cur else 0})


async def state(_req: Request):
    cur = _current()
    if not cur:
        return JSONResponse({"project": None, "recent": api._state().get("recent", [])})
    v = _version(cur)
    text = (cur / "project.json").read_text(encoding="utf-8")
    last = cur / "renders" / "last.json"
    return JSONResponse({"project": str(cur), "version": v,
                         "doc": json.loads(text),
                         "last_render": json.loads(last.read_text(encoding="utf-8")) if last.exists() else None,
                         "board": studio.board(str(cur)), "crew": host.crew_state(str(cur))})


async def call(req: Request):
    if not hmac.compare_digest(req.headers.get("x-tunetrack-token", ""), TOKEN):
        return JSONResponse({"error": "missing or bad session token (reload the editor)"}, status_code=403)
    if not _origin_ok(req):
        return JSONResponse({"error": "cross-origin request refused"}, status_code=403)
    if req.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return JSONResponse({"error": "Content-Type must be application/json"}, status_code=415)
    try:
        body = await req.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    fn = body.get("fn")
    if fn not in CALLABLE and fn not in STUDIO:
        return JSONResponse({"error": f"unknown function {fn!r}"}, status_code=400)
    target = STUDIO.get(fn) or getattr(api, fn)
    args = body.get("args") or {}
    if not isinstance(args, dict):
        return JSONResponse({"error": "args must be an object"}, status_code=400)
    rev = body.get("rev")

    def run():
        tok = api.expected_rev.set(int(rev) if rev is not None else None)
        try:
            return target(**args)
        finally:
            api.expected_rev.reset(tok)

    try:
        res = await run_in_threadpool(run)
        return JSONResponse({"result": res})
    except api.ConflictError as e:
        return JSONResponse({"error": str(e), "conflict": True}, status_code=409)
    except Exception as e:  # noqa: BLE001 - surface the message in the UI
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=400)


def _pulse(cur: Path | None, seq: int) -> tuple[dict, int]:
    """What changed since the last look: the project file's version and any new board events."""
    if not cur:
        return {"project": None, "version": 0, "events": []}, 0
    ev = studio.events(seq, 500, str(cur))
    return {"project": str(cur), "version": _version(cur), "events": ev["events"]}, ev["last"]


async def events(req: Request):
    """Server-sent events: one message whenever the open project, its file, or its studio board changes, so the
    editor follows agents live without polling. ?since=<event number> resumes the board's activity log."""
    try:
        seq = int(req.query_params.get("since", 0))
    except ValueError:
        seq = 0

    async def stream():
        nonlocal seq
        last = None
        idle = 0.0
        while not await req.is_disconnected():
            cur = await run_in_threadpool(_current)
            try:
                msg, seq = await run_in_threadpool(_pulse, cur, seq)
            except OSError:  # the file is mid-write: look again in a moment
                await asyncio.sleep(0.2)
                continue
            mark = (msg["project"], msg["version"])
            if mark != last or msg["events"]:
                last, idle = mark, 0.0
                yield f"id: {seq}\ndata: {json.dumps(msg)}\n\n"
            elif idle >= 15.0:  # keep proxies and the browser from dropping a quiet connection
                idle = 0.0
                yield ": keep-alive\n\n"
            await asyncio.sleep(0.25)
            idle += 0.25

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


async def audio(req: Request):
    f = Path(req.query_params.get("file", "")).resolve()
    roots = [config.HOME.resolve(), config.PROJECTS_DIR.resolve()]
    cur = _current()
    if cur:
        roots.append(cur.resolve())
    if not f.is_file() or not any(r == f or r in f.parents for r in roots):
        return Response(status_code=404)
    return FileResponse(f, headers={"Cache-Control": "no-store"})


app = _HostGuard(Starlette(routes=[
    Route("/", index), Route("/web/{name}", static), Route("/api/ping", ping), Route("/api/version", version),
    Route("/api/state", state), Route("/api/call", call, methods=["POST"]), Route("/api/audio", audio),
    Route("/api/events", events),
]))


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
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    finally:
        host.stop_all()  # agents the editor started don't outlive it


if __name__ == "__main__":
    main()
