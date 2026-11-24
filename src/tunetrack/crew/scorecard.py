"""How each agent did, counted from the session's activity log, and remembered across sessions.

The numbers are plumbing, not judgement: runs and how they ended, tool calls, minutes, takes handed in and what
happened to them, how often the seat had to ask the user. The Producer writes the judgement on top of them
(crew_review asks for it) and files it with report_write, so the user reads both in the studio's REPORTS.

    crew_scorecard()          # this session: one row per seat and agent
    crew_review()             # file the scorecard as a report and ask the Producer for a written review
    crew_history("composer")  # every session so far: how each model did in that seat (the seat picker shows it)
    crew_pick()               # a model per seat from that history, for the user to apply (or not)

Usage: runs that report it (tokens, and dollars where the runner gives them) are added up per row, and the cost
per accepted take (per finished task for a seat that hands in no takes) is worked out from those runs alone. A row
whose runs reported nothing has no cost figure and is not ranked worse for it.

History lives in <library>/crew_history.json, one entry per session, replaced each time that session is counted.
"""
from __future__ import annotations

import functools
import json
import re
import time
from pathlib import Path

from .. import config, roles, studio
from ..model import _atomic_write

_QUOTA = re.compile(r"usage limit|rate.?limit|too many requests|quota|\b429\b|out of credit|insufficient credit|billing", re.I)
# metered: runs that reported tokens; priced: runs that reported dollars (cost_usd is kept beside the counts)
_COUNTS = ("runs", "ok", "out_of_usage", "failed", "stopped", "tool_calls", "handed_in", "accepted", "first_time",
           "returned", "rejected", "tasks_done", "asked_you", "tool_errors", "tokens", "metered", "priced")
_WHO = ("runner", "model", "effort")
_RUNNERS = ("claude", "codex", "kilo", "agy", "script")  # crew.runners.RUNNERS, without importing the launcher


def _wins(r: dict) -> int:
    """What a seat's work came to: takes accepted, or tasks finished for a seat that hands in no takes."""
    return r["accepted"] if r["handed_in"] else r["tasks_done"]


def _with_cost(r: dict) -> dict:
    """The row with its cost per accepted take, where its runs reported usage: usd_per_take and tokens_per_take."""
    r = {k: v for k, v in r.items() if k not in ("usd_per_take", "tokens_per_take")}
    r["cost_usd"] = round(r.get("cost_usd", 0.0), 4)
    w = _wins(r)
    if w and r.get("priced"):
        r["usd_per_take"] = round(r["cost_usd"] / w, 4)
    if w and r.get("metered"):
        r["tokens_per_take"] = round(r["tokens"] / w)
    return r


def _per_take(r: dict, unit: bool = False) -> str:
    """Cost per accepted take; unit=True names it ('/take', or '/task' for a seat that hands in no takes)."""
    per = ("/take" if r.get("handed_in") else "/task") if unit else ""
    if "usd_per_take" in r:
        return f"${r['usd_per_take']:.2f}{per}"
    if "tokens_per_take" in r:
        return f"{r['tokens_per_take'] / 1000:.0f}k tok{per}"
    return "-"


def _history_file() -> Path:
    return config.HOME / "crew_history.json"


