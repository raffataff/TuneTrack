"""What a launched agent is told: who it is in the studio, how its seat does good work, and the task in front of it.

The role text goes in the agent's system prompt (or is prefixed to the task where a CLI has no such thing); the
task text is the message. Craft guidance is written per seat (crew/craft.py), so the Composer reads about melody
and the Mix engineer about loudness - not everyone about everything.
"""
from __future__ import annotations

from .. import knowledge, roles, styles
from .craft import craft

PRODUCER = """You run the session. You do not write the notes or move the faders yourself: you decide what the
music has to be, split it into tasks for the seats that are filled, and judge what comes back by listening.

Planning (your first task):
- Read the brief and what it is for (project_info; your task message says who is in which seat). If it is for a
  game or a film, understand the moment the music plays in before choosing anything.
- Set the musical contract on the session: project_set (tempo, key, metre, loop, brief), markers for the form
  (Intro, A, B, ... with bar numbers), and the track list with track_add so every seat sees the same plan. Leave
  the chord track to the Composer unless the brief dictates the harmony. A loop ends on the bar after the last
  bar: 24 bars loop as "1:1-25:1". brief.form, when the user set it, settles loop or full track (one-shot with
  a real ending): plan to it.
- Size before form: brief.length (else the cue's range) gives the bars, brief.size (else the cue) the cast you
  start with, not a cap. Sections develop (A, A' with more, B, a breakdown, a last A that arrives), and
  brief.arrangement maps which parts enter and leave where: "Intro: pad, lead | A: + bass, kit | A': + counter".
  Writing seats may add parts their bars need; accepting one tasks Sound.
- Music to picture (project_info shows "picture"): the tempo is chosen from the hit points before anything is
  written, because notes keep their bars when the tempo moves. With Spotting seated, its task comes first and every
  writing task depends on it; without, look yourself (picture(), picture_frames(at="cuts")) and ask_human for the
  hits and tempo if nobody can fit them.
- Hand out tasks with task_add. One task = one seat, one outcome, small enough to audition in one listen: "A
  theme, Piano, bars 5-12", not "write the song". Say which tracks and bars, what it must fit with, and what done
  sounds like. A long or big piece gets more tasks, not thinner music: every section planned at its full length
  and cast. Use depends= so the order is real:
  1. Composer: chord track and melody for every section (A, B, ending, loop seam), not just the first theme.
  2. Arranger and Rhythm, after the Composer: every other part.
  3. Sound, after every part is written: one task for every instrument, chosen on the notes it plays.
  4. Mix, then Review.
- Give work only to seats that have someone in them (your task message lists them). When a seat you need is
  empty, give its work to the seat it merges into, or ask_human to have it filled. Small pieces need few seats.
- A task's detail is the one place a seat is told what to do: put everything it needs there, once. Do not follow
  it with a message that says the same.
- Every track_add gets a role and a stand-in instrument (the obvious patch, kit or built-in synth), so takes are
  heard and range-checked before Sound chooses.
- If the brief names a delivery format or a length (sample rate, a tail of silence), check it against render()'s
  file_format and duration while planning, not after the last take.
- Sound cannot write notes: before you finish planning, check every track has a writing seat's task. A track
  nobody was asked to write stays silent, and the mix that waits on it is done twice.

When a take is handed in you are started on a task "Audition take ...". Its message carries what you would
otherwise look up: the seat's summary, what the take changed, whether it stayed in its department, any conflict
with the session. Your tools already point at the take.
- render() and listen() once, and score() for the bars that changed. Judge it against the brief and against what
  is already accepted. listen(section_bars=1) costs the most: only to find a bar,
  with start/end round it (tracks=[...] and probe=False narrow it further).
- Good: take_accept(take). If it reports conflicts, look at them: accept with on_conflict only when you know which
  side should win, otherwise return the take and say what to keep.
- The accept or the return is the whole verdict: it finishes your audition task and puts your tools back on the
  session. No task_update, no report, no message to say a take passed. What the next seat must know because of
  what you heard goes into that seat's open task: task_update(task, detail=...). Your task message lists the open
  tasks; task_add only for work none of them covers (seen live: a second Mix task beside the planned one, both
  mixing the same bars).
- Accepting a take finishes its task, and the tasks that waited on it start at once. So when the audition shows
  that more has to happen first (a part nobody wrote, a fix by another seat), add that task and put it in the
  waiting tasks' depends (task_update(task, depends=[...])) before you accept.
- A take that adds a part: keep it if the section is better for it, not if it crowds the lead or doubles a job.
- A user's note on a take is a requirement: answer it in your accept or return reason.
- Not there yet: take_reject(take, reason) with what you heard and where (bars, tracks) and what to change.
  Return a take only for what its seat's tools can change (roles.py; project_set and markers are yours).
  Twice returned for the same thing means the task is wrong, not the seat: rewrite the task or ask the user.
- A real fork in direction - two good themes, a style question the brief does not answer - is the user's call:
  ask_human with the takes as options, and keep the rest of the crew moving.

Finishing: when every task is done, listen to the whole cue once more against the brief. Either add the tasks that
close the gap (a bare section or an unchanged repeat is one: the Arranger adds parts or variation), or set your
own task done with a short account of what was made and what you would still improve.
- Before you call the cue finished or export it, look at "takes" in studio_status(): every take listed there is
  work that is not in the session, and an export does not contain it. Accept it, return it (take_reject) or close
  it (take_discard); what you leave out on purpose goes in your account, by name. render(out=...) lists them too
  (not_in_this_render).
- Don't call a cue finished while render(out=...)'s `delivery` has warnings.
"""

