"""VST3 plugin registry (scan results) and the patch library (saved plugin states with tags)."""
from __future__ import annotations

import difflib
import json
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config


# ---- registry ---------------------------------------------------------------------------------------

def find_vst3_files(dirs: list[str]) -> list[Path]:
    found: list[Path] = []

    def walk(d: Path):
        try:
            entries = list(d.iterdir())
        except OSError:
            return
        for e in entries:
            if e.name.lower().endswith(".vst3"):
                found.append(e)
            elif e.is_dir():
                walk(e)

    for d in dirs:
        p = Path(d)
        if p.exists():
            walk(p)
    return sorted(found)


def load_registry() -> dict:
    if config.REGISTRY_FILE.exists():
        return json.loads(config.REGISTRY_FILE.read_text(encoding="utf-8"))
    return {"plugins": [], "failed": []}


def save_registry(reg: dict) -> None:
    config.HOME.mkdir(parents=True, exist_ok=True)
    config.REGISTRY_FILE.write_text(json.dumps(reg, indent=1), encoding="utf-8")


def run_worker(cmd: str, arg: str, timeout: float = 300) -> dict:
    """Run a worker command in a fresh process (plugins can crash; the caller survives).

    Output goes to a temp file, not a pipe: some plugins spawn helper servers (e.g. Steinberg MediaBay) that
    inherit the handles and would keep a pipe open forever.
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryDirectory(prefix="tunetrack_", ignore_cleanup_errors=True) as td:
        log = Path(td) / "worker.log"
        with open(log, "w", encoding="utf-8") as fo:
            proc = subprocess.Popen([config.python_exe(), "-m", "tunetrack.worker", cmd, arg], stdout=fo,
                                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=flags)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise RuntimeError(f"worker {cmd} timed out after {timeout:.0f}s") from None
        text = log.read_text(encoding="utf-8", errors="replace")
    lines = [ln for ln in text.splitlines() if ln.startswith("@@RESULT ")]
    if not lines:
        tail = text.strip().splitlines()[-12:]
        raise RuntimeError(f"worker {cmd} failed (exit {proc.returncode}): " + " | ".join(tail))
    res = json.loads(lines[-1][len("@@RESULT "):])
    if isinstance(res, dict) and res.get("error"):
        raise RuntimeError(res["error"])
    return res


def scan(force: bool = False, retry_failed: bool = False, timeout: float = 120, progress=None) -> dict:
    """Probe every new VST3 file in the configured folders (cached; force=True rescans everything)."""
    cfg = config.load_config()
    reg = load_registry() if not force else {"plugins": [], "failed": []}
    if retry_failed:
        reg["failed"] = []
    known = {p["path"] for p in reg["plugins"]} | {f["path"] for f in reg.get("failed", [])}
    files = [f for f in find_vst3_files(cfg["vst3_dirs"]) if str(f) not in known
             and not any(s.lower() in f.name.lower() for s in cfg.get("skip_plugins", []))]

    def probe(f: Path):
        try:
            names = run_worker("names", str(f), timeout=40)["names"]
        except Exception:  # noqa: BLE001 - pedalboard can't scan everything; DawDreamer may still load it
            names = []
        if len(names) > 1:
            return f, {"plugins": [{"name": n, "path": str(f), "kind": "effect", "host": "pedalboard", "shell": n,
                                    "inputs": None, "outputs": None, "params": None} for n in names]}, None
        try:
            res = run_worker("probe", str(f), timeout=timeout)
            if names:
                res["plugins"][0]["name"] = names[0]
            return f, res, None
        except Exception as e:  # noqa: BLE001 - report every failure, keep scanning
            return f, None, str(e)[:400]

    with ThreadPoolExecutor(max_workers=3) as ex:
        for f, res, err in ex.map(probe, files):
            if progress:
                progress(f"{f.name}: {'ok' if res else 'failed'}")
            if res:
                for entry in res["plugins"]:
                    entry["scanned_at"] = time.strftime("%Y-%m-%d %H:%M")
                    reg["plugins"].append(entry)
            else:
                reg.setdefault("failed", []).append({"path": str(f), "error": err})
    reg["plugins"].sort(key=lambda p: (p["kind"], p["name"].lower()))
    save_registry(reg)
    return {"scanned": len(files), "total": len(reg["plugins"]), "failed": len(reg.get("failed", []))}


_EXT_SC = re.compile(r"\bext(?:ernal)?\.?\s*(?:s/?c|side\s*-?chain|key)\b", re.I)
_EXT_SC_NOT = re.compile(r"listen|solo|audition|monitor|hpf|lpf|filter|freq|gain|level|mix|eq\b", re.I)


def is_ext_sidechain_switch(param_name: str) -> bool:
    """A plugin parameter that switches its detector to the external sidechain input ("External S/C",
    "Low Ext S/C", "EXT Sidechain", "Ext Key"), not a sidechain filter or listen button."""
    return bool(_EXT_SC.search(param_name)) and not _EXT_SC_NOT.search(param_name)


def resolve(name: str, kind: str | None = None) -> dict:
    plugins = [p for p in load_registry()["plugins"] if kind is None or p["kind"] == kind]
    n = name.strip().lower()
    for p in plugins:
        if p["name"].lower() == n:
            return p
    subs = [p for p in plugins if n in p["name"].lower()]
    if len(subs) == 1:
        return subs[0]
    close = difflib.get_close_matches(name, [p["name"] for p in plugins], n=5, cutoff=0.4)
    hint = f" Did you mean: {', '.join(close)}?" if close else ""
    if subs:
        hint = f" Ambiguous: {', '.join(p['name'] for p in subs[:8])}."
    if not plugins:
        hint = " The registry is empty: run plugins_scan first."
    raise KeyError(f"No {kind or 'plugin'} named {name!r}.{hint}")


# ---- patches ------------------------------------------------------------------------------------------

def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "patch"


def patch_path(pid: str) -> Path:
    return config.PATCH_DIR / f"{pid}.json"


def list_patches(query: str | None = None, kind: str | None = None, plugin: str | None = None) -> list[dict]:
    out = []
    if not config.PATCH_DIR.exists():
        return out
    words = [w for w in (query or "").lower().split() if w]
    for f in sorted(config.PATCH_DIR.glob("*.json")):
        meta = json.loads(f.read_text(encoding="utf-8"))
        if kind and meta.get("kind") != kind:
            continue
        if plugin and plugin.lower() not in meta.get("plugin", "").lower():
            continue
        hay = " ".join([meta.get("id", ""), meta.get("name", ""), meta.get("plugin", ""), meta.get("description", ""),
                        " ".join(meta.get("tags", []))]).lower()
        if all(w in hay for w in words):
            out.append(meta)
    return out


def get_patch(pid: str) -> dict:
    f = patch_path(pid)
    if not f.exists():
        close = difflib.get_close_matches(pid, [p.stem for p in config.PATCH_DIR.glob("*.json")], n=5, cutoff=0.4)
        raise KeyError(f"No patch {pid!r}." + (f" Did you mean: {', '.join(close)}?" if close else ""))
    return json.loads(f.read_text(encoding="utf-8"))


def save_patch(meta: dict) -> dict:
    config.PATCH_DIR.mkdir(parents=True, exist_ok=True)
    patch_path(meta["id"]).write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return meta


def state_file(meta: dict) -> Path | None:
    s = meta.get("state")
    return config.PATCH_DIR / s if s else None


def _device(entry: dict) -> dict:
    """A registry entry as a worker device spec. The registry's "params" is a parameter count, not values to set."""
    return {k: v for k, v in entry.items() if k != "params"}


