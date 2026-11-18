"""Gear: the one list of instruments and effects, the NEW marks, the folder watcher and the audition session
(no real plugin scans: plugins.scan is a fake that adds whatever .vst3 sits in tmp_path to a fake registry)."""
from pathlib import Path

import pytest

from tunetrack import api, config, gear, plugins, synths

FRESH_STATE = {"watching": False, "scanning": [], "last_poll": None, "last_scan": None, "found": [], "error": None,
               "new": 0}


@pytest.fixture(autouse=True)
def studio(tmp_path, monkeypatch):
    """A fake config, registry and scanner; gear's module state and gear.json reset."""
    cfg = dict(config.DEFAULT_CONFIG, vst3_dirs=[str(tmp_path / "vst3")], sample_dirs=[str(tmp_path / "samples")],
               skip_plugins=[])
    (tmp_path / "vst3").mkdir()
    (tmp_path / "samples").mkdir()
    reg = {"plugins": [], "failed": []}
    scans = []

    def fake_scan(retry_failed=False, **_kw):
        scans.append(retry_failed)
        known = {p["path"] for p in reg["plugins"]}
        for f in plugins.find_vst3_files(cfg["vst3_dirs"]):
            if str(f) not in known and not any(s.lower() in f.name.lower() for s in cfg["skip_plugins"]):
                reg["plugins"].append({"name": f.stem, "path": str(f), "kind": "instrument"})
        return reg

    monkeypatch.setattr(config, "load_config", lambda: {k: list(v) if isinstance(v, list) else v
                                                         for k, v in cfg.items()})
    monkeypatch.setattr(config, "save_config", lambda new: cfg.update(new))
    monkeypatch.setattr(plugins, "load_registry", lambda: reg)
    monkeypatch.setattr(plugins, "scan", fake_scan)
    monkeypatch.setattr(plugins, "list_patches", lambda *a, **k: [])
    saved_state = dict(gear._state)
    gear._state.clear()
    gear._state.update(FRESH_STATE)
    gear._pending.clear()
    gear._rescan.clear()
    gear._seen_file().unlink(missing_ok=True)
    yield {"cfg": cfg, "reg": reg, "scans": scans, "vst3": tmp_path / "vst3", "samples": tmp_path / "samples"}
    gear._state.clear()
    gear._state.update(saved_state)
    gear._pending.clear()
    gear._rescan.clear()
    gear._seen_file().unlink(missing_ok=True)


def _ids(items):
    return [i["id"] for i in items]


def _item(cat, item_id):
    return next(i for i in cat["items"] if i["id"] == item_id)


def _sfz(folder: Path, name: str) -> Path:
    f = folder / "MyLib" / f"{name}.sfz"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("<region> sample=a.wav\n", encoding="utf-8")
    return f


# ---- the list --------------------------------------------------------------------------------------------------

def test_catalog_lists_builtin_synths_and_effects_that_parse_device_accepts():
    items = gear.catalog()["items"]
    for name in synths.SYNTHS:
        it = next(i for i in items if i["id"] == f"synth:{name}")
        assert it["kind"] == "instrument"
        assert api.parse_device(it["id"], instrument=True).name == name
    for name in synths.FX:
        it = next(i for i in items if i["id"] == f"builtin:{name}")
        assert it["kind"] == "effect"
        assert api.parse_device(it["id"], instrument=False).name == name


def test_first_look_marks_nothing_new_and_writes_gear_json(studio):
    _sfz(studio["samples"], "Harp")
    cat = gear.catalog()
    assert cat["new"] == 0
    assert gear._seen_file().exists()
    assert f"sfz:{studio['samples'] / 'MyLib' / 'Harp.sfz'}" in gear._load_seen()["seen"]


def test_sfz_added_later_is_new_then_stamped_then_cleared_by_gear_seen(studio):
    gear.catalog()
    f = _sfz(studio["samples"], "Cello")
    sid = f"sfz:{f}"
    assert _item(gear.catalog(), sid)["new"] is True

    gear.poll()
    assert isinstance(_item(gear.catalog(), sid)["added"], float)
    assert gear.gear_state()["new"] == 1

    gear.gear_seen([sid])
    assert "new" not in _item(gear.catalog(), sid)
    assert gear.gear_state()["new"] == 0


def test_gear_seen_without_ids_marks_everything_seen(studio):
    gear.catalog()
    _sfz(studio["samples"], "Oboe")
    _sfz(studio["samples"], "Flute")
    assert gear.catalog()["new"] == 2
    gear.gear_seen()
    assert gear.catalog()["new"] == 0


