"""Where the user signs off the crew's work: the stops on Needs You.

Every seat has a SIGN OFF switch (the board's seat field "signoff"):
  - a seat that hands in takes: its agent's hand-in stops at the user before the Producer hears it (stage "signoff");
  - the Producer, which makes the user Co-Producer: its accept, return or discard of a take waits for the user to
    confirm (stage "confirm"), and the tasks it adds in a run wait for the user's OK as one plan (stage "plan");
  - Review: the tasks it files wait for the user's OK as one plan.
A seat can also be asked for options: it hands in two or three takes for one task and the user chooses one (stage
"choose"). Away mode lets sign-off, confirm and plan stops through after some minutes, as if the switch were off.

Each stop is a Needs You item with a "stage" and options whose "do" says what the answer does; carry() does it.
A take that has no stop in its way goes on as before: to the Producer's agent, or to the user when there is none.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import roles, studio, takes
from .model import session_root

PLAN_SEATS = ("producer", "review")  # seats whose tasks can wait for the user's OK
OPTIONS = (0, 2, 3)  # how many takes a seat offers for one task (0: just the one)
AWAY_MINUTES = 20
STAGES = ("signoff", "confirm", "plan", "choose")
_PLAN_QUIET = 3.0  # seconds without a new task before an outside app's plan is put to the user
CHANGES_SHOWN = 30  # of the session changes a Producer's run made, on its plan ticket

PRESETS = {
    "hands_off": "Every hand-in goes straight to the Producer: you hear the finished song.",
    "co_producer": "The Producer's decisions on takes and its plans wait for your OK.",
    "every": "Every seat's hand-in stops at you first, and so do the Producer's calls.",
}


def _now() -> float:
    return round(time.time(), 3)


def on(st: dict, seat: str) -> bool:
    return bool(st["seats"][seat].get("signoff"))


def co_producer(session: str | None = None) -> bool:
    return on(studio.read(session), "producer")


def involve_name(st: dict) -> str:
    """Which preset the seats' switches match: hands_off, co_producer, every, or custom."""
    lit = {sid for sid in roles.ORDER if on(st, sid)}
    if not lit:
        return "hands_off"
    if lit == {"producer"}:
        return "co_producer"
    if lit == set(roles.ORDER):
        return "every"
    return "custom"


def _makes_takes(sid: str) -> bool:
    return sid not in PLAN_SEATS and bool(roles.get(sid).owns)


def _switch(b, sid: str, lit: bool) -> str | None:
    """Turn one seat's SIGN OFF switch inside an open board. A seat that works on the session directly has nothing
    to stop, so signing it off moves it to takes (the answer says so). Turning a plan seat off lets its tasks that
    were waiting for a plan ticket go."""
    s = b.st["seats"][sid]
    note = None
    if bool(s.get("signoff")) == lit:
        return None
    s["signoff"] = lit
    if lit and s["mode"] == "direct" and _makes_takes(sid):
        s["mode"] = "propose"
        note = f"{roles.get(sid).title} now works in takes, so its hand-ins can stop at you."
    if not lit and sid in PLAN_SEATS:
        for t in b.st["tasks"]:
            if t.get("held") == "plan" and t.get("by") == sid:
                t.pop("held")
                b.emit("task", id=t["id"], seat=t["seat"], state=t["state"], title=t["title"], by=roles.YOU,
                       reason="sign-off turned off")
    b.emit("seat", seat=sid, occupant=studio.occupant_label(s["occupant"]) or "empty", mode=s["mode"], signoff=lit)
    return note


def seat_signoff(seat: str, on_: bool = True, session: str | None = None) -> dict:
    """Turn a seat's SIGN OFF switch: its work stops at you before it goes on (the Producer's: Co-Producer)."""
    if studio.me() != roles.YOU:
        raise PermissionError("Sign-off is the user's switch")
    sid = roles.get(seat).id
    with studio._Board(session) as b:
        note = _switch(b, sid, bool(on_))
        out = {"seat": sid, "signoff": bool(b.st["seats"][sid].get("signoff")), "involve": involve_name(b.st)}
    if note:
        out["note"] = note
    return out


