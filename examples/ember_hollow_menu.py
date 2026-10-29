"""Worked example: a fantasy main-menu loop built only with TuneTrack API calls (the same calls the MCP tools make).

Form: 4-bar intro (played once) + 16-bar loop (A 8 bars, B 8 bars) in D minor, 84 BPM.
Uses built-in synths, MODO DRUM (VST3) and a Valhalla Supermassive reverb bus, so it runs without captured patches.

    uv run python examples/ember_hollow_menu.py
"""
from tunetrack import api

api.project_new("Ember Hollow - Main Menu", tempo=84, key="D minor", overwrite=True, brief={
    "genre": "cozy fantasy adventure", "mood": "warm, wistful, inviting", "use": "main menu loop",
    "notes": "intro once, then A/B loop; melody carries a 4-note rising motif (D-E-F-A)"})
api.markers(add=[{"at": "1:1", "name": "Intro"}, {"at": "5:1", "name": "A"}, {"at": "13:1", "name": "B"}])
api.project_set(loop="5:1-21:1", target_lufs=-17)

# ---- tracks ----------------------------------------------------------------------------------------------
api.track_add("Reverb", kind="bus", fx=[{"type": "vst3", "name": "ValhallaSupermassive", "params": {
    "Mix": "100 %", "Delay_Ms": "140 ms", "Feedback": "70 %", "Density": "60 %", "LowCut": "220 Hz",
    "HighCut": "7000 Hz"}}], volume_db=-7)
api.track_add("Pad", "synth:pad", role="pad", volume_db=-12, fx=["builtin:eq"])
api.track_update("Pad", instrument_params={"cutoff": 1700})
api.fx_update("Pad", 0, params={"hpf": 160, "mid_freq": 350, "mid_gain": -3})
api.track_add("Harp", "synth:pluck", role="harp", volume_db=-10, pan=-0.25)
api.track_update("Harp", instrument_params={"bright": 9000})
api.track_add("Melody", "synth:epiano", role="lead", volume_db=-6, pan=0.1)
api.track_update("Melody", instrument_params={"index": 2.6, "mdecay": 1.0})
api.track_add("Bell", "synth:bell", role="celesta", volume_db=-14, pan=0.3)
api.track_add("Bass", "synth:analog_bass", role="bass", volume_db=-9)
api.track_update("Bass", instrument_params={"cutoff": 160, "envamt": 2.5, "drive": 1.2})
api.track_add("Drums", "vst3:MODO DRUM", role="drums", volume_db=-2)
for t, db in (("Pad", -8), ("Harp", -6), ("Melody", -9), ("Bell", -3)):
    api.track_update(t, sends=[{"to": "Reverb", "db": db}])

# ---- harmony -----------------------------------------------------------------------------------------------
INTRO = "i VI III VII"
A = "i VI III VII i VI iv V"
B = "VI VII III i iv VI V V"
api.chords("Pad", INTRO, at="1:1", voices=4, low="F3", high="A4", vel=55)
api.chords("Pad", A, at="5:1", voices=4, low="F3", high="A4", vel=62)
api.chords("Pad", B, at="13:1", voices=4, low="F3", high="C5", vel=72)

arp = "1 5 8 10 12 10 8 5"
api.pattern("Harp", INTRO, arp, at="1:1", octave=3, rate=0.5, vel=58)
api.pattern("Harp", A, arp, at="5:1", octave=3, rate=0.5, vel=66)
api.pattern("Harp", B, "1 5 8 10 12 15 12 10", at="13:1", octave=3, rate=0.5, vel=74)
api.notes_edit("Harp", "humanize", timing_ms=8, velocity=8, seed=3)

api.pattern("Bass", A, "1 - - 1 . 1 5- .", at="5:1", octave=2, rate=0.5, vel=92)
api.pattern("Bass", B, "1 - - - 1 - 5- -", at="13:1", octave=2, rate=0.5, vel=100)

# ---- melody (motif D-E-F-A, answered and varied; B lifts an octave and leads back via C#) -------------------
api.write("Bell", "r:h D6:q!p A5 | F6:w | r:h C6:q A5 | G5:w", at="1:1")
api.write("Melody", """
    D5:q.!mf E5:e F5:q A5 | F5:h. D5:q | F5:q. E5:e C5:q A4 | C5:h G4:h |
    D5:q. E5:e F5:q A5 | Bb5:h A5:q G5 | G5:q. F5:e E5:q D5 | C#5:h E5:h
""", at="5:1")
api.write("Melody", """
    D6:h.!f C6:e Bb5 | C6:h G5:h | A5:q. G5:e F5:q C6 | D6:h. A5:q |
    Bb5:q. A5:e G5:q D6 | D6:h C6:q Bb5 | A5:w!mf | E5:q. F5:e G5:q C#5
""", at="13:1")
api.notes_edit("Melody", "velocity", start="13:1", end="17:1", ramp_from=86, ramp_to=104)
api.notes_edit("Melody", "humanize", timing_ms=6, velocity=4, seed=7)
api.write("Bell", "D6:h. C6:e Bb5 | C6:h G5:h | A5:q. G5:e F5:q C6 | D6:h. A5:q", at="13:1", vel=60)

# ---- drums: none in the intro, light in A, fuller in B with a fill into the loop point --------------------
api.drums("Drums", {"kick": "x.........x.....", "rim": "....x.......x...", "pedal_hat": "..x...x...x...x."},
          at="5:1", bars=8, vel=78)
api.drums("Drums", {"kick": "x.....x...x.....|x.....x...x...x.", "snare": "....x.......x...",
                    "hat": "x.x.x.x.x.x.x.xo", "ride_bell": "x...............|................"},
          at="13:1", bars=7, vel=86, swing=0.15)
api.drums("Drums", {"kick": "x.....x.........", "snare": "....x...o.o.x.x.", "tom_high": "........x.......",
                    "tom_mid": "..........x.....", "tom_low": "............x.x.", "crash": "x..............."},
          at="20:1", bars=1, vel=92)
api.notes_edit("Drums", "humanize", timing_ms=5, velocity=6, seed=11)

# first render flagged "very little energy above 2 kHz": open up the top end on the master
api.fx_add("master", {"type": "builtin", "name": "eq", "params": {"hpf": 28, "high_freq": 4500, "high_gain": 4,
                                                                   "low_freq": 90, "low_gain": -1.5}})
print(api.analyze())
rep = api.render()
print({k: rep[k] for k in ("files", "warnings", "loop_mode", "normalize_gain_db")})
print(rep["mix"])