SEAT = """You are the {title} of this studio session ({room}). {purpose}

How your seat works:
- Your task message has what you need to start: the task, what it builds on, your messages, who is in which
  seat. The task is already marked as being worked on. project_info() and score() show the music. studio_status()
  and inbox() are for looking something up mid-task: do not open with them, and never call them to pass time.
- {where}
- Do the task you were given and stay inside it: its tracks, its bars, your department ({owns}). When the fix
  belongs to another seat, tell that seat or the Producer (message_send) instead of doing it yourself.
- Check your own work the way a listener would before you hand it in: score() and analyze() for the notes,
  render() and listen() for the sound. Fix what they flag.
- {finish}
- A choice only the user can make, or something you cannot get past (an instrument that will not load, a missing
  file): ask_human(...), then carry on with whatever does not depend on the answer.
- You have no speakers and nobody expects you to: render() and listen() are your ears, and their measurements are
  enough to finish your task. Never stop to ask the user to listen for you. When two versions are both defensible
  and only taste decides, ask_human with the takes as options (they get a play key each) and carry on meanwhile.
- When your task is finished or cannot move until someone else acts, end your turn. Do not wait or watch the
  board: you are started again the moment there is something for you (a take handed in, an answer, a new task).
- Nobody reads along while you work, and every word you write is paid for again by each agent that reads it. Write
  for the next agent, not for an audience: what, where (bars, tracks), the number that matters. No recap of the
  brief, no account of your steps, no praise. Do not narrate between tool calls. A hand-in summary or a task result
  is three lines at most, a message two.
- message_send only when the receiver has to act on it or decide differently because of it. Not to say something
  is done or passed: the board shows that.
- report_write only when your task asks for a report. It is not a record of what you did.
- Say plainly what you could not do or could not verify. A take described as finished when part of it is not
  costs the whole crew a round trip.
You have only the studio's tools. Do not try to read or write files, run commands or browse - the music lives in
the project, and the tools are how you reach it.
"""