def crew_involve(preset: str, session: str | None = None) -> dict:
    """Set every seat's SIGN OFF switch at once. preset: hands_off, co_producer or every."""
    if studio.me() != roles.YOU:
        raise PermissionError("Sign-off is the user's switch")
    p = str(preset).strip().lower().replace("-", "_").replace(" ", "_")
    if p not in PRESETS:
        raise ValueError(f"preset must be one of {', '.join(PRESETS)}")
    notes = []
    with studio._Board(session) as b:
        for sid in roles.ORDER:
            n = _switch(b, sid, p == "every" or (p == "co_producer" and sid == "producer"))
            if n:
                notes.append(n)
    return {"involve": p, "about": PRESETS[p], **({"notes": notes} if notes else {})}


def away_set(on_: bool = True, minutes: float = AWAY_MINUTES, session: str | None = None) -> dict:
    """Away mode: a sign-off, confirm or plan stop that has waited `minutes` goes through as if its switch were
    off, so a crew left running does not stall on you. A choice between options still waits."""
    if studio.me() != roles.YOU:
        raise PermissionError("Away mode is the user's switch")
    with studio._Board(session) as b:
        b.st["away"] = {"on": True, "since": _now(), "minutes": float(minutes)} if on_ else None
        b.emit("crew", what="away", on=bool(on_), minutes=float(minutes) if on_ else None)
    return {"away": bool(on_), **({"minutes": float(minutes)} if on_ else {})}


def options_for(st: dict, task: dict) -> int:
    """How many takes this task should come back as: the task's own setting, else its seat's. Only an agent in a
    seat that works in takes makes options; anyone else hands in one."""
    sid = task.get("seat")
    if sid not in roles.ROLES or task.get("audition") or not _makes_takes(sid):
        return 0
    s = st["seats"][sid]
    if (s["occupant"] or {}).get("kind") != "agent" or s["mode"] != "propose":
        return 0
    n = int(task.get("options") or s.get("options") or 0)
    return n if n in OPTIONS else 0


# ---- the tickets -----------------------------------------------------------------------------------------------------

def _item(b, stage: str, title: str, detail: str, options: list[dict], sender: str, **extra) -> dict:
    a = {"id": b.new_id("N"), "kind": "decision", "stage": stage, "from": sender, "task": extra.pop("task", None),
         "title": title, "detail": detail, "options": [{"id": chr(ord("a") + i), **o} for i, o in enumerate(options)],
         "blocks": [], "state": "open", "answer": None, "created": _now(), "resolved": None, **extra}
    b.st["attention"].append(a)
    b.emit("attention", id=a["id"], state="open", what=stage, sender=sender, title=title, take=extra.get("take"))
    return a


def _seat_of(tk: dict) -> str | None:
    return tk.get("seat") if tk.get("seat") in roles.ROLES else None


def _back(seat: str | None) -> dict:
    return {"label": f"Send back to the {roles.get(seat).title}…" if seat else "Send back…", "do": "return"}


def _agent_producer(st: dict) -> bool:
    return (st["seats"]["producer"]["occupant"] or {}).get("kind") == "agent"


def take_ticket(b, tk: dict, title: str, note: str = "") -> dict:
    """A hand-in for the user's ears. With the Producer's agent seated, the first answer passes it on to be heard
    there; without one, the user is the one who accepts."""
    seat = _seat_of(tk)
    opts = ([{"label": "Pass to the Producer", "do": "pass"}, _back(seat), {"label": "Accept", "do": "accept"},
             {"label": "Discard", "do": "discard"}] if _agent_producer(b.st) and seat != "producer" else
            [{"label": "Accept", "do": "accept"}, _back(seat), {"label": "Discard", "do": "discard"}])
    detail = "\n\n".join(x for x in (tk.get("summary") or "", note) if x)
    return _item(b, "signoff", title, detail, opts, seat or roles.YOU, take=tk["id"], hand_in=tk.get("submitted"))


