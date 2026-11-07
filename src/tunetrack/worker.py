"""Subprocess entry point for anything that loads plugins: python -m tunetrack.worker <cmd> <arg>.

Prints one line "@@RESULT {json}" on success. Running each job in its own process keeps a misbehaving
plugin from taking the MCP server or UI down with it.
"""
from __future__ import annotations

import json
import re
import sys
import time
import traceback
from pathlib import Path

import numpy as np

# Seconds to pump the message thread after a plugin loads a saved state or preset. HALion finishes loading programs
# from message-thread callbacks: without pumping it renders pure silence however long you wait (0.1 s of pumping
# was still silent, 0.25 s played with a warm disk cache; the margin is for cold starts and bigger multis).
SETTLE_S = 2.0


def _result(obj) -> None:
    print("@@RESULT " + json.dumps(obj), flush=True)


def _engine(sr: int, bpm: float, block: int = 512):
    import dawdreamer as daw
    eng = daw.RenderEngine(int(sr), int(block))
    eng.set_bpm(float(bpm))
    return eng


# ---- parameters ---------------------------------------------------------------------------------------

def _num(text: str) -> float | None:
    m = re.search(r"[-+]?\d+(?:\.\d+)?", text.replace(",", "."))
    if not m:
        return None
    v = float(m.group(0))
    t = text.lower()
    if re.search(r"\d\s*k(hz)?\b", t):
        v *= 1000
    return v


def _param_index(desc: list[dict], key: str) -> int:
    k = str(key).strip().lower()
    if k.isdigit():
        return int(k)
    for d in desc:
        if d["name"].strip().lower() == k:
            return d["index"]
    subs = [d for d in desc if k in d["name"].lower()]
    if len(subs) == 1:
        return subs[0]["index"]
    raise KeyError(f"No parameter {key!r}" + (f"; candidates: {[d['name'] for d in subs[:8]]}" if subs else ""))


def _value_for_text(get_text, set_value, target: str) -> float:
    """Find the normalised value whose display text matches `target` ("-12 dB", "On", "Hall")."""
    want = target.strip().lower()
    want_n = _num(want)
    grid = np.linspace(0, 1, 201)
    best, best_err, samples = 0.0, float("inf"), []
    for v in grid:
        set_value(float(v))
        txt = str(get_text()).strip().lower()
        samples.append((float(v), txt))
        if txt == want:
            return float(v)
        n = _num(txt)
        if want_n is not None and n is not None and abs(n - want_n) < best_err:
            best, best_err = float(v), abs(n - want_n)
    if want_n is None:
        for v, txt in samples:
            if want in txt or txt in want:
                return v
        raise ValueError(f"No value shows as {target!r}; seen e.g. {[t for _, t in samples[::40]]}")
    # refine numerically between neighbours of the best grid point (assumes local monotonicity)
    lo, hi = max(0.0, best - 0.005), min(1.0, best + 0.005)
    for _ in range(18):
        mid = (lo + hi) / 2
        set_value(mid)
        n = _num(str(get_text()))
        set_value(lo)
        n_lo = _num(str(get_text()))
        if n is None or n_lo is None:
            break
        if (n_lo <= want_n <= n) or (n <= want_n <= n_lo):
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def _apply_dd_params(p, params: dict | None) -> None:
    if not params:
        return
    desc = p.get_parameters_description()
    for k, v in params.items():
        idx = _param_index(desc, k)
        if isinstance(v, (int, float)):
            p.set_parameter(idx, float(v))
        else:
            val = _value_for_text(lambda: p.get_parameter_text(idx), lambda x: p.set_parameter(idx, x), str(v))
            p.set_parameter(idx, val)


def _apply_pb_params(p, params: dict | None) -> None:
    if not params:
        return
    by_name = {name.lower(): param for name, param in p.parameters.items()}
    for k, v in params.items():
        key = k.strip().lower().replace(" ", "_")
        param = by_name.get(key) or next((pp for n, pp in by_name.items() if key in n), None)
        if param is None:
            raise KeyError(f"No parameter {k!r}")
        if isinstance(v, (int, float)):
            param.raw_value = float(v)
        else:
            setattr(p, [n for n, pp in p.parameters.items() if pp is param][0], v)