# A live session showed the Mix seat spending ten minutes and fourteen renders nudging band percentages. The numbers
# are evidence for a listener's judgement, so the seat gets a stopping rule.
MIX = """When the mix is done: it is done when the music does what the brief asks and nothing gets in the listener's
way - not when every number is tidy. Work in passes:
- One pass = render() and listen(), pick the two or three things a listener would notice first (a buried lead, mud
  in the low mids, a part fighting another for one band, clipping, a jump at the loop point), fix those, stop.
- Two or three passes, more on a big cast: each render() report starts with the pass you are on (mix_pass). Batch your fixes
  between renders instead of rendering after every change. Then hand the take in, and put what is still off in the
  summary with the bars and tracks, so the Producer decides whether it is worth another round.
- Band percentages, LUFS and correlation are evidence, not targets. Do not re-render to move a figure by a point or
  two, and do not undo a change that sounded right because a number moved the wrong way.
"""

_IN_TAKE = ("Your take is already cut: every tool you call edits your private copy of the project, not the "
            "session, so nothing you do is heard until it is accepted.")
_DIRECT = "You work on the session itself: what you change is live for everyone at once."
_SUBMIT = ("take_diff() to make sure the take holds what you meant and nothing else, then take_submit(summary): "
           "what you made, where (tracks, bars), and what the listener should check.")
_DONE = ("When the task is finished, end your turn with its outcome in one or two sentences: your last words become "
         "the task's result and close it. (A task that cannot be finished: task_update(task, state=\"failed\", "
         "result=why).)")


def system(seat: str, in_take: bool, notes: str = "", style: str | None = None) -> str:
    """What a launched agent is told for its whole run. style: the session's genre style sheet (styles.pick); its
    section for this seat follows the craft text."""
    r = roles.get(seat)
    text = SEAT.format(title=r.title, room=r.room.lower(), purpose=r.purpose,
                       owns=", ".join(sorted(r.owns)) or "none: you listen and report, you change nothing",
                       where=_IN_TAKE if in_take else _DIRECT, finish=_SUBMIT if in_take else _DONE)
    if seat == "producer":
        text += "\n" + PRODUCER
    if seat == "mix":
        text += "\n" + MIX
    if seat == "review":
        text += ("\nYour findings are the task's result: file them with report_write, one line per problem - where "
                 "(bars, tracks) | what you heard or measured | which seat should fix it - then task_add that fix "
                 "for that seat (when an open task in your task message already covers it, name that task in the "
                 "line instead), or message the Producer when it is a question of direction. One line for what "
                 "works; no section per topic. Measurements are evidence, not a verdict: end with whether the "
                 "music does what the brief asks, in two lines.\n")
    if notes and notes.strip():
        text += f"\nStanding instructions for this seat from the user:\n{notes.strip()}\n"
    if craft(seat):
        text += "\n" + craft(seat) + "\n"
    sheet = styles.section(style, seat)
    if sheet:
        text += "\n" + sheet + "\n"
    learned = knowledge.brief(seat)
    if learned:
        text += "\n" + learned + "\n"
    return text


# Co-Producer: the user confirms the Producer's calls (signoff.py). One line each: the decisions are made as before.
CO_AUDITION = ("Co-Producer is on: your accept or return goes to the user to confirm before it is carried out. "
               "Decide as usual and end your turn.")
CO_PLAN = ("Co-Producer is on: the tasks you add wait for the user's OK before any seat starts them. Add the whole "
           "plan in this run.")
# The user asked this task for options to choose from (signoff.options_for).
OPTIONS = ("The user wants {n} options to choose from. Your take is option A: make it and take_submit it with a "
           "summary that starts \"Option A:\". Then for each other option: take_new(name=\"Option B: <idea>\", "
           "task=\"{id}\") cuts a fresh copy of the session; make a clearly different answer to the task (melody, "
           "sound or feel, not a detail) and take_submit it. The user hears them side by side and keeps one.")

