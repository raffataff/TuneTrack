"""Tracking: what a recording becomes in the project, and comping between passes.

The live engine records one continuous stretch of device time per recording: a WAV of each armed audio track's
input, the MIDI keyboard's events, and `marks` saying where the transport was (one at the start, one each time it
jumped back round the loop). Here that becomes clips.

A **pass** is one time through: each round of the loop, and each new recording over old material. Every clip
carries the pass it belongs to in `Clip.lane` (0 = not recorded, or there before any recording). Passes of one
track share a stretch of time and one of them is heard: the others are muted clips, which the renderer already
skips. So comping is ordinary clip editing (`comp`: cut at the edges, mute and unmute), nothing is ever deleted,
and a project with passes plays the same in every renderer.

Latency: what the player hears is late by the plugins' latency and the way out of the interface, and what they
play arrives late by the way in. A sample that arrived while the engine was at song position P was played against
the music at P - comp, so everything is moved earlier by comp (`record_offset_ms` in the library config adds a
correction for a driver that reports its latency wrong).
"""
from __future__ import annotations

import math
import time
from pathlib import Path

from . import config
from .model import Clip, Note, Project, Track

DECLICK = 0.004    # seconds: the fade either side of a cut the comp makes in audio
MIN_PASS = 0.25    # seconds: a shorter pass is the tail of stopping, not a pass
EPS = 1e-6


class NothingRecorded(RuntimeError):
    pass


# ---- where the recording was --------------------------------------------------------------------------------

def _pairs(flat: list) -> list[tuple[int, int]]:
    return [(int(flat[i]), int(flat[i + 1])) for i in range(0, len(flat) - 1, 2)]


def passes(marks: list, total: int, comp: int, floor: int = 0, ceil: int | None = None) -> list[dict]:
    """The passes of a recording: [{"at": first sample in the recording, "pos": song sample it was played at,
    "n": samples}]. marks: flat [samples into the recording, song position, ...]; comp: how late the recording is.
    Nothing before song sample `floor` is kept (a count-in, or the latency before the punch), and nothing from
    `ceil` on (the punch out)."""
    ms = _pairs(marks)
    out = []
    for k, (at, pos) in enumerate(ms):
        # the first `comp` samples after a jump were still played against the end of the pass before
        a = at + (comp if k else 0)
        b = (ms[k + 1][0] + comp) if k + 1 < len(ms) else total
        b = min(b, total)
        p = pos if k else pos - comp
        if p < floor:
            a, p = a + (floor - p), floor
        if ceil is not None:
            b = min(b, a + ceil - p)
        if b - a > 0:
            out.append({"k": k, "at": a, "pos": p, "n": b - a})
    return out


def locate(at: int, marks: list, comp: int) -> tuple[int, int]:
    """(pass index, song sample) of one moment of the recording (a keyboard event)."""
    ms = _pairs(marks)
    k = 0
    for i, (a, _pos) in enumerate(ms):
        if i and a + comp <= at:
            k = i
    a, pos = ms[k]
    return k, pos + (at - a) - comp


def offset_samples(sr: int) -> int:
    return int(round(float(config.load_config().get("record_offset_ms", 0.0)) * sr / 1000.0))


def at_project_rate(rec: dict) -> dict:
    """A recording made through a device that could not run at the project's rate: the files are at the device's
    rate (`file_sr`) and so is every time counted in the recording (marks, events, the device's latency). The same
    recording with those times in project samples; a clip's place in its file is in seconds, so nothing else moves."""
    sr, fsr = int(rec["sr"]), int(rec.get("file_sr") or rec["sr"])
    if fsr == sr:
        return rec
    k = sr / fsr
    marks, ev = list(rec.get("marks") or []), list(rec.get("midi") or [])
    marks[0::2] = [int(round(a * k)) for a in marks[0::2]]
    ev[0::4] = [int(round(a * k)) for a in ev[0::4]]
    c = dict(rec.get("comp") or {})
    for way in ("out", "in"):
        c[way] = int(round(int(c.get(way, 0)) * k))
    return {**rec, "marks": marks, "midi": ev, "comp": c, "samples": int(round(int(rec["samples"]) * k)), "file_sr": sr}


