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
import threading
import time
import urllib.request
from pathlib import Path

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from . import api, bench, config, gear, livehost, picture as pic, review, signoff, studio, styles, takes, tracking
from .crew import detect, host, scorecard, usage

PORT = 7373
WEB = Path(__file__).parent / "web"
# Only what the editor UI uses. Imports, patch deletion, opening files/apps etc. stay agent/CLI-only.
CALLABLE = {
    "audition", "automation", "cleanup", "clip_create", "clip_edit", "clip_set", "comp", "fx_add", "fx_remove", "fx_update",
    "harmony", "hits", "library_status", "markers", "patch_capture", "picture", "plugin_params", "preview", "project_list",
    "project_new", "project_open", "project_set", "redo", "render", "sfz_list", "synths_list", "tempo_fit",
    "track_add", "track_duplicate", "track_move", "track_remove", "track_update", "undo",
}


def crew_detect(refresh: bool = False) -> dict:
    """The agent CLIs on this computer and the models each offers (what can sit in a seat)."""
    return detect.detect(refresh=bool(refresh))


def crew_usage() -> dict:
    """Each agent app's 5-hour and weekly limits, as it last reported them."""
    cur = _current()
    return usage.usage([str(cur)] if cur else None)


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


def _live_path(take=None) -> Path:
    cur = _current()
    if not cur:
        raise RuntimeError("No project open")
    return _take_dir(cur, take) if take else cur


def live_play(beat: float = 0.0, loop: bool | None = None, take: str | None = None) -> dict:
    """Play what the Desk shows (the session, or the take it has open) on the live engine, from a beat. The first
    call starts the engine and loads every plugin, which takes seconds on a big session."""
    return livehost.play(_live_path(take), beat, loop)


def live_stop() -> dict:
    return livehost.stop()


def live_seek(beat: float) -> dict:
    return livehost.seek(beat)


def live_loop(on: bool) -> dict:
    return livehost.loop(on)


def live_mix(changes: list, take: str | None = None) -> dict:
    """A fader, pan or send being dragged, heard at once on LIVE (see livehost.mix); the drag saves on release."""
    return livehost.mix(_live_path(take), changes)


def live_window(track: str, slot: str | int = "inst", take: str | None = None) -> dict:
    """Open a hosted plugin's own window on this computer (slot: "inst" or the effect's number, 0 = first)."""
    return livehost.window(_live_path(take), track, slot)


def live_inputs(take: str | None = None) -> dict:
    """Open the interface's inputs and the MIDI keyboards; the state then lists them (`inputs`)."""
    return livehost.inputs(_live_path(take))


def live_arm(track: str, arm: bool | None = None, monitor: bool | None = None, take: str | None = None,
             keyboard: str | None = None) -> dict:
    """Arm a track of what the Desk shows for recording, and/or monitor its input through its strip. keyboard:
    the one MIDI keyboard that plays this track (a name from inputs.midi; "" = any)."""
    return livehost.arm(_live_path(take), track, arm, monitor, keyboard)


def live_devices(take: str | None = None) -> dict:
    """The audio devices to play through and record from, the one in use and the one chosen."""
    return livehost.devices(_live_path(take))


def live_device(name: str | None = None, input: str | None = None, take: str | None = None) -> dict:  # noqa: A002
    """Change the audio device ("type: name"; "" = pick for me) and/or the input device; kept for next time."""
    return livehost.device(_live_path(take), name, input)


def live_guide(db: float | None = None, take: str | None = None) -> dict:
    """The picture's own sound under the music on LIVE at `db` (None: off). Never in a render."""
    return livehost.guide(_live_path(take), db)


def live_click(on: bool) -> dict:
    return livehost.click(on)


def live_keys(msgs: list) -> dict:
    return livehost.keys(msgs)


def live_record(beat: float = 0.0, count_in: float = 1.0, loop: bool | None = None, take: str | None = None,
                until: float | None = None, quantise: float = 0.0) -> dict:
    """Record the armed tracks into what the Desk shows (the session, or the take it has open). until: a punch
    out in beats (one pass, then it stops by itself; the state's `done` then carries the result); quantise: a
    grid in beats for the recorded notes' starts (0 = as played)."""
    return livehost.record(_live_path(take), beat, count_in, loop, until, quantise)