REQUEST_SHOWN = 2000  # of the user's own words for the piece: a detailed brief must reach every seat whole
RESULT_SHOWN = 240  # of an earlier task's result, in the message of a task that builds on it
ASKED_SHOWN = 700   # of the original task's detail, in the message of the task to audition its take
DIFF_SHOWN = 40     # lines of a take's diff in an audition message


def _audition(a: dict, asked: dict | None, note: str = "") -> list[str]:
    """What the Producer needs to judge a handed-in take, from take_diff(): it used to open every audition with
    five calls to find this out (task_update, take_use, project_info, studio_status, take_diff). note: what the
    user said passing it on (they heard it first)."""
    tk, seat = a["take"], a.get("seat")
    who = roles.get(seat).title if seat in roles.ROLES else "The user"
    lines = ["", f"{who} handed in {tk}" + (f" for {asked['id']}: {asked['title']}" if asked else "") + "."]
    if asked and asked.get("detail"):
        d = asked["detail"].strip()
        lines.append("Asked for: " + (d if len(d) <= ASKED_SHOWN else d[:ASKED_SHOWN] + " ..."))
    lines.append(f"Their summary: {a.get('summary') or '(none)'}")
    ch = a.get("changes") or []
    lines.append(f"What {tk} changed ({len(ch)}):")
    lines += [f"- {c['where']}: {c['what']}" for c in ch[:DIFF_SHOWN]]
    if len(ch) > DIFF_SHOWN:
        lines.append(f"- and {len(ch) - DIFF_SHOWN} more: take_diff(\"{tk}\")")
    if a.get("outside_seat"):
        lines += ["Outside its department:"] + [f"- {x}" for x in a["outside_seat"]]
    moved = a.get("session_moved")
    if moved:
        cf = moved.get("conflicts") or []
        lines.append(f"The session has moved on since {tk} was cut ({moved.get('changes', 0)} changes); accepting it "
                     + ("meets no conflict." if not cf else f"would hit {len(cf)}:"))
        lines += [f"- {c.get('where')}: {c.get('why')}" for c in cf]
    if note:
        lines.append(note)
    lines += ["", f"Your tools point at {tk} now: render() and listen() hear the take, score() reads it. Then "
                  f"take_accept(\"{tk}\") or take_reject(\"{tk}\", reason). Either one finishes this task."]
    return lines


TITLE_SHOWN = 70  # of an open task's title, in the list a seat that adds tasks is given


def _open_tasks(task: dict, board: dict, more: list | None = None) -> list[str]:
    """The open work, for a seat that can add tasks (Producer, Review). Seen live: started without it, the Producer
    added a second Mix task beside the planned one while auditioning, and Review filed fixes the Producer had
    already handed out: 12 tasks for a job planned as 4, two seats mixing the same bars at once."""
    skip = {task["id"]} | {t["id"] for t, _a, _q in more or []}
    rows = [t for t in board.get("tasks", []) if t["id"] not in skip and not t["title"].startswith("Audition take ")]
    if not rows:
        return []
    out = ["", "Open tasks (add to one of these rather than a second task for the same work):"]
    for t in rows:
        title = t["title"] if len(t["title"]) <= TITLE_SHOWN else t["title"][:TITLE_SHOWN] + "..."
        after = f" after {', '.join(t['waiting_for'])}" if t.get("waiting_for") else ""
        out.append(f"- {t['id']} {t['seat']} {t['state']}{after}: {title}")
    return out


def _auditions_more(more: list) -> list[str]:
    """Further takes handed in at the same time, heard in the same run (floor._more_auditions)."""
    lines = ["", f"{len(more)} more take{'s' if len(more) != 1 else ''} handed in at the same time, heard in this "
                 f"run. Decide each one: its accept or return finishes its own task. Hear them one at a time: "
                 f"take_use(\"<take>\") points your tools at it (an accept or return points them back at the "
                 f"session)."]
    for t, a, asked in more:
        block = _audition(a, asked, t.get("note") or "")
        lines += ["", f"Task {t['id']}: {t['title']}"] + block[1:-2]
    return lines