# ---- plugin loading ---------------------------------------------------------------------------------

def _settle(seconds: float) -> None:
    """Dispatch the window messages JUCE posts for this (main) thread, so async plugin loading can complete."""
    if sys.platform != "win32":
        return
    import ctypes
    import ctypes.wintypes as wt
    user32 = ctypes.windll.user32
    msg = wt.MSG()
    end = time.time() + seconds
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):  # PM_REMOVE
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)


def _dd_plugin(eng, name: str, dev: dict, sidechain: bool = False):
    p = eng.make_plugin_processor(name, dev["path"])
    if p.get_num_input_channels() > 2 and not sidechain:
        # no key: feed only the main pair. (Never on instruments - it crashes HALion Sonic.)
        try:
            p.disable_nonmain_buses()
        except Exception:  # noqa: BLE001 - some plugins refuse; keep their default layout
            pass
    if dev.get("state"):
        p.load_state(dev["state"])
    if dev.get("preset"):
        ok = p.load_vst3_preset(dev["preset"]) if dev["preset"].lower().endswith(".vstpreset") else p.load_preset(dev["preset"])
        if not ok:
            raise RuntimeError(f"Plugin refused preset {dev['preset']}")
    if dev.get("state") or dev.get("preset"):
        _settle(SETTLE_S)
    _apply_dd_params(p, dev.get("params"))
    return p


_JUCE_B64 = ".ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+"


def fxp_chunk(path: str) -> bytes | None:
    """The opaque plugin chunk in a VST2 program file ('CcnK' ... 'FPCh'), or None for any other preset file."""
    raw = Path(path).read_bytes()
    if len(raw) < 60 or raw[:4] != b"CcnK" or raw[8:12] != b"FPCh":
        return None
    return raw[60:60 + int.from_bytes(raw[56:60], "big")]


def juce_base64(data: bytes) -> str:
    """JUCE MemoryBlock::toBase64Encoding: '<size>.' then 6-bit groups, least significant bit first."""
    bits = np.unpackbits(np.frombuffer(data, dtype=np.uint8), bitorder="little")
    bits = np.concatenate([bits, np.zeros(-len(bits) % 6, dtype=np.uint8)]).reshape(-1, 6)
    vals = bits @ (1 << np.arange(6))
    return f"{len(data)}." + "".join(_JUCE_B64[v] for v in vals)


def vst3_state_from_chunk(chunk: bytes) -> bytes:
    """A JUCE-hosted VST3 state (the format DawDreamer's save_state writes) whose component state is `chunk`.
    JUCE-built plugins (Surge XT, Dexed, ...) store the same data as a VST2 .fxp chunk and as VST3 component state."""
    xml = ('<?xml version="1.0" encoding="UTF-8"?> <VST3PluginState><IComponent>' + juce_base64(chunk)
           + "</IComponent></VST3PluginState>").encode("utf-8")
    return b"VC2!" + len(xml).to_bytes(4, "little") + xml + b"\0"


def _pb_plugin(dev: dict):
    import pedalboard
    p = pedalboard.load_plugin(dev["path"], plugin_name=dev.get("shell"))
    if dev.get("state"):
        p.raw_state = Path(dev["state"]).read_bytes()
    if dev.get("preset"):
        p.load_preset(dev["preset"])
    _apply_pb_params(p, dev.get("params"))
    return p


def _midi_events(midi_path: str, bends: list | None = None):
    """MIDI file -> (notes [(on, off, key, vel)], ccs [(t, num, val)]) in seconds.
    Pass a list as `bends` to also collect pitch-wheel events [(t, value -8192..8191)]."""
    import mido
    mf = mido.MidiFile(midi_path)
    t = 0.0
    active: dict[int, list] = {}
    notes, ccs = [], []
    for msg in mf:
        t += msg.time
        if msg.type == "note_on" and msg.velocity > 0:
            active.setdefault(msg.note, []).append((t, msg.velocity))
        elif msg.type in ("note_off", "note_on"):
            if active.get(msg.note):
                on, vel = active[msg.note].pop(0)
                notes.append((on, t, msg.note, vel))
        elif msg.type == "control_change":
            ccs.append((t, msg.control, msg.value))
        elif msg.type == "pitchwheel" and bends is not None:
            bends.append((t, msg.pitch))
    for k, lst in active.items():
        for on, vel in lst:
            notes.append((on, t, k, vel))
    return notes, ccs