def _title(b, tk: dict) -> str:
    t = next((x for x in b.st["tasks"] if x["id"] == tk.get("task")), None)
    return t["title"] if t else tk.get("name") or tk["id"]


def audition_task(b, tk: dict, note: str = "", by: str = roles.YOU) -> dict:
    """The Producer's task to hear a handed-in take; floor._launch starts it with the take in front of it.
    note: what the user said when they passed it on (briefs puts it in the task message)."""
    t = next((x for x in b.st["tasks"] if x["id"] == tk.get("task")), None)
    seat = _seat_of(tk)
    who = roles.get(seat).title if seat else "The user"
    title = f"Audition take {tk['id']}: {_title(b, tk)}"
    detail = (f"{who} handed in {tk['id']}" + (f" for task {t['id']}" if t else "") + ".\n"
              f"Their summary: {tk.get('summary') or '(none)'}\n"
              f"Listen to it against the brief, then take_accept(\"{tk['id']}\") or "
              f"take_reject(\"{tk['id']}\", reason).")
    now = _now()
    task = {"id": b.new_id("T"), "seat": "producer", "title": title, "detail": detail, "state": "queued",
            "depends": [], "scope": {}, "by": by, "take": None,
            "result": "", "audition": tk["id"], "created": now, "updated": now, "attempts": 0}
    if note:
        task["note"] = note
    b.st["tasks"].append(task)
    b.emit("task", id=task["id"], seat="producer", state="queued", title=title, by=task["by"])
    return task


def route(b, tk: dict) -> None:
    """A fresh hand-in, inside the floor's open board: stop at the user when its seat is signed off, else to the
    Producer's agent, else to the user (who is then the one who accepts)."""
    seat = _seat_of(tk)
    s = b.st["seats"][seat] if seat else None
    agent = bool(s) and (s["occupant"] or {}).get("kind") == "agent"
    title = f"Audition take {tk['id']}: {_title(b, tk)}"
    if s and seat != "producer" and agent and s.get("signoff") and _agent_producer(b.st):
        take_ticket(b, tk, f"{roles.get(seat).title} handed in {tk['id']}: {_title(b, tk)}")
    elif _agent_producer(b.st) and seat != "producer":
        audition_task(b, tk, by="floor")
    else:
        take_ticket(b, tk, title)


def choose_ticket(b, task: dict, tks: list[dict]) -> dict:
    """A seat handed in several takes for one task: the user hears them and keeps one."""
    seat = task["seat"]
    opts = [{"label": f"Use {tk.get('name') or tk['id']}", "do": "choose", "take": tk["id"]} for tk in tks]
    opts.append({"label": "None of these…", "do": "return"})
    detail = "\n".join(f"{tk['id']} · {tk.get('name') or ''}: {tk.get('summary') or '(no summary)'}" for tk in tks)
    return _item(b, "choose", f"Choose one of {len(tks)} options for {task['id']}: {task['title']}", detail, opts,
                 seat, task=task["id"], takes=[tk["id"] for tk in tks])


def plan_ticket(b, seat: str, tids: list[str], changes: list[str] | None = None) -> dict:
    """The tasks a plan seat added in one run, put to the user as one plan. changes: what the Producer's run changed
    on the session itself (tempo, form, tracks), shown with it: live already, undoable, not held."""
    tasks = [b.task(t) for t in tids]
    title = (f"The Producer's plan: {len(tasks)} task{'s' if len(tasks) != 1 else ''}" if seat == "producer" else
             f"{roles.get(seat).title}'s fixes: {len(tasks)} task{'s' if len(tasks) != 1 else ''}")
    detail = "\n".join(f"{t['id']} {t['seat']}: {t['title']}" + (f" (after {', '.join(t['depends'])})" if t.get("depends") else "")
                       for t in tasks)
    opts = [{"label": "Approve the plan" if seat == "producer" else "Approve the fixes", "do": "plan_ok"},
            {"label": f"Send it back to the {roles.get(seat).title}…", "do": "plan_back"}]
    a = _item(b, "plan", title, detail, opts, seat, tasks=[t["id"] for t in tasks],
              changes=list(changes or [])[:CHANGES_SHOWN])
    for t in tasks:
        t["held"] = a["id"]
    return a