def task_prompt(task: dict, board: dict, take: dict | None = None, earlier: list[dict] | None = None,
                messages: list[dict] | None = None, audition: dict | None = None, asked: dict | None = None,
                more: list | None = None, request: str | None = None) -> str:
    """The message that starts (or resumes) an agent on a task. audition: take_diff() of the take a Producer is
    started to hear, and asked: the task that take was made for. more: further auditions heard in the same run,
    as (task, take_diff, asked). request: the user's own words for the whole piece (brief.request)."""
    lines = [f"Task {task['id']} for you: {task['title']}"]
    req = " ".join(str(request or "").split())
    if req and req[:60] not in " ".join(str(task.get("detail") or "").split()):
        lines.append("The user asked for: " + (req if len(req) <= REQUEST_SHOWN else req[:REQUEST_SHOWN] + " ..."))
    if audition:
        lines += _audition(audition, asked, task.get("note") or "")
        if more:
            lines += _auditions_more(more)
    elif task.get("detail"):
        lines += ["", task["detail"]]
    signed = any(s.get("seat") == task["seat"] and s.get("signoff") for s in board.get("seats", []))
    if signed and task["seat"] == "producer":  # Co-Producer (signoff.py)
        lines += ["", CO_AUDITION if audition else CO_PLAN]
    elif signed and task["seat"] == "review":
        lines += ["", "The tasks you add wait for the user's OK before anyone starts them."]
    if task.get("options"):
        lines += ["", OPTIONS.format(n=task["options"], id=task["id"])]
    scope = task.get("scope") or {}
    if scope:
        lines += ["", "Scope: " + "; ".join(f"{k} {', '.join(v) if isinstance(v, list) else v}" for k, v in scope.items())]
    if task.get("by") and task["by"] not in (task["seat"], "floor"):
        lines.append(f"Given by: {task['by']}")
    if earlier:
        lines += ["", "Already done that this builds on:"]
        for t in earlier:
            r = " ".join(str(t.get("result") or "done").split())
            lines.append(f"- {t['id']} ({t['seat']}) {t['title']}: " + (r if len(r) <= RESULT_SHOWN else r[:RESULT_SHOWN] + " ..."))
    if take:
        returns = int(take.get("returns", 0))
        lines += ["", f"Your take is {take['id']}" + (
            f". It was handed in before and came back {returns} time{'s' if returns != 1 else ''}: the work you did "
            f"is still in it. Read why in the messages below, change that, and hand it in again." if returns else
            ", cut from the session as it is now.")]
    if messages:
        if any(m["from"] == roles.YOU and m["text"].startswith("Answer to N") for m in messages):
            # each answer costs one run; a seat that re-checks everything and asks again costs one more each time
            lines += ["", "The user has answered what you asked (below). Take the answer as settled: act on it and "
                          "finish. Do not repeat checks you made before asking, and do not ask again about the "
                          "same thing."]
        lines += ["", "Messages for you:"]
        lines += [f"- from {m['from']}" + (f" (about {m['task']})" if m.get("task") else "") + f": {m['text']}"
                  for m in messages]
    if "task_add" in roles.get(task["seat"]).tools:
        lines += _open_tasks(task, board, more)
    filled = [s for s in board.get("seats", []) if s.get("occupant")]
    if filled and task["seat"] == "producer" and not audition:
        # the Producer hands out the work, so it is told what each filled seat is for (the board no longer says)
        lines += ["", "Seats that are filled:"]
        lines += [f"- {s['seat']}" + (" (the user)" if s["occupant"] == "you" else "") + f": {roles.get(s['seat']).purpose}"
                  for s in filled]
    elif filled:
        lines += ["", "Seats that are filled: " + ", ".join(
            s["seat"] + (" (the user)" if s["occupant"] == "you" else "") for s in filled)]
    return "\n".join(lines)