def _run_dd(specs: list[dict], audio: np.ndarray | None, sr: int, bpm: float, duration: float, midi: str | None):
    """Render a chain of DawDreamer-hosted devices. specs[0] may be an instrument (when audio is None)."""
    if not specs:
        return audio, 0
    eng = _engine(sr, bpm)
    graph = []
    prev = None
    latency = 0
    if audio is not None:
        pb = eng.make_playback_processor("src", audio.astype(np.float32))
        graph.append((pb, []))
        prev = "src"
    for i, dev in enumerate(specs):
        name = f"n{i}"
        sc = dev.get("sidechain_in") if prev else None
        if dev["type"] == "synth":
            p = eng.make_faust_processor(name)
            p.num_voices = int(dev.get("voices", 32))
            p.set_dsp_string(dev["dsp"])
            p.compile()
            if midi:
                _load_midi_faust(p, midi)
        elif dev["type"] == "faust":
            p = eng.make_faust_processor(name)
            p.set_dsp_string(dev["dsp_sc"] if sc and dev.get("dsp_sc") else dev["dsp"])
            p.compile()
        else:
            p = _dd_plugin(eng, name, dev, sidechain=bool(sc))
            if dev.get("is_instrument") and (dev.get("state") or dev.get("preset")):
                _process_briefly(eng, p)  # else plugins that apply a loaded patch on the audio thread drop notes at 0
            if dev.get("is_instrument") and midi:
                p.load_midi(midi, clear_previous=True, beats=False, all_events=True)
        inputs = [prev] if prev else []
        if sc:
            # the key arrives as extra input channels after the main pair (DawDreamer stacks inputs in order),
            # delayed by what the devices before this one add, so it lines up with the signal it controls
            key = _sidechain_audio(sc, sr, int(round(duration * sr)), latency, p.get_num_input_channels() - 2)
            graph.append((eng.make_playback_processor(f"key{i}", key), []))
            inputs.append(f"key{i}")
        if dev["type"] == "vst3":
            latency += int(p.get_latency_samples() or 0)
        graph.append((p, inputs))
        prev = name
    eng.load_graph(graph)
    eng.render(duration + latency / sr)
    out = eng.get_audio()
    if out.shape[0] == 1:
        out = np.repeat(out, 2, axis=0)
    out = out[:2]
    if latency:
        out = out[:, latency:]
    return out, latency


def _sidechain_audio(src: str, sr: int, n: int, delay: int, channels: int) -> np.ndarray:
    """The key signal for a sidechain input: a rendered track ("silent" = none), `delay` samples late."""
    import soundfile as sf
    channels = max(1, min(2, channels))
    if src == "silent":
        return np.zeros((channels, n + delay), dtype=np.float32)
    data, fsr = sf.read(src, dtype="float32", always_2d=True)
    if fsr != sr:
        raise RuntimeError(f"sidechain key {src} is {fsr} Hz, the project renders at {sr} Hz")
    key = data.T[:2]
    if key.shape[0] == 1:
        key = np.repeat(key, 2, axis=0)
    key = key[:, :n]
    key = np.pad(key, ((0, 0), (delay, n - key.shape[1])))
    if channels == 1:
        key = key.mean(axis=0, keepdims=True)
    return np.ascontiguousarray(key, dtype=np.float32)


def _load_midi_faust(p, midi: str) -> None:
    try:
        p.load_midi(midi, clear_previous=True, beats=False, all_events=True)
    except Exception:  # noqa: BLE001 - older builds: add notes one by one
        notes, _ = _midi_events(midi)
        for on, off, key, vel in notes:
            p.add_midi_note(key, vel, on, max(0.001, off - on))


