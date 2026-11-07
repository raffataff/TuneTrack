"""SFZ parsing details real free libraries depend on, and .fxp chunks as VST3 state."""
import struct

from tunetrack import sfz, worker


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_default_path_may_contain_spaces(tmp_path):
    # VSCO 2 CE: default_path=Strings\Cello Section\pizzT\
    f = _write(tmp_path / "cello.sfz", "<control>\ndefault_path=Strings\\Cello Section\\pizzT\\\n\n"
                                       "<region> key=48 sample=pizz_C3.wav\n")
    inst = sfz.Instrument(f)
    assert inst.regions[0].sample == (tmp_path / "Strings" / "Cello Section" / "pizzT" / "pizz_C3.wav").resolve()


def test_include_mid_line_with_defines_from_the_included_file(tmp_path):
    # Salamander Grand Piano: <group> #include "Data/vel.txt" lovel=1 hivel=26 #include "Data/region.txt"
    _write(tmp_path / "Data" / "vel.txt", "#define $VEL v1\n")
    _write(tmp_path / "Data" / "region.txt", "<region> key=60 sample=A0$VEL.flac\n")
    f = _write(tmp_path / "piano.sfz",
               '<group> #include "Data/vel.txt" lovel=1 hivel=26 #include "Data/region.txt"\n')
    inst = sfz.Instrument(f)
    assert len(inst.regions) == 1
    r = inst.regions[0]
    assert (r.lovel, r.hivel, r.sample.name) == (1, 26, "A0v1.flac")


def test_opcode_names_are_case_insensitive(tmp_path):
    f = _write(tmp_path / "s.sfz", "<region> pitch_keycenter=c4 lokey=c4 hikey=d4 Sample=Shn 01.flac\n")
    inst = sfz.Instrument(f)
    assert inst.regions[0].sample.name == "Shn 01.flac"


def test_note_offset_transposes_incoming_notes(tmp_path):
    # note_offset=-12: note 72 plays the region mapped at 60, at that region's own pitch
    f = _write(tmp_path / "s.sfz", "<control> note_offset=-12\n<region> key=60 sample=*sine\n")
    r = sfz.Instrument(f).regions[0]
    assert (r.lokey, r.hikey, r.keycenter) == (72, 72, 72)


def test_fxp_chunk_becomes_vst3_component_state(tmp_path):
    chunk = bytes(range(256)) * 3 + b"sub3 patch"
    header = b"CcnK" + struct.pack(">i", 52 + len(chunk)) + b"FPCh" + struct.pack(">ii", 1, 0x636A7333)
    header += struct.pack(">ii", 1, 1) + b"Init".ljust(28, b"\0") + struct.pack(">i", len(chunk))
    fxp = tmp_path / "p.fxp"
    fxp.write_bytes(header + chunk)
    assert worker.fxp_chunk(str(fxp)) == chunk
    other = tmp_path / "p.vstpreset"
    other.write_bytes(b"VST3" + bytes(100))
    assert worker.fxp_chunk(str(other)) is None

    state = worker.vst3_state_from_chunk(chunk)
    assert state[:4] == b"VC2!" and state[-1:] == b"\0"
    assert struct.unpack("<I", state[4:8])[0] == len(state) - 9  # JUCE copyXmlToBinary length field
    encoded = state[8:-1].decode().split("<IComponent>")[1].split("</IComponent>")[0]
    assert _juce_b64_decode(encoded) == chunk


def _juce_b64_decode(s: str) -> bytes:
    size, enc = s.split(".", 1)
    bits = []
    for ch in enc:
        v = worker._JUCE_B64.index(ch)
        bits.extend((v >> i) & 1 for i in range(6))
    bits = bits[:int(size) * 8]
    return bytes(sum(bits[i * 8 + j] << j for j in range(8)) for i in range(int(size)))
