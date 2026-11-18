"""Gear: every instrument and effect on this computer in one list, the folders TuneTrack watches for new ones (a
new plugin is scanned once its files stop changing), and hearing a piece of gear while choosing it.

Hearing it: on the live engine the gear plays a short phrase in a loop, in the open project's key and tempo, from
its own little session (`<library>/audition-live/`), and an instrument is armed so the MIDI keyboard and the
Desk's on-screen keys play it too. The Desk's next play points the engine back at the Desk's project. Without the
engine it is a short render (`api.audition`), or the one a patch was saved with.

What is new: `<library>/gear.json` keeps the ids the user has seen. The first time it is made, everything already
installed counts as seen, so only what arrives later is marked new.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from . import config, notation, plugins, synths, theory
from .model import Clip, Device, Project, Track

POLL_S = 15.0      # how often the watched folders are looked at
SETTLE_POLLS = 2   # a new plugin is scanned once its files looked the same this many times running
AUDITION_DIR = "audition-live"

_lock = threading.RLock()
_wake = threading.Event()
_state: dict = {"watching": False, "scanning": [], "last_poll": None, "last_scan": None, "found": [], "error": None,
                "new": 0}
_pending: dict[str, list] = {}  # new VST3 file -> [stamp, polls it looked the same]
_rescan: dict = {}              # asked for by the user: {"retry_failed": bool}


# ---- the list --------------------------------------------------------------------------------------------------

def _seen_file() -> Path:
    return config.HOME / "gear.json"


def _load_seen() -> dict:
    try:
        return json.loads(_seen_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_seen(d: dict) -> None:
    config.HOME.mkdir(parents=True, exist_ok=True)
    tmp = _seen_file().with_suffix(".tmp")
    tmp.write_text(json.dumps(d), encoding="utf-8")
    tmp.replace(_seen_file())


def _sample_dirs() -> list[str]:
    return [d for d in config.load_config().get("sample_dirs", []) if d]


def _sfz_files() -> list[Path]:
    out: list[Path] = []
    for d in _sample_dirs():
        p = Path(d)
        if p.is_dir():
            out.extend(p.rglob("*.sfz"))
    return sorted(set(out))


def _sfz_item(f: Path) -> dict:
    by = f.parent.name
    for d in _sample_dirs():
        try:
            rel = f.relative_to(d)
            by = rel.parts[0] if len(rel.parts) > 1 else Path(d).name
            break
        except ValueError:
            continue
    return {"id": f"sfz:{f}", "kind": "instrument", "source": "sfz", "name": f.stem, "by": by,
            "maker": _SFZ_MAKERS.get(by.lower(), by),
            "type": _by_rules(_INSTRUMENT_RULES, _text(f.stem, by, f.parent.name)) or "Other"}


# ---- maker and type: how the drawer groups the list -------------------------------------------------------------

BUILTIN_MAKER = "TuneTrack"
EFFECT_TYPES = ("EQ", "Dynamics", "Reverb", "Delay", "Modulation", "Distortion / Saturation", "Filter", "Pitch",
                "Stereo / Spatial", "Utility / Analyzer", "Other")
INSTRUMENT_TYPES = ("Synth", "Sampler", "Drums / Percussion", "Piano / Keys", "Orchestral", "Other")

# a plugin's maker from its name (lower case, matched from the start of a word); first match wins
_VENDORS = [(re.compile(p), v) for p, v in (
    (r"\bssl\b", "Solid State Logic"), (r"\bvalhalla", "Valhalla DSP"), (r"\btdr\b", "Tokyo Dawn Records"),
    (r"\byoulean\b", "Youlean"), (r"\bt-de-esser\b", "Techivation"), (r"\bsurge\b", "Surge Synth Team"),
    (r"\bdexed\b", "Digital Suburban"), (r"\b(halion|groove agent|padshop|retrologue|cubase)\b", "Steinberg"),
    (r"\b(bbc symphony|spitfire|labs)\b", "Spitfire Audio"), (r"\bmodo\b", "IK Multimedia"),
    (r"\bsfizz\b", "sfztools"), (r"\bharrison\b", "Harrison"), (r"\b(fabfilter|pro-[qlcrg])", "FabFilter"),
    (r"\b(ozone|neutron|nectar|izotope)\b", "iZotope"), (r"\b(kontakt|reaktor|massive|guitar rig)\b",
                                                         "Native Instruments"),
    (r"\bvital\b", "Vital Audio"), (r"\b(serum|ott)\b", "Xfer Records"), (r"\barturia\b", "Arturia"),
)]
# SFZ libraries whose maker is known, by folder name
_SFZ_MAKERS = {"vsco2-ce": "Versilian Studios", "salamander-grand-piano": "Alexander Holm",
               "greg-sullivan-epianos": "Greg Sullivan", "mtg-solo-sax": "MTG (UPF)", "jrhodes3c": "jlearman",
               "steel-drum": "jlearman", "virtuosity-drums": "Karoryfer Samples", "meatbass": "Karoryfer Samples",
               "black-and-green-guitars": "Karoryfer Samples", "horse-pulse": "Karoryfer Samples",
               "caveman-cosmonaut": "Karoryfer Samples", "discord-gm-bank": "Discord SFZ GM Bank"}

# VST3 sub-categories ("Fx|Reverb", "Instrument|Synth") -> type, when the scan stored them
_EFFECT_SUBCATS = {"eq": "EQ", "dynamics": "Dynamics", "reverb": "Reverb", "delay": "Delay",
                   "modulation": "Modulation", "distortion": "Distortion / Saturation", "filter": "Filter",
                   "pitch shift": "Pitch", "pitch": "Pitch", "spatial": "Stereo / Spatial",
                   "surround": "Stereo / Spatial", "analyzer": "Utility / Analyzer", "tools": "Utility / Analyzer"}
_INSTRUMENT_SUBCATS = {"synth": "Synth", "sampler": "Sampler", "drum": "Drums / Percussion",
                       "piano": "Piano / Keys"}

# name / tag keywords -> type, in order: the first that matches wins
_EFFECT_RULES = [(t, re.compile(p)) for t, p in (
    ("Utility / Analyzer", r"meter|loudness|analy[sz]|tuner|scope|spectrum|utility|\bgain\b|\btrim\b"),
    ("Reverb", r"verb|\broom\b|\bhall\b|\bplate\b|\bspring\b|supermassive|chamber|convol"),
    ("Delay", r"delay|echo"),
    ("EQ", r"\beq\b|equali|\bnova\b|pro-?q|pultec|channel ?strip|drumstrip|vocalstrip"),
    ("Dynamics", r"comp|limit|\bl[123]\b|gate|expand|de-?ess|dynamic|maxim|\bc[46]\b|rvox|\b2a\b|\b76\b|1176|"
                 r"\blmc|\bidx\b|transient|mastering|mixdown|multiband"),
    ("Modulation", r"chorus|flang|phaser|tremolo|vibrato|doubler|ensemble|rotary|leslie|modulat"),
    ("Pitch", r"pitch|auto-?tune|harmoni[sz]|octav|shifter"),
    ("Distortion / Saturation", r"satur|distort|drive|fuzz|\bamp\b|stomp|\bgtr\b|tape|tube|valve|crush|"
                                r"lo-?fi|decapit|\bclip"),
    ("Filter", r"filter|\bwah\b"),
    ("Stereo / Spatial", r"imager|widen|width|spatial|panner|binaural|surround"),
)]
_INSTRUMENT_RULES = [(t, re.compile(p)) for t, p in (
    ("Orchestral", r"timpani|glocken|vibraphone|marimba|xylo|mallet|tuned perc|tubular|celest"),
    ("Piano / Keys", r"piano|\bkeys\b|rhodes|wurli|\bep\b|e-?piano|organ|harpsi|clavi|cp80|pianet"),
    ("Drums / Percussion", r"drum|\bkit\b|percussion|\bperc\b|groove agent|\bbeat\b|taiko|snare|kick|\btoms?\b|"
                           r"cymbal|hi-?hat|cajon|conga|bongo"),
    ("Synth", r"synth|surge|dexed|\bfm\b|analog|wavetable|\bchip|serum|vital|massive|retrologue|juno|jupiter|"
              r"moog|oberheim|ppg|string machine"),
    ("Orchestral", r"orchestr|symphon|string|violin|viola|cello|contrabass|double bass|brass|horn|trumpet|"
                   r"trombone|tuba|flute|oboe|clarinet|bassoon|woodwind|choir|\bharp\b|pizz|\barco\b|vsco"),
    ("Synth", r"\bpads?\b|\blead\b|\bplucks?\b|\barp\b|\bsaw|square|digital|polysynth|sequence"),
    ("Sampler", r"sampler|halion|sfizz|kontakt|\bsfz\b"),
)]


def _text(*parts) -> str:
    """Words to match: lower case, channel layouts ("Mono/Stereo") and underscores dropped."""
    s = " ".join(str(p) for p in parts if p).lower().replace("_", " ")
    return re.sub(r"\b(mono/stereo|mono|stereo)\b", " ", s)


def _words(tags) -> list[str]:
    """Tags that describe the sound, not slugs naming where a patch came from ("halion-sonic-brass-multi")."""
    return [t for t in tags if str(t).count("-") < 2]


def _by_rules(rules, text: str) -> str | None:
    return next((t for t, rx in rules if rx.search(text)), None)


def plugin_maker(p: dict) -> str:
    """A registry entry's maker: what the scan stored, else known from its name, else the folder it is installed
    in below a VST3 folder, else the plugin's own name."""
    for k in ("vendor", "manufacturer", "maker"):
        if p.get(k):
            return str(p[k])
    hit = next((v for rx, v in _VENDORS if rx.search(p["name"].lower())), None)
    if hit:
        return hit
    path = Path(p.get("path", ""))
    if "waveshell" in path.name.lower():
        return "Waves"
    parts = path.parts[:-1]
    for i, x in enumerate(parts):
        if x.lower() == "vst3" and i + 1 < len(parts) and not parts[i + 1].lower().endswith(".vst3"):
            return parts[i + 1]
    return p["name"]


