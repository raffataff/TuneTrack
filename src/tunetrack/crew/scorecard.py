"""How each agent did, counted from the session's activity log, and remembered across sessions.

The numbers are plumbing, not judgement: runs and how they ended, tool calls, minutes, takes handed in and what
happened to them, how often the seat had to ask the user. The Producer writes the judgement on top of them
(crew_review asks for it) and files it with report_write, so the user reads both in the studio's REPORTS.

    crew_scorecard()          # this session: one row per seat and agent
    crew_review()             # file the scorecard as a report and ask the Producer for a written review
    crew_history("composer")  # every session so far: how each model did in that seat (the seat picker shows it)

History lives in <library>/crew_history.json, one entry per session, replaced each time that session is counted.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .. import config, roles, studio
from ..model import _atomic_write

_QUOTA = re.compile(r"usage limit|rate.?limit|too many requests|quota|\b429\b|out of credit|insufficient credit|billing", re.I)
_COUNTS = ("runs", "ok", "out_of_usage", "failed", "stopped", "tool_calls", "handed_in", "accepted", "first_time",
           "returned", "tasks_done", "asked_you")


def _history_file() -> Path:
    return config.HOME / "crew_history.json"


def crew_scorecard(session: str | None = None) -> dict:
    """This session, one row per seat and whoever sat in it: runs (ok / out of usage / failed / stopped), tool
    calls, minutes at work, takes handed in / accepted / accepted first time / returned, tasks finished, and how
    often the seat put something on Needs You."""
    path = studio.session_path(session)
    rows: dict[tuple[str, str], dict] = {}
    who: dict[str, str] = {}      # seat -> who sits there now
    started: dict[str, float] = {}
    owner: dict[str, tuple[str, str]] = {}  # take -> the row that handed it in
    came_back: set[str] = set()

    def row(seat: str) -> dict:
        key = (seat, who.get(seat) or "unknown")
        return rows.setdefault(key, {"seat": seat, "agent": key[1], "minutes": 0.0, **{k: 0 for k in _COUNTS}})

    for e in studio.events(0, 10 ** 9, str(path))["events"]:
        kind, seat = e.get("kind"), e.get("seat")
        if kind == "seat" and seat in roles.ROLES:
            who[seat] = e.get("occupant") or "empty"
        elif kind == "run" and seat in roles.ROLES:
            if e.get("what") == "start":
                who[seat] = e.get("agent") or who.get(seat) or "unknown"
                started[seat] = e["at"]
                row(seat)["runs"] += 1
            elif e.get("what") == "tool":
                row(seat)["tool_calls"] += 1
            elif e.get("what") == "end":
                r = row(seat)
                r["minutes"] += max(0.0, e["at"] - started.pop(seat, e["at"])) / 60
                err = e.get("error") or ""
                r["ok" if e.get("ok") else "out_of_usage" if _QUOTA.search(err) else
                  "stopped" if err.startswith("it was stopped") else "failed"] += 1
        elif kind == "take" and seat in roles.ROLES:
            state = e.get("state")
            if state == "submitted":
                if e["id"] not in owner:
                    owner[e["id"]] = (seat, who.get(seat) or "unknown")
                    row(seat)["handed_in"] += 1
            elif state in ("accepted", "returned") and e["id"] in owner:
                r = rows[owner[e["id"]]]
                if state == "returned":
                    r["returned"] += 1
                    came_back.add(e["id"])
                else:
                    r["accepted"] += 1
                    r["first_time"] += e["id"] not in came_back
        elif kind == "task" and e.get("state") == "done" and e.get("by") == seat and seat in roles.ROLES:
            row(seat)["tasks_done"] += 1
        elif kind == "attention" and e.get("state") == "open" and e.get("sender") in roles.ROLES:
            row(e["sender"])["asked_you"] += 1
    out = [dict(r, minutes=round(r["minutes"], 1)) for r in rows.values()
           if r["runs"] or r["handed_in"] or r["tasks_done"]]
    out.sort(key=lambda r: (roles.ORDER.index(r["seat"]), -r["runs"]))
    return {"session": str(path), "name": path.name, "rows": out,
            "totals": {k: round(sum(r[k] for r in out), 1) for k in (*_COUNTS, "minutes")}}


def as_markdown(card: dict) -> str:
    head = "| Seat | Agent | Runs (ok) | Out of usage | Failed | Tool calls | Minutes | Takes in / accepted / first time / returned | Tasks done | Asked you |"
    lines = [head, "|" + "---|" * 10]
    for r in card["rows"]:
        lines.append(f"| {roles.get(r['seat']).title} | {r['agent']} | {r['runs']} ({r['ok']}) | {r['out_of_usage']} | "
                     f"{r['failed']} | {r['tool_calls']} | {r['minutes']} | {r['handed_in']} / {r['accepted']} / "
                     f"{r['first_time']} / {r['returned']} | {r['tasks_done']} | {r['asked_you']} |")
    t = card["totals"]
    lines += ["", f"Totals: {t['runs']} runs ({t['ok']} ended ok, {t['out_of_usage']} out of usage, {t['failed']} failed), "
                  f"{t['tool_calls']} tool calls, {t['minutes']} minutes at work, {t['accepted']} of {t['handed_in']} "
                  f"takes accepted ({t['first_time']} first time)."]
    return "\n".join(lines)


def remember(session: str | None = None) -> dict:
    """Count this session and keep the result in the library's crew history (replacing its earlier entry)."""
    card = crew_scorecard(session)
    if not card["rows"]:
        return card
    f = _history_file()
    try:
        hist = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        hist = {}
    hist[card["session"]] = {"name": card["name"], "at": round(time.time(), 3), "rows": card["rows"]}
    config.HOME.mkdir(parents=True, exist_ok=True)
    _atomic_write(f, json.dumps(hist, indent=1))
    return card