def capture(plugin: str, name: str, tags: list[str] | None = None, description: str = "", pid: str | None = None,
            base_patch: str | None = None, kind: str | None = None, extra: dict | None = None) -> dict:
    """Open the plugin's own editor window; whatever sound is loaded when the window closes is saved as a patch."""
    entry = resolve(plugin, kind)
    pid = pid or slug(f"{entry['name']} {name}")
    base_state = None
    if base_patch:
        base_state = str(state_file(get_patch(base_patch)))
    config.PATCH_DIR.mkdir(parents=True, exist_ok=True)
    out = config.PATCH_DIR / f"{pid}.state"
    spec = {"plugin": _device(entry), "state_in": base_state, "state_out": str(out)}
    spec_file = config.PATCH_DIR / f".{pid}.capture.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    try:
        res = run_worker("capture", str(spec_file), timeout=3600)
    finally:
        spec_file.unlink(missing_ok=True)
    meta = {"id": pid, "name": name, "plugin": entry["name"], "kind": entry["kind"], "tags": tags or [],
            "description": description, "state": out.name, "created": time.strftime("%Y-%m-%d %H:%M"),
            "param_count": res.get("params"), **(extra or {})}
    save_patch(meta)
    return meta


def import_preset(plugin: str, preset_file: str, name: str, tags: list[str] | None = None, description: str = "",
                  pid: str | None = None, extra: dict | None = None) -> dict:
    """Load a .vstpreset (or .fxp) into the plugin headlessly and store the resulting state as a patch."""
    entry = resolve(plugin)
    pid = pid or slug(f"{entry['name']} {name}")
    out = config.PATCH_DIR / f"{pid}.state"
    config.PATCH_DIR.mkdir(parents=True, exist_ok=True)
    spec_file = config.PATCH_DIR / f".{pid}.import.json"
    spec_file.write_text(json.dumps({"plugin": _device(entry), "preset": preset_file, "state_out": str(out)}),
                         encoding="utf-8")
    try:
        run_worker("import_preset", str(spec_file), timeout=300)
    finally:
        spec_file.unlink(missing_ok=True)
    meta = {"id": pid, "name": name, "plugin": entry["name"], "kind": entry["kind"], "tags": tags or [],
            "description": description, "state": out.name, "source": preset_file,
            "created": time.strftime("%Y-%m-%d %H:%M"), **(extra or {})}
    return save_patch(meta)


