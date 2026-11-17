"""Genre style sheets: how each seat does one genre properly. One markdown file per style in `skill/styles/`: a
front matter (title, the genre words that pick it, one line about it) and a section per seat (`## composer`).

A launched agent's brief carries its seat's section of the session's style: `brief["style"]` when the Producer or
the user set one, else the style the words of `brief["genre"]` name. `craft.GENRES` stays the overview every seat
that plans or chooses sounds reads; a sheet is the detail for the seat doing the work.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import config

DIR = config.ROOT / "skill" / "styles"
_cache: dict = {"key": None, "styles": {}}


def _parse(text: str) -> dict:
    meta, body = {}, text
    m = re.match(r"---\s*\n(.*?)\n---\s*\n", text, re.S)
    if m:
        body = text[m.end():]
        for line in m.group(1).splitlines():
            k, _, v = line.partition(":")
            if v.strip():
                meta[k.strip()] = v.strip()
    sections: dict[str, str] = {}
    for part in re.split(r"^## +", body, flags=re.M)[1:]:
        head, _, rest = part.partition("\n")
        if rest.strip():
            sections[head.strip().lower()] = rest.strip()
    return {"title": meta.get("title", ""), "about": meta.get("about", ""),
            "match": [w.strip().lower() for w in meta.get("match", "").split(",") if w.strip()],
            "sections": sections}


def styles() -> dict[str, dict]:
    """Every style sheet by name (its file's stem), read again when a file changes."""
    files = sorted(DIR.glob("*.md")) if DIR.is_dir() else []
    key = tuple((f.name, f.stat().st_mtime_ns) for f in files)
    if key != _cache["key"]:
        out = {}
        for f in files:
            s = _parse(f.read_text(encoding="utf-8"))
            s["title"] = s["title"] or f.stem
            out[f.stem] = s
        _cache.update(key=key, styles=out)
    return _cache["styles"]


def names() -> list[str]:
    return list(styles())


def style_list() -> list[dict]:
    """The styles a session can be set to: name, title, one line about it, the genre words that pick it."""
    return [{"name": k, "title": s["title"], "about": s["about"], "match": s["match"]} for k, s in styles().items()]


def check(name) -> str | None:
    """A style name as stored in a brief: a known name, or None for none ("", "none", "auto")."""
    n = str(name or "").strip().lower()
    if n in ("", "none", "auto"):
        return None
    if n not in styles():
        raise ValueError(f"Unknown style {name!r}: one of {', '.join(names())}, or none")
    return n


def _words(text: str) -> str:
    return " " + " ".join(re.sub(r"[^a-z0-9&]+", " ", str(text).lower()).split()) + " "


def pick(brief: dict | None) -> str | None:
    """The style a brief asks for: its "style" when that is a known one, else the style whose genre words appear
    most in its "genre" ("epic orchestral battle" is epic: two of its words against one), then the longest words,
    then the earliest. None when nothing fits."""
    brief = brief or {}
    st = str(brief.get("style") or "").strip().lower()
    if st in styles():
        return st
    genre = _words(brief.get("genre") or "")
    best = None
    for name, s in styles().items():
        found = [(genre.find(w), len(w)) for w in map(_words, s["match"]) if w.strip() and w in genre]
        if found:
            score = (len(found), sum(n for _, n in found), -min(a for a, _ in found))
            if best is None or score > best[0]:
                best = (score, name)
    return best[1] if best else None


def section(name: str | None, seat: str) -> str:
    """What a seat reads for a style: "Style: <title>." and its section, or "" when the style has none for it."""
    s = styles().get(name or "")
    if not s or seat not in s["sections"]:
        return ""
    return f"Style: {s['title']}.\n{s['sections'][seat]}"