def _subcats(p: dict) -> list[str]:
    raw = p.get("subcategories") or p.get("categories") or p.get("category") or []
    if isinstance(raw, str):
        raw = raw.split("|")
    return [str(x).strip().lower() for x in raw if str(x).strip()]


def plugin_type(p: dict) -> str:
    """A registry entry's type: its VST3 sub-categories when the scan stored them, else keywords in its name."""
    table = _EFFECT_SUBCATS if p["kind"] == "effect" else _INSTRUMENT_SUBCATS
    for c in _subcats(p):
        if c in table:
            return table[c]
    rules = _EFFECT_RULES if p["kind"] == "effect" else _INSTRUMENT_RULES
    return _by_rules(rules, _text(p["name"])) or "Other"


def _items() -> list[dict]:
    """Everything, unsorted, without the new marks."""
    out: list[dict] = []
    reg = {p["name"]: p for p in plugins.load_registry()["plugins"]}
    for p in reg.values():
        it = {"id": f"vst3:{p['name']}", "kind": p["kind"], "source": "vst3", "name": p["name"],
              "by": Path(p["path"]).stem, "maker": plugin_maker(p), "type": plugin_type(p)}
        if not Path(p["path"]).exists():
            it["missing"] = True
        out.append(it)
    for m in plugins.list_patches():
        kind = m.get("kind", "instrument")
        host = reg.get(m["plugin"], {"name": m["plugin"], "kind": kind})
        rules = _EFFECT_RULES if kind == "effect" else _INSTRUMENT_RULES
        it = {"id": f"patch:{m['id']}", "kind": kind, "source": "patch", "name": m["name"],
              "by": m["plugin"], "tags": m.get("tags", []), "maker": plugin_maker(host),
              "type": _by_rules(rules, _text(m["name"], *_words(m.get("tags", [])), m.get("role")))
              or plugin_type(dict(host, kind=kind))}
        if m.get("role"):
            it["role"] = m["role"]
        if m.get("description"):
            it["about"] = m["description"]
        out.append(it)
    out.extend(_sfz_item(f) for f in _sfz_files())
    for k, (_t, desc, _p) in synths.SYNTHS.items():
        out.append({"id": f"synth:{k}", "kind": "instrument", "source": "synth", "name": k, "by": "built-in",
                    "about": desc, "maker": BUILTIN_MAKER,
                    "type": _by_rules(_INSTRUMENT_RULES[1:3], _text(k)) or "Synth"})
    for k, (desc, _p, _b) in synths.FX.items():
        out.append({"id": f"builtin:{k}", "kind": "effect", "source": "builtin", "name": k, "by": "built-in",
                    "about": desc, "maker": BUILTIN_MAKER, "type": _by_rules(_EFFECT_RULES, _text(k)) or "Other"})
    return out


