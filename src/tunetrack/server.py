"""Local web editor server (127.0.0.1 only). Every edit goes through tunetrack.api, same as the agent's tools.

Hardening against other web pages in the user's browser reaching this local service:
- every request must carry a Host header naming this server (blocks DNS-rebinding),
- /api/call needs the per-process session token (embedded in the editor page, which other origins can't read),
  a same-origin Origin header and a JSON content type, and only runs an explicit allowlist of editor operations.
"""
from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import re
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

from . import api, bench, config, studio, takes
from .crew import detect, host, scorecard

PORT = 7373
WEB = Path(__file__).parent / "web"
# Only what the editor UI uses. Imports, patch deletion, opening files/apps etc. stay agent/CLI-only.
CALLABLE = {
    "audition", "clip_create", "clip_edit", "clip_set", "fx_add", "fx_remove", "fx_update", "harmony", "library_status",
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


def take_cut(seat: str | None = None, task: str | None = None, name: str | None = None) -> dict:
    """Cut a take for the user to work in on the Desk, or pick up the open take a task already has (one that came
    back, or one cut earlier). seat: the seat the take is for (default: the task's seat, else the user with no
    seat). A queued task is marked working, as it is when an agent starts on it."""
    cur = _current()
    if not cur:
        raise RuntimeError("No project open")
    session = str(cur)
    t = None
    if task:
        ref = str(task).strip().lower()
        t = next((x for x in studio.read(session)["tasks"] if x["id"].lower() == ref), None)
        if not t:
            raise KeyError(f"No task {task!r}")
        seat = seat or t["seat"]
    have = t and next((k for k in studio.take_index(cur) if k["id"] == t.get("take") and k.get("state") == "open"), None)
    tid = have["id"] if have else takes.take_new(name or None, t["id"] if t else None, seat or None, use=False,
                                                 session=session)["take"]
    if t and t["state"] == "queued":
        studio.task_update(t["id"], state="working", session=session)
    return {"take": tid, "continued": bool(have)}


# The studio board, as the user (this server never speaks for a seat). Read-only calls and the user's own
# decisions: assigning seats, handing out tasks, talkback, answering Needs You, accepting or returning takes,
# cutting and handing in a take of their own, and starting or stopping the crew.
STUDIO = {f.__name__: f for f in (
    studio.studio_status, studio.board, studio.seat_assign, studio.task_add, studio.task_update, studio.task_list,
    studio.message_send, studio.inbox, studio.attention_list, studio.attention_resolve, studio.events,
    takes.take_list, takes.take_diff, takes.take_accept, takes.take_reject, takes.take_discard,
    takes.take_audition, takes.take_submit, takes.take_sync, take_cut, host.crew_start, host.crew_stop, host.crew_state, host.seat_hold, crew_detect,
    studio_view, bench.seat_bench, studio.report_list, studio.report_read,
    scorecard.crew_scorecard, scorecard.crew_review, scorecard.crew_history,
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


_TAKE_ID = re.compile(r"tk\d+")


def _take_dir(cur: Path, take) -> Path:
    """The folder of one of the open session's takes. The Desk names a take by id only ("tk3"), never by path."""
    tid = str(take or "").strip().lower()
    d = cur / "takes" / tid
    if not _TAKE_ID.fullmatch(tid) or not (d / "take.json").is_file() or not (d / "project.json").is_file():
        raise KeyError(f"No take {take!r} in this session")
    return d


def _view(cur: Path, take) -> tuple[Path, str]:
    """What the Desk is showing - the session, or one of its takes - and a version that changes when it does."""
    if not take:
        return cur, _version(cur)
    d = _take_dir(cur, take)
    return d, f"{d.name}@{_version(d)}"


async def version(req: Request):
    cur = _current()
    if not cur:
        return JSONResponse({"project": None, "version": 0, "session_version": 0})
    try:
        v = _view(cur, req.query_params.get("take"))[1]
    except (KeyError, OSError):
        v = None  # the take is gone: the Desk's next state load puts it back on the session
    return JSONResponse({"project": str(cur), "version": v, "session_version": _version(cur)})


def _take_view(cur: Path, d: Path) -> dict:
    """What the Desk's take bar shows about the take it has open."""
    meta = takes._meta(d)
    doc, base = takes._read_json(d / "project.json"), takes._read_json(d / "base.json")
    return {**{k: meta.get(k) for k in ("id", "name", "seat", "task", "state", "summary", "returns", "reason", "base_rev")},
            "changes": len(takes.changes(base, doc)),
            "session_moved": takes._read_json(cur / "project.json").get("rev") != meta.get("base_rev")}


async def state(req: Request):
    cur = _current()
    if not cur:
        return JSONResponse({"project": None, "recent": api._state().get("recent", [])})
    out = {"project": str(cur), "session_version": _version(cur), "take": None}
    d, v = cur, out["session_version"]
    want = req.query_params.get("take")
    if want:
        try:
            d, v = _view(cur, want)
            out["take"] = _take_view(cur, d)
        except (KeyError, OSError, ValueError) as e:  # not a take of this session: show the session and say so
            d, v, out["take_error"] = cur, out["session_version"], str(e.args[0] if e.args else e)
    last = d / "renders" / "last.json"
    out.update(version=v, doc=json.loads((d / "project.json").read_text(encoding="utf-8")),
               last_render=json.loads(last.read_text(encoding="utf-8")) if last.exists() else None,
               board=studio.board(str(cur)), crew=host.crew_state(str(cur)))
    return JSONResponse(out)


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
    if body.get("take") and fn in CALLABLE:
        # The Desk has a take open: its edits, undo, playback and export go to that take, not to the session.
        cur = _current()
        if not cur or (body.get("session") and Path(str(body["session"])).resolve() != cur.resolve()):
            return JSONResponse({"error": "The open project changed; reloading", "conflict": True}, status_code=409)
        try:
            d = _take_dir(cur, body["take"])
        except KeyError as e:
            return JSONResponse({"error": str(e.args[0]), "conflict": True}, status_code=409)
        if "project" in inspect.signature(target).parameters:
            args = {**args, "project": str(d)}

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


def _pulse(cur: Path | None, seq: int, take: str | None = None) -> tuple[dict, int]:
    """What changed since the last look: the project file's version, any new board events, and the version of
    the take the Desk has open (None once that take is gone)."""
    if not cur:
        return {"project": None, "version": 0, "events": []}, 0
    ev = studio.events(seq, 500, str(cur))
    msg = {"project": str(cur), "version": _version(cur), "events": ev["events"]}
    if take:
        try:
            msg["take"] = {"id": take, "version": _view(cur, take)[1]}
        except KeyError:
            msg["take"] = {"id": take, "version": None}
    return msg, ev["last"]


async def events(req: Request):
    """Server-sent events: one message whenever the open project, its file, or its studio board changes, so the
    editor follows agents live without polling. ?since=<event number> resumes the board's activity log;
    ?take=<id> also follows the take the Desk has open."""
    try:
        seq = int(req.query_params.get("since", 0))
    except ValueError:
        seq = 0
    take = (req.query_params.get("take") or "").strip().lower() or None

    async def stream():
        nonlocal seq
        last = None
        idle = 0.0
        while not await req.is_disconnected():
            cur = await run_in_threadpool(_current)
            try:
                msg, seq = await run_in_threadpool(_pulse, cur, seq, take)
            except OSError:  # the file is mid-write: look again in a moment
                await asyncio.sleep(0.2)
                continue
            mark = (msg["project"], msg["version"], (msg.get("take") or {}).get("version"))
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
