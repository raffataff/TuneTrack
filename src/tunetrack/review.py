"""A person's review from the Review seat: the sheet they fill in while they listen, and what sending it does.

An agent in the Review seat files its findings with report_write and hands the fixes out with task_add. A person
does the same from the review sheet (web/review.js) in one step, review_send():
- The review is filed as a report (kind "review"). Its findings are kept beside it as data
  (studio/reviews/R<n>.json), so a second listen can tick them off one by one.
- Each seat named on a finding gets one task holding all of its findings: one run and one audition, not one per
  finding. Mix's task waits for the others', since a mix made before the notes change is made twice.
- Findings left to the Producer go to it as one task. What it hands out while on that task is waited for by
  whatever waits on the task (studio._review_links), so nobody keeps depends lists by hand.
- The review task closes with a result the next seat reads. On request a second listen is queued for the Review
  seat, which starts once every fix is in.
Drafts are kept in studio/drafts/, so a long review survives a reload or a closed window.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from . import roles, studio
from .model import _atomic_write

VERDICTS = {"ready": "ready", "fix": "needs work"}
LATE = {"mix"}  # seats whose fixes wait for the other seats' (and the Producer's) from the same review
MAX_FINDINGS = 60
TEXT_MAX = 4000  # of one finding, and of the overall note
DRAFT_MAX = 200_000
NOTE_SHOWN = 400  # of the overall note, in a seat's task when no Producer task carries it
_BARS = re.compile(r"(\d+)(?:\s*-\s*(\d+))?")
_TASK = re.compile(r"T\d+")
_REPORT = re.compile(r"R\d+")


def _task_id(task) -> str | None:
    """A task id as the browser sends it: an id, never a path (it names a draft file)."""
    if task in (None, ""):
        return None
    t = str(task).strip().upper()
    if not _TASK.fullmatch(t):
        raise KeyError(f"No task {task!r}")
    return t


def _draft_file(path: Path, task: str | None) -> Path:
    return path / "studio" / "drafts" / f"review-{task or 'free'}.json"


def stored(report: str, session: str | None = None) -> dict | None:
    """A review as a person sent it (review_send), its findings numbered; None for any other report."""
    rid = str(report or "").strip().upper()
    if not _REPORT.fullmatch(rid):
        return None
    try:
        f = studio.session_path(session) / "studio" / "reviews" / f"{rid}.json"
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def review_draft(task: str | None = None, draft: dict | None = None, session: str | None = None) -> dict:
    """The review sheet's draft for a review task (None: a review that answers no task). With draft, keep it;
    without, read it back, with the earlier review a second listen follows up (its findings, to tick off)."""
    path = studio.session_path(session)
    tid = _task_id(task)
    f = _draft_file(path, tid)
    if draft is not None:
        if not isinstance(draft, dict):
            raise ValueError("draft must be an object")
        now = round(time.time(), 3)
        text = json.dumps({**draft, "saved": now})
        if len(text) > DRAFT_MAX:
            raise ValueError("That draft is too long to keep")
        f.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(f, text)
        return {"saved": now}
    try:
        kept = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        kept = None
    t = next((x for x in studio.read(session)["tasks"] if x["id"] == tid), None) if tid else None
    return {"draft": kept, "earlier": stored(t["recheck"], session) if t and t.get("recheck") else None}


def _bars(v, n: int) -> str:
    s = str(v or "").strip().lower().replace("–", "-").replace("bars", "").replace("bar", "").strip()
    if not s:
        return ""
    m = _BARS.fullmatch(s)
    if not m or int(m.group(1)) < 1:
        raise ValueError(f"Finding {n}: write the bars as 17 or 17-24")
    a, b = sorted((int(m.group(1)), int(m.group(2) or m.group(1))))
    return str(a) if a == b else f"{a}-{b}"


def _findings(raw, filled: set[str]) -> list[dict]:
    """The sheet's rows, checked: each says what was heard, where, and which seat fixes it."""
    if not isinstance(raw, list):
        raise ValueError("findings must be a list")
    out = []
    for i, f in enumerate(raw, 1):
        if not isinstance(f, dict):
            raise ValueError(f"Finding {i} must be an object")
        text = str(f.get("text") or "").strip()
        bars = _bars(f.get("bars"), i)
        tracks = list(dict.fromkeys(str(x).strip() for x in f.get("tracks") or [] if str(x).strip()))[:24]
        if not text:
            if bars or tracks:
                raise ValueError(f"Finding {i} says where but not what: write what you heard")
            continue  # an empty row left open on the sheet
        to = str(f.get("to") or "").strip().lower() or ("producer" if "producer" in filled else "")
        if not to:
            raise ValueError(f"Finding {i}: say which seat should fix it")
        role = roles.get(to)
        if role.id != "producer" and not role.owns:
            raise ValueError(f"Finding {i} is for the {role.title}, which changes nothing: give it to a seat that fixes it")
        if role.id not in filled:
            raise ValueError(f"Finding {i} is for the {role.title}, and nobody is in that seat: give it to "
                             + ("another seat or the Producer" if "producer" in filled else "another seat"))
        try:
            at = round(max(0.0, float(f["at"])), 2) if f.get("at") not in (None, "") else None
        except (TypeError, ValueError):
            at = None
        out.append({"n": len(out) + 1, "text": text[:TEXT_MAX], "bars": bars, "tracks": tracks, "at": at, "to": role.id})
    if len(out) > MAX_FINDINGS:
        raise ValueError(f"{len(out)} findings is more than one review can hand out ({MAX_FINDINGS}): send the "
                         "ones that matter most")
    return out