def _marked(items: list[dict]) -> list[dict]:
    with _lock:
        seen = _load_seen()
        if "seen" not in seen:  # the first look: what is here already is not news
            seen = {"seen": sorted(i["id"] for i in items), "added": {}}
            _save_seen(seen)
        known = set(seen["seen"])
        added = seen.get("added", {})
    for it in items:
        if it["id"] not in known and it["source"] != "synth" and it["source"] != "builtin":
            it["new"] = True
            if it["id"] in added:
                it["added"] = added[it["id"]]
    return items


def catalog(query: str | None = None, kind: str | None = None) -> dict:
    """Every instrument and effect: VST3 plugins, saved patches, SFZ instruments, built-in synths and effects.
    Each item: id (what track_add / track_update / fx_add take), kind (instrument | effect), source (vst3 | patch |
    sfz | synth | builtin), name, by (the plugin file, the plugin a patch is of, or the SFZ library), maker (the
    company: a patch's is its plugin's, built-ins are TuneTrack's), type (one of EFFECT_TYPES or INSTRUMENT_TYPES),
    and when they apply: tags, role, about, new (arrived since the user last looked), missing (its file is gone)."""
    items = _marked(_items())
    words = [w for w in (query or "").lower().split() if w]
    if kind:
        items = [i for i in items if i["kind"] == kind]
    if words:
        def hay(i):
            return " ".join([i["name"], i["by"], i["maker"], *i.get("tags", [])]).lower()
        items = [i for i in items if all(w in hay(i) for w in words)]
    order = {"patch": 0, "vst3": 1, "sfz": 2, "synth": 3, "builtin": 4}
    items.sort(key=lambda i: (not i.get("new"), i["kind"], order[i["source"]], i["by"].lower(), i["name"].lower()))
    return {"items": items, "new": sum(1 for i in items if i.get("new")), "watch": gear_state()}