def cmd_render(spec_path: str) -> None:
    import soundfile as sf
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    sr, bpm, dur = int(spec["sr"]), float(spec["bpm"]), float(spec["duration"])
    midi = spec.get("midi")
    n = int(round(dur * sr))
    audio = None
    pending: list[dict] = []
    warnings = []
    inst = spec.get("instrument")
    if spec.get("audio_in"):
        data, _ = sf.read(spec["audio_in"], dtype="float32", always_2d=True)
        audio = data.T
    elif inst:
        if inst["type"] == "sfz":
            from .sfz import Instrument
            bends: list = []
            notes, ccs = _midi_events(midi, bends) if midi else ([], [])
            events = [("note", on, off, k, v) for on, off, k, v in notes] + [("cc", t, c, v) for t, c, v in ccs]                 + [("bend", t, v) for t, v in bends]
            audio = Instrument(inst["path"]).render(events, dur, sr)
        elif inst["type"] == "vst3" and inst.get("host") == "pedalboard":
            import mido
            p = _pb_plugin(inst)
            bends = []
            notes, ccs = _midi_events(midi, bends) if midi else ([], [])
            msgs = []
            for on, off, k, v in notes:
                msgs.append(mido.Message("note_on", note=k, velocity=v, time=on))
                msgs.append(mido.Message("note_off", note=k, velocity=0, time=off))
            for t, c, v in ccs:
                msgs.append(mido.Message("control_change", control=c, value=v, time=t))
            for t, v in bends:
                msgs.append(mido.Message("pitchwheel", pitch=v, time=t))
            msgs.sort(key=lambda m: m.time)
            audio = p(msgs, duration=dur, sample_rate=sr, num_channels=2)
        elif inst["type"] == "vst3":
            # render alone and keep the main stereo pair (multi-out instruments like HALion have 32+ outputs)
            audio, _ = _run_dd([{**inst, "is_instrument": True}], None, sr, bpm, dur, midi)
        else:
            pending.append({**inst, "is_instrument": True})
    for fx in spec.get("fx", []):
        if fx["type"] == "vst3" and fx.get("host") == "pedalboard":
            if fx.get("sidechain_in"):
                warnings.append(f"{fx.get('name')} is hosted through pedalboard, which can't feed a sidechain: "
                                f"key ignored")
            audio, _ = _run_dd(pending, audio, sr, bpm, dur, midi)
            pending = []
            if audio is None:
                audio = np.zeros((2, n), dtype=np.float32)
            audio = _pb_plugin(fx)(audio.astype(np.float32), sr)
        else:
            pending.append(fx)
    audio, latency = _run_dd(pending, audio, sr, bpm, dur, midi)
    if audio is None:
        audio = np.zeros((2, n), dtype=np.float32)
    audio = np.nan_to_num(audio.astype(np.float32))
    if audio.shape[1] < n:
        audio = np.pad(audio, ((0, 0), (0, n - audio.shape[1])))
    audio = audio[:, :n]
    peak = float(np.abs(audio).max()) if audio.size else 0.0
    if peak > 8:
        warnings.append(f"output peaked at {peak:.1f} (+{20 * np.log10(peak):.0f} dBFS) - check gain staging/feedback")
    sf.write(spec["out"], audio.T, sr, subtype="FLOAT")
    _result({"ok": True, "peak": peak, "latency": latency, "warnings": warnings})


def cmd_names(path: str) -> None:
    """Sub-plugin names in a VST3 file (Waves-style shells hold many). Separate process: some plugins hang here."""
    import pedalboard
    _result({"names": list(pedalboard.VST3Plugin.get_plugin_names_for_file(path))})


def cmd_probe(path: str) -> None:
    eng = _engine(48000, 120)
    p = eng.make_plugin_processor("probe", path)
    ins, outs = p.get_num_input_channels(), p.get_num_output_channels()
    nparams = p.get_plugin_parameter_size()
    kind = "instrument" if ins == 0 else None
    if kind is None:
        p.add_midi_note(60, 100, 0.0, 0.5)
        eng.load_graph([(p, [])])
        eng.render(1.0)
        kind = "instrument" if float(np.abs(eng.get_audio()).max()) > 1e-4 else "effect"
    _result({"plugins": [{"name": Path(path).stem, "path": path, "kind": kind, "host": "dawdreamer", "shell": None,
                          "inputs": ins, "outputs": outs, "params": nparams}]})