def import_folder(plugin: str, folder: str, tags: list[str] | None = None, role: str | None = None) -> dict:
    """Import every .vstpreset/.fxp under a folder (subfolders included). Each subfolder name becomes a tag and
    part of the patch id, so presets with the same name in different subfolders don't overwrite each other."""
    root = Path(folder)
    if not root.is_dir():
        raise NotADirectoryError(f"No folder {folder!r}.")
    files = sorted(f for f in root.rglob("*") if f.suffix.lower() in (".vstpreset", ".fxp"))
    if not files:
        raise FileNotFoundError(f"No .vstpreset or .fxp files under {folder!r}.")
    entry = resolve(plugin)
    patches, errors = [], []
    for f in files:
        rel = f.relative_to(root)
        groups = [g for g in rel.parent.parts]
        pid = slug(" ".join([entry["name"], *groups, f.stem]))
        ptags = [*(tags or []), *(g.lower() for g in groups)]
        try:
            patches.append(import_preset(plugin, str(f), f.stem, ptags, pid=pid,
                                         extra={"role": role} if role else None))
        except Exception as e:  # noqa: BLE001 - keep going; report each failure
            errors.append({"file": str(rel), "error": f"{type(e).__name__}: {e}"})
    return {"imported": [p["id"] for p in patches], "errors": errors}


def delete_patch(pid: str) -> None:
    meta = get_patch(pid)
    sf_ = state_file(meta)
    if sf_ and sf_.exists():
        sf_.unlink()
    patch_path(pid).unlink()


def find_sfz(query: str | None = None, limit: int = 200) -> list[str]:
    cfg = config.load_config()
    words = [w for w in (query or "").lower().split() if w]
    out = []
    for d in cfg.get("sample_dirs", []):
        p = Path(d)
        if not p.exists():
            continue
        for f in p.rglob("*.sfz"):
            rel = str(f)
            if all(w in rel.lower() for w in words):
                out.append(rel)
                if len(out) >= limit:
                    return out
    return sorted(out)