def gear_seen(ids: list[str] | None = None) -> dict:
    """Mark gear as seen (every item when ids is not given), so it is no longer new."""
    items = _items()
    with _lock:
        seen = _load_seen()
        if "seen" not in seen:  # the first look: what is here already is not news
            seen = {"seen": sorted(i["id"] for i in items), "added": {}}
        known = set(seen["seen"])
        known |= set(ids) if ids else {i["id"] for i in items}
        added = {k: v for k, v in seen.get("added", {}).items() if k not in known}
        _save_seen({"seen": sorted(known), "added": added})
        _state["new"] = sum(1 for i in items if i["id"] not in known and i["source"] in ("vst3", "patch", "sfz"))
    return gear_state()


# ---- the watched folders -----------------------------------------------------------------------------------------

def gear_state() -> dict:
    """What the watcher is doing: folders, plugins being scanned, the last finds, how many items are new."""
    cfg = config.load_config()
    reg = plugins.load_registry()
    with _lock:
        out = {k: (list(v) if isinstance(v, list) else v) for k, v in _state.items()}
    out["folders"] = {"vst3": list(cfg.get("vst3_dirs", [])), "samples": _sample_dirs()}
    out["failed"] = [{"name": Path(f["path"]).name, "path": f["path"], "error": f.get("error", "")[:200]}
                     for f in reg.get("failed", [])]
    out["waiting"] = sorted(Path(p).name for p in _pending)
    return out