def hold(d: Path, meta: dict, decision: str, reason: str = "", on_conflict: str = "fail",
         session: str | None = None) -> dict | None:
    """Co-Producer: the Producer's accept, return or discard of a take waits for the user to confirm it. None: it
    goes through now (Co-Producer is off, or the user already heard this hand-in and passed it, for an accept)."""
    if studio.me() != "producer":
        return None
    path = session_root(d)
    st = studio.read(str(path))
    if not on(st, "producer"):
        return None
    passed = meta.get("submitted") and (meta.get("signed") or {}).get("hand_in") == meta.get("submitted")
    if decision == "accept" and passed:
        return None
    verb = {"accept": "accept", "return": "send back", "discard": "discard"}[decision]
    if meta.get("proposal"):
        return {"proposed": meta["proposal"]["decision"], "take": meta["id"],
                "note": "Already waiting for the user to confirm your decision on it. Nothing more to do for it."}
    meta = takes._meta(d, proposal={"decision": decision, "reason": str(reason).strip(), "on_conflict": on_conflict,
                                    "at": _now(), "hand_in": meta.get("submitted")})
    seat = _seat_of(meta)
    back, again = _back(seat), {"label": "Ask the Producer again…", "do": "ask"}
    opts = {"accept": [{"label": "Confirm: accept it", "do": "accept"}, back, again],
            "return": [{"label": "Confirm: send it back", "do": "return", "confirm": True},
                       {"label": "Accept it anyway", "do": "accept"}, again],
            "discard": [{"label": "Confirm: discard it", "do": "discard"}, {"label": "Accept it instead", "do": "accept"},
                        back, again]}[decision]
    with studio._Board(str(path)) as b:
        b.close_auditions(meta["id"], f"{verb} proposed, waiting on the user")
        detail = "\n\n".join(x for x in ((f"The Producer's reason: {reason}" if str(reason).strip() else ""),
                                         (f"{roles.get(seat).title}'s summary: " if seat else "Summary: ")
                                         + (meta.get("summary") or "(none)")) if x)
        _item(b, "confirm", f"The Producer wants to {verb} {meta['id']}: {_title(b, meta)}", detail, opts,
              "producer", take=meta["id"], proposal=meta["proposal"], hand_in=meta.get("submitted"))
    takes._leave(d)
    return {"proposed": decision, "take": meta["id"],
            "note": "Co-Producer is on: the user confirms this before it is carried out. Nothing more to do for it."}


# ---- what the floor manager does with them ---------------------------------------------------------------------------

def waiting(a: dict, index: dict) -> bool:
    """An open item about takes still has something to decide: its take (or one of its options) is handed in."""
    ids = a.get("takes") or ([a["take"]] if a.get("take") else [])
    return any(index.get(t, {}).get("state") == "submitted" for t in ids)


def gather_plans(session: str, running: set[str], changes: dict[str, list[str]]) -> list[str]:
    """Tasks a plan seat added while signed off, put to the user as one plan once that seat has finished adding:
    its agent's run is over and nothing of its is about to start, or (an outside app) a few seconds have passed
    since its last task. changes: what each seat's last run changed on the session, taken as it is shown."""
    st = studio.read(session)
    made = []
    for sid in PLAN_SEATS:
        pend = [t for t in st["tasks"] if t.get("held") == "plan" and t.get("by") == sid and t["state"] in studio.OPEN]
        if not pend or sid in running:
            continue
        occ = st["seats"][sid]["occupant"] or {}
        if occ.get("kind") == "agent":
            if any(t["seat"] == sid for t in studio.ready_tasks(session)):
                continue  # its next run (an audition, a revision) may add more: one ticket for all of it
        elif time.time() - max(t["created"] for t in pend) < _PLAN_QUIET:
            continue
        with studio._Board(session) as b:
            ids = [t["id"] for t in b.st["tasks"] if t.get("held") == "plan" and t.get("by") == sid
                   and t["state"] in studio.OPEN]
            if ids:
                made.append(plan_ticket(b, sid, ids, changes.pop(sid, None))["id"])
    return made


