"""Reference notes (knowledge) and per-seat lessons, and how they reach a launched agent's brief."""
import pytest

from tunetrack import config, knowledge, roles, studio
from tunetrack.crew import briefs

NOTE = """# Cadences
seats: composer, review
summary: how phrases end; look up when ending a phrase or section

## Half cadence
- Ends on V: a question. Use before a loop seam.

## Deceptive cadence
- V to vi instead of I: delays the ending.
"""


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "library")
    shelf = config.HOME / "knowledge" / "theory"
    shelf.mkdir(parents=True)
    (shelf / "cadences.md").write_text(NOTE, encoding="utf-8")
    (shelf / "mixing.md").write_text("# Mixing\nseats: mix\nsummary: levels\n\n## Bus\n- glue\n", encoding="utf-8")
    (shelf / "README.md").write_text("source and licence", encoding="utf-8")
    monkeypatch.setattr(studio, "_seat", roles.YOU)
    return config.HOME


def test_index_lists_notes_and_filters_by_seat(lib):
    assert [t["topic"] for t in knowledge.knowledge()["topics"]] == ["cadences", "mixing"]
    assert [t["topic"] for t in knowledge.knowledge(seat="mix")["topics"]] == ["mixing"]


def test_note_and_one_section(lib):
    full = knowledge.knowledge("cadences")
    assert full["sections"] == ["Half cadence", "Deceptive cadence"]
    part = knowledge.knowledge("cadences#deceptive")
    assert part["topic"] == "cadences#Deceptive cadence" and "vi" in part["text"] and "loop seam" not in part["text"]
    with pytest.raises(ValueError, match="Sections"):
        knowledge.knowledge("cadences#plagal")
    with pytest.raises(ValueError, match="Topics"):
        knowledge.knowledge("fugue")


def test_find_returns_sections(lib):
    hits = knowledge.knowledge(find="loop seam")["hits"]
    assert [h["at"] for h in hits] == ["cadences#Half cadence"]


def test_same_stem_on_two_shelves_is_named_by_shelf(lib):
    other = lib / "knowledge" / "mine"
    other.mkdir()
    (other / "cadences.md").write_text("# My cadences\n\n## A\n- x\n", encoding="utf-8")
    topics = {t["topic"] for t in knowledge.knowledge()["topics"]}
    assert {"theory/cadences", "mine/cadences"} <= topics


def test_lessons_add_dedupe_remove_and_cap(lib):
    out = knowledge.lessons("composer", add=["Vary rhythm in B, not only pitch.", "vary rhythm in B not only pitch"])
    assert out["added"] == ["Vary rhythm in B, not only pitch."] and out["already_there"]
    knowledge.lessons("composer", add=[f"rule {i}" for i in range(knowledge.LESSONS_KEPT + 5)])
    rows = knowledge.lessons("composer")["lessons"]
    assert len(rows) == knowledge.LESSONS_KEPT and rows[-1].endswith(f"rule {knowledge.LESSONS_KEPT + 4}")
    out = knowledge.lessons("composer", remove=[1])
    assert out["removed"] and len(out["lessons"]) == knowledge.LESSONS_KEPT - 1
    with pytest.raises(ValueError):
        knowledge.lessons("composer", remove=[999])


def test_only_lead_seats_write_for_others(lib, monkeypatch):
    monkeypatch.setattr(studio, "_seat", "rhythm")
    with pytest.raises(PermissionError):
        knowledge.lessons("composer", add=["x"])
    assert knowledge.lessons(add=["Ghost notes at !p, not !mf."])["seat"] == "rhythm"
    monkeypatch.setattr(studio, "_seat", "producer")
    assert knowledge.lessons("composer", add=["y"])["added"] == ["y"]


def test_brief_carries_lessons_and_seat_topics(lib):
    knowledge.lessons("all", add=["Check the loop seam before handing in."])
    knowledge.lessons("composer", add=["Put the hook's peak in the second half of the phrase."])
    text = briefs.system("composer", in_take=True)
    assert "Check the loop seam" in text and "hook's peak" in text
    assert "Yours: cadences." in text and "mixing" not in text.split("Yours:")[1]
    assert "lessons(seat=" not in text  # writing them is the session review's task, not every step's


def test_empty_library_adds_nothing_to_brief(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path / "empty")
    assert knowledge.brief("mix") == ""
    assert knowledge.knowledge()["topics"] == []


def test_tools_registered_for_every_seat():
    for r in roles.ROLES.values():
        assert {"knowledge", "lessons"} <= r.tools


@pytest.mark.parametrize("tok,key,pcs,bass", [
    ("iim7b5", "C major", [2, 5, 8, 0], None),
    ("viio7", "C major", [11, 2, 5, 8], None),
    ("Ger+6", "C major", [8, 0, 3, 6], None),
    ("It+6", "A minor", [5, 9, 3], None),
    ("Fr+6", "C major", [8, 0, 2, 6], None),
    ("N6", "A minor", [10, 2, 5], 2),
    ("I64", "C major", [0, 4, 7], 7),
    ("V65", "C major", [7, 11, 2, 5], 11),
    ("V42", "C major", [7, 11, 2, 5], 5),
    ("V65/V", "C major", [2, 6, 9, 0], 6),
    ("I6", "C major", [0, 4, 7, 9], None),  # "6" alone is still an added sixth
])
def test_numerals_the_notes_use(tok, key, pcs, bass):
    from tunetrack import theory
    c = theory.parse_any(tok, theory.Key(key))
    assert c.pcs == pcs and c.bass == bass
    assert theory.parse_progression(f"iv {tok} V", theory.Key(key))[1][0].pcs == pcs
