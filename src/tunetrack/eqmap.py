"""builtin:eq's settings on plugin EQs: SSL Native Channel Strip 2 ("ssl") and TDR Nova ("nova").

A project's eq_engine decides what its `builtin:eq` inserts run on; "eq:ssl", "eq:nova" or "eq:builtin" pick one
for a single insert. Every engine takes the same parameters (hpf, low shelf, one mid bell, high shelf, lpf: see
synths.FX["eq"]), so a mix moves between them without retuning. The worker sets plugin parameters by display text,
so values are written in each plugin's own units (SSL shows its LMF/HMF/HF/LPF frequencies in kHz with no unit).
Settings a plugin can't reach are clamped, with a note. When the plugin isn't installed the built-in EQ runs.
"""
from __future__ import annotations

from . import plugins
from .synths import FX

ENGINES = {"builtin": None, "ssl": "SSL Native Channel Strip 2", "nova": "TDR Nova"}
_ALIASES = {"built-in": "builtin", "faust": "builtin", "default": "builtin", "channel strip": "ssl",
            "ssl channel strip": "ssl", "ssl native channel strip 2": "ssl", "tdr": "nova", "tdr nova": "nova"}
PARAMS: dict = FX["eq"][1]

HPF_OFF = 20.0  # hpf at or below this = no high-pass (the built-in default)
LPF_OFF = 20000.0  # lpf at or above this = no low-pass
FLAT = 0.05  # dB: a band with less gain than this is left alone

# Calibration, measured with noise through each plugin at 48 kHz against builtin:eq (whose shelf frequency is where
# the shelf has half its gain, and whose mid_q 1 bell is about an octave wide at half its gain).
# TDR Nova: a bell is ~1.4x wider than the built-in one at the same Q; Q 0.71 shelves match the built-in slope.
NOVA_Q = 1.4
NOVA_SHELF_Q = "0.71"
# SSL Channel Strip 2 (G type) is a console EQ: proportional-Q bells that are 2.5-5 octaves wide (Q 2.5 is its
# narrowest, ~3.5 octaves at -6 dB), broad shelves, and a gain dial that reads above what the bells do at moderate
# settings (dial -6 dB = -3.8 dB). Bell gains go through the measured dial law below; Q scales so mid_q 1 or more
# is SSL's narrowest; shelf frequencies move so the half-gain point lands where the built-in shelf's does.
SSL_Q = 2.5
SSL_LF_HALF = 2.15  # the LF shelf reaches half its gain at ~2.15x its dial frequency
SSL_HF_HALF = 0.28  # the HF shelf reaches half its gain at ~0.28x its dial frequency
_SSL_DIAL = [-20, -16, -12, -9, -6, -4, -2, 0, 2, 4, 6, 9, 12, 16, 20]
_SSL_BELL = {  # measured peak gain (dB) for each dial setting
    "LMF": [-20.7, -17.6, -10.3, -6.9, -3.8, -1.9, -1.0, 0, 0.8, 1.5, 3.4, 6.3, 9.3, 16.3, 20.6],
    "HMF": [-20.6, -16.2, -9.6, -6.4, -3.5, -1.8, -0.9, 0, 0.8, 1.6, 3.6, 6.7, 10.1, 16.7, 20.4],
}


def engine_name(value) -> str:
    v = str(value or "builtin").strip().lower()
    v = _ALIASES.get(v, v)
    if v not in ENGINES:
        raise ValueError(f"Unknown EQ engine {value!r}; use builtin (TuneTrack's own EQ), ssl (SSL Native Channel "
                         f"Strip 2) or nova (TDR Nova)")
    return v


def is_eq(dev) -> bool:
    return dev.type == "builtin" and dev.name == "eq"


def engine_for(dev, project) -> str:
    """The engine an eq insert runs on: its own choice, else the project's."""
    return dev.engine or getattr(project, "eq_engine", None) or "builtin"


def plugin_entry(engine: str) -> dict | None:
    """The registry entry of an engine's plugin; None for the built-in EQ or when the plugin isn't installed."""
    name = ENGINES.get(engine)
    if not name:
        return None
    try:
        return plugins.resolve(name)
    except KeyError:
        return None


