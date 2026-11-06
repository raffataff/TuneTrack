from pathlib import Path

import numpy as np
import pytest

from tunetrack import api, listening
from tunetrack.model import Project

SR = 48000


def _tone(f0: float, secs: float = 0.6, partials: int = 6) -> np.ndarray:
    t = np.arange(int(secs * SR)) / SR
    return sum(np.sin(2 * np.pi * f0 * k * t) / k for k in range(1, partials + 1)).astype(np.float32)


def _noise_hit(lo: float, tau: float, secs: float = 3.0, seed: int = 0) -> np.ndarray:
    from scipy.signal import butter, sosfilt
    rng = np.random.default_rng(seed)
    n = int(secs * SR)
    x = sosfilt(butter(4, lo, "highpass", fs=SR, output="sos"), rng.standard_normal(n))
    return (x * np.exp(-np.arange(n) / (tau * SR))).astype(np.float32)


# ---- probe measurements -------------------------------------------------------------------------------------------

def test_pitch_offset_reads_tuning_and_octaves():
    a3 = _tone(220.0)  # MIDI 57
    assert listening.pitch_offset(a3, SR, 57) == 0
    assert listening.pitch_offset(a3, SR, 55) == 2  # written G3, sounds A3
    assert listening.pitch_offset(a3, SR, 69) == -12  # written A4, sounds an octave lower
    assert listening.pitch_offset(a3, SR, 45) == 12  # written A2, nothing at 110 Hz


def test_pitch_offset_ignores_the_previous_notes_tail():
    tail = 3 * _tone(196.0)  # a louder G3 still ringing
    note = _tone(220.0) + 0.6 * tail
    assert listening.pitch_offset(note, SR, 57, pre=tail) == 0


def test_drum_classes():
    n = int(3 * SR)
    t = np.arange(n) / SR
    kick = (np.sin(2 * np.pi * 55 * t) * np.exp(-t / 0.3)).astype(np.float32)
    click = np.zeros(n, np.float32)
    click[:480] = np.sin(2 * np.pi * 2000 * t[:480])
    assert listening.drum_class(kick, SR)[0] == "kick"
    assert listening.drum_class(_noise_hit(7000, 0.05), SR)[0] == "hat"
    assert listening.drum_class(_noise_hit(4000, 0.6), SR)[0] == "cymbal"
    assert listening.drum_class(click, SR)[0] == "click"


# ---- mix measurements ---------------------------------------------------------------------------------------------

def test_masking_finds_a_buried_lead_and_a_low_end_clash():
    secs = 4
    t = np.arange(secs * SR) / SR
    rng = np.random.default_rng(1)
    from scipy.signal import butter, sosfilt
    band = sosfilt(butter(4, [700, 1400], "bandpass", fs=SR, output="sos"), rng.standard_normal(len(t)))
    sources = {
        "Lead": 0.01 * np.sin(2 * np.pi * 1000 * t),
        "Pad": 0.5 * band,
        "Kick": 0.4 * np.sin(2 * np.pi * 60 * t),
        "Bass": 0.4 * np.sin(2 * np.pi * 65 * t),
    }
    spectra = {k: listening.Spectrum(v.astype(np.float32), SR) for k, v in sources.items()}
    frames = len(spectra["Lead"].times)
    tracks, pairs, warns = listening.masking(spectra, set(), {"Lead"}, np.ones(frames, int), np.ones(frames, bool))
    assert tracks["Lead"]["vs_rest_db"] < -10 and tracks["Lead"]["covered_by"] == "Pad"
    assert any("'Lead' (lead)" in w for w in warns)
    assert any(set(p["tracks"]) == {"Kick", "Bass"} and p["band"] == "63 Hz" for p in pairs)


def test_section_spans_and_contrast():
    p = Project(name="x", markers=[{"at": 8.0, "name": "A"}, {"at": 40.0, "name": "B"}, {"at": 72.0, "name": "A2"}])
    spans = listening.section_spans(p, 104.0)
    assert [s[0] for s in spans] == ["Start", "A", "B", "A2"]
    assert listening.section_spans(p, 64.0, section_bars=8) == [("bars 1-8", 0.0, 32.0), ("bars 9-16", 32.0, 64.0)]

    def sec(name):
        return {"name": name, "lufs": -18.0, "brightness_hz": 2000, "notes_per_bar": 12.0, "_beats": 32.0,
                "_active": {"Lead", "Pad"}}
    assert listening.contrast_warnings([sec("A"), sec("B")], 4.0)
    assert not listening.contrast_warnings([sec("A - Theme"), sec("A' - Again")], 4.0)
    louder = {**sec("B"), "lufs": -15.0}
    assert not listening.contrast_warnings([sec("A"), louder], 4.0)


# ---- end to end -----------------------------------------------------------------------------------------------------

@pytest.fixture()
def song(tmp_path):
    api.project_new("Listen Song", folder=str(tmp_path / "song"), tempo=120, key="A minor")
    api.markers(add=[{"at": "1:1", "name": "A"}, {"at": "3:1", "name": "B"}])
    api.track_add("Lead", "synth:lead", role="lead", volume_db=-6)
    api.track_add("Bass", "synth:sub_bass", role="bass", volume_db=-8)
    api.track_add("Drums", "synth:drums", role="drums")
    api.write("Lead", "A4:q!mf C5 E5 D5 | C5:h A4:h | E5:q G5 A5 G5 | E5:w", at="1:1")
    api.write("Bass", "A2:h A2 | F2:h G2 | A2:h A2 | E2:w", at="1:1")
    api.drums("Drums", {"kick": "x...x...x...x...", "hat": "..x...x...x...x."}, at="1:1", bars=4)
    api.add_notes("Drums", [[49, "3:1", 0.25, 110]])  # a crash written as a 16th: the synth kit chokes it
    return tmp_path


def test_listen_end_to_end(song):
    rep = api.listen()
    assert [s["name"] for s in rep["sections"]] == ["A", "B"]
    assert len(rep["bar_lufs"]) == 4 and all(v is not None for v in rep["bar_lufs"])
    assert {"Lead", "Bass", "Drums"} <= set(rep["masking"]["tracks"])
    lead = rep["verify"]["Lead"]
    assert lead["probe"]["pitches_probed"] == 6 and "silent_pitches" not in lead and "sounds_transposed" not in lead
    notes = rep["verify"]["Drums"]["drum_notes"]
    assert any(n.startswith("C2 (36) kick: low thump") for n in notes)
    assert any("('crash') stops when its note ends" in w for w in rep["warnings"])
    assert not [w for w in rep["warnings"] if "make no sound" in w or "failed" in w]
    assert (Path(api.load().path) / "renders" / "listen.json").exists()
    # second run comes from the caches
    assert api.listen()["verify"]["Lead"]["probe"]["rendered"] == "cached"


def test_listen_compares_with_a_reference(song):
    mix = api.render(out=str(song / "ref.wav"))["files"][0]
    rep = api.listen(reference=mix, probe=False)
    ref = rep["reference"]
    assert ref["reference"]["lufs"] == pytest.approx(-16.0, abs=1.5)
    assert abs(ref["this_mix"]["brightness_hz"] - ref["reference"]["brightness_hz"]) < 0.25 * ref["reference"]["brightness_hz"]
    assert not [d for d in ref["differences"] if "band balance" in d or "darker" in d or "brighter" in d]
    assert "verify" in rep and "probe" not in rep["verify"].get("Lead", {})