def crew_scorecard(session: str | None = None) -> dict:
    """This session, one row per seat and whoever sat in it: runs (ok / out of usage / failed / stopped), tool
    calls, minutes at work, takes handed in / accepted / accepted first time / returned, tasks finished, and how
    often the seat put something on Needs You. Where runs reported usage: tokens, cost_usd, and the cost per
    accepted take (usd_per_take, tokens_per_take)."""
    path = studio.session_path(session)
    rows: dict[tuple[str, str], dict] = {}
    who: dict[str, str] = {}      # seat -> who sits there now
    occ: dict[str, dict] = {}     # seat -> runner/model/effort of its last run, so the history can seat it again
    started: dict[str, float] = {}
    owner: dict[str, tuple[str, str]] = {}  # take -> the row that handed it in
    came_back: set[str] = set()
    run_errors: dict[str, int] = {}  # seat -> failed tool calls in its current run

    def row(seat: str) -> dict:
        key = (seat, who.get(seat) or "unknown")
        r = rows.setdefault(key, {"seat": seat, "agent": key[1], "minutes": 0.0, "cost_usd": 0.0,
                                  **{k: 0 for k in _COUNTS}})
        if not r.get("runner") and occ.get(seat, {}).get("agent") == key[1]:
            r.update({k: occ[seat].get(k) for k in _WHO})
        return r

    for e in studio.events(0, 10 ** 9, str(path))["events"]:
        kind, seat = e.get("kind"), e.get("seat")
        if kind == "seat" and seat in roles.ROLES:
            who[seat] = e.get("occupant") or "empty"
        elif kind == "run" and seat in roles.ROLES:
            if e.get("what") == "start":
                who[seat] = e.get("agent") or who.get(seat) or "unknown"
                if e.get("runner"):
                    occ[seat] = {"agent": who[seat], **{k: e.get(k) for k in _WHO}}
                started[seat] = e["at"]
                row(seat)["runs"] += 1
            elif e.get("what") == "tool":
                row(seat)["tool_calls"] += 1
                if e.get("ok") is False:
                    run_errors[seat] = run_errors.get(seat, 0) + 1
            elif e.get("what") == "end":
                r = row(seat)
                r["minutes"] += max(0.0, e["at"] - started.pop(seat, e["at"])) / 60
                if "input_tokens" in e or "output_tokens" in e:
                    r["metered"] += 1
                    r["tokens"] += int(e.get("input_tokens") or 0) + int(e.get("output_tokens") or 0)
                if e.get("cost_usd") is not None:
                    r["priced"] += 1
                    r["cost_usd"] += float(e["cost_usd"])
                err = e.get("error") or ""
                # tool calls that failed (the end event's tool_errors, or tool events with ok=False)
                bad = int(e.get("tool_errors") or 0) + run_errors.pop(seat, 0)
                r["tool_errors"] += bad
                calls = int(e.get("tools") or 0)
                r["ok" if e.get("ok") and (not bad or 2 * bad < calls) else "out_of_usage" if _QUOTA.search(err) else
                  "stopped" if err.startswith("it was stopped") else "failed"] += 1
        elif kind == "take" and seat in roles.ROLES:
            state = e.get("state")
            if state in ("submitted", "rejected") and e["id"] not in owner:
                # a take closed without ever being submitted still counts as handed in (and rejected)
                owner[e["id"]] = (seat, who.get(seat) or "unknown")
                row(seat)["handed_in"] += 1
            if state in ("accepted", "returned", "rejected") and e["id"] in owner:
                r = rows[owner[e["id"]]]
                if state == "rejected":
                    r["rejected"] += 1
                elif state == "returned":
                    r["returned"] += 1
                    came_back.add(e["id"])
                else:
                    r["accepted"] += 1
                    r["first_time"] += e["id"] not in came_back
        elif kind == "task" and e.get("state") == "done" and e.get("by") == seat and seat in roles.ROLES:
            row(seat)["tasks_done"] += 1
        elif (kind == "attention" and e.get("state") == "open" and e.get("sender") in roles.ROLES
              and e.get("what") in studio.ATTENTION_KINDS and not e.get("stage")):  # ask_human, not sign-off stops
            row(e["sender"])["asked_you"] += 1
    out = [_with_cost(dict(r, minutes=round(r["minutes"], 1))) for r in rows.values()
           if r["runs"] or r["handed_in"] or r["tasks_done"]]
    out.sort(key=lambda r: (roles.ORDER.index(r["seat"]), -r["runs"]))
    return {"session": str(path), "name": path.name, "rows": out,
            "totals": {k: round(sum(r[k] for r in out), 4 if k == "cost_usd" else 1)
                       for k in (*_COUNTS, "minutes", "cost_usd")}}


