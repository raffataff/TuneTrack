"""Built-in instruments and effects, written in Faust and compiled by DawDreamer at render time.

Each synth preset = a DSP template + parameter defaults; any parameter can be overridden per track.
"""
from __future__ import annotations

_HEAD = """import("stdfaust.lib");
freq = hslider("freq", 440, 20, 20000, 0.01);
gain = hslider("gain", 0.5, 0, 1, 0.001);
gate = button("gate");
vamp = (1 - velsens) + velsens * gain;
"""

TEMPLATES = {
    # Detuned saw stack + sub, resonant low-pass with its own envelope, vibrato. Pads, leads, strings, brass, plucks.
    "saws": """
vib = ba.semi2ratio(vibdepth * os.osc(vibrate));
spr = detune * 0.012;
o(r) = os.sawtooth(freq * vib * r);
sL = (o(1) + o(1 + spr) * 0.8 + o(1 - spr * 0.6) * 0.8 + o(1 + spr * 0.3) * 0.7) / 3.3 + os.square(freq * vib * 0.5) * sub;
sR = (o(1) + o(1 - spr) * 0.8 + o(1 + spr * 0.6) * 0.8 + o(1 - spr * 0.3) * 0.7) / 3.3 + os.square(freq * vib * 0.5) * sub;
fenv = en.adsr(fattack, fdecay, fsustain, release, gate);
aenv = en.adsr(attack, decay, sustain, release, gate);
fc = min(18000, cutoff * (1 + envamt * fenv) * pow(freq / 261.6, keytrack));
filt = fi.resonlp(fc, q, 1);
amp = aenv * vamp * level;
process = (sL : filt : *(amp)), (sR : filt : *(amp));
""",
    # Mono-style bass: saw + sub square + sine, resonant filter, drive.
    "bass": """
osc = os.sawtooth(freq) * saw + os.square(freq * 0.5) * sub + os.osc(freq) * sine;
fenv = en.adsr(0.001, fdecay, fsustain, release, gate);
aenv = en.adsr(attack, decay, sustain, release, gate);
fc = min(16000, cutoff * (1 + envamt * fenv) * pow(freq / 65.4, keytrack));
process = osc : fi.resonlp(fc, q, 1) : *(drive) : ma.tanh : *(aenv * vamp * level) <: _, _;
""",
    "sub": """
aenv = en.adsr(attack, decay, sustain, release, gate);
process = (os.osc(freq) + os.osc(freq * 2) * h2 + os.osc(freq * 3) * h3) : *(drive) : ma.tanh : *(aenv * vamp * level) <: _, _;
""",
    # Karplus-Strong plucked string.
    "ks": """
trig = gate > gate';
burst = trig : + ~ *(exp(-1.0 / (excite * ma.SR)));
dl = ma.SR / freq - 1.5;
lp2(x) = (x + x') * 0.5 * damp;
ks = (+ : de.fdelay(8192, dl)) ~ lp2;
tone = no.noise * min(1, burst) : fi.lowpass(1, bright * (0.4 + 0.6 * gain)) : ks;
amp = en.adsr(0.0005, 0.01, 1, release, gate) * vamp * level;
process = tone : fi.dcblocker : *(amp) <: _, _;
""",
    # Two-operator FM: e-pianos, bells, mallets, FM basses.
    "fm": """
menv = en.adsr(mattack, mdecay, msustain, release, gate);
aenv = en.adsr(attack, decay, sustain, release, gate);
mod = os.osc(freq * ratio) * index * menv * freq * (0.4 + 0.6 * gain);
car(d) = os.osc(freq * d + mod);
amp = aenv * vamp * level;
process = (car(1 + spread * 0.001) * amp), (car(1 - spread * 0.001) * amp);
""",
    # Chiptune: 0 = pulse (duty), 1 = 4-bit triangle, 2 = noise (pitch sets the noise clock).
    "chip": """
vib = ba.semi2ratio(vibdepth * os.osc(vibrate));
ph = os.lf_sawpos(freq * vib);
pulse = (ph < duty) * 2 - 1;
tri0 = abs(ph * 2 - 1) * 2 - 1;
tri = floor((tri0 + 1) * 7.5) / 7.5 - 1;
sh(t, x) = (*(1 - t) : +(x * t)) ~ _;
clk = os.lf_sawpos(freq * 8) < os.lf_sawpos(freq * 8)';
noiz = sh(clk, no.noise);
sig = pulse, tri, noiz : ba.selectn(3, int(shape));
aenv = en.adsr(attack, decay, sustain, release, gate);
process = sig * aenv * vamp * level * 0.5 <: _, _;
""",
    # Drawbar organ with rotary-style tremolo.
    "organ": """
h(r, l) = os.osc(freq * r) * l;
sig = (h(0.5, d16) + h(1, d8) + h(1.5, d5) + h(2, d4) + h(3, d3) + h(4, d2)) / 3;
aenv = en.adsr(0.004, 0.05, 1, release, gate);
trig = gate > gate';
click = no.noise * (trig : + ~ *(exp(-1.0 / (0.004 * ma.SR))) : min(1)) * clickamt : fi.bandpass(1, 1000, 4000);
amp = aenv * vamp * level;
process = (sig + click) * amp <: *(1 + rot * os.osc(rotrate)), *(1 + rot * os.osc(rotrate * 1.07 + 0.3));
""",
    # General MIDI mapped drum machine (kick 36, snare 38, clap 39, hats 42/44/46, toms 41-50, crash 49/57, ride 51...).
    "drums": """
note = int(ba.hz2midikey(freq) + 0.5);
trig = gate > gate';
dec(t) = trig : + ~ *(exp(-1.0 / (t * ma.SR))) : min(1);
isn(n) = note == n;
metal = (os.square(205.3) + os.square(304.4) + os.square(369.6) + os.square(522.7) + os.square(540) + os.square(800)) / 6;
hisig = metal * 0.6 + no.noise * 0.4 : fi.highpass(2, 7000);
kick = os.osc(kfreq + kpitch * dec(0.04)) * dec(kdecay) + (no.noise : fi.highpass(1, 2500)) * dec(0.003) * kclick;
snare = (os.osc(185) * 0.6 + os.osc(330) * 0.3) * dec(0.07) + (no.noise : fi.bandpass(1, 1200, 9000)) * dec(sdecay) * snoise;
burst(d) = (trig : de.delay(4096, int(d * ma.SR))) : + ~ *(exp(-1.0 / (0.007 * ma.SR))) : min(1);
clap = (no.noise : fi.bandpass(1, 900, 2600)) * (burst(0) + burst(0.011) + burst(0.022) + dec(0.16) * 0.6);
tomf = 62 + (note - 41) * 13;
tom = os.osc(tomf * (1 + 0.6 * dec(0.05))) * dec(0.38);
rim = os.osc(1700) * dec(0.018) + (no.noise : fi.highpass(1, 3000)) * dec(0.008);
cowbell = (os.square(540) + os.square(800)) * 0.5 : fi.bandpass(1, 500, 2500) : *(dec(0.25));
shaker = no.noise : fi.highpass(2, 6000) : *(dec(0.05));
mono = kick * (isn(35) + isn(36)) + snare * (isn(38) + isn(40)) + clap * isn(39) + rim * isn(37)
     + tom * (isn(41) + isn(43) + isn(45) + isn(47) + isn(48) + isn(50)) + cowbell * isn(56) * 0.5;
hats = hisig * (dec(hdecay) * isn(42) + dec(0.03) * isn(44) + dec(hdecay * 8) * isn(46)) * 0.7
     + (hisig + no.noise * 0.3 : fi.highpass(1, 4000)) * (dec(1.4) * (isn(49) + isn(57) + isn(52)) + dec(0.6) * isn(55)) * 0.45
     + metal * 0.3 * dec(0.9) * (isn(51) + isn(53)) + shaker * (isn(70) + isn(69) + isn(54)) * 0.5;
amp = vamp * level;
process = (mono + hats * 0.8) * amp, (mono + hats) * amp;
""",
}