def _file_stamp(file: str | None) -> str | None:
    """The recording a recorded file belongs to: audio/rec/<stamp>-<track>.wav."""
    name = Path(file or "").name
    return name.rsplit("-", 1)[0] if (file or "").startswith("audio/rec/") and "-" in name else None


def _mark(c: Clip) -> tuple[str | None, int | None]:
    """(which recording, which time round the loop) a pass came from; the round is None for audio recorded
    before clips carried `rec` (only the file name says which recording)."""
    if c.rec:
        stamp, _, k = c.rec.partition("/")
        return stamp, int(k) if k.isdigit() else None
    return _file_stamp(c.file), None


def _stamp(c: Clip) -> str | None:
    """Which recording a clip came from (tracks recorded together share it)."""
    return _mark(c)[0]


def mates(p: Project, t: Track, start: float, end: float, use: int) -> list[tuple[Track, int]]:
    """The other tracks recorded together with pass `use` of this one over a stretch (the mics of a drum kit, or
    a keyboard played with them), each with its own number for that pass: the same time round the loop. Passes
    that carry their round are matched by it; older audio passes by counting from the first pass of that
    recording on each track."""
    inside = lambda c: c.start < end - EPS and c.end > start + EPS  # noqa: E731
    mine = [_mark(c) for c in t.clips if c.lane == use and inside(c)]
    stamps = {s for s, _ in mine} - {None}
    if not stamps:
        return []
    rounds = {m for m in mine if m[0] and m[1] is not None}
    first = min(c.lane for c in t.clips if _stamp(c) in stamps)
    out = []
    for o in p.tracks:
        theirs = [c for c in o.clips if _stamp(c) in stamps] if o is not t else []
        if not theirs:
            continue
        if rounds and all(_mark(c)[1] is not None for c in theirs):
            # both say their round: the same one, or nothing (a MIDI track that sat that round out)
            same = [c for c in theirs if _mark(c) in rounds and inside(c)]
            if same:
                out.append((o, same[0].lane))
            continue
        want = min(c.lane for c in theirs) + (use - first)
        if any(c.lane == want and inside(c) for c in theirs):
            out.append((o, want))
    return out


# ---- clips -----------------------------------------------------------------------------------------------------

def split_clip(p: Project, t: Track, c: Clip, at: float) -> Clip:
    """Cut a clip in two at a beat; returns the right half (a new clip on the same track)."""
    cut = at - c.start
    if not 0 < cut < c.length:
        raise ValueError("split point must be inside the clip")
    tm = p.tempo_map()
    src_cut = cut * 60 / c.bpm if c.bpm else tm.sec(c.start + cut) - tm.sec(c.start)  # source seconds
    right = Clip(id=p.new_id("c"), kind=c.kind, name=c.name, start=c.start + cut, length=c.length - cut,
                 file=c.file, offset=c.offset + src_cut, gain_db=c.gain_db, fade_out=c.fade_out,
                 bpm=c.bpm, pitch=c.pitch, muted=c.muted, color=c.color, lane=c.lane, rec=c.rec)
    right.notes = [Note(n.pitch, n.start - cut, n.dur, n.vel) for n in c.notes if n.start >= cut]
    right.cc = {k: [[b - cut, v] for b, v in pts if b >= cut] for k, pts in c.cc.items()}
    c.notes = [n for n in c.notes if n.start < cut]
    for n in c.notes:
        n.dur = min(n.dur, cut - n.start)
    c.cc = {k: [pt for pt in pts if pt[0] < cut] for k, pts in c.cc.items()}
    c.length = cut
    c.fade_out = 0
    t.clips.append(right)
    return right


def lanes(t: Track) -> dict[int, list[Clip]]:
    out: dict[int, list[Clip]] = {}
    for c in sorted(t.clips, key=lambda c: c.start):
        out.setdefault(c.lane, []).append(c)
    return out