def gear_folders(add: str | None = None, remove: str | None = None, kind: str = "vst3") -> dict:
    """Add or remove a folder TuneTrack watches. kind: vst3 (plugins) | samples (SFZ libraries). A folder added is
    looked at straight away; new plugins in it are scanned."""
    if kind not in ("vst3", "samples"):
        raise ValueError("kind: vst3 or samples")
    key = "vst3_dirs" if kind == "vst3" else "sample_dirs"
    cfg = config.load_config()
    dirs = list(cfg.get(key, []))
    if add:
        p = Path(str(add).strip().strip('"')).expanduser()
        if not p.is_dir():
            raise ValueError(f"No such folder: {p}")
        if not any(Path(d) == p for d in dirs):
            dirs.append(str(p))
    if remove:
        dirs = [d for d in dirs if Path(d) != Path(str(remove).strip().strip('"')).expanduser()]
    if dirs != cfg.get(key, []):
        cfg[key] = dirs
        config.save_config(cfg)
        _wake.set()
    return gear_state()


def gear_rescan(retry_failed: bool = False) -> dict:
    """Look at the watched folders now and scan what is new; retry_failed: try the plugins that failed again."""
    with _lock:
        _rescan["retry_failed"] = _rescan.get("retry_failed", False) or bool(retry_failed)
    _wake.set()
    start_watch()
    return gear_state()


def _stamp(f: Path) -> tuple:
    """How a plugin's files look now: their count, size and newest change (a bundle is a folder)."""
    try:
        files = [x for x in f.rglob("*") if x.is_file()] if f.is_dir() else [f]
        st = [x.stat() for x in files]
        return len(st), sum(s.st_size for s in st), max((s.st_mtime for s in st), default=0.0)
    except OSError:
        return (-1,)


def poll(scan_now: bool = False) -> dict:
    """One look at the watched folders: scan the plugins that arrived and have settled, note new SFZ files and
    patches. scan_now: scan new plugins without waiting for them to settle (tests, a rescan the user asked for)."""
    cfg = config.load_config()
    reg = plugins.load_registry()
    with _lock:
        retry = _rescan.pop("retry_failed", False)
    known = {p["path"] for p in reg["plugins"]} | (set() if retry else {f["path"] for f in reg.get("failed", [])})
    skip = [s.lower() for s in cfg.get("skip_plugins", [])]
    fresh = [f for f in plugins.find_vst3_files(cfg["vst3_dirs"])
             if str(f) not in known and not any(s in f.name.lower() for s in skip)]
    ready = []
    with _lock:
        for f in fresh:
            st = _stamp(f)
            was = _pending.get(str(f))
            n = was[1] + 1 if was and was[0] == st else 1
            _pending[str(f)] = [st, n]
            if scan_now or n >= SETTLE_POLLS:
                ready.append(f)
        for k in [k for k in _pending if Path(k) not in fresh]:
            del _pending[k]
    found: list[str] = []
    if ready or retry:
        before = {p["name"] for p in reg["plugins"]}
        with _lock:
            _state["scanning"] = sorted(f.name for f in ready) or ["failed plugins"]
        try:
            plugins.scan(retry_failed=retry)
        finally:
            with _lock:
                _state["scanning"] = []
                _state["last_scan"] = time.time()
                for f in ready:
                    _pending.pop(str(f), None)
        found = sorted({p["name"] for p in plugins.load_registry()["plugins"]} - before)
    items = _marked(_items())
    now = time.time()
    with _lock:
        seen = _load_seen()
        added = seen.setdefault("added", {})
        was = dict(added)
        known_ids = set(seen.get("seen", []))
        for it in items:
            if it.get("new") and it["id"] not in added:
                added[it["id"]] = now
        for k in [k for k in added if k in known_ids]:
            del added[k]
        if added != was:  # the watcher looks every 15 s: write only when something arrived or was seen
            _save_seen(seen)
        _state.update(last_poll=now, new=sum(1 for i in items if i.get("new")), error=None)
        if found:
            _state["found"] = (found + [x for x in _state["found"] if x not in found])[:12]
    return {"found": found, **gear_state()}


def _watch() -> None:
    while True:
        try:
            poll()
        except Exception as e:  # noqa: BLE001 - the watcher must outlive one bad look (a drive gone, a bad file)
            with _lock:
                _state["error"] = str(e)[:300]
        _wake.wait(POLL_S)
        _wake.clear()