def test_gear_seen_with_ids_before_first_look_keeps_installed_gear_not_new(studio):
    _sfz(studio["samples"], "Harp")
    gear.gear_seen(["synth:pad"])
    assert gear.catalog()["new"] == 0


def test_catalog_query_needs_every_word_and_kind_filters(studio):
    ids = _ids(gear.catalog(query="bass analog")["items"])
    assert ids == ["synth:analog_bass"]
    assert _ids(gear.catalog(query="built-in reverb")["items"]) == ["builtin:reverb"]
    assert all(i["kind"] == "effect" for i in gear.catalog(kind="effect")["items"])
    assert "synth:pad" not in _ids(gear.catalog(kind="effect")["items"])


def test_catalog_new_items_sort_first(studio):
    gear.catalog()
    f = _sfz(studio["samples"], "Zither")
    assert gear.catalog()["items"][0]["id"] == f"sfz:{f}"
    assert gear.catalog(kind="instrument")["items"][0]["id"] == f"sfz:{f}"


# ---- maker and type ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("entry, maker", [
    ({"name": "SSL Native X-Echo", "path": r"C:\VST3\SSL Native X-Echo.vst3"}, "Solid State Logic"),
    ({"name": "C4 Stereo", "path": r"C:\VST3\WaveShell1-VST3 14.12_x64.vst3"}, "Waves"),
    ({"name": "Legacy Thing", "path": r"C:\Common Files\VST3\Harrison\Legacy\Legacy.vst3"}, "Harrison"),
    ({"name": "HALion Sonic", "path": r"C:\VST3\Steinberg\HALion Sonic.vst3"}, "Steinberg"),
    ({"name": "Foo", "path": r"C:\VST3\Foo.vst3", "vendor": "Acme"}, "Acme"),
    ({"name": "Magic7", "path": r"C:\VST3\Magic7.vst3"}, "Magic7"),
])
def test_plugin_maker(entry, maker):
    assert gear.plugin_maker(dict(entry, kind="effect")) == maker


@pytest.mark.parametrize("name, kind, typ", [
    ("TDR Nova", "effect", "EQ"), ("CLA-76 Mono", "effect", "Dynamics"), ("SSL Native X-ValveComp v6", "effect",
                                                                        "Dynamics"),
    ("ValhallaSupermassive", "effect", "Reverb"), ("H-Delay Stereo", "effect", "Delay"),
    ("Doubler2 Mono/Stereo", "effect", "Modulation"), ("GTR Stomp 4 Mono", "effect", "Distortion / Saturation"),
    ("GTR Tuner Mono", "effect", "Utility / Analyzer"), ("Youlean Loudness Meter 2", "effect", "Utility / Analyzer"),
    ("L2 Stereo", "effect", "Dynamics"), ("Magic7", "effect", "Other"),
    ("Surge XT", "instrument", "Synth"), ("HALion Sonic", "instrument", "Sampler"),
    ("MODO DRUM", "instrument", "Drums / Percussion"), ("BBC Symphony Orchestra (64 Bit)", "instrument", "Orchestral"),
])
def test_plugin_type_from_name(name, kind, typ):
    assert gear.plugin_type({"name": name, "kind": kind, "path": "x.vst3"}) == typ


def test_plugin_type_prefers_vst3_subcategories():
    assert gear.plugin_type({"name": "Mystery", "kind": "effect", "category": "Fx|Delay"}) == "Delay"
    assert gear.plugin_type({"name": "TDR Nova", "kind": "effect", "subcategories": ["Fx", "Dynamics"]}) == "Dynamics"
    assert gear.plugin_type({"name": "Thing", "kind": "instrument", "category": "Instrument|Synth"}) == "Synth"