def settings(params: dict | None) -> dict:
    """The full builtin:eq settings: defaults overlaid with `params`. Unknown names raise."""
    params = params or {}
    bad = [k for k in params if k not in PARAMS]
    if bad:
        raise KeyError(f"Effect 'eq' has no parameter {bad[0]!r}. Parameters: {', '.join(PARAMS)}")
    return {**PARAMS, **{k: float(v) for k, v in params.items()}}


def translate(engine: str, params: dict | None) -> tuple[dict, list[str]]:
    """builtin:eq params -> (the engine's plugin params as display text, notes on what had to be clamped)."""
    s = settings(params)
    notes: list[str] = []
    if engine == "ssl":
        return _ssl(s, notes), notes
    if engine == "nova":
        return _nova(s, notes), notes
    return dict(params or {}), notes


def _fit(notes: list, what: str, v: float, lo: float, hi: float, unit: str = " Hz", why: str = "") -> float:
    if lo - 1e-9 <= v <= hi + 1e-9:
        return v
    c = min(max(v, lo), hi)
    notes.append(f"{what} {v:g}{unit} is outside {lo:g}-{hi:g}{unit}{why}: using {c:g}{unit}")
    return c


def _interp(x: float, xs: list, ys: list) -> float:
    """Piecewise-linear y(x); xs ascending, x within [xs[0], xs[-1]]."""
    for i in range(1, len(xs)):
        if x <= xs[i]:
            t = (x - xs[i - 1]) / (xs[i] - xs[i - 1])
            return ys[i - 1] + t * (ys[i] - ys[i - 1])
    return ys[-1]


def _ssl_bell_dial(notes: list, gain: float, law: str) -> str:
    """The SSL dial setting whose bell has `gain` dB at its peak (through the measured dial law)."""
    measured = _SSL_BELL[law]
    g = _fit(notes, "mid_gain", gain, measured[0], measured[-1], " dB")
    return f"{_interp(g, measured, _SSL_DIAL):.1f}"


def _ssl(s: dict, notes: list) -> dict:
    out = {"Dynamics In": "Out"}  # the strip's compressor and gate stay out of an EQ
    if s["hpf"] > HPF_OFF:  # SSL's high-pass is 18 dB/oct (the built-in's is 12)
        out["High Pass Filter"] = f"{_fit(notes, 'hpf', s['hpf'], 20, 500):.1f}"
    if s["lpf"] < LPF_OFF:
        out["Low Pass Filter"] = f"{_fit(notes, 'lpf', s['lpf'], 3000, 20000) / 1000:.2f}"
    low, high = abs(s["low_gain"]) >= FLAT, abs(s["high_gain"]) >= FLAT
    if low:
        f = _fit(notes, "low_freq", s["low_freq"], 40 * SSL_LF_HALF, 600 * SSL_LF_HALF, why=" (SSL's LF shelf)")
        out["LF Frequency"] = f"{f / SSL_LF_HALF:.1f}"
        out["LF Gain"] = f"{_fit(notes, 'low_gain', s['low_gain'], -16.5, 16.5, ' dB'):.1f}"
    if high:
        f = _fit(notes, "high_freq", s["high_freq"], 1500 * SSL_HF_HALF, 22000 * SSL_HF_HALF,
                 why=" (SSL's HF shelf; nova or builtin reach higher)")
        out["HF Freq"] = f"{f / SSL_HF_HALF / 1000:.2f}"
        out["HF Gain"] = f"{_fit(notes, 'high_gain', s['high_gain'], -20, 20, ' dB'):.1f}"
    if abs(s["mid_gain"]) >= FLAT:
        f, g = s["mid_freq"], s["mid_gain"]
        if f < 200 and not low:  # below the LMF band: the free LF band as a bell (no Q control there)
            out.update({"LF Type": "Bell", "LF Frequency": f"{_fit(notes, 'mid_freq', f, 40, 600):.1f}",
                        "LF Gain": _ssl_bell_dial(notes, _fit(notes, "mid_gain", g, -16.5, 16.5, " dB"), "LMF")})
        elif f > 7000 and not high:  # above the HMF band: the free HF band as a bell
            out.update({"HF Type": "Bell", "HF Freq": f"{_fit(notes, 'mid_freq', f, 1500, 22000) / 1000:.2f}",
                        "HF Gain": _ssl_bell_dial(notes, g, "HMF")})
        else:
            band, freq_name, lo, hi = ("LMF", "LMF Frequency", 200, 2000) if f <= 2000 else ("HMF", "HMF Freq", 600, 7000)
            out[freq_name] = f"{_fit(notes, 'mid_freq', f, lo, hi) / 1000:.2f}"
            out[f"{band} Gain"] = _ssl_bell_dial(notes, g, band)
            out[f"{band} Q"] = f"{min(2.5, max(0.5, s['mid_q'] * SSL_Q)):.2f}"  # it can't get narrower: no note
    return out