def away_pass(session: str) -> list[str]:
    """Away mode: stops that have waited long enough go through as if their switch were off."""
    st = studio.read(session)
    aw = st.get("away") or {}
    if not aw.get("on"):
        return []
    now, out = time.time(), []
    for a in st["attention"]:
        if a["state"] != "open" or a.get("stage") not in ("signoff", "confirm", "plan") or a.get("away_tried"):
            continue
        wait = 60 * float(aw["minutes"] if aw.get("minutes") is not None else AWAY_MINUTES)
        if wait > 0 and now - max(a["created"], aw.get("since", 0)) < wait:
            continue
        want = {"signoff": "pass", "plan": "plan_ok", "confirm": (a.get("proposal") or {}).get("decision")}[a["stage"]]
        o = next((o for o in a["options"] if o.get("do") == want), None)
        if not o:
            continue  # a hand-in with no Producer to pass it to: only the user can decide it
        try:
            studio.attention_resolve(a["id"], o["id"], f"Away: let through after {aw.get('minutes'):g} minutes", session)
            out.append(a["id"])
        except Exception as e:  # noqa: BLE001 - a clash or a take gone: leave it for the user, and stop trying
            with studio._Board(session) as b:
                x = b.item(a["id"])
                x["away_tried"] = f"{type(e).__name__}: {e}"[:300]
    return out


# ---- carrying out the user's answer ----------------------------------------------------------------------------------