def test_catalog_items_carry_maker_and_type(studio, monkeypatch):
    halion = studio["vst3"] / "HALion Sonic.vst3"
    halion.write_bytes(b"")
    studio["reg"]["plugins"].append({"name": "HALion Sonic", "path": str(halion), "kind": "instrument"})
    monkeypatch.setattr(plugins, "list_patches", lambda *a, **k: [
        {"id": "h1", "name": "Big Band Brass", "plugin": "HALion Sonic", "kind": "instrument",
         "tags": ["brass", "halion-sonic-taiko-bigband-multi"]},
        {"id": "h2", "name": "Chunky", "plugin": "HALion Sonic", "kind": "instrument", "tags": []},
        {"id": "s1", "name": "Wobble", "plugin": "Surge XT", "kind": "instrument", "tags": ["surge", "basses"]},
    ])
    _sfz(studio["samples"], "Grand Piano")
    vsco = studio["samples"] / "vsco2-ce" / "Cello Pizz.sfz"
    vsco.parent.mkdir()
    vsco.write_text("<region> sample=a.wav\n", encoding="utf-8")
    cat = gear.catalog()
    assert {k: _item(cat, "vst3:HALion Sonic")[k] for k in ("maker", "type")} == {"maker": "Steinberg",
                                                                                 "type": "Sampler"}
    assert _item(cat, "patch:h1")["type"] == "Orchestral"  # the multi's slug ("taiko") does not count
    assert _item(cat, "patch:h2")["type"] == "Sampler"     # nothing in its name: its plugin's type
    assert (_item(cat, "patch:s1")["maker"], _item(cat, "patch:s1")["type"]) == ("Surge Synth Team", "Synth")
    piano = _item(cat, f"sfz:{studio['samples'] / 'MyLib' / 'Grand Piano.sfz'}")
    assert (piano["maker"], piano["type"]) == ("MyLib", "Piano / Keys")
    cello = _item(cat, f"sfz:{vsco}")
    assert (cello["maker"], cello["type"]) == ("Versilian Studios", "Orchestral")
    assert _item(cat, "synth:drums")["type"] == "Drums / Percussion"
    assert _item(cat, "synth:supersaw")["type"] == "Synth"
    assert _item(cat, "builtin:reverb")["type"] == "Reverb"
    for i in cat["items"]:
        assert i["type"] in (gear.EFFECT_TYPES if i["kind"] == "effect" else gear.INSTRUMENT_TYPES)
        if i["source"] in ("synth", "builtin"):
            assert i["maker"] == "TuneTrack"
    assert _ids(gear.catalog("steinberg")["items"]) == ["patch:h1", "patch:h2", "vst3:HALion Sonic"]


# ---- the watched folders ---------------------------------------------------------------------------------------

def test_gear_folders_add_once_and_remove(tmp_path):
    extra = tmp_path / "more plugins"
    extra.mkdir()
    assert str(extra) in gear.gear_folders(add=str(extra))["folders"]["vst3"]
    assert gear.gear_folders(add=str(extra))["folders"]["vst3"].count(str(extra)) == 1
    assert str(extra) not in gear.gear_folders(remove=str(extra))["folders"]["vst3"]
    assert str(extra) in gear.gear_folders(add=str(extra), kind="samples")["folders"]["samples"]


def test_gear_folders_rejects_missing_folder_and_bad_kind(tmp_path):
    with pytest.raises(ValueError):
        gear.gear_folders(add=str(tmp_path / "nope"))
    with pytest.raises(ValueError):
        gear.gear_folders(add=str(tmp_path), kind="drums")


def test_new_vst3_is_scanned_on_the_second_poll_once_settled(studio):
    (studio["vst3"] / "Fresh.vst3").write_bytes(b"x" * 10)
    first = gear.poll()
    assert studio["scans"] == [] and first["waiting"] == ["Fresh.vst3"]
    second = gear.poll()
    assert studio["scans"] == [False]
    assert second["found"] == ["Fresh"] and second["waiting"] == []
    assert "Fresh" in gear.gear_state()["found"]


def test_vst3_still_growing_waits_again(studio):
    f = studio["vst3"] / "Growing.vst3"
    f.write_bytes(b"x" * 10)
    gear.poll()
    f.write_bytes(b"x" * 500)
    gear.poll()
    assert studio["scans"] == []
    assert gear.poll()["found"] == ["Growing"]


def test_vst3_bundle_folder_settles_like_a_file(studio):
    b = studio["vst3"] / "Bundle.vst3" / "Contents"
    b.mkdir(parents=True)
    (b / "Bundle.dll").write_bytes(b"x" * 10)
    gear.poll()
    assert gear.poll()["found"] == ["Bundle"]


def test_poll_scan_now_scans_at_once(studio):
    (studio["vst3"] / "Quick.vst3").write_bytes(b"x")
    assert gear.poll(scan_now=True)["found"] == ["Quick"]
    assert studio["scans"] == [False]