def live_record_stop() -> dict:
    return livehost.record_stop()


def live_off() -> dict:
    """Shut the live engine down and give the audio interface back."""
    return livehost.off()


def live_state() -> dict:
    return livehost.state()


def gear_audition(item: str, phrase: str = "auto", live: bool = False) -> dict:
    """Hear gear in the open project's key and tempo: looping on the live engine, or a short render."""
    cur = _current()
    kw = {}
    if cur:
        from .model import Project
        pr = Project.load(cur)
        kw = {"key": pr.key, "tempo": pr.tempo, "time_sig": pr.time_sig}
    return gear.audition(item, phrase, bool(live), **kw)


def gear_audition_stop(take: str | None = None) -> dict:
    """Stop a live audition and point the engine back at what the Desk shows (the session, or its open take)."""
    return gear.audition_stop(str(_live_path(take)) if _current() else None)


UI_FILE = "ui.json"  # in the library: how the user has laid the rooms out (one set for every browser and the app)


def ui_prefs(set: dict | None = None) -> dict:  # noqa: A002 - the page's word for it
    """The UI's layout and switches, kept in the library so the desktop app and any browser share them.
    set: keys to change (a key set to null is removed). Returns everything kept."""
    f = config.HOME / UI_FILE
    try:
        prefs = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    except (OSError, ValueError):
        prefs = {}
    if set:
        if not isinstance(set, dict) or len(json.dumps(set)) > 20000:
            raise ValueError("set: a small object of layout keys")
        for k, v in set.items():
            if v is None:
                prefs.pop(str(k), None)
            else:
                prefs[str(k)] = v
        config.HOME.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(prefs, indent=1), encoding="utf-8")
        tmp.replace(f)
    return prefs


_FONT_STYLES = {"regular", "bold", "italic", "oblique", "light", "semilight", "semibold", "demibold", "black", "heavy",
                "thin", "extralight", "ultralight", "extrabold", "ultrabold", "medium", "book", "normal", "roman"}


def font_list() -> dict:
    """Font family names installed on this computer (machine-wide and for this user), for Setup's font pickers.
    Names come from the font registry, so a few are guesses; the page keeps only the ones the browser can draw."""
    names: set[str] = set()
    try:
        import winreg
    except ImportError:
        return {"fonts": []}
    key = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key) as k:
                for i in range(winreg.QueryInfoKey(k)[1]):
                    raw = re.sub(r"\s*\((TrueType|OpenType|All res|VGA res)[^)]*\)\s*$", "", winreg.EnumValue(k, i)[0])
                    for part in raw.split(" & "):
                        words = part.split()
                        while len(words) > 1 and words[-1].lower().replace("-", "") in _FONT_STYLES:
                            words.pop()
                        names.add(" ".join(words))
        except OSError:
            continue
    return {"fonts": sorted((n for n in names if n and not n.startswith("@") and len(n) < 64), key=str.lower)}