def _nova(s: dict, notes: list) -> dict:
    out = {"EQ Auto Gain": "Off"}  # on by default: it would make up for every cut and skew the level readings
    if s["hpf"] > HPF_OFF:  # 12 dB/oct, like the built-in filters
        out.update({"HP Active": "On", "HP Type": "12dB/oct",
                    "HP Frequency": f"{_fit(notes, 'hpf', s['hpf'], 10, 40000):.0f}"})
    if s["lpf"] < LPF_OFF:
        out.update({"LP Active": "On", "LP Type": "12dB/oct",
                    "LP Frequency": f"{_fit(notes, 'lpf', s['lpf'], 10, 40000):.0f}"})
    if abs(s["low_gain"]) >= FLAT:
        out.update({"Band 1 Type": "Low S", "Band 1 Q": NOVA_SHELF_Q,
                    "Band 1 Frequency": f"{_fit(notes, 'low_freq', s['low_freq'], 10, 40000):.0f}",
                    "Band 1 Gain": f"{_fit(notes, 'low_gain', s['low_gain'], -18, 18, ' dB'):.1f}"})
    if abs(s["mid_gain"]) >= FLAT:  # band 2 is a bell by default
        out.update({"Band 2 Frequency": f"{_fit(notes, 'mid_freq', s['mid_freq'], 10, 40000):.0f}",
                    "Band 2 Gain": f"{_fit(notes, 'mid_gain', s['mid_gain'], -18, 18, ' dB'):.1f}",
                    "Band 2 Q": f"{_fit(notes, 'mid_q', s['mid_q'], 0.1 / NOVA_Q, 6 / NOVA_Q, '') * NOVA_Q:.2f}"})
    if abs(s["high_gain"]) >= FLAT:  # band 4 is a high shelf by default
        out.update({"Band 4 Q": NOVA_SHELF_Q,
                    "Band 4 Frequency": f"{_fit(notes, 'high_freq', s['high_freq'], 10, 40000):.0f}",
                    "Band 4 Gain": f"{_fit(notes, 'high_gain', s['high_gain'], -18, 18, ' dB'):.1f}"})
    return out


def warnings(project) -> list[str]:
    """Render warnings for eq inserts: engines whose plugin isn't installed, and settings a plugin had to clamp."""
    out: list[str] = []
    missing: dict[str, int] = {}
    chains = [(f"track '{t.name}'", t.fx) for t in project.tracks] + [("master", project.master.get("fx", []))]
    for owner, chain in chains:
        for i, d in enumerate(chain):
            if not hasattr(d, "type") or d.bypass or not is_eq(d):
                continue
            engine = engine_for(d, project)
            if engine == "builtin":
                continue
            if plugin_entry(engine) is None:
                missing[engine] = missing.get(engine, 0) + 1
                continue
            try:
                notes = translate(engine, d.params)[1]
            except KeyError as e:
                notes = [str(e).strip("'\"")]
            out += [f"{owner} fx {i} ({engine} EQ): {n}" for n in notes]
    for engine, n in missing.items():
        out.insert(0, f"{ENGINES[engine]} isn't in the plugin registry (install it or run plugins_scan), so {n} EQ "
                      f"insert{'s' if n > 1 else ''} set to '{engine}' use{'' if n > 1 else 's'} the built-in EQ")
    return out
