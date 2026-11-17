"""Picture: a video locked to the song, its hit points, and the tempo that makes the hits land on beats.

The picture lives in seconds, the music in beats. `Project.picture["start"]` is the song time (seconds) at which
the video's first frame plays; a hit is a moment of the picture (`t`, seconds from its first frame), so it stays
with the picture whatever the tempo does. With no video, picture time is song time.

    <session>/picture/<name>.mp4          the video (copied in)
    <session>/picture/<name>.proxy.mp4    what the browser plays, when the original is not something it can
    <session>/picture/<name>.scan.json    cuts and production-sound levels (found once per file)
    <session>/picture/<name>.strip.jpg    thumbnails for the Desk's picture lane
    <session>/picture/stills/             screenshots (mood references, no timeline)

Needs ffmpeg and ffprobe on PATH.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np

from .model import Project, session_root

VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".wmv"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
WEB_CODECS = {"h264", "vp8", "vp9", "av1"}
WEB_CONTAINERS = {".mp4", ".m4v", ".webm", ".mov"}
SCAN_FLOOR = 0.08   # scene scores kept in the scan file; a threshold is applied when it is read
CUT = 0.3           # default scene score that counts as a cut
STRIP_TILES = 120
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---- ffmpeg ---------------------------------------------------------------------------------------------------------

def _tool(name: str) -> str:
    exe = shutil.which(name)
    if not exe:
        raise RuntimeError(f"{name} is not on PATH: picture work needs ffmpeg (winget install Gyan.FFmpeg)")
    return exe


def _run(args: list, binary: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run([str(a) for a in args], capture_output=True, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    if check and r.returncode:
        raise RuntimeError(f"{Path(str(args[0])).stem} failed: {r.stderr.decode('utf-8', 'replace').strip()[-400:]}")
    if not binary:
        r.stdout = r.stdout.decode("utf-8", "replace")
    return r


def _ffmpeg(*args, binary: bool = False, check: bool = True) -> subprocess.CompletedProcess:
    return _run([_tool("ffmpeg"), "-hide_banner", "-nostdin", "-loglevel", "error", *args], binary, check)


def probe(file: str | Path) -> dict:
    """What a video file is: duration, frame rate, size, codec, whether it has sound, its start timecode."""
    r = _run([_tool("ffprobe"), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", file])
    d = json.loads(r.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    if not v:
        raise ValueError(f"{Path(file).name} has no video stream")
    num, _, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    if not fps:
        num, _, den = (v.get("r_frame_rate") or "25/1").partition("/")
        fps = float(num) / float(den or 1)
    fmt = d.get("format") or {}
    dur = float(v.get("duration") or fmt.get("duration") or 0)
    tags = [s.get("tags") or {} for s in d.get("streams", [])] + [fmt.get("tags") or {}]
    tc = next((t["timecode"] for t in tags if t.get("timecode")), None)
    out = {"duration": round(dur, 3), "fps": round(fps, 3), "size": [int(v.get("width", 0)), int(v.get("height", 0))],
           "codec": v.get("codec_name"), "audio": any(s.get("codec_type") == "audio" for s in d.get("streams", []))}
    if tc:
        out["tc"] = tc.replace(";", ":")
    if _drop(fps):  # NTSC rates: drop-frame when the file says so (";"), and when it has no timecode at all
        out["df"] = ";" in tc if tc else True
    return out


# ---- time -----------------------------------------------------------------------------------------------------------

def _drop(fps: float) -> int:
    """Frame numbers drop-frame timecode skips at each minute but every tenth: 2 at 29.97, 4 at 59.94, else 0."""
    return 2 if abs(fps - 29.97) < 0.01 else 4 if abs(fps - 59.94) < 0.02 else 0


def _tc_frames(tc: str, fps: float, df: bool = False) -> int:
    h, m, s, f = (int(x) for x in re.split(r"[:;]", tc))
    n = ((h * 60 + m) * 60 + s) * max(1, round(fps)) + f
    if df and (d := _drop(fps)):
        mins = h * 60 + m
        n -= d * (mins - mins // 10)
    return n


def timecode(t: float, fps: float, tc_start: str | None = None, df: bool = False) -> str:
    """Picture seconds as HH:MM:SS:FF, or HH:MM:SS;FF in drop-frame (29.97 and 59.94: the labels skip frame
    numbers so the timecode keeps up with the clock; frames themselves are never dropped)."""
    fps = fps or 25.0
    base, d = max(1, round(fps)), (_drop(fps) if df else 0)
    n = int(round(t * fps)) + (_tc_frames(tc_start, fps, df) if tc_start else 0)
    sign, n = ("-" if n < 0 else ""), abs(n)
    if d:
        tens, r = divmod(n, base * 600 - d * 9)
        n += d * 9 * tens + (d * ((r - d) // (base * 60 - d)) if r > d else 0)
    sep = ";" if d else ":"
    return f"{sign}{n // (base * 3600):02d}:{n // (base * 60) % 60:02d}:{n // base % 60:02d}{sep}{n % base:02d}"


def parse_time(v, fps: float = 25.0, tc_start: str | None = None, df: bool = False) -> float:
    """A moment of the picture in seconds from its first frame. Accepts seconds (12.4), "m:ss.s" ("1:02.5"),
    and timecode "HH:MM:SS:FF" (measured from the file's start timecode when it has one; "HH:MM:SS;FF" or a
    drop-frame picture reads it as drop-frame)."""
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().lower().removesuffix("s").strip()
    if re.fullmatch(r"-?\d+(\.\d+)?", s):
        return float(s)
    if re.fullmatch(r"\d+[:;]\d+[:;]\d+[:;]\d+", s):
        df = bool(_drop(fps)) and (df or ";" in s)
        return (_tc_frames(s, fps, df) - (_tc_frames(tc_start, fps, df) if tc_start else 0)) / (fps or 25.0)
    m = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", s)
    if m:
        return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    raise ValueError(f"Can't read the time {v!r}: use seconds (12.4), 'm:ss.s' or timecode 'HH:MM:SS:FF'")


def start_of(p: Project) -> float:
    return float((p.picture or {}).get("start", 0.0))


def song_sec(p: Project, t: float) -> float:
    return float(t) + start_of(p)


def _fps(p: Project) -> float:
    return float((p.picture or {}).get("fps") or 25.0)


def tc_args(p: Project) -> tuple[float, str | None, bool]:
    """(fps, start timecode, drop-frame) of the project's picture: what timecode() and parse_time() take."""
    pic = p.picture or {}
    return _fps(p), pic.get("tc"), bool(pic.get("df"))