# The studio board, as the user (this server never speaks for a seat). Read-only calls and the user's own
# decisions: assigning seats, handing out tasks, talkback, answering Needs You, accepting or returning takes,
# cutting and handing in a take of their own, and starting or stopping the crew.
STUDIO = {f.__name__: f for f in (
    studio.studio_status, studio.board, studio.seat_assign, studio.task_add, studio.task_update, studio.task_list,
    studio.message_send, studio.inbox, studio.attention_list, studio.attention_resolve, studio.events,
    takes.take_list, takes.take_diff, takes.take_accept, takes.take_reject, takes.take_discard,
    takes.take_audition, takes.take_submit, takes.take_sync, take_cut, host.crew_start, host.crew_stop, host.crew_state, host.seat_hold, crew_detect, crew_usage,
    studio_view, bench.seat_bench, studio.report_list, studio.report_read, review.review_draft, review.review_send,
    review.review_check, review.final_status,
    scorecard.crew_scorecard, scorecard.crew_review, scorecard.crew_history, scorecard.crew_pick,
    live_play, live_stop, live_seek, live_loop, live_window, live_off, live_state,
    live_inputs, live_arm, live_click, live_keys, live_record, live_record_stop, live_devices, live_device,
    live_guide, live_mix, ui_prefs, font_list,
    gear.catalog, gear.gear_state, gear.gear_seen, gear.gear_folders, gear.gear_rescan, gear_audition,
    gear_audition_stop, styles.style_list, signoff.seat_signoff, signoff.crew_involve, signoff.away_set,
    signoff.your_version, signoff.hand_over, signoff.take_yours,
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
    try:
        out["picture"] = pic.view(api.load(str(d)))
    except Exception:  # noqa: BLE001 - the Desk works without its picture lane
        out["picture"] = None
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
        state = takes._meta(d).get("state")
        if state not in bench.LIVE:  # a page that has not caught up: the edit would land where nobody will hear it
            was = "accepted into the session" if state == "accepted" else "closed"
            return JSONResponse({"error": f"{d.name} was {was}; reloading", "conflict": True}, status_code=409)
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

    async def counted():  # every open page holds one of these streams: that is how app mode knows a window is open
        _page_opened()
        try:
            async for chunk in stream():
                yield chunk
        finally:
            _page_closed()

    return StreamingResponse(counted(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


async def live_events(req: Request):
    """Server-sent events for the transport: the live engine's position, meters and state, about 20 times a
    second while it plays and whenever anything else about it changes."""
    async def stream():
        last, idle = None, 0.0
        while not await req.is_disconnected():
            st = livehost.state()
            mark = json.dumps(st, sort_keys=True)
            if mark != last:
                last, idle = mark, 0.0
                yield f"data: {mark}\n\n"
            elif idle >= 15.0:
                idle = 0.0
                yield ": keep-alive\n\n"
            pause = 0.05 if st.get("playing") or st.get("loading") else 0.25
            await asyncio.sleep(pause)
            idle += pause

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


async def peaks(req: Request):
    """An audio file of the open session as the Desk draws it: ?file=<path inside the project, as a clip names
    it>[&take=<id>]. One byte per 1/200 s or so (the X-Peaks-Per-Sec header says exactly): the loudest sample
    there, 0-255. A take's clips name files in the take or, when the take has no copy, the session."""
    cur = _current()
    rel = req.query_params.get("file", "")
    if not cur or not rel:
        return Response(status_code=404)
    try:
        roots = [_take_dir(cur, req.query_params["take"])] if req.query_params.get("take") else []
    except KeyError:
        roots = []
    for root in [*roots, cur]:
        f = (root / rel).resolve()
        if root.resolve() in f.parents and f.is_file():
            try:
                pk = await run_in_threadpool(tracking.peaks, f)
            except Exception:  # noqa: BLE001 - not an audio file we can read: the Desk draws the clip plain
                return Response(status_code=404)
            return Response(pk["peaks"], media_type="application/octet-stream",
                            headers={"X-Peaks-Per-Sec": f"{pk['per_sec']:.6f}", "Cache-Control": "no-store"})
    return Response(status_code=404)


async def picture_file(req: Request):
    """The open session's video (the browser's copy of it) or its thumbnail strip. Starlette answers Range
    requests, which is what lets the page seek in the video. kind=frame&t=<picture s>&w=<120-480>: one frame as a
    JPEG, kept in picture/frames/ (the page's URL carries a version, so it may cache it for good).
    kind=still&n=<i>: a screenshot from brief.stills."""
    cur = _current()
    if not cur:
        return Response(status_code=404)
    q, keep = req.query_params, "no-store"
    try:
        d = _take_dir(cur, q["take"]) if q.get("take") else cur
        p = api.load(str(d))
        kind = q.get("kind")
        if kind == "strip":
            f = pic._side(p, ".strip.jpg")
        elif kind == "still":
            n = int(q.get("n", ""))
            stills = p.brief.get("stills") or []
            f = p.root / stills[n]["file"] if 0 <= n < len(stills) else None
        elif kind == "frame":
            t, w, video = float(q.get("t", "")), int(q.get("w", "160")), pic.video_file(p, web=True)
            fps = pic._fps(p)
            if not (0 <= t <= float(p.picture.get("duration") or 0) + 1 and 120 <= w <= 480) \
                    or not video or not video.is_file():
                return Response(status_code=404)
            frame = int(round(t * fps))
            f = pic.folder(p) / "frames" / f"{video.stem}-{frame}-{w}.jpg"
            if not f.is_file():
                data = await run_in_threadpool(pic.frame_jpeg, video, frame / fps, w)
                if not data:
                    return Response(status_code=404)
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(data)
            keep = "public, max-age=31536000, immutable"
        else:
            f = pic.video_file(p, web=True)
    except (KeyError, IndexError, OSError, ValueError, TypeError, AttributeError, RuntimeError):
        return Response(status_code=404)
    if not f or not f.is_file() or cur.resolve() not in f.resolve().parents:
        return Response(status_code=404)
    return FileResponse(f, headers={"Cache-Control": keep})


app = _HostGuard(Starlette(routes=[
    Route("/", index), Route("/web/{name}", static), Route("/api/ping", ping), Route("/api/version", version),
    Route("/api/state", state), Route("/api/call", call, methods=["POST"]), Route("/api/audio", audio), Route("/api/picture", picture_file),
    Route("/api/peaks", peaks),
    Route("/api/events", events), Route("/api/live", live_events),
]))


def url() -> str:
    return f"http://127.0.0.1:{PORT}/"


def is_running() -> bool:
    try:
        with urllib.request.urlopen(url() + "api/ping", timeout=1) as r:
            return b"tunetrack" in r.read()
    except OSError:
        return False


def ensure_running(app_mode: bool = False) -> str:
    """Start the editor server in the background if it isn't up yet. app_mode: the server quits by itself once no
    page has been open for IDLE_QUIT_S (`tunetrack app`, whose window is the only way in). A server that is already
    up is used as it is."""
    if not is_running():
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) \
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        log = open(config.HOME / "server.log", "a", encoding="utf-8")  # noqa: SIM115 - handed to the child
        cmd = [sys.executable, "-m", "tunetrack.server"] + (["--idle-quit"] if app_mode else [])
        subprocess.Popen(cmd, stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True)
        for _ in range(40):
            if is_running():
                break
            time.sleep(0.25)
    return url()


# App mode (`tunetrack app`): the server goes away when its window does. Pages are counted by their /api/events
# stream; the server quits once none has been open for IDLE_QUIT_S, or none arrived within FIRST_PAGE_S of start.
IDLE_QUIT_S = 90.0
FIRST_PAGE_S = 120.0
_pages_lock = threading.Lock()
_pages = 0
_last_page: float | None = None  # time.monotonic() when a page last opened or closed; None: no page yet


def _page_opened() -> None:
    global _pages, _last_page
    with _pages_lock:
        _pages += 1
        _last_page = time.monotonic()


def _page_closed() -> None:
    global _pages, _last_page
    with _pages_lock:
        _pages = max(0, _pages - 1)
        _last_page = time.monotonic()


def _should_quit(now: float, pages: int, last_seen: float | None, started: float, busy: bool) -> bool:
    """Whether an app-mode server should shut down: no page open, nothing busy (a crew at work, a recording), and
    IDLE_QUIT_S since the last page went (FIRST_PAGE_S since start if no page ever came)."""
    if pages > 0 or busy:
        return False
    if last_seen is None:
        return now - started >= FIRST_PAGE_S
    return now - last_seen >= IDLE_QUIT_S


def _busy() -> bool:
    """A crew hosted by this server is running, or the live engine is recording."""
    with host._lock:
        crews = list(host._hosts.values())
    return any(h["thread"].is_alive() for h in crews) or livehost._rec is not None


def _idle_watch(srv, started: float, every: float = 5.0) -> None:
    while not srv.should_exit:
        time.sleep(every)
        with _pages_lock:
            pages, last = _pages, _last_page
        if _should_quit(time.monotonic(), pages, last, started, _busy()):
            print("tunetrack app: no window open, shutting down", flush=True)
            srv.should_exit = True


def main(port: int = PORT, idle_quit: bool = False) -> None:
    import uvicorn
    config.ensure_dirs()
    gear.start_watch()  # new plugins and libraries are scanned as they arrive
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    if idle_quit:
        threading.Thread(target=_idle_watch, args=(srv, time.monotonic()), daemon=True, name="idle-quit").start()
    try:
        srv.run()
    finally:
        host.stop_all()  # agents the editor started don't outlive it
        livehost.off()   # nor does the live engine (it holds the audio interface)


if __name__ == "__main__":
    main(idle_quit="--idle-quit" in sys.argv[1:])