def start_watch() -> None:
    """Start the watcher thread (the editor server does, once)."""
    with _lock:
        if _state["watching"]:
            return
        _state["watching"] = True
    threading.Thread(target=_watch, daemon=True, name="gear-watch").start()


# ---- hearing it --------------------------------------------------------------------------------------------------

# Written in C; transposed to the open project's key. Major and minor versions of each.
PHRASES = {
    "chords": ("(C3 G3 E4):h!70 (A2 E3 C4):h!78 | (F2 C3 A3):h!85 (G2 D3 B3):h!100 | (C3 G3 E4 C5):w!90 | r:w",
               "(C3 G3 Eb4):h!70 (Ab2 Eb3 C4):h!78 | (F2 C3 Ab3):h!85 (G2 D3 B3):h!100 | (C3 G3 Eb4 C5):w!90 | r:w"),
    "melody": ("C4:e!64 D4 E4 F4 G4:q!80 A4:e G4 | E4:q!90 C5:q. B4:e A4:q | G4:e!80 F4 E4 D4 E4:q G4 | C4:h!96 r:h",
               "C4:e!64 D4 Eb4 F4 G4:q!80 Ab4:e G4 | Eb4:q!90 C5:q. B4:e Ab4:q | G4:e!80 F4 Eb4 D4 Eb4:q G4 | C4:h!96 r:h"),
    "bass": ("C2:q!100 C2:e C3 G2:q E2 | A1:q A1:e A2 E2:q C2 | F1:q F1:e F2 G1:q G2 | C2:h!110 G1:h",
             "C2:q!100 C2:e C3 G2:q Eb2 | Ab1:q Ab1:e Ab2 Eb2:q C2 | F1:q F1:e F2 G1:q G2 | C2:h!110 G1:h"),
    "drums": ("C2:q!110 F#2:e!90 F#2:e!70 D2:q!105 F#2:e!90 F#2:e!70 | C2:e!110 C2:e!95 F#2:e!90 A#2:e!80 "
              "D2:q!105 F#2:e!90 C#3:e!100",) * 2,
}
FX_SOURCE = ("synth:epiano", "chords"), ("synth:drums", "drums")  # what an effect is heard on


def _phrase_for(item_id: str) -> str:
    """The phrase that suits an instrument: drums for kits, bass for basses, chords for pads, else a melody."""
    words = item_id.lower()
    if item_id.startswith("patch:"):
        try:
            m = plugins.get_patch(item_id[6:])
        except Exception:  # noqa: BLE001 - a patch file gone or broken: guess from the id
            m = {}
        if m.get("drum_map") or "drums" in m.get("tags", []) or m.get("role") in ("drums", "percussion"):
            return "drums"
        words += " " + " ".join(m.get("tags", [])) + " " + str(m.get("role") or "")
        lo_hi = m.get("range")
        if lo_hi and lo_hi[1] <= 60:
            return "bass"
    if any(w in words for w in ("drum", "kit", "perc", "groove agent")):
        return "drums"
    if "bass" in words:
        return "bass"
    if any(w in words for w in ("pad", "string", "choir", "organ", "piano", "epiano", "halion", "ensemble")):
        return "chords"
    return "melody"


def _shift(key: str) -> tuple[int, bool]:
    """Semitones from C to the key's tonic (-5..+6) and whether the key is minor-ish."""
    try:
        k = theory.Key(key)
    except ValueError:
        return 0, False
    s = k.tonic if k.tonic <= 6 else k.tonic - 12
    return s, k.mode in ("minor", "aeolian", "dorian", "phrygian", "locrian", "harmonic minor", "melodic minor")