def cmd_capture(spec_path: str) -> None:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    entry = spec["plugin"]
    if entry.get("host") == "pedalboard":
        p = _pb_plugin({**entry, "state": spec.get("state_in")})
        p.show_editor()
        Path(spec["state_out"]).write_bytes(p.raw_state)
        n = len(p.parameters)
    else:
        eng = _engine(48000, 120)
        p = _dd_plugin(eng, "cap", {**entry, "state": spec.get("state_in")})
        p.open_editor()
        p.save_state(spec["state_out"])
        n = p.get_plugin_parameter_size()
    _result({"ok": True, "params": n})


def cmd_import_preset(spec_path: str) -> None:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    entry = spec["plugin"]
    if entry.get("host") == "pedalboard":
        p = _pb_plugin({**entry, "preset": spec["preset"]})
        Path(spec["state_out"]).write_bytes(p.raw_state)
    else:
        eng = _engine(48000, 120)
        try:
            p = _dd_plugin(eng, "imp", {**entry, "preset": spec["preset"]})
        except RuntimeError:
            chunk = fxp_chunk(spec["preset"])
            if chunk is None:
                raise
            # DawDreamer reads .fxp files only into VST2 plugins: hand the chunk to the VST3 as its component state.
            out = Path(spec["state_out"])
            tmp, default = out.with_suffix(".fxp.tmp"), out.with_suffix(".default.tmp")
            try:
                eng.make_plugin_processor("default", entry["path"]).save_state(str(default))
                tmp.write_bytes(vst3_state_from_chunk(chunk))
                p = _dd_plugin(eng, "imp_chunk", {**entry, "state": str(tmp)})
                _process_briefly(eng, p)
                p.save_state(str(out))
                if out.read_bytes() == default.read_bytes():
                    out.unlink()
                    raise RuntimeError(f"Plugin ignored the chunk in {spec['preset']} (state unchanged)") from None
            finally:
                tmp.unlink(missing_ok=True)
                default.unlink(missing_ok=True)
            _result({"ok": True, "via": "fxp chunk as VST3 component state"})
            return
        _process_briefly(eng, p)
        p.save_state(spec["state_out"])
    _result({"ok": True})


def _process_briefly(eng, p) -> None:
    """Run a few audio blocks: some plugins (Surge XT) queue a loaded patch and apply it on the audio thread, so
    saving the state straight after loading would save the previous sound."""
    eng.load_graph([(p, [])])
    eng.render(0.25)


def cmd_params(spec_path: str) -> None:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    dev = spec["device"]
    q = (spec.get("query") or "").lower()
    out = []
    if dev.get("host") == "pedalboard":
        p = _pb_plugin(dev)
        for name, param in p.parameters.items():
            if q and q not in name.lower():
                continue
            out.append({"name": name, "value": round(float(param.raw_value), 4), "text": str(param.string_value)})
    else:
        eng = _engine(48000, 120)
        p = _dd_plugin(eng, "par", dev)
        for d in p.get_parameters_description():
            nm = d["name"]
            if nm.startswith("MIDI CC ") or (q and q not in nm.lower()):
                continue
            out.append({"index": d["index"], "name": nm, "value": round(float(p.get_parameter(d["index"])), 4),
                        "text": str(p.get_parameter_text(d["index"])), "steps": d.get("numSteps")})
    _result({"params": out[: int(spec.get("limit", 300))], "total": len(out)})


COMMANDS = {"render": cmd_render, "probe": cmd_probe, "names": cmd_names, "capture": cmd_capture, "import_preset": cmd_import_preset,
            "params": cmd_params}

if __name__ == "__main__":
    import os
    code = 0
    try:
        COMMANDS[sys.argv[1]](sys.argv[2])
    except Exception as e:  # noqa: BLE001 - report to the parent as JSON
        traceback.print_exc()
        _result({"error": f"{type(e).__name__}: {e}"})
        code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)  # don't wait on plugin threads that never finish