def _clock(s: float) -> str:
    return f"{int(s // 60)}:{int(s % 60):02d}"


def _line(f: dict, at: bool = False) -> str:
    """'Bars 17-24 (Horns, Woodwind): what was heard'. at: with the time in the render, for people."""
    where = (("Bars " if "-" in f["bars"] else "Bar ") + f["bars"]) if f["bars"] else ""
    if f["tracks"]:
        where += f" ({', '.join(f['tracks'])})" if where else ", ".join(f["tracks"])
    if at and f.get("at") is not None:
        where += f" at {_clock(f['at'])}"
    return f"{where}: {f['text']}" if where else f["text"]


def _short(text: str, n: int) -> str:
    one = " ".join(text.split())
    return one if len(one) <= n else one[: n - 3].rstrip() + "..."


def review_send(verdict: str, findings: list | None = None, notes: str = "", task: str | None = None,
                again: bool = True, checked: dict | None = None, session: str | None = None) -> dict:
    """Send a review written in the Review seat: file it, hand its findings out, close the review task.

    verdict: "ready" (it does what the brief asks; any findings are polish) or "fix" (not finished until the
    findings are dealt with). findings: [{"text", "bars": "17-24", "tracks": [names], "at": seconds into the
    render, "to": the seat that should fix it, or "producer" to leave it to the Producer}]. notes: the overall
    view. task: the Review task this answers. again: queue a second listen for the Review seat that starts once
    every fix is in. checked: on a second listen, {"<n>": "fixed" | "still"} for the earlier review's findings."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}")
    path = studio.session_path(session)
    st = studio.read(session)
    filled = {sid for sid, s in st["seats"].items() if s["occupant"]}
    rows = _findings(findings or [], filled)
    notes = str(notes or "").strip()[:TEXT_MAX]
    if verdict == "fix" and not rows and not notes:
        raise ValueError("Say what needs work: add a finding, or a note for the Producer")
    if verdict == "fix" and not rows and "producer" not in filled:
        raise ValueError("Nobody is in the Producer seat to act on a note: write what needs work as findings, each "
                         "for the seat that fixes it")
    tid = _task_id(task)
    earlier = None
    if tid:
        t = next((x for x in st["tasks"] if x["id"] == tid), None)
        if not t or t["seat"] != "review":
            raise KeyError(f"{task} is not a Review task")
        if t["state"] not in studio.OPEN:
            raise ValueError(f"{tid} is already {t['state']}")
        earlier = stored(t["recheck"], session) if t.get("recheck") else None
    ticks = {}
    if earlier:
        for f in earlier.get("findings") or []:
            v = str((checked or {}).get(str(f["n"])) or "").lower()
            ticks[str(f["n"])] = v if v in ("fixed", "still") else "unchecked"
    rev = json.loads((path / "project.json").read_text(encoding="utf-8")).get("rev")
    verdict_said = VERDICTS[verdict]
    ask = (f"The user listened to the session (rev {rev}) in the Review seat"
           + (": it does what the brief asks, with polish left" if verdict == "ready" else ": it needs work"))
    by_seat: dict[str, list[dict]] = {}
    for f in rows:
        by_seat.setdefault(f["to"], []).append(f)
    early = [s for s in roles.ORDER if s in by_seat and s != "producer" and s not in LATE]
    late = [s for s in roles.ORDER if s in by_seat and s in LATE]
    to_producer = by_seat.get("producer", [])
    wants_producer = bool(to_producer) or (verdict == "fix" and notes and not early and not late)

    with studio._Board(session) as b:
        rid = b.new_id("R")
        made: dict[str, dict] = {}

        def fix_task(seat: str, depends: list[str]) -> dict:
            mine = by_seat[seat]
            title = (f"Fix from review {rid}: {_short(mine[0]['text'], 60)}" if len(mine) == 1 else
                     f"Fixes from review {rid}: {_short(mine[0]['text'], 44)} (+{len(mine) - 1} more)")
            lines = [f"{ask}. Review {rid}, for you:"] + [f"{i}. {_line(f)}" for i, f in enumerate(mine, 1)]
            lines.append("Fix these and nothing else. When you finish, say what you changed for each, by number."
                         if len(mine) > 1 else "Fix this and nothing else. When you finish, say what you changed.")
            if verdict == "fix" and notes and not wants_producer:
                lines.append("The user's overall note: " + _short(notes, NOTE_SHOWN))
            if depends:
                lines.append(f"Waits for {', '.join(depends)}, so you work on what they change.")
            scope = {k: v for k, v in (("bars", ", ".join(dict.fromkeys(f["bars"] for f in mine if f["bars"]))),
                                       ("tracks", list(dict.fromkeys(x for f in mine for x in f["tracks"]))))
                     if v}
            return studio._new_task(b, seat, title, "\n".join(lines), depends, scope, from_review=rid)

        for seat in early:
            made[seat] = fix_task(seat, [])
        prod = None
        if wants_producer:
            prod = studio._new_task(b, "producer", f"Act on review {rid}", "", [], {}, from_review=rid)
        before = [made[s]["id"] for s in early] + ([prod["id"]] if prod else [])
        for seat in late:
            made[seat] = fix_task(seat, before)
        second = None
        if again and (made or prod):
            person = (st["seats"]["review"]["occupant"] or {}).get("kind") == "human"
            second = studio._new_task(
                b, "review", f"Second listen: the fixes from review {rid}",
                f"Hear the session again now that the fixes from review {rid} are in: tick off each finding that "
                f"is fixed, note what is still off, and say whether it is ready." if person else
                f"Hear the session again now that the fixes from review {rid} are in: for each finding, is it "
                f"fixed (report_read(\"{rid}\") has them)? Then whether the cue does what the brief asks.",
                [t["id"] for t in made.values()] + ([prod["id"]] if prod else []), {}, recheck=rid)
        if prod:
            handed = [f"{t['id']} {s} ({len(by_seat[s])})" for s, t in made.items()]
            waiting = [t["id"] for s, t in made.items() if s in LATE] + ([second["id"]] if second else [])
            lines = [f"{ask}. Review {rid}."]
            if to_producer:
                lines += [f"Left to you ({len(to_producer)}):"] + [f"{i}. {_line(f)}" for i, f in enumerate(to_producer, 1)]
            if notes:
                lines.append("The user's overall note: " + notes)
            if handed:
                lines.append("Already handed out by the user: " + ", ".join(handed) + ".")
            lines.append("Give each finding to the seat that owns it: in the detail of an open task that covers it "
                         "(task_update), otherwise task_add with the bars and tracks. A finding you would not act "
                         "on: say which and why in your result.")
            if waiting:
                lines.append(f"What you add or rewrite while on this task is waited for by {', '.join(waiting)} "
                             "without you setting depends.")
            prod["detail"] = "\n".join(lines)
        for f in rows:
            f["task"] = (prod if f["to"] == "producer" else made[f["to"]])["id"]

        # the review task closes with what became of it: the next seat (and the Producer's board) reads this
        outcome = ", ".join(f"{t['id']} {s}" for s, t in made.items())
        result = (f"{verdict_said.capitalize()}: {len(rows)} finding{'s' if len(rows) != 1 else ''} in review {rid}"
                  + (f". Fixes: {outcome}" if outcome else "")
                  + (f"; {prod['id']} producer hands out {len(to_producer)}" if prod and to_producer else
                     f"; {prod['id']} producer acts on the note" if prod else "")
                  + (f". Second listen: {second['id']}" if second else "") + ".")
        if not rows:
            result = f"{verdict_said.capitalize()} (review {rid})" + (f": {_short(notes, 200)}" if notes else ".")
        if tid:
            t = b.task(tid)
            t.update(state="done", result=result, report=rid, updated=round(time.time(), 3))
            b.emit("task", id=t["id"], seat=t["seat"], state="done", title=t["title"], by=studio.me(), result=result)

        body = _report(rev, verdict, notes, rows, made, prod, second, earlier, ticks)
        n = len(rows)
        studio._file_report(b, rid, "review", f"Review of rev {rev}: {verdict_said}"
                            + (f", {n} finding{'s' if n != 1 else ''}" if n else ""), body, "review")
        d = b.path / "studio" / "reviews"
        d.mkdir(parents=True, exist_ok=True)
        _atomic_write(d / f"{rid}.json", json.dumps({
            "report": rid, "task": tid, "verdict": verdict, "notes": notes, "findings": rows, "rev": rev,
            "earlier": earlier["report"] if earlier else None, "checked": ticks, "at": round(time.time(), 3)}, indent=1))
    try:
        _draft_file(path, tid).unlink()
    except OSError:
        pass
    if verdict == "ready" and not made and not prod:  # nothing more for the crew: render the deliverable
        START_FINAL(session)
    return {"report": rid, "verdict": verdict, "findings": len(rows), "final": verdict == "ready" and not made and not prod,
            "tasks": [{"id": t["id"], "seat": s, "fixes": len(by_seat[s]), "after": t["depends"]} for s, t in made.items()],
            "producer": prod["id"] if prod else None, "again": second["id"] if second else None, "closed": tid}


# ---- before and after IT'S READY ------------------------------------------------------------------------------------
# Seen live: a cue was passed as ready on the wording of the fixes, and the only render of it was a take's copy made
# before the last fixes landed. review_check() puts the numbers in front of the person before they send; a ready
# verdict then renders the session as the deliverable (renders/<name>_final.wav + .json) and files it as a report.

ENDING_DROP_LU = 3.0  # the last 4 bars this far under the 8 before them: the ending tails off
FINAL_FILE = "final.json"  # studio/final.json: the state of the last final render


def _lu_mean(vals: list) -> float | None:
    """Loudness of bars together (power mean); None when any of them is silent."""
    if not vals or any(v is None for v in vals):
        return None
    import math
    return round(10 * math.log10(sum(10 ** (v / 10) for v in vals) / len(vals)), 1)


def _checks(bar_lufs: list, earlier: dict | None) -> list[str]:
    """What a person should look at before passing the cue: silent bars inside it, an ending that falls away, and
    each finding of the review this one follows up, measured beside its neighbours."""
    out = []
    last = max((i for i, v in enumerate(bar_lufs) if v is not None), default=-1)
    silent = [i + 1 for i, v in enumerate(bar_lufs[:last]) if v is None]
    if silent:
        out.append(f"Silent: bar{'s' if len(silent) > 1 else ''} {', '.join(map(str, silent[:12]))}"
                   + (f" (+{len(silent) - 12} more)" if len(silent) > 12 else "") + ".")
    if last >= 11:
        end, before = _lu_mean(bar_lufs[last - 3:last + 1]), _lu_mean(bar_lufs[last - 11:last - 3])
        if end is not None and before is not None and before - end > ENDING_DROP_LU:
            out.append(f"The ending falls away: bars {last - 2}-{last + 1} are {before - end:.1f} LU under the 8 "
                       f"bars before them ({end} vs {before} LUFS).")
    for f in (earlier or {}).get("findings") or []:
        m = _BARS.fullmatch(f.get("bars") or "")
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2) or m.group(1))
        here = bar_lufs[a - 1:b]
        around = [v for v in bar_lufs[max(0, a - 5):a - 1] + bar_lufs[b:b + 4] if v is not None]
        lv = _lu_mean(here)
        out.append(f"Finding {f['n']} (bar{'s' if a != b else ''} {f['bars']}): "
                   + ("silent" if lv is None and here else f"{lv} LUFS") + (f", around it {_lu_mean(around)}" if around else "")
                   + ".")
    return out


def review_check(task: str | None = None, session: str | None = None) -> dict:
    """The numbers to look at before sending IT'S READY: measured now on the session as it is (not a take), so a fix
    that only exists in a take's copy shows. Warnings only: the verdict stays the person's."""
    from . import api
    path = studio.session_path(session)
    tid = _task_id(task)
    t = next((x for x in studio.read(session)["tasks"] if x["id"] == tid), None) if tid else None
    earlier = stored(t["recheck"], session) if t and t.get("recheck") else None
    heard = api.listen(project=str(path), probe=False)
    rev = json.loads((path / "project.json").read_text(encoding="utf-8")).get("rev")
    pending = [k["id"] for k in studio.take_index(path) if k.get("state") == "submitted"]
    out = _checks(heard.get("bar_lufs") or [], earlier)
    if pending:
        out.append(f"Handed in, not accepted: {', '.join(pending)} (not in what you hear).")
    return {"rev": rev, "checks": out, "arc": heard.get("energy_arc")}


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "cue"


def finalize(session: str | None = None) -> dict:
    """Render the session as the finished cue: renders/<name>_final.wav with the project's loop settings, a .json
    beside it (loudness, peak, crest, bands, energy arc, width, open warnings, the takes and reviews that made it),
    and a report "Final mix" under REPORTS."""
    from . import api
    path = studio.session_path(session)
    doc = json.loads((path / "project.json").read_text(encoding="utf-8"))
    name = _slug(path.name)
    looped = bool(doc.get("loop"))
    rep = api.render(project=str(path), out=f"renders/{name}_final.wav", loop="auto" if looped else "off")
    heard = api.listen(project=str(path), probe=False)
    mix = rep.get("mix") or {}
    widths = [s.get("width") for s in heard.get("sections") or [] if s.get("width") is not None]
    st = studio.read(session)
    record = {
        "file": (rep.get("files") or [None])[0], "rev": doc.get("rev"), "rendered": round(time.time(), 3),
        "duration_s": mix.get("duration_s"), "loop": rep.get("loop_mode") or ("auto" if looped else "off"),
        "lufs": mix.get("lufs"), "true_peak_dbtp": mix.get("true_peak_dbtp"), "crest_db": mix.get("crest_db"),
        "bands_pct": mix.get("bands_pct"), "stereo_correlation": mix.get("stereo_correlation"),
        "energy_arc": heard.get("energy_arc"), "width": [min(widths), max(widths)] if widths else None,
        "warnings": list(dict.fromkeys((rep.get("warnings") or []) + (heard.get("warnings") or []))),
        "checks": _checks(heard.get("bar_lufs") or [], None),
        "takes": [{"take": k["id"], "seat": k.get("seat"), "rev": k.get("merged_rev")}
                  for k in studio.take_index(path) if k.get("state") == "accepted"],
        "reviews": [{"report": r["id"], "title": r["title"]} for r in st["reports"] if r.get("kind") == "review"],
    }
    side = path / "renders" / f"{name}_final.json"
    _atomic_write(side, json.dumps(record, indent=1))
    num = lambda v, u="": "–" if v is None else f"{v}{u}"  # noqa: E731
    b = record["bands_pct"] or {}
    lines = [f"**Final mix** · rev {record['rev']} · `{Path(record['file']).name if record['file'] else '?'}`", "",
             f"- {num(record['duration_s'], ' s')}, " + ("one-shot" if record["loop"] == "off" else f"looped ({record['loop']})"),
             f"- {num(record['lufs'], ' LUFS')}, true peak {num(record['true_peak_dbtp'], ' dBTP')}, "
             f"crest {num(record['crest_db'], ' dB')}, correlation {num(record['stereo_correlation'])}",
             "- Bands: " + " · ".join(f"{k.replace('_', '-')} {v}%" for k, v in b.items()) if b else "- Bands: –"]
    if record["energy_arc"]:
        lines.append(f"- Energy arc: {record['energy_arc']}")
    if record["width"]:
        lines.append(f"- Section width: {record['width'][0]}-{record['width'][1]}")
    if record["checks"] or record["warnings"]:
        lines += ["", "## Still open"] + [f"- {w}" for w in record["checks"] + record["warnings"]]
    if record["takes"] or record["reviews"]:
        lines += ["", "## Made from"]
    if record["takes"]:
        lines.append("- Takes: " + ", ".join(f"{k['take']} ({k['seat']}, rev {k['rev']})" for k in record["takes"]))
    if record["reviews"]:
        lines.append("- Reviews: " + "; ".join(f"{r['report']} {r['title']}" for r in record["reviews"]))
    with studio._Board(session) as bd:
        rid = bd.new_id("R")
        studio._file_report(bd, rid, "studio", f"Final mix of rev {record['rev']}", "\n".join(lines) + "\n", "final")
    record["report"] = rid
    return record


def _final_state(path: Path, **state) -> None:
    f = path / "studio" / FINAL_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(f, json.dumps({**state, "at": round(time.time(), 3)}))


def final_status(session: str | None = None) -> dict | None:
    """The last final render: {"state": "rendering" | "done" | "failed", "report", "file", "error", "at"}."""
    try:
        return json.loads((studio.session_path(session) / "studio" / FINAL_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _final_in_background(session: str | None) -> None:
    """A ready verdict's final render, off the request that sent the review: a full render can take minutes."""
    import threading
    path = studio.session_path(session)
    _final_state(path, state="rendering")

    def run():
        try:
            rec = finalize(str(path))
            _final_state(path, state="done", report=rec["report"], file=rec["file"], rev=rec["rev"])
        except Exception as e:  # noqa: BLE001 - shown to the person on the sheet, not a crash of the server
            _final_state(path, state="failed", error=str(e)[:300])
    threading.Thread(target=run, daemon=True, name="final-render").start()