_ENV = {"attack": 0.005, "decay": 0.3, "sustain": 0.8, "release": 0.3}

SYNTHS = {
    "supersaw": ("saws", "Wide detuned saw lead/pad for trance, synthwave, EDM", dict(
        _ENV, detune=0.35, sub=0.15, cutoff=2200, q=0.9, envamt=1.5, keytrack=0.3, fattack=0.005, fdecay=0.6,
        fsustain=0.4, release=0.35, vibdepth=0, vibrate=5, level=0.5, velsens=0.5)),
    "lead": ("saws", "Bright mono-ish synth lead with delayed vibrato feel", dict(
        _ENV, detune=0.08, sub=0.0, cutoff=3200, q=1.2, envamt=1.0, keytrack=0.5, fattack=0.002, fdecay=0.3,
        fsustain=0.6, release=0.15, vibdepth=0.12, vibrate=5.5, level=0.5, velsens=0.6)),
    "pad": ("saws", "Warm slow pad, soft filter", dict(
        attack=0.9, decay=1.5, sustain=0.85, release=2.2, detune=0.25, sub=0.1, cutoff=900, q=0.7, envamt=0.8,
        keytrack=0.4, fattack=1.2, fdecay=2.0, fsustain=0.5, vibdepth=0, vibrate=5, level=0.45, velsens=0.3)),
    "dark_pad": ("saws", "Dark, filtered ambient pad for tension/horror/space", dict(
        attack=1.8, decay=2.0, sustain=0.9, release=3.0, detune=0.4, sub=0.25, cutoff=450, q=1.4, envamt=0.6,
        keytrack=0.2, fattack=2.5, fdecay=3.0, fsustain=0.5, vibdepth=0, vibrate=5, level=0.45, velsens=0.2)),
    "string_machine": ("saws", "Vintage string ensemble synth", dict(
        attack=0.35, decay=1.0, sustain=0.9, release=0.9, detune=0.3, sub=0.0, cutoff=2600, q=0.6, envamt=0.2,
        keytrack=0.6, fattack=0.3, fdecay=1.0, fsustain=0.8, vibdepth=0.08, vibrate=5.2, level=0.45, velsens=0.4)),
    "brass_synth": ("saws", "Synth brass stabs and swells", dict(
        attack=0.04, decay=0.4, sustain=0.75, release=0.25, detune=0.12, sub=0.1, cutoff=700, q=0.8, envamt=4.0,
        keytrack=0.5, fattack=0.06, fdecay=0.5, fsustain=0.5, vibdepth=0.03, vibrate=5, level=0.5, velsens=0.7)),
    "saw_pluck": ("saws", "Short filtered pluck for arps (house, synthwave)", dict(
        attack=0.001, decay=0.35, sustain=0.0, release=0.25, detune=0.15, sub=0.0, cutoff=350, q=1.3, envamt=14,
        keytrack=0.6, fattack=0.001, fdecay=0.18, fsustain=0.0, vibdepth=0, vibrate=5, level=0.55, velsens=0.6)),
    "analog_bass": ("bass", "Resonant analog bass (saw+sub)", dict(
        attack=0.002, decay=0.4, sustain=0.7, release=0.08, saw=0.8, sub=0.5, sine=0.0, cutoff=260, q=1.6,
        envamt=5.0, keytrack=0.5, fdecay=0.25, fsustain=0.2, drive=1.6, level=0.6, velsens=0.5)),
    "reese_bass": ("saws", "Detuned dark reese bass for DnB/dubstep/industrial", dict(
        attack=0.005, decay=0.5, sustain=0.9, release=0.15, detune=0.6, sub=0.4, cutoff=500, q=0.9, envamt=0.5,
        keytrack=0.3, fattack=0.01, fdecay=0.4, fsustain=0.6, vibdepth=0, vibrate=5, level=0.55, velsens=0.3)),
    "sub_bass": ("sub", "Clean sine sub bass", dict(attack=0.004, decay=0.3, sustain=0.9, release=0.08, h2=0.08, h3=0.02,
                                                  drive=1.2, level=0.7, velsens=0.3)),
    "pluck": ("ks", "Karplus-Strong plucked string (harp/guitar/koto-like)", dict(
        excite=0.004, damp=0.996, bright=6000, release=0.35, level=0.8, velsens=0.6)),
    "nylon_pluck": ("ks", "Soft, dark plucked string", dict(excite=0.006, damp=0.994, bright=2500, release=0.3,
                                                            level=0.9, velsens=0.6)),
    "epiano": ("fm", "FM electric piano (Rhodes/DX-style)", dict(
        attack=0.001, decay=2.5, sustain=0.0, release=0.4, ratio=1.0, index=1.6, mattack=0.001, mdecay=0.7,
        msustain=0.15, spread=1.5, level=0.45, velsens=0.7)),
    "bell": ("fm", "FM bell / celesta-like chime", dict(
        attack=0.001, decay=4.0, sustain=0.0, release=1.2, ratio=3.5, index=2.6, mattack=0.001, mdecay=1.8,
        msustain=0.0, spread=2.0, level=0.35, velsens=0.6)),
    "mallet": ("fm", "FM marimba/mallet", dict(
        attack=0.001, decay=0.6, sustain=0.0, release=0.2, ratio=4.0, index=1.3, mattack=0.001, mdecay=0.08,
        msustain=0.0, spread=0.5, level=0.5, velsens=0.7)),
    "fm_bass": ("fm", "Punchy FM bass", dict(
        attack=0.001, decay=0.5, sustain=0.6, release=0.08, ratio=1.0, index=2.2, mattack=0.001, mdecay=0.15,
        msustain=0.2, spread=0.0, level=0.6, velsens=0.5)),
    "chip_square": ("chip", "8-bit 50% square", dict(attack=0.002, decay=0.1, sustain=0.8, release=0.05, shape=0, duty=0.5,
                                                    vibdepth=0.0, vibrate=6, level=0.4, velsens=0.4)),
    "chip_pulse": ("chip", "8-bit 25% pulse (NES lead)", dict(attack=0.002, decay=0.1, sustain=0.8, release=0.05, shape=0,
                                                             duty=0.25, vibdepth=0.1, vibrate=6, level=0.4, velsens=0.4)),
    "chip_thin": ("chip", "8-bit 12.5% thin pulse", dict(attack=0.002, decay=0.1, sustain=0.8, release=0.05, shape=0,
                                                        duty=0.125, vibdepth=0.0, vibrate=6, level=0.4, velsens=0.4)),
    "chip_triangle": ("chip", "8-bit 4-bit triangle (NES bass)", dict(attack=0.001, decay=0.1, sustain=1.0, release=0.03,
                                                                     shape=1, duty=0.5, vibdepth=0, vibrate=6, level=0.6,
                                                                     velsens=0.2)),
    "chip_noise": ("chip", "8-bit noise channel; pitch = noise colour (use for chip drums)", dict(
        attack=0.001, decay=0.12, sustain=0.0, release=0.05, shape=2, duty=0.5, vibdepth=0, vibrate=6, level=0.4,
        velsens=0.6)),
    "organ": ("organ", "Drawbar organ with rotary tremolo", dict(d16=0.8, d8=1.0, d5=0.6, d4=0.5, d3=0.2, d2=0.3,
                                                                  clickamt=0.3, rot=0.12, rotrate=6.0, release=0.08,
                                                                  level=0.35, velsens=0.0)),
    "drums": ("drums", "GM-mapped synth drum machine (808/909-ish)", dict(
        kfreq=48, kpitch=110, kdecay=0.45, kclick=0.35, sdecay=0.17, snoise=0.8, hdecay=0.05, level=0.8, velsens=0.7)),
}


