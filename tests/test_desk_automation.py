"""The Desk draws automation lanes (web/automation.js) and saves a whole lane per gesture through /api/call:
automation(clear=True, points=[...]) with the revision the page read."""
import json

import pytest

from tunetrack import api


@pytest.fixture
def proj(tmp_path):
    api.project_new("Lanes", folder=str(tmp_path / "song"), tempo=120)
    api.track_add("Lead", "synth:lead")
    return str(tmp_path / "song")


def _call(fn, args, rev=None):
    from starlette.testclient import TestClient

    from tunetrack import server
    c = TestClient(server.app, base_url=f"http://127.0.0.1:{server.PORT}")
    body = {"fn": fn, "args": args}
    if rev is not None:
        body["rev"] = rev
    r = c.post("/api/call", content=json.dumps(body), headers={
        "content-type": "application/json", "x-tunetrack-token": server.TOKEN, "origin": f"http://127.0.0.1:{server.PORT}"})
    return r.status_code, r.json()


def test_the_desk_replaces_a_whole_lane_in_one_call(proj):
    rev = api.load().rev
    code, res = _call("automation", {"track": "Lead", "param": "volume_db", "clear": True,
                                     "points": [[4, 0.0], [0, -12.5]]}, rev)
    assert code == 200, res
    assert api.load().track("Lead").automation["volume_db"] == [[0.0, -12.5], [4.0, 0.0]]
    # a second gesture sends the lane as it now is: the old points do not linger
    code, _ = _call("automation", {"track": "Lead", "param": "volume_db", "clear": True, "points": [[2, -3]]},
                    api.load().rev)
    assert code == 200 and api.load().track("Lead").automation["volume_db"] == [[2.0, -3.0]]
    # the last point removed: the lane goes
    code, _ = _call("automation", {"track": "Lead", "param": "volume_db", "clear": True, "points": []}, api.load().rev)
    assert code == 200 and "volume_db" not in api.load().track("Lead").automation


def test_the_desk_draws_a_device_setting_and_a_stale_page_is_refused(proj):
    code, res = _call("automation", {"track": "Lead", "param": "cutoff", "device": "instrument", "clear": True,
                                     "points": [[0, 800], [8, 4000]]}, api.load().rev)
    assert code == 200, res
    assert api.load().track("Lead").instrument.automation["cutoff"] == [[0.0, 800.0], [8.0, 4000.0]]
    stale = api.load().rev - 1
    code, res = _call("automation", {"track": "Lead", "param": "cutoff", "device": "instrument", "clear": True,
                                     "points": []}, stale)
    assert code == 409 and res.get("conflict")
    assert api.load().track("Lead").instrument.automation["cutoff"]  # untouched