START_FINAL = _final_in_background  # tests swap this for a call that runs in line


def _report(rev, verdict: str, notes: str, rows: list[dict], made: dict, prod: dict | None, second: dict | None,
            earlier: dict | None, ticks: dict) -> str:
    """The review as people read it under REPORTS."""
    title = lambda s: roles.get(s).title  # noqa: E731
    lines = [f"**{VERDICTS[verdict].capitalize()}** · rev {rev} · written in the Review seat by the user", ""]
    if notes:
        lines += [notes, ""]
    if rows:
        lines.append("## Findings")
        lines += [f"- **{f['n']}.** {_line(f, at=True)} · {title(f['to'])}, {f['task']}" for f in rows]
        lines.append("")
    if earlier:
        said = {"fixed": "Fixed", "still": "Still off", "unchecked": "Not checked"}
        lines.append(f"## Since review {earlier['report']}")
        lines += [f"- {said[ticks.get(str(f['n']), 'unchecked')]}: {f['n']}. {_line(f)}" for f in earlier.get("findings") or []]
        lines.append("")
    if made or prod or second:
        lines.append("## What happens next")
        for s, t in made.items():
            k = sum(1 for f in rows if f["to"] == s)
            lines.append(f"- {t['id']} {title(s)}: {k} fix{'es' if k != 1 else ''}"
                         + (", once the others are in" if t["depends"] else ""))
        if prod:
            k = sum(1 for f in rows if f["to"] == "producer")
            lines.append(f"- {prod['id']} Producer: " + (f"hands out {k} finding{'s' if k != 1 else ''}" if k else "acts on the note"))
        if second:
            lines.append(f"- {second['id']} Review (you): a second listen once they are all in")
    return "\n".join(lines).rstrip() + "\n"