def synth_dsp(preset: str, overrides: dict | None = None) -> str:
    if preset not in SYNTHS:
        raise KeyError(f"Unknown synth {preset!r}. Built-in synths: {', '.join(SYNTHS)}")
    template, _desc, defaults = SYNTHS[preset]
    params = {**defaults}
    for k, v in (overrides or {}).items():
        if k not in params:
            raise KeyError(f"Synth {preset!r} has no parameter {k!r}. Parameters: {', '.join(params)}")
        params[k] = float(v)
    consts = "".join(f"{k} = {float(v)!r};\n" for k, v in params.items())
    return _HEAD.replace("vamp =", consts + "vamp =", 1) + TEMPLATES[template]


# ---- effects ---------------------------------------------------------------------------------------

FX = {
    "eq": ("Parametric EQ: high-pass, low shelf, mid bell, high shelf, low-pass", dict(
        hpf=20, low_freq=120, low_gain=0, mid_freq=1000, mid_gain=0, mid_q=1.0, high_freq=8000, high_gain=0, lpf=20000),
           """chain = fi.highpass(2, hpf) : fi.low_shelf(low_gain, low_freq) : fi.peak_eq_cq(mid_gain, mid_freq, mid_q)
        : fi.high_shelf(high_gain, high_freq) : fi.lowpass(2, min(lpf, ma.SR * 0.45));
process = chain, chain;"""),
    "compressor": ("Stereo compressor (threshold dB, ratio, attack/release s, makeup dB, mix 0..1); takes a "
                   "sidechain key (fx_add sidechain=<track>) for ducking", dict(
        threshold=-18, ratio=3, attack=0.01, release=0.15, makeup=3, mix=1.0),
                   """comp = co.compressor_stereo(ratio, threshold, attack, release) : *(g), *(g) with { g = ba.db2linear(makeup); };
process = _, _ <: (*(1 - mix), *(1 - mix)), (comp : *(mix), *(mix)) :> _, _;"""),
    "reverb": ("Zita hall reverb (predelay ms, decay s, damping Hz, mix 0..1, low_cut Hz)", dict(
        predelay=25, decay=2.8, low_decay=3.0, damping=6000, mix=0.25, low_cut=180),
               """rev = fi.highpass(1, low_cut), fi.highpass(1, low_cut) : re.zita_rev1_stereo(predelay, 200, damping, low_decay, decay, 48000);
process = _, _ <: (*(1 - mix * 0.5), *(1 - mix * 0.5)), (rev : *(mix), *(mix)) :> _, _;"""),
    "delay": ("Tempo-synced echo (time in beats, feedback 0..0.95, damping Hz, mix, spread ms for width)", dict(
        time=0.75, feedback=0.35, damping=4500, mix=0.2, spread=12),
              """echo(d) = (+ : de.delay(192000, int(d * ma.SR))) ~ (fi.lowpass(1, damping) : *(feedback));
process = (_ <: _, (echo(delay_sec) : *(mix)) :> _), (_ <: _, (echo(delay_sec + spread * 0.001) : *(mix)) :> _);"""),
    "chorus": ("Stereo chorus (rate Hz, depth ms, mix)", dict(rate=0.6, depth=3.0, mix=0.4),
               """ch(r) = _ <: _, de.fdelay(4096, (8 + depth * (1 + os.osc(r)) * 0.5) * ma.SR * 0.001) : *(1 - mix * 0.5), *(mix) :> _;
process = ch(rate), ch(rate * 1.13);"""),
    "saturator": ("Tape-style saturation (drive dB, output dB, mix)", dict(drive=6, output=-3, mix=1.0),
                  """sat = _ <: *(1 - mix), (*(ba.db2linear(drive)) : ma.tanh : *(ba.db2linear(output)) : *(mix)) :> _;
process = sat, sat;"""),
    "width": ("Stereo width (0 = mono, 1 = unchanged, 2 = extra wide)", dict(width=1.3),
              """process = _, _ <: ((_, _ :> *(0.5)), (_, *(-1) :> *(0.5 * width))) <: (_, _ :> _), (_, *(-1) :> _);"""),
    "filter": ("Resonant filter (mode 0 = low-pass, 1 = high-pass; cutoff Hz, q)", dict(mode=0, cutoff=2000, q=0.8),
               """f = _ <: fi.resonlp(cutoff, q, 1), fi.resonhp(cutoff, q, 1) : select2(mode > 0.5);
process = f, f;"""),
    "bitcrush": ("Lo-fi bit/sample-rate reduction (bits, rate Hz, mix)", dict(bits=8, rate=11025, mix=1.0),
                 """sh(t, x) = (*(1 - t) : +(x * t)) ~ _;
clk = os.lf_sawpos(rate) < os.lf_sawpos(rate)';
q(x) = floor(x * pow(2, bits - 1) + 0.5) / pow(2, bits - 1);
shx(x) = sh(clk, x);
c = _ <: *(1 - mix), (shx : q : *(mix)) :> _;
process = c, c;"""),
    "gain": ("Gain trim in dB", dict(gain_db=0), "process = *(ba.db2linear(gain_db)), *(ba.db2linear(gain_db));"),
}


