"""Genre style sheets (skill/styles/*.md): each seat's brief carries its section of the session's style."""
import re

import pytest

from tunetrack import api, mcp_server, roles, styles
from tunetrack.crew import briefs, floor


def test_every_sheet_has_a_title_genre_words_and_only_real_seats():
    all_ = styles.styles()
    assert {"chiptune", "lofi", "orchestral", "epic", "synthwave", "electronic"} <= set(all_)
    for name, s in all_.items():
        assert s["title"] and s["about"] and s["match"], name
        assert set(s["sections"]) <= set(roles.ORDER), (name, set(s["sections"]) - set(roles.ORDER))
        assert {"producer", "composer", "mix", "review"} <= set(s["sections"]), name


def test_a_sheet_names_only_tools_its_seat_has():
    """Like craft text: a line that sends a seat to a tool it was not given costs a failed call every run."""
    for name, s in styles.styles().items():
        for seat, text in s["sections"].items():
            named = {w for w in re.findall(r"\b([a-z_]+)\(", text) if w in mcp_server.TOOLS}
            assert named <= roles.get(seat).tools, (name, seat, sorted(named - roles.get(seat).tools))


def test_the_producers_craft_lists_every_style():
    from tunetrack.crew import craft
    for name in styles.names():
        assert name in craft.PRODUCER, name


@pytest.mark.parametrize("genre, style", [
    ("lo-fi hip hop", "lofi"), ("8-bit chiptune", "chiptune"), ("NES 8bit", "chiptune"),
    ("Orchestral fantasy", "orchestral"), ("epic orchestral battle", "epic"), ("dark synthwave", "synthwave"),
    ("dark ambient", "ambient"), ("cozy village", "cozy"), ("drum and bass", "electronic"),
    ("survival horror", "horror"), ("quirky puzzle", "playful"), ("polka", None), ("", None),
])
def test_the_genre_picks_a_style(genre, style):
    assert styles.pick({"genre": genre}) == style


def test_a_chosen_style_wins_over_the_genre():
    assert styles.pick({"genre": "lo-fi", "style": "orchestral"}) == "orchestral"
    assert styles.pick({"genre": "lo-fi", "style": "no-such"}) == "lofi"


def test_project_set_keeps_a_known_style_and_refuses_others(tmp_path):
    api.project_new("Styled", folder=str(tmp_path / "s"), tempo=90, key="C major", brief={"genre": "lo-fi"})
    try:
        api.project_set(brief={"style": "Chiptune"})
        assert api.project_info()["brief"]["style"] == "chiptune"
        with pytest.raises(ValueError, match="one of .*lofi"):
            api.project_set(brief={"style": "polka"})
        api.project_set(brief={"style": "none", "mood": "sunny"})
        br = api.project_info()["brief"]
        assert "style" not in br and br["mood"] == "sunny" and br["genre"] == "lo-fi"
    finally:
        api.set_default_project(None)


def test_a_seats_brief_carries_its_section_of_the_style():
    plain = briefs.system("composer", True)
    styled = briefs.system("composer", True, style="chiptune")
    assert "Style: Chiptune." in styled and "Style:" not in plain
    assert styles.styles()["chiptune"]["sections"]["composer"] in styled
    assert briefs.system("sound", True, style="horror").count("Style:") == 1
    # the longest brief with the longest sheet still fits the bound the craft test keeps
    longest = max(len(briefs.system("producer", False, style=n)) for n in styles.names())
    assert longest < 14500


def test_the_floor_reads_the_sessions_style_at_launch(tmp_path):
    api.project_new("Floor style", folder=str(tmp_path / "f"), tempo=90, key="C major", brief={"genre": "synthwave"})
    try:
        f = floor.Floor(str(tmp_path / "f"))
        assert f._style() == "synthwave"
        api.project_set(brief={"style": "epic"})
        assert f._style() == "epic"
    finally:
        api.set_default_project(None)