def test_skip_plugins_are_never_scanned(studio):
    studio["cfg"]["skip_plugins"] = ["badplug"]
    (studio["vst3"] / "BadPlug.vst3").write_bytes(b"x")
    gear.poll()
    gear.poll()
    assert gear.poll(scan_now=True)["found"] == []
    assert studio["scans"] == []


def test_gear_rescan_retry_failed_reaches_the_next_scan(studio, monkeypatch):
    monkeypatch.setattr(gear, "start_watch", lambda: None)
    studio["reg"]["failed"].append({"path": str(studio["vst3"] / "Crashy.vst3"), "error": "boom"})
    gear.gear_rescan(retry_failed=True)
    gear.poll()
    assert studio["scans"] == [True]
    gear.poll()
    assert studio["scans"] == [True]  # the retry was used up


# ---- hearing it ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("item_id, phrase", [
    ("synth:drums", "drums"), ("synth:analog_bass", "bass"), ("synth:pad", "chords"),
    ("synth:string_machine", "chords"), ("synth:lead", "melody"),
])
def test_phrase_for_synths(item_id, phrase):
    assert gear._phrase_for(item_id) == phrase


@pytest.mark.parametrize("meta, phrase", [
    ({"tags": ["drums"]}, "drums"), ({"drum_map": {"kick": 36}}, "drums"), ({"role": "percussion"}, "drums"),
    ({"tags": ["bass"]}, "bass"), ({"range": [28, 55]}, "bass"), ({"tags": ["warm pad"]}, "chords"),
    ({"tags": ["strings"]}, "chords"), ({"tags": ["lead"]}, "melody"),
])
def test_phrase_for_patches(monkeypatch, meta, phrase):
    monkeypatch.setattr(plugins, "get_patch", lambda pid: dict(meta, plugin="FakeSynth"))
    assert gear._phrase_for("patch:my-sound") == phrase


def test_phrase_for_patch_gone_guesses_from_the_id(monkeypatch):
    def gone(pid):
        raise KeyError(pid)
    monkeypatch.setattr(plugins, "get_patch", gone)
    assert gear._phrase_for("patch:big-kit") == "drums"


def _pitches(p, track=0):
    return [n.pitch for n in p.tracks[track].clips[0].notes]


def test_audition_transposes_to_the_key_but_not_drums():
    in_c = _pitches(gear.audition_project("synth:lead"))
    in_d = _pitches(gear.audition_project("synth:lead", key="D major"))
    assert in_d == [x + 2 for x in in_c]
    drums_c = _pitches(gear.audition_project("synth:drums"))
    assert _pitches(gear.audition_project("synth:drums", key="D major")) == drums_c


def test_audition_minor_key_uses_the_minor_phrase():
    assert 63 in _pitches(gear.audition_project("synth:lead", key="C minor"))        # Eb4
    assert 63 not in _pitches(gear.audition_project("synth:lead", key="C major"))
    assert 63 in _pitches(gear.audition_project("synth:pad", key="C minor"))         # chords: Eb4


def test_audition_loop_covers_the_clip():
    p = gear.audition_project("synth:analog_bass")
    clip = p.tracks[0].clips[0]
    assert p.loop == {"start": 0.0, "end": clip.length}
    assert p.length == clip.length
    assert max(n.start + n.dur for n in clip.notes) <= clip.length


def test_audition_keys_phrase_writes_no_clip():
    p = gear.audition_project("synth:epiano", phrase="keys")
    assert p.tracks[0].clips == []
    assert p.loop["end"] == 4.0


def test_audition_effect_is_a_bus_the_epiano_and_drums_play_through():
    p = gear.audition_project("builtin:reverb")
    bus = next(t for t in p.tracks if t.kind == "bus")
    assert [d.name for d in bus.fx] == ["reverb"]
    sources = [t for t in p.tracks if t.kind != "bus"]
    assert sorted(t.instrument.name for t in sources) == ["drums", "epiano"]
    assert all(t.output == bus.id for t in sources)


def test_audition_project_is_saved_in_audition_live():
    p = gear.audition_project("synth:pad")
    assert p.path == config.HOME / "audition-live"
    assert any(p.path.iterdir())
    assert gear.is_audition(p.path)
    assert not gear.is_audition(None)
    assert not gear.is_audition(config.HOME / "elsewhere")


def test_offline_audition_renders_a_file_with_sound():
    r = gear.audition("synth:pad")
    assert r["live"] is False
    assert Path(r["file"]).exists()
    assert r["silent"] is False