# Sidechain versions of built-in effects: 4 inputs (main L/R, key L/R); the key drives the detector.
SIDECHAIN_BODIES = {
    "compressor": """process(l, r, kl, kr) = l * w, r * w with {
    g = co.compression_gain_mono(ratio, threshold, attack, release, abs(kl) + abs(kr));
    w = (1 - mix) + mix * g * ba.db2linear(makeup);
};""",
}
SIDECHAIN_FX = set(SIDECHAIN_BODIES)


def fx_dsp(name: str, overrides: dict | None = None, bpm: float = 120.0, sidechain: bool = False) -> str:
    if name not in FX:
        raise KeyError(f"Unknown built-in effect {name!r}. Built-in effects: {', '.join(FX)}")
    _desc, defaults, body = FX[name]
    if sidechain:
        if name not in SIDECHAIN_BODIES:
            raise ValueError(f"Built-in effect {name!r} has no sidechain input")
        body = SIDECHAIN_BODIES[name]
    params = {**defaults}
    for k, v in (overrides or {}).items():
        if k not in params:
            raise KeyError(f"Effect {name!r} has no parameter {k!r}. Parameters: {', '.join(params)}")
        params[k] = float(v)
    consts = "".join(f"{k} = {float(v)!r};\n" for k, v in params.items())
    if name == "delay":
        consts += f"delay_sec = {params['time'] * 60.0 / bpm!r};\n"
    return 'import("stdfaust.lib");\n' + consts + body


def catalog() -> dict:
    return {
        "synths": {k: {"description": d, "params": p} for k, (_t, d, p) in SYNTHS.items()},
        "effects": {k: {"description": d, "params": p} for k, (d, p, _b) in FX.items()},
    }
