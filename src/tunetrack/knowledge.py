"""What the crew knows and what it has learned: reference notes to look up, and lessons kept per seat.

Reference notes are markdown files under <library>/knowledge/<shelf>/<topic>.md (shelf "theory" holds the notes
distilled from a music theory textbook; any other folder is another shelf, and the user can drop their own notes
in). They are read on demand, a note or one section of it at a time, so a book is digested once and never re-read:

    # Cadences
    seats: composer, review
    summary: one line - what it covers and when to look it up

    ## Half cadence
    - ...

Lessons are what earlier sessions taught a seat, one line each, in <library>/lessons/<seat>.md ("all" for the
whole crew). A launched agent's brief carries the latest few for its seat; the Producer writes them when it
reviews a session (crew_review), and any seat can add one for itself.

Both stay on this machine (the library folder is gitignored for them): the notes are derived from the user's
copy of a book, and the lessons from their sessions.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

from . import config, roles, studio

KNOWLEDGE = "knowledge"
LESSONS = "lessons"
LESSONS_SHOWN = 10   # in a seat's brief
LESSONS_KEPT = 30    # per seat on disk; the oldest go first
LESSON_CHARS = 240
FIND_HITS = 12


def _kdir() -> Path:
    return config.HOME / KNOWLEDGE


def _ldir() -> Path:
    return config.HOME / LESSONS


# ---- reference notes -------------------------------------------------------------------------------------------

def _notes() -> dict[str, Path]:
    """topic -> file, for every note on every shelf. A topic is the file's stem; a stem found on two shelves is
    known as shelf/stem."""
    root = _kdir()
    if not root.is_dir():
        return {}
    files = sorted(p for p in root.rglob("*.md") if p.name.lower() != "readme.md")
    stems: dict[str, int] = {}
    for p in files:
        stems[p.stem] = stems.get(p.stem, 0) + 1
    return {(p.stem if stems[p.stem] == 1 else p.relative_to(root).with_suffix("").as_posix()): p for p in files}


def _head(text: str) -> dict:
    """Title, seats and summary from a note's first lines."""
    out = {"title": "", "seats": [], "summary": ""}
    for line in text.splitlines()[:8]:
        s = line.strip()
        if s.startswith("# ") and not out["title"]:
            out["title"] = s[2:].strip()
        elif s.lower().startswith("seats:"):
            out["seats"] = [x.strip().lower() for x in s[6:].split(",") if x.strip()]
        elif s.lower().startswith("summary:"):
            out["summary"] = s[8:].strip()
    return out


def _sections(text: str) -> list[tuple[str, str]]:
    """(heading, body) per "## " section, in order."""
    out, name, buf = [], None, []
    for line in text.splitlines():
        if line.startswith("## "):
            if name is not None:
                out.append((name, "\n".join(buf).strip()))
            name, buf = line[3:].strip(), []
        elif name is not None:
            buf.append(line)
    if name is not None:
        out.append((name, "\n".join(buf).strip()))
    return out


def _resolve(topic: str) -> tuple[str, Path]:
    notes = _notes()
    t = topic.strip().lower().removesuffix(".md")
    if t in notes:
        return t, notes[t]
    hits = [k for k in notes if k.endswith("/" + t) or t in k]
    if len(hits) == 1:
        return hits[0], notes[hits[0]]
    if not notes:
        raise ValueError("No reference notes yet: the knowledge folder in the library is empty")
    raise ValueError(f"No note {topic!r}" + (f"; did you mean {', '.join(hits[:6])}?" if hits else
                                              f". Topics: {', '.join(notes)}"))


def topics(seat: str | None = None) -> list[dict]:
    """The index: one row per note (topic, summary, seats), only those for a seat when one is given."""
    rows = []
    for t, p in _notes().items():
        h = _head(p.read_text(encoding="utf-8", errors="replace"))
        if seat and h["seats"] and seat not in h["seats"]:
            continue
        rows.append({"topic": t, "summary": h["summary"] or h["title"], "seats": h["seats"]})
    return rows


def knowledge(topic: str | None = None, find: str | None = None, seat: str | None = None) -> dict:
    """Reference notes on music theory and craft, read a piece at a time. No arguments: the index (topic and
    one-line summary; seat= narrows it). topic="cadences": that note. topic="cadences#half": just the section
    whose heading matches. find="deceptive": the sections, across all notes, that mention it."""
    if find:
        words = [w for w in re.split(r"\s+", find.lower().strip()) if w]
        if not words:
            raise ValueError("find needs a word to look for")
        hits = []
        for t, p in _notes().items():
            for name, body in _sections(p.read_text(encoding="utf-8", errors="replace")):
                hay = (name + "\n" + body).lower()
                if all(w in hay for w in words):
                    line = next((ln.strip() for ln in body.splitlines() if words[0] in ln.lower()), name)
                    hits.append({"at": f"{t}#{name}", "line": line[:200]})
        return {"find": find, "hits": hits[:FIND_HITS], "more": max(0, len(hits) - FIND_HITS),
                "read": "knowledge(topic=\"<at>\") for a section"}
    if not topic:
        rows = topics(seat)
        return {"topics": [{"topic": r["topic"], "summary": r["summary"]} for r in rows],
                "read": "knowledge(topic=\"<topic>\") or \"<topic>#<section>\"; find=\"<words>\" to search"}
    name, _, part = topic.partition("#")
    t, p = _resolve(name)
    text = p.read_text(encoding="utf-8", errors="replace")
    secs = _sections(text)
    if part.strip():
        want = part.strip().lower()
        pick = [s for s in secs if s[0].lower() == want] or [s for s in secs if want in s[0].lower()]
        if not pick:
            raise ValueError(f"No section {part!r} in {t}. Sections: {', '.join(s[0] for s in secs)}")
        return {"topic": f"{t}#{pick[0][0]}", "text": pick[0][1]}
    return {"topic": t, "sections": [s[0] for s in secs], "text": text.strip()}