# ---- files ----------------------------------------------------------------------------------------------------------

def folder(p: Project) -> Path:
    return p.root / "picture"


def video_file(p: Project, web: bool = False) -> Path | None:
    pic = p.picture or {}
    rel = (pic.get("proxy") if web else None) or pic.get("file")
    return (p.root / rel) if rel else None


def _side(p: Project, suffix: str) -> Path:
    return (p.root / p.picture["file"]).with_suffix(suffix)


def _dest(folder_: Path, src: Path) -> Path:
    """Where a file is copied to: its own name, numbered when another file already has it."""
    folder_.mkdir(parents=True, exist_ok=True)
    dst = folder_ / src.name
    n = 2
    while dst.exists() and not (dst.stat().st_size == src.stat().st_size and dst.stat().st_mtime_ns == src.stat().st_mtime_ns):
        dst = folder_ / f"{src.stem}-{n}{src.suffix}"
        n += 1
    return dst


def set_video(p: Project, file: str | Path, start: float = 0.0) -> dict:
    """Copy a video into the session and make it the picture (the caller saves the project)."""
    src = Path(file).expanduser()
    if not src.is_file():
        raise FileNotFoundError(f"No file {src}")
    if src.suffix.lower() not in VIDEO_EXT:
        raise ValueError(f"{src.name} is not a video TuneTrack knows ({', '.join(sorted(VIDEO_EXT))})")
    info = probe(src)
    dst = src if folder(p).resolve() in src.resolve().parents else _dest(folder(p), src)
    if dst != src and not dst.exists():
        shutil.copy2(src, dst)
    pic = {"file": dst.relative_to(p.root).as_posix(), "start": round(float(start), 4), **info}
    if info["codec"] not in WEB_CODECS or dst.suffix.lower() not in WEB_CONTAINERS or max(info["size"]) > 1920:
        proxy = dst.with_suffix(".proxy.mp4")
        if not proxy.exists():
            _ffmpeg("-y", "-i", dst, "-vf", "scale=-2:'min(720,ih)'", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "23", "-g", "12", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", proxy)
        pic["proxy"] = proxy.relative_to(p.root).as_posix()
    p.picture = pic
    return pic


def add_still(p: Project, file: str | Path, note: str = "") -> dict:
    """Copy a screenshot into the session as a mood reference (kept in the brief)."""
    src = Path(file).expanduser()
    if not src.is_file():
        raise FileNotFoundError(f"No file {src}")
    dst = _dest(folder(p) / "stills", src)
    if not dst.exists():
        shutil.copy2(src, dst)
    rel = dst.relative_to(p.root).as_posix()
    stills = [s for s in p.brief.get("stills", []) if s.get("file") != rel]
    stills.append({"file": rel, **({"note": note} if note else {})})
    p.brief["stills"] = stills
    return stills[-1]


def remove_still(p: Project, which: str | int) -> None:
    """Take a screenshot out of the brief: its number (from 1) or its file. The file stays in the session folder
    (a take may still show it)."""
    stills = list(p.brief.get("stills", []))
    s = str(which).strip()
    i = int(s) - 1 if s.isdigit() else next((k for k, x in enumerate(stills) if x["file"] == s or Path(x["file"]).name == s), -1)
    if not 0 <= i < len(stills):
        raise ValueError(f"No screenshot {which!r}: there are {len(stills)}")
    del stills[i]
    if stills:
        p.brief["stills"] = stills
    else:
        p.brief.pop("stills", None)


# ---- reading the picture ------------------------------------------------------------------------------------------

def _scan_cuts(video: Path) -> list[list[float]]:
    r = _ffmpeg("-i", video, "-an", "-vf", f"scale=320:-2,select='gt(scene,{SCAN_FLOOR})',metadata=print:file=-",
                "-f", "null", "-")
    out, t = [], None
    for line in r.stdout.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", line)
        if m:
            t = float(m.group(1))
        m = re.search(r"scene_score=([0-9.]+)", line)
        if m and t is not None:
            out.append([round(t, 3), round(float(m.group(1)), 3)])
    return out


def _scan_sound(video: Path) -> dict:
    """The production sound: its level twice a second, and sudden loud moments (candidates for hit points)."""
    sr, win = 16000, 800  # 50 ms
    raw = _ffmpeg("-i", video, "-vn", "-ac", "1", "-ar", sr, "-f", "f32le", "-", binary=True).stdout
    x = np.frombuffer(raw, dtype=np.float32)
    if x.size < win * 4:
        return {}
    n = x.size // win
    rms = np.sqrt(np.mean(x[:n * win].reshape(n, win).astype(np.float64) ** 2, axis=1))
    db = 20 * np.log10(np.maximum(rms, 1e-6))
    half = [round(float(10 * np.log10(np.mean(10 ** (db[i:i + 10] / 10)))), 1) for i in range(0, n, 10)]
    before = np.array([db[max(0, i - 10):i].mean() if i else db[0] for i in range(n)])
    jump = db - before
    peaks = []
    for i in np.argsort(-jump):
        if jump[i] < 12 or db[i] < -30:
            break
        t = i * win / sr
        if all(abs(t - q[0]) >= 0.5 for q in peaks):
            peaks.append([round(t, 2), round(float(db[i]), 1)])
        if len(peaks) >= 40:
            break
    return {"level": half, "peaks": sorted(peaks)}


def _make_strip(video: Path, out: Path, duration: float) -> dict:
    step = max(1.0, round(duration / STRIP_TILES, 2))
    n = max(1, math.ceil(duration / step))
    _ffmpeg("-y", "-i", video, "-an", "-vf", f"fps=1/{step},scale=128:72:force_original_aspect_ratio=decrease,"
            f"pad=128:72:(ow-iw)/2:(oh-ih)/2,tile={n}x1", "-frames:v", "1", "-q:v", "5", out)
    return {"step": step, "n": n, "w": 128, "h": 72}


def scan(p: Project, redo: bool = False) -> dict:
    """Cuts (with their scene scores), production-sound levels and the thumbnail strip of the project's picture.
    Found once per video and kept beside it."""
    if not p.picture:
        return {}
    f = _side(p, ".scan.json")
    video = p.root / p.picture["file"]
    mark = [video.stat().st_size, video.stat().st_mtime_ns]
    if f.exists() and not redo:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if d.get("mark") == mark:
                return d
        except ValueError:
            pass
    small = video_file(p, web=True) or video
    d = {"mark": mark, "scores": _scan_cuts(small), "sound": _scan_sound(video) if p.picture.get("audio") else {}}
    try:
        d["strip"] = _make_strip(small, _side(p, ".strip.jpg"), float(p.picture.get("duration") or 0))
    except RuntimeError:
        d["strip"] = None
    f.write_text(json.dumps(d), encoding="utf-8")
    return d


def view(p: Project) -> dict | None:
    """What the Desk draws of the picture besides the video itself: cuts and the thumbnail strip. Only what a scan
    has already found (a scan runs when the picture is set, not when a page loads)."""
    if not p.picture:
        return None
    try:
        d = json.loads(_side(p, ".scan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"cuts": [], "strip": None}
    cut, last = [], -1.0
    for t, score in d.get("scores", []):
        if score >= CUT and t - last >= 0.25:
            cut.append(t)
            last = t
    return {"cuts": cut, "strip": d.get("strip") if _side(p, ".strip.jpg").exists() else None}


def cuts(p: Project, threshold: float = CUT) -> list[float]:
    """Picture times where the shot changes. Cuts closer than a quarter second to the one before are one cut."""
    out: list[float] = []
    for t, score in scan(p).get("scores", []):
        if score >= threshold and (not out or t - out[-1] >= 0.25):
            out.append(t)
    return out


def _loud(level: list[float]) -> list[list[float]]:
    """Stretches where the production sound is up (dialogue, effects): [from, to, mean dB], gaps under 1 s joined."""
    if not level:
        return []
    arr = np.array(level)
    gate = max(-38.0, float(np.percentile(arr, 90)) - 14.0)
    spans: list[list[float]] = []
    for i, v in enumerate(arr):
        if v < gate:
            continue
        a = i * 0.5
        if spans and a - spans[-1][1] <= 1.0:
            spans[-1][1] = a + 0.5
            spans[-1][2].append(v)
        else:
            spans.append([a, a + 0.5, [v]])
    return [[a, b, round(float(np.mean(vs)), 1)] for a, b, vs in spans if b - a >= 1.0]


def frame_jpeg(video: Path, t: float, width: int = 320) -> bytes:
    r = _ffmpeg("-ss", f"{max(0.0, t):.3f}", "-i", video, "-frames:v", "1", "-vf", f"scale={width}:-2",
                "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "4", "-", binary=True, check=False)
    return r.stdout


def _tile(tiles: list, out: Path, cols: int, width: int) -> None:
    """tiles: (label, PIL image) in reading order -> one image with the label above each."""
    from PIL import Image, ImageDraw
    tiles = [(lab, im.resize((width, max(1, round(im.height * width / im.width))))) for lab, im in tiles]
    h = max(im.height for _, im in tiles)
    cols = max(1, min(cols, len(tiles)))
    img = Image.new("RGB", (cols * width, math.ceil(len(tiles) / cols) * (h + 16)), (12, 12, 13))
    draw = ImageDraw.Draw(img)
    for i, (lab, im) in enumerate(tiles):
        x, y = (i % cols) * width, (i // cols) * (h + 16)
        img.paste(im, (x, y + 16))
        draw.text((x + 3, y + 3), lab, fill=(255, 190, 60))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, quality=80)


def sheet(p: Project, times: list[float], out: Path, cols: int = 4, width: int = 320) -> dict:
    """One image of frames in reading order, each labelled with its number, timecode and bar:beat."""
    from PIL import Image
    video = video_file(p, web=True)
    if not video or not video.exists():
        raise ValueError("this project has no picture: set one with picture(file=...)")
    (fps, tc0, df), tm = tc_args(p), p.tempo_map()
    tiles, listed = [], []
    for t in times:
        data = frame_jpeg(video, t, width)
        if not data:
            continue
        s = song_sec(p, t)
        at = p.fmt_pos(round(tm.beat(s), 2)) if s >= 0 else "before the music"
        listed.append({"n": len(tiles) + 1, "t": round(t, 2), "at": at})
        tiles.append((f"{len(tiles) + 1}  {timecode(t, fps, tc0, df)}  {at}", Image.open(io.BytesIO(data)).convert("RGB")))
    if not tiles:
        raise RuntimeError("no frames could be read from the picture")
    _tile(tiles, out, cols, width)
    return {"file": str(out), "frames": listed}


def stills_sheet(p: Project, out: Path, cols: int = 3, width: int = 420) -> dict:
    """The session's screenshots on one image, numbered."""
    from PIL import Image
    stills = [s for s in p.brief.get("stills", []) if (p.root / s["file"]).is_file()]
    if not stills:
        raise ValueError("no screenshots in this session: add one with picture(file=<image>)")
    tiles = [(f"{i + 1}  {Path(s['file']).name}", Image.open(p.root / s["file"]).convert("RGB")) for i, s in enumerate(stills)]
    _tile(tiles, out, cols, width)
    return {"file": str(out), "stills": [{"n": i + 1, **s} for i, s in enumerate(stills)]}


# ---- hit points -----------------------------------------------------------------------------------------------------

def _grid_off(beat: float, grid: float) -> float:
    return beat - round(beat / grid) * grid


def hit_rows(p: Project, hits: list[dict] | None = None, tm=None) -> list[dict]:
    """Each hit with where it falls in the music: bar:beat, and how far it is from the nearest beat (ms; + = the
    hit comes after the beat). `on`: "bar" or "beat" when it is within half a frame of one."""
    tm = tm or p.tempo_map()
    (fps, tc0, df), bpb = tc_args(p), p.beats_per_bar
    near = 0.5 / fps + 1e-6
    rows = []
    for h in sorted(hits if hits is not None else p.hits, key=lambda h: h["t"]):
        s = song_sec(p, h["t"])
        row = {"name": h["name"], "t": round(h["t"], 3), **({"tc": timecode(h["t"], fps, tc0, df)} if p.picture else {})}
        if h.get("soft"):
            row["soft"] = True
        if s < 0:
            row["at"] = "before the music"
        else:
            b = tm.beat(s)
            nb = round(b)
            off = s - tm.sec(nb)
            row.update(at=p.fmt_pos(round(b, 2)), off_ms=round(off * 1000))
            if abs(off) <= near:
                row["on"] = "bar" if abs(_grid_off(nb, bpb)) < 1e-6 else "beat"
                row["at"] = p.fmt_pos(nb)
        rows.append(row)
    return rows


def edit_hits(p: Project, add: list | None = None, remove: list | None = None, clear: bool = False) -> None:
    fps, tc0, df = tc_args(p)
    if clear:
        p.hits = []
    for r in remove or []:
        try:
            t = parse_time(r, fps, tc0, df)
        except ValueError:
            t = None
        name = str(r).strip().lower()
        p.hits = [h for h in p.hits if h["name"].lower() != name and (t is None or abs(h["t"] - t) > 0.5 / fps)]
    for a in add or []:
        if "at" in a:  # a place in the music: the picture moment that is there now
            t = p.tempo_map().sec(p.pos(a["at"])) - start_of(p)
        else:
            t = parse_time(a.get("t", a.get("time")), fps, tc0, df)
        name = str(a.get("name") or f"Hit {len(p.hits) + 1}")
        p.hits = [h for h in p.hits if abs(h["t"] - t) > 0.5 / fps]
        p.hits.append({"t": round(t, 4), "name": name, **({"soft": True} if a.get("soft") else {})})
    p.hits.sort(key=lambda h: h["t"])


# ---- tempo ----------------------------------------------------------------------------------------------------------

def _errors(secs: np.ndarray, bpm: float, grid: float) -> np.ndarray:
    beats = secs * bpm / 60.0
    return (beats - np.round(beats / grid) * grid) * 60.0 / bpm


def _shift(secs: np.ndarray, w: np.ndarray, bpm: float, grid: float, start: float) -> float:
    """How far to move the music against the picture (seconds; + = the picture's first frame later in the song) so
    the hits sit best on the grid: the weighted circular mean of where they fall in a beat. Under half a beat
    either way, except that a picture starting at or after the music is not pushed before it."""
    period = grid * 60.0 / bpm
    z = np.sum(w * np.exp(2j * np.pi * secs / period))
    d = -float(np.angle(z)) / (2 * np.pi) * period if abs(z) > 1e-9 else 0.0
    return d + period if start >= 0 and start + d < 0 else d


def fit_constant(p: Project, low: float, high: float, grid: float = 1.0, top: int = 5,
                 free_start: bool = False) -> list[dict]:
    """Steady tempos, best first: the ones at which the hits fall nearest a beat (soft hits count a third).
    free_start: the picture's first frame may move too (by under a beat), so where the music enters is chosen with
    the tempo; each candidate then says the `start` it needs."""
    if free_start and not p.picture:
        raise ValueError("a free start moves the picture against the music: set a picture first")
    hits = [h for h in p.hits if song_sec(p, h["t"]) > 0]
    if not hits:
        raise ValueError("no hit points after the start of the music: add them with hits(add=[...])")
    secs = np.array([song_sec(p, h["t"]) for h in hits])
    w = np.array([1 / 3 if h.get("soft") else 1.0 for h in hits])
    start = start_of(p)
    shift = (lambda b: _shift(secs, w, b, grid, start)) if free_start else (lambda b: 0.0)  # noqa: E731

    def cost(b: float) -> float:
        return float(np.sum(w * _errors(secs + shift(b), b, grid) ** 2))

    bpms = np.arange(low, high + 1e-9, 0.05)
    costs = np.array([cost(b) for b in bpms])
    order, picked = np.argsort(costs), []
    for i in order:
        if all(abs(bpms[i] - bpms[j]) >= 1.5 for j in picked):
            picked.append(i)
        if len(picked) >= top:
            break
    out = []
    for i in picked:
        # refine around the candidate: the grid search steps 0.05 bpm, a late hit moves more than a frame in that
        fine = np.arange(bpms[i] - 0.05, bpms[i] + 0.05, 0.002)
        bpm = round(float(fine[int(np.argmin([cost(b) for b in fine]))]), 3)
        d = shift(bpm)
        err = _errors(secs + d, bpm, grid) * 1000
        c = {"tempo": bpm, "rms_ms": round(math.sqrt(float(np.sum(w * err ** 2) / w.sum())), 1),
             "worst_ms": round(float(np.max(np.abs(err)))),
             "hits": {h["name"]: round(float(e)) for h, e in zip(hits, err)}}
        if free_start:
            c.update(start=round(start + d, 4), shift_ms=round(d * 1000))
        out.append(c)
    return sorted(out, key=lambda c: c["rms_ms"])


def best_start(p: Project, bpm: float, grid: float = 1.0) -> float:
    """The picture start at which the hits sit best on the grid of a steady `bpm` (see fit_constant)."""
    hits = [h for h in p.hits if song_sec(p, h["t"]) > 0]
    if not hits:
        return start_of(p)
    secs = np.array([song_sec(p, h["t"]) for h in hits])
    w = np.array([1 / 3 if h.get("soft") else 1.0 for h in hits])
    return round(start_of(p) + _shift(secs, w, bpm, grid, start_of(p)), 4)


def fit_map(p: Project, tempo: float, bend: float = 8.0, on: str = "beat") -> dict:
    """A tempo map that puts every hard hit exactly on a beat (on="bar": on a bar line): between two hits the tempo
    is nudged, by at most `bend` percent of `tempo`, so the next one arrives on the grid. A hit that would need
    more is left where it falls. Changes sit on the beat of the hit before."""
    grid = p.beats_per_bar if on == "bar" else 1.0
    lo, hi = tempo * (1 - bend / 100), tempo * (1 + bend / 100)
    beat, sec, changes, missed = 0.0, 0.0, [], []
    first = None
    for h in sorted(p.hits, key=lambda h: h["t"]):
        s = song_sec(p, h["t"])
        if h.get("soft") or s <= sec + 0.05:
            continue
        want = (s - sec) * tempo / 60.0
        best = None
        for k in (math.floor(want / grid) * grid, math.ceil(want / grid) * grid):
            if k <= 0:
                continue
            bpm = 60.0 * k / (s - sec)
            if lo <= bpm <= hi and (best is None or abs(bpm - tempo) < abs(best[1] - tempo)):
                best = (k, bpm)
        if not best:
            missed.append(h["name"])
            continue
        k, bpm = best
        if first is None:
            first = bpm
        else:
            changes.append([round(beat, 6), round(bpm, 6)])
        beat, sec = beat + k, s
    if first is None:
        raise ValueError("no hard hit could be reached within the bend: widen it, or try another tempo")
    if abs((changes[-1][1] if changes else first) - tempo) > 1e-6:
        changes.append([round(beat, 6), round(float(tempo), 6)])  # after the last hit: back to the tempo asked for
    return {"tempo": round(first, 6), "tempo_changes": changes, "missed": missed}


# ---- the guide ------------------------------------------------------------------------------------------------------

def guide_stem(p: Project, sr: int, cache: Path) -> Path:
    """The picture's own sound as a stereo WAV at the song's rate that starts at song time 0 (silence before the
    first frame, cut where the picture starts before the music): what the live engine plays as the guide. Made
    once per video, start and rate, kept in the render cache."""
    video = p.root / p.picture["file"]
    st, start = video.stat(), start_of(p)
    key = hashlib.sha1(f"{video.name}|{st.st_size}|{st.st_mtime_ns}|{start:.6f}|{int(sr)}".encode()).hexdigest()[:16]
    out = Path(cache) / f"guide-{key}.wav"
    if out.exists():
        return out
    n = int(round(start * sr))
    place = f",adelay=delays={n}S:all=1" if n > 0 else f",atrim=start_sample={-n},asetpts=PTS-STARTPTS" if n < 0 else ""
    part = out.with_name(out.stem + ".part.wav")
    out.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg("-y", "-i", video, "-vn", "-af", f"aresample={int(sr)}{place}", "-ac", "2", "-ar", int(sr),
            "-c:a", "pcm_f32le", "-f", "wav", part)
    part.replace(out)
    return out


# ---- delivery -------------------------------------------------------------------------------------------------------

def mux(p: Project, audio: str | Path, out: str | Path) -> str:
    """The picture with the music on it: the video from its first frame, the music where it sits against it."""
    video = video_file(p)
    if not video or not video.exists():
        raise ValueError("this project has no picture: set one with picture(file=...)")
    out, start = Path(out), start_of(p)
    out.parent.mkdir(parents=True, exist_ok=True)
    a_in = ["-ss", f"{start:.4f}", "-i", audio] if start > 0 else ["-itsoffset", f"{-start:.4f}", "-i", audio]
    copy = p.picture.get("codec") == "h264" and out.suffix.lower() in (".mp4", ".mov", ".mkv")
    v_out = ["-c:v", "copy"] if copy else (["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0"] if out.suffix.lower() == ".webm"
                                           else ["-c:v", "libx264", "-crf", "18", "-preset", "fast", "-pix_fmt", "yuv420p"])
    a_out = ["-c:a", "libopus", "-b:a", "160k"] if out.suffix.lower() == ".webm" else ["-c:a", "aac", "-b:a", "256k"]
    _ffmpeg("-y", "-i", video, *a_in, "-map", "0:v:0", "-map", "1:a:0", *v_out, *a_out,
            "-t", f"{float(p.picture.get('duration') or 0):.3f}", out)
    return str(out)