def comp(p: Project, t: Track, start: float, end: float, use: int) -> dict:
    """From start to end (beats), hear pass `use` on this track and mute everything else there."""
    if end - start <= EPS:
        raise ValueError("comp needs an end after its start")
    inside = lambda c: c.start < end - EPS and c.end > start + EPS  # noqa: E731
    have = sorted({c.lane for c in t.clips if inside(c)})
    if use not in have:
        raise ValueError(f"'{t.name}' has no pass {use} between {p.fmt_pos(start)} and {p.fmt_pos(end)}"
                         f" (passes there: {', '.join(map(str, have)) or 'none'})")
    for edge in (start, end):
        for c in list(t.clips):
            if c.start + EPS < edge < c.end - EPS:
                right = split_clip(p, t, c, edge)
                if c.kind == "audio" and not c.bpm:
                    c.fade_out, right.fade_in = DECLICK, DECLICK
    changed = 0
    for c in t.clips:
        if c.start >= start - EPS and c.end <= end + EPS:
            mute = c.lane != use
            changed += c.muted != mute
            c.muted = mute
    _join(p, t)
    return {"heard": use, "from": p.fmt_pos(start), "to": p.fmt_pos(end), "changed": changed,
            "passes": {str(k): [f"{p.fmt_pos(c.start)}-{p.fmt_pos(c.end)}" for c in v if not c.muted]
                       for k, v in lanes(t).items() if any(not c.muted for c in v)}}


def _join(p: Project, t: Track) -> None:
    """Put back together audio clips a comp cut that are heard (or muted) as one again: neighbours of one pass,
    cut from the same place in the same file. Keeps a much-comped track from turning into slivers."""
    tm = p.tempo_map()
    for clips in lanes(t).values():
        i = 0
        while i + 1 < len(clips):
            a, b = clips[i], clips[i + 1]
            flows = (a.kind == "audio" and b.kind == "audio" and a.file == b.file and a.muted == b.muted
                     and not a.bpm and not b.bpm and a.pitch == b.pitch and a.gain_db == b.gain_db
                     and abs(a.end - b.start) < EPS
                     and abs(a.offset + tm.sec(a.end) - tm.sec(a.start) - b.offset) < 2e-4)
            if not flows:
                i += 1
                continue
            a.length = b.end - a.start
            a.fade_out = b.fade_out if b.fade_out != DECLICK else 0.0
            t.clips.remove(b)
            clips.pop(i + 1)


def _next_lane(t: Track) -> int:
    return max((c.lane for c in t.clips), default=0) + 1


def _hear(p: Project, t: Track, made: list[Clip]) -> None:
    """After a recording: each new pass is heard over its own stretch, a later one over an earlier one, and the
    older material under them is muted. The last pass is left out when it is the short tail of stopping
    mid-loop (it stays there, muted, to comp from)."""
    for c in made:
        c.muted = True
    order = [(c.start, c.end, c.lane) for c in made]
    if len(order) > 1 and order[-1][1] - order[-1][0] < 0.6 * (order[-2][1] - order[-2][0]):
        order.pop()
    for start, end, lane in order:
        comp(p, t, start, end, lane)


def _ceil(tm, sr: int, until: float | None) -> int | None:
    return None if until is None else int(round(tm.sec(until) * sr))


def place_audio(p: Project, t: Track, file: str, rec: dict, punch: float = 0.0, until: float | None = None) -> list[Clip]:
    """Put a recorded file on an audio track: one clip per pass, the last full pass heard. file: relative to the
    project's root. punch: the beat recording was meant to start at (nothing before it is kept); until: the punch
    out (nothing from there on is kept)."""
    rec = at_project_rate(rec)
    sr = int(rec["sr"])
    tm = p.tempo_map()
    c = rec.get("comp") or {}
    late = int(c.get("graph", 0)) + int(c.get("out", 0)) + int(c.get("in", 0)) + offset_samples(sr)
    floor = max(0, int(round(tm.sec(max(0.0, punch)) * sr)))
    lane, made, stamp = _next_lane(t), [], _file_stamp(file)
    for ps in passes(rec["marks"], int(rec["samples"]), late, floor, _ceil(tm, sr, until)):
        if ps["n"] < MIN_PASS * sr:
            continue
        start = tm.beat(ps["pos"] / sr)
        clip = Clip(id=p.new_id("c"), kind="audio", name=f"{t.name} {lane}", start=start,
                    length=tm.beat((ps["pos"] + ps["n"]) / sr) - start, file=file, offset=ps["at"] / sr, lane=lane,
                    rec=f"{stamp}/{ps['k']}" if stamp else None)
        t.clips.append(clip)
        made.append(clip)
        lane += 1
    if made:
        _hear(p, t, made)
    return made