def crew_history(seat: str | None = None) -> dict:
    """Every counted session so far, added up per seat and agent: what the seat picker shows next to a model."""
    try:
        hist = json.loads(_history_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        hist = {}
    want = roles.get(seat).id if seat else None
    out: dict[str, dict] = {}
    for entry in hist.values():
        for r in entry.get("rows", []):
            if want and r["seat"] != want:
                continue
            a = out.setdefault(f"{r['seat']}|{r['agent']}", {"seat": r["seat"], "agent": r["agent"], "sessions": 0,
                                                              "minutes": 0.0, **{k: 0 for k in _COUNTS}})
            a["sessions"] += 1
            a["minutes"] = round(a["minutes"] + r.get("minutes", 0), 1)
            for k in _COUNTS:
                a[k] += r.get(k, 0)
    return {"history": sorted(out.values(), key=lambda a: (roles.ORDER.index(a["seat"]), -a["runs"]))}


def crew_review(session: str | None = None) -> dict:
    """File this session's scorecard as a report, keep it in the crew history, and - when an agent sits in the
    Producer seat - give it the task of writing the review: what each seat did well, where it cost time, and
    whether to seat that agent there again."""
    if studio.me() != roles.YOU:
        raise PermissionError("The session review is asked for by the user")
    card = remember(session)
    if not card["rows"]:
        raise ValueError("Nothing to review yet: no agent has worked in this session")
    table = as_markdown(card)
    old = next((r["id"] for r in studio.report_list(session)["reports"] if r["kind"] == "scorecard"), None)
    rep = studio.report_write("Crew scorecard", f"# Crew scorecard: {card['name']}\n\n{table}\n", "scorecard", old,
                              session, by="floor")
    out = {"scorecard": rep["report"], "rows": len(card["rows"])}
    prod = studio.read(session)["seats"]["producer"]["occupant"]
    if prod and prod.get("kind") == "agent":
        t = studio.task_add(
            "producer", "Write the session review",
            "The user wants a review of how each seat's agent did in this session, to decide who to seat where "
            "next time.\n\nThe counts, from the activity log:\n\n" + table + "\n\n"
            "Write the review in markdown and file it with report_write(title=\"Session review\", kind=\"review\", "
            "body=...). For every seat that worked: what it did well, where it cost time or had to be corrected "
            "(name the takes and tasks), and whether you would seat that agent there again or try another. Ground it "
            "in the counts above and in what you heard in its takes; say plainly where you cannot tell. End with "
            "the crew you would pick for the next session of this kind. Do not put the review in talkback. Then set "
            "this task done with a two-line result.", session=session)
        out["task"] = t["id"]
    else:
        out["note"] = "No agent sits in the Producer seat, so only the scorecard was filed."
    return out