def your_changes(d: Path) -> list[str]:
    """What the user changed in a take after it was handed in (take_submit keeps a copy as handed.json)."""
    try:
        handed = json.loads((d / "handed.json").read_text(encoding="utf-8"))
        doc = json.loads((d / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [f"{c['where']}: {c['what']}" for c in takes.changes(handed, doc)]


def _sign(d: Path, meta: dict, note: str) -> list[str]:
    changed = your_changes(d)
    takes._meta(d, signed={"by": roles.YOU, "at": _now(), "hand_in": meta.get("submitted"), "note": note,
                           "changed": changed})
    return changed


def _accept(take: str, on_conflict: str, session: str | None) -> None:
    res = takes.take_accept(take, on_conflict=on_conflict, session=session)
    if not res["accepted"]:
        raise RuntimeError(f"Take {take} conflicts with the session and was not accepted: "
                           + "; ".join(f"{c['where']} ({c['why']})" for c in res["conflicts"])
                           + ". Open it on the Desk and SYNC it, or send it back.")


def option(a: dict, ref: str) -> dict:
    o = str(ref).strip().lower()
    found = next((x for x in a["options"] if x["id"] == o or x["label"].lower() == o or x.get("do") == o), None)
    if not found:
        raise KeyError(f"No option {ref!r} on {a['id']}. Options: "
                       + ", ".join(f"{x['id']}: {x['label']}" for x in a["options"]))
    return found


def carry(a: dict, ref: str, text: str, session: str | None) -> dict:
    """Do what the user answered to a stop (or to an audition request from before stages). Runs before
    attention_resolve locks the board, like the take calls it makes. Returns the option chosen."""
    o = option(a, ref)
    do = o.get("do") or {"a": "accept", "b": "return", "c": "discard"}.get(o["id"])
    text, stage = str(text or "").strip(), a.get("stage")
    prop = a.get("proposal") or {}
    if do == "accept":
        _accept(a["take"], prop.get("on_conflict") or "fail", session)
    elif do == "return" and stage == "choose":
        if not text:
            raise ValueError("Say what was wrong with all of them so the seat can try again")
        first, rest = a["takes"][0], a["takes"][1:]
        for x in rest:
            _drop(x, session)
        takes.take_reject(first, f"None of the {len(a['takes'])} options was right: {text}", session=session)
        with studio._Board(session) as b:
            if a.get("task"):
                b.task(a["task"])["take"] = first
    elif do == "return":
        reason = text
        if o.get("confirm"):
            reason = "\n\n".join(x for x in (prop.get("reason"), f"The user adds: {text}" if text else "") if x)
        if not reason:
            raise ValueError("Say what has to change so the seat can act on it")
        takes.take_reject(a["take"], reason, session=session)
    elif do == "discard":
        takes.take_discard(a["take"], session=session)
    elif do == "pass":
        d = takes._take_dir(a["take"], session)
        meta = takes._meta(d)
        if meta["state"] != "submitted":
            raise ValueError(f"{meta['id']} is {meta['state']} now")
        with studio._Board(session) as b:
            if not _agent_producer(b.st):
                raise ValueError("No agent sits in the Producer seat now: accept it, send it back or discard it")
        changed = _sign(d, meta, text)
        note = ("The user listened first and passed it to you" + (f": {text}" if text else ".")
                + (" They changed: " + "; ".join(changed[:8]) + (" ..." if len(changed) > 8 else "") if changed else ""))
        with studio._Board(session) as b:
            audition_task(b, meta, note)
    elif do == "ask":
        if not text:
            raise ValueError("Say what the Producer should listen for")
        d = takes._take_dir(a["take"], session)
        meta = takes._meta(d, proposal=None)
        with studio._Board(session) as b:
            audition_task(b, meta, f"The user asks you to hear it again before deciding: {text}")
    elif do == "choose":
        chosen = o["take"]
        for x in a["takes"]:
            if x != chosen:
                _drop(x, session)
        d = takes._take_dir(chosen, session)
        meta = takes._meta(d)
        _sign(d, meta, text)
        with studio._Board(session) as b:
            if a.get("task"):
                b.task(a["task"])["take"] = chosen
            pass_on = _agent_producer(b.st)
            if pass_on:
                audition_task(b, meta, f"The user chose this one from {len(a['takes'])} options"
                                       + (f": {text}" if text else "."))
        if not pass_on:
            _accept(chosen, "fail", session)
    elif do == "plan_ok":
        with studio._Board(session) as b:
            for t in b.st["tasks"]:
                if t.get("held") == a["id"]:
                    t.pop("held")
                    b.emit("task", id=t["id"], seat=t["seat"], state=t["state"], title=t["title"], by=roles.YOU,
                           reason="plan approved")
            if text:
                b.message(roles.YOU, a["from"], f"On your plan: {text}")
    elif do == "plan_back":
        if not text:
            raise ValueError("Say what to change in the plan")
        with studio._Board(session) as b:
            held = [t for t in b.st["tasks"] if t.get("held") == a["id"] and t["state"] in studio.OPEN]
            for t in held:
                t["held"] = "plan"
            rows = "\n".join(f"- {t['id']} {t['seat']}: {t['title']}" for t in held) or "- (none left)"
            studio._new_task(b, a["from"], "Revise your plan with the user's notes",
                             f"The user sent your plan back: {text}\n\nIts tasks, still waiting for their OK:\n{rows}\n\n"
                             "Change them with task_update (title, detail, depends, or state=\"cancelled\") and add "
                             "what is missing with task_add. When you end your turn, the plan goes back to the user.")
    else:
        raise ValueError(f"{a['id']}: option {o['id']} does nothing here")
    return o


def _drop(take: str, session: str | None) -> None:
    """Discard an option the user did not keep (one already over is left as it is)."""
    try:
        takes.take_discard(take, session=session)
    except ValueError:
        pass