def place_midi(p: Project, t: Track, rec: dict, punch: float = 0.0, until: float | None = None,
               quantise: float = 0.0, src: int = 0, stamp: str | None = None) -> list[Clip]:
    """Put the keyboard's recorded events on a MIDI track: one clip per pass, on whole bars, as played. Sustain
    and other controllers go to the clip's cc lanes, pitch bend to "bend". until: the punch out (a note played
    after it is dropped, one held across it ends there). quantise: move each note's start to the nearest multiple
    of this many beats (0 = as played; lengths are kept). src: the keyboard this track listens to (its number in
    the recording's `midi_src`; 0 = every keyboard). The on-screen keys (source 0) play every armed track.
    stamp: the recording's stamp (the one in its audio files' names), kept on each pass with its round so a
    group comp finds the MIDI passes recorded with the audio ones."""
    rec = at_project_rate(rec)
    sr = int(rec["sr"])
    tm = p.tempo_map()
    c = rec.get("comp") or {}
    late = int(c.get("graph", 0)) + int(c.get("out", 0)) + offset_samples(sr)
    ev = rec.get("midi") or []
    floor = max(0, int(round(tm.sec(max(0.0, punch)) * sr)))
    ceil = _ceil(tm, sr, until)
    from_ = rec.get("midi_src") or []
    by_pass: dict[int, list[tuple[float, int, int, int]]] = {}
    for i in range(0, len(ev) - 3, 4):
        if src and i // 4 < len(from_) and int(from_[i // 4]) not in (0, src):
            continue  # another keyboard's
        k, pos = locate(int(ev[i]), rec["marks"], late)
        if pos < floor - 0.05 * sr:
            continue  # played in the count-in
        if ceil is not None and pos >= ceil:
            if (int(ev[i + 1]) & 0xF0) == 0x90 and int(ev[i + 3]) > 0:
                continue  # played after the punch out
            pos = ceil    # let go after it: the note ends there
        by_pass.setdefault(k, []).append((tm.beat(max(floor, pos) / sr), int(ev[i + 1]), int(ev[i + 2]), int(ev[i + 3])))
    spans = {ps["k"]: ps for ps in passes(rec["marks"], int(rec["samples"]), late, floor, ceil)}
    bpb = p.beats_per_bar
    lane, made = _next_lane(t), []
    for k in sorted(by_pass):
        notes, cc, down = [], {}, {}
        last = max(b for b, *_ in by_pass[k])
        for beat, status, d1, d2 in by_pass[k]:
            kind = status & 0xF0
            if kind == 0x90 and d2 > 0:
                down.setdefault(d1, []).append((beat, d2))
            elif kind in (0x80, 0x90):
                if down.get(d1):
                    on, vel = down[d1].pop(0)
                    notes.append((d1, on, max(beat - on, 1 / 64), vel))
            elif kind == 0xB0:
                cc.setdefault(str(d1), []).append([beat, float(d2)])
            elif kind == 0xE0:
                cc.setdefault("bend", []).append([beat, ((d1 | (d2 << 7)) - 8192) / 8192.0])
        end = tm.beat((spans[k]["pos"] + spans[k]["n"]) / sr) if k in spans else last
        for pitch, held in down.items():  # still down when the pass ended
            for on, vel in held:
                notes.append((pitch, on, max(end - on, 1 / 64), vel))
        if not notes:
            continue
        if quantise > 0:
            notes = [(pitch, max(0.0, round(on / quantise) * quantise), dur, vel) for pitch, on, dur, vel in notes]
        first = min(n[1] for n in notes)
        start = math.floor((min(first, tm.beat(spans[k]["pos"] / sr)) if k in spans else first) / bpb + EPS) * bpb
        stop = max(end, max(n[1] + n[2] for n in notes))
        clip = Clip(id=p.new_id("c"), kind="midi", name=f"{t.name} {lane}", start=start,
                    length=max(bpb, math.ceil((stop - start) / bpb - EPS) * bpb), lane=lane,
                    rec=f"{stamp}/{k}" if stamp else None)
        clip.notes = [Note(pitch, on - start, dur, vel) for pitch, on, dur, vel in notes]
        clip.cc = {name: [[b - start, v] for b, v in pts] for name, pts in cc.items()}
        clip.sort()
        t.clips.append(clip)
        made.append(clip)
        lane += 1
    if made:
        _hear(p, t, made)
    return made