# ---- lessons ---------------------------------------------------------------------------------------------------

def _seat_id(seat: str | None) -> str:
    s = str(seat or "").strip().lower()
    if s in ("all", "crew", "everyone"):
        return roles.EVERYONE
    return roles.get(s).id


def _lfile(seat: str) -> Path:
    return _ldir() / f"{seat}.md"


_ROW = re.compile(r"^- (?:(\d{4}-\d{2}-\d{2}) )?(.*)$")


def _read(seat: str) -> list[dict]:
    f = _lfile(seat)
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _ROW.match(line.strip())
        if m and m.group(2).strip():
            out.append({"date": m.group(1) or "", "text": m.group(2).strip()})
    return out


def _write(seat: str, rows: list[dict]) -> None:
    f = _lfile(seat)
    f.parent.mkdir(parents=True, exist_ok=True)
    who = "the whole crew" if seat == roles.EVERYONE else f"the {roles.get(seat).title} seat"
    body = [f"# Lessons for {who}", "",
            "One line each, newest last. Edit freely: a launched agent reads the last "
            f"{LESSONS_SHOWN} in its brief.", ""]
    body += [f"- {r['date']} {r['text']}".replace("-  ", "- ") for r in rows]
    tmp = f.with_suffix(".tmp")
    tmp.write_text("\n".join(body) + "\n", encoding="utf-8")
    tmp.replace(f)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def lessons_for(seat: str, n: int = LESSONS_SHOWN) -> list[str]:
    """The latest n lessons a seat's brief carries: the crew's first, then the seat's own."""
    crew = [r["text"] for r in _read(roles.EVERYONE)][-max(1, n // 3):]
    own = [r["text"] for r in _read(seat)] if seat != roles.EVERYONE else []
    return crew + own[-max(0, n - len(crew)):]


def lessons(seat: str | None = None, add: list[str] | str | None = None, remove: list[int] | None = None) -> dict:
    """What earlier sessions taught a seat: one line each, kept across sessions and put in that seat's brief
    (the latest few). seat: a seat id or "all" (the whole crew); default your own. add: new lessons - each a
    rule that changes a decision, with the reason ("B kept A's rhythm and sounded the same: vary rhythm, not
    only pitch"). remove: numbers from the list to drop (stale, wrong, merged into a new line). The Producer,
    Review and the user write for any seat; another seat for itself only."""
    me = studio.me()
    target = _seat_id(seat or (me if me != roles.YOU else roles.EVERYONE))
    if (add or remove) and me not in (roles.YOU, "producer", "review") and target != me:
        raise PermissionError(f"Only the Producer, Review or the user write lessons for {target}; "
                              f"add yours with seat=\"{me}\"")
    rows = _read(target)
    dropped = []
    if remove:
        idx = {int(i) for i in remove}
        bad = [i for i in idx if not 1 <= i <= len(rows)]
        if bad:
            raise ValueError(f"No lesson {bad[0]}: the list has {len(rows)}")
        dropped = [rows[i - 1]["text"] for i in sorted(idx)]
        rows = [r for i, r in enumerate(rows, 1) if i not in idx]
    added, skipped = [], []
    for text in ([add] if isinstance(add, str) else (add or [])):
        t = " ".join(str(text).split())[:LESSON_CHARS]
        if not t:
            continue
        if any(_norm(t) == _norm(r["text"]) for r in rows):
            skipped.append(t)
            continue
        rows.append({"date": time.strftime("%Y-%m-%d"), "text": t})
        added.append(t)
    trimmed = max(0, len(rows) - LESSONS_KEPT)
    rows = rows[trimmed:]
    if add or remove:
        _write(target, rows)
    out = {"seat": target, "lessons": [f"{i}. {r['text']}" for i, r in enumerate(rows, 1)]}
    for k, v in (("added", added), ("already_there", skipped), ("removed", dropped)):
        if v:
            out[k] = v
    if trimmed:
        out["trimmed_oldest"] = trimmed
    return out


def brief(seat: str) -> str:
    """The part of a launched agent's brief that comes from here: its lessons and which notes it can look up."""
    parts = []
    rows = lessons_for(seat)
    if rows:
        parts.append("Lessons from earlier sessions (keep to them unless the task says otherwise):\n"
                     + "\n".join(f"- {r}" for r in rows))
    mine = [r["topic"] for r in topics(seat)]
    if mine:
        parts.append("Theory notes when a decision needs one: knowledge(topic=\"<topic>#<section>\") or find=. "
                     "Yours: " + ", ".join(mine) + ".")
    # Writing lessons is the Producer's job in the session review (crew_review's task says how), not every step's.
    return "\n\n".join(parts)