def _clip(text: str, shift: int, drums: bool) -> tuple[Clip, float]:
    notes, length, _ = notation.parse(text)
    if not drums and shift:
        for n in notes:
            n.pitch += shift
    length = max(4.0, -(-length // 4) * 4)
    return Clip(id="c1", start=0, length=length, notes=notes), length


def audition_project(item_id: str, phrase: str = "auto", key: str = "C major", tempo: float = 100.0,
                     time_sig: list[int] | None = None) -> Project:
    """The little session a piece of gear is heard in: the instrument playing `phrase`, or an effect on a bus that
    a built-in electric piano and drum kit play through. Saved in `<library>/audition-live/`."""
    path = config.HOME / AUDITION_DIR
    shift, minor = _shift(key)
    p = Project(name="audition", tempo=float(tempo), key=key, path=path)
    if time_sig:
        p.time_sig = list(time_sig)
    p.master["target_lufs"] = -18.0
    p.tail = 2.0
    from .api import parse_device
    if item_id.startswith(("builtin:", "eq:")) or (item_id.startswith(("vst3:", "patch:")) and
                                                   _kind_of(item_id) == "effect"):
        bus = Track(id="t3", name="effect", kind="bus", fx=[parse_device(item_id, instrument=False)])
        length = 4.0
        for i, (inst, ph) in enumerate(FX_SOURCE, 1):
            c, n = _clip(PHRASES[ph][minor], shift, ph == "drums")
            length = max(length, n)
            p.tracks.append(Track(id=f"t{i}", name=ph, instrument=parse_device(inst), clips=[c], output="t3",
                                  volume_db=-3.0 if ph == "drums" else 0.0))
        p.tracks.append(bus)
    else:
        ph = _phrase_for(item_id) if phrase in ("auto", "", None) else phrase
        t = Track(id="t1", name="gear", instrument=parse_device(item_id))
        if ph == "keys":  # nothing written: the keyboard plays it
            length = 4.0 * (p.time_sig[0] / 4 if p.time_sig else 1)
        else:
            c, length = _clip(PHRASES[ph][minor] if ph in PHRASES else ph, shift, ph == "drums")
            t.clips.append(c)
        p.tracks.append(t)
    p.loop = {"start": 0.0, "end": float(length)}
    p.length = float(length)
    p.next_id = len(p.tracks) + 1
    p.save(snapshot=False)
    return p


def _kind_of(item_id: str) -> str:
    if item_id.startswith("patch:"):
        try:
            return plugins.get_patch(item_id[6:]).get("kind", "instrument")
        except Exception:  # noqa: BLE001
            return "instrument"
    if item_id.startswith("vst3:"):
        name = item_id[5:]
        for p in plugins.load_registry()["plugins"]:
            if p["name"] == name:
                return p["kind"]
        return "instrument"
    return "effect" if item_id.startswith(("builtin:", "eq:")) else "instrument"


def is_audition(path) -> bool:
    return path is not None and Path(path) == config.HOME / AUDITION_DIR


def audition(item: str, phrase: str = "auto", live: bool = False, key: str = "C major", tempo: float = 100.0,
             time_sig: list[int] | None = None) -> dict:
    """Hear a piece of gear. live: loop it on the live engine (an instrument is armed for the keyboards), else a
    short render whose file comes back."""
    p = audition_project(item, phrase, key, tempo, time_sig)
    if live:
        from . import livehost
        livehost.play(p.path, 0.0, loop=True)
        if p.tracks[0].id == "t1" and p.tracks[0].kind == "midi" and len(p.tracks) == 1:
            livehost.arm(p.path, "t1", arm=True, monitor=True)
        return {"live": True, "item": item, "beats": p.loop["end"]}
    import numpy as np
    import soundfile as sf
    from . import engine
    r = engine.Renderer(p)
    rep = r.run()
    mix = r.master
    dest = config.HOME / "auditions" / f"gear-{plugins.slug(item)[:60]}.wav"
    dest.parent.mkdir(parents=True, exist_ok=True)
    peak = float(np.abs(mix).max(initial=0))
    sf.write(str(dest), (mix / max(peak, 1e-9) * 0.89 if peak > 0.89 else mix).T, p.sample_rate, subtype="PCM_24")
    return {"live": False, "item": item, "file": str(dest), "silent": peak < 1e-4, "warnings": rep["warnings"]}


def audition_stop(back: str | None = None) -> dict:
    """Stop a live audition; back: the project folder to point the engine at again (the Desk's)."""
    from . import livehost
    livehost.stop()
    if back and livehost.state().get("on"):
        livehost._ensure(Path(back))
    return livehost.state()