def commit(project: str | Path, rec: dict, files: dict[str, str], keys: list[str] | dict[str, int],
           punch: float = 0.0, until: float | None = None, quantise: float = 0.0, stamp: str | None = None) -> dict:
    """Write a finished recording into a project as one edit (one undo step). files: {audio track id: recorded
    file, relative to the project's root}; keys: the MIDI tracks the keyboard was playing, or {track id: which
    keyboard (0 = any)}. until, quantise: see place_midi. stamp: which recording this is (default: the one in the
    files' names, else now), marked on the MIDI passes."""
    from . import api
    if not isinstance(keys, dict):
        keys = {tid: 0 for tid in keys}
    stamp = stamp or next(filter(None, map(_file_stamp, files.values())), None) or time.strftime("%Y%m%d-%H%M%S")
    out: dict = {"clips": [], "heard": {}}
    with api._Edit(str(project)) as p:
        for tid, file in files.items():
            if tid not in (rec.get("audio") or {}):
                continue
            t = p.track(tid)
            made = place_audio(p, t, file, rec, punch, until)
            out["clips"] += [c.id for c in made]
            if made:
                out["heard"][tid] = sorted({c.lane for c in made if not c.muted})
        for tid, src in keys.items():
            t = p.track(tid)
            made = place_midi(p, t, rec, punch, until, quantise, int(src or 0), stamp)
            out["clips"] += [c.id for c in made]
            if made:
                out["heard"][tid] = sorted({c.lane for c in made if not c.muted})
        if not out["clips"]:
            raise NothingRecorded("nothing was recorded (no armed track got a signal long enough to keep)")
    out["dropped"] = int(rec.get("dropped", 0))
    return out


# ---- what the Desk draws ---------------------------------------------------------------------------------------

PEAKS_PER_SEC = 200
_peaks: dict[str, tuple] = {}


def peaks(file: str | Path) -> dict:
    """An audio file as the Desk draws it: {"per_sec", "peaks": bytes}, one byte per 1/200 s or so: the loudest
    sample of any channel there (0-255 for 0-1). Kept in memory until the file changes."""
    import numpy as np
    import soundfile as sf
    f = Path(file)
    st = f.stat()
    mark = (st.st_mtime_ns, st.st_size)
    if _peaks.get(str(f), (None,))[0] != mark:
        with sf.SoundFile(str(f)) as fh:
            sr, step, out = fh.samplerate, max(1, fh.samplerate // PEAKS_PER_SEC), []
            for block in fh.blocks(blocksize=step * 4096, dtype="float32", always_2d=True):
                mono = np.abs(block).max(axis=1)
                out.append(np.pad(mono, (0, (-len(mono)) % step)).reshape(-1, step).max(axis=1))
        pk = np.concatenate(out) if out else np.zeros(0, np.float32)
        _peaks[str(f)] = (mark, {"per_sec": sr / step,
                                 "peaks": np.clip(np.round(pk * 255), 0, 255).astype(np.uint8).tobytes()})
    return _peaks[str(f)][1]