def as_markdown(card: dict) -> str:
    """The scorecard as a table (the Producer reads it for the review). The cost column is there only when some
    run reported usage: cost per accepted take, or per finished task for a seat that hands in no takes."""
    cost = any(r.get("metered") or r.get("priced") for r in card["rows"])
    head = ("| Seat | Agent | Runs (ok) | Out of usage | Failed | Tool calls | Minutes | Takes in / accepted / first time "
            "/ returned / rejected | Tasks done | Asked you | Tool errors |" + (" Cost per |" if cost else ""))
    lines = [head, "|" + "---|" * (12 if cost else 11)]
    for r in card["rows"]:
        lines.append(f"| {roles.get(r['seat']).title} | {r['agent']} | {r['runs']} ({r['ok']}) | {r['out_of_usage']} | "
                     f"{r['failed']} | {r['tool_calls']} | {r['minutes']} | {r['handed_in']} / {r['accepted']} / "
                     f"{r['first_time']} / {r['returned']} / {r.get('rejected', 0)} | {r['tasks_done']} | "
                     f"{r['asked_you']} | {r.get('tool_errors', 0)} |" + (f" {_per_take(r, unit=True)} |" if cost else ""))
    t = card["totals"]
    spent = (f" Usage reported by {t['metered']} runs: {round(t['tokens'] / 1000)}k tokens"
             + (f", ${t['cost_usd']:.2f}" if t["priced"] else "") + "." if t["metered"] or t["priced"] else "")
    lines += ["", f"Totals: {t['runs']} runs ({t['ok']} ended ok, {t['out_of_usage']} out of usage, {t['failed']} failed), "
                  f"{t['tool_calls']} tool calls, {t['minutes']} minutes at work, {t['accepted']} of {t['handed_in']} "
                  f"takes accepted ({t['first_time']} first time, {t['rejected']} rejected)." + spent]
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
    """Every counted session so far, added up per seat and agent: what the seat picker shows next to a model, with
    the cost per accepted take where runs reported usage."""
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
                                                              "minutes": 0.0, "cost_usd": 0.0,
                                                              **{k: 0 for k in _COUNTS}})
            a["sessions"] += 1
            a["minutes"] = round(a["minutes"] + r.get("minutes", 0), 1)
            a["cost_usd"] += r.get("cost_usd", 0.0)
            for k in _COUNTS:
                a[k] += r.get(k, 0)
            for k in _WHO:
                if r.get(k) and not a.get(k):
                    a[k] = r[k]
    return {"history": sorted((_with_cost(a) for a in out.values()),
                              key=lambda a: (roles.ORDER.index(a["seat"]), -a["runs"]))}


def _occupant(a: dict) -> dict | None:
    """runner/model/effort of a history row: from its runs (newer history), else read back from its label
    ("kilo · free-a · high"). None when the row is a person or an app, not a model the studio can launch."""
    if a.get("runner") in _RUNNERS:
        return {k: a.get(k) for k in _WHO}
    parts = str(a.get("agent") or "").split(" · ")
    if parts[0] in _RUNNERS and 2 <= len(parts) <= 3:
        return {"runner": parts[0], "model": parts[1], "effort": parts[2] if len(parts) == 3 else None}
    return None


def _score(a: dict) -> float:
    """How well a model did in a seat, from -1 to 1. Takes: accepted minus returned, over takes handed in plus one
    (so one lucky take does not beat nine of ten). A seat that hands in no takes: tasks finished over runs plus one."""
    if a["handed_in"]:
        return (a["accepted"] - a["returned"]) / (a["handed_in"] + 1)
    return a["tasks_done"] / (a["runs"] + 1)


def _better(a: dict, b: dict) -> int:
    """Sort order for crew_pick: the higher score first; on a tie (to 0.05), the cheaper per accepted take when both
    reported the same kind of usage (dollars, else tokens; a row without usage is never ranked worse for it); then
    the one with more runs behind it."""
    sa, sb = round(_score(a) * 20), round(_score(b) * 20)
    if sa != sb:
        return -1 if sa > sb else 1
    for k in ("usd_per_take", "tokens_per_take"):
        if k in a and k in b and a[k] != b[k]:
            return -1 if a[k] < b[k] else 1
        if k in a or k in b:
            break  # only one side knows this cost: not a reason to prefer either
    return (b["runs"] > a["runs"]) - (b["runs"] < a["runs"])


def crew_pick(session: str | None = None) -> dict:
    """Propose a model per seat from the crew history: what did best there before (takes accepted, fewer
    returned), cost per accepted take breaking ties. Changes nothing: the studio UI shows it and the user applies
    the seats they want (seat_assign per seat). picks: one per seat with history; same: that occupant already sits
    there. Seats with no history are left out."""
    st = studio.read(session)
    picks = []
    for sid in roles.ORDER:
        rows = [a for a in crew_history(sid)["history"] if _occupant(a) and a["runs"]]
        if not rows:
            continue
        best = sorted(rows, key=functools.cmp_to_key(_better))[0]
        who = _occupant(best)
        cur = st["seats"][sid]["occupant"] or {}
        took = f"{best['accepted']} of {best['handed_in']} takes accepted, {best['returned']} returned" \
            if best["handed_in"] else f"{best['tasks_done']} tasks finished in {best['runs']} runs"
        cost = _per_take(best)
        picks.append({"seat": sid, **who, "agent": best["agent"],
                      "same": cur.get("kind") == "agent" and all(cur.get(k) == who[k] for k in _WHO),
                      "why": f"{took} over {best['sessions']} session{'s' if best['sessions'] != 1 else ''}"
                             + (f"; {cost} per {'take' if best['handed_in'] else 'task'}" if cost != "-" else "")
                             + (f" (best of {len(rows)})" if len(rows) > 1 else "")})
    return {"picks": picks}


def _returned(session: str | None = None) -> str:
    """The takes that came back, with why: what the review is mostly about, given to the Producer so it does not
    read every take and report to find them (on a real session that review was 48 calls and half a million tokens)."""
    back = [t for t in studio.take_index(studio.session_path(session)) if int(t.get("returns", 0)) and t.get("reason")]
    if not back:
        return "No take was returned.\n\n"
    return "Takes that were returned (the last reason given):\n" + "\n".join(
        f"- {t['id']} ({t.get('seat')}, {t.get('task') or 'no task'}), {t['returns']}x: "
        + " ".join(str(t["reason"]).split())[:300] for t in back) + "\n\n"


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
            "The user wants to know who to seat where next time.\n\nThe counts, from the activity log:\n\n"
            + table + "\n\n" + _returned(session) +
            "File the review with report_write(title=\"Session review\", kind=\"review\", body=...). It is read to "
            "pick a crew, not as an account of the session: 40 lines at most. Per seat that worked, three lines: "
            "keep or swap (and for what); the one thing that cost most, with the take or task; the one instruction "
            "that would have avoided it. Then the crew you would pick, one line per seat. Work from the counts and "
            "the returns above and from what you remember hearing: do not re-read every report and take, and do "
            "not render again. Say \"cannot tell\" where you cannot. Nothing in talkback.\n\n"
            "Then keep what this session taught: each \"instruction that would have avoided it\" that holds beyond "
            "this cue becomes a lesson for that seat - lessons(seat=\"composer\", add=[\"...\"]), seat=\"all\" for "
            "the whole crew. One line each: the rule and why. lessons(seat) first shows what is there: remove "
            "(by number) lines this session proved wrong or that a new line replaces. Nothing about one cue's "
            "bars or tracks; no lesson at all is fine. A lesson is about the seat's craft, not about a tool error or a "
            "missing tool: those go in the review, for the user.", session=session)
        out["task"] = t["id"]
    else:
        out["note"] = "No agent sits in the Producer seat, so only the scorecard was filed."
    return out
