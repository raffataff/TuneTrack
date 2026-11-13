"""Accepting a take when the session hasn't moved must give exactly the take; an untouched take must change nothing."""
import json, os, random, sys, tempfile
home = tempfile.mkdtemp(prefix="tt_prop_")
os.environ["TUNETRACK_HOME"] = home; os.environ["TUNETRACK_PROJECTS"] = os.path.join(home, "projects")
from pathlib import Path
from tunetrack import api, takes, studio
from tunetrack.model import Project

def doc(p): return json.loads((Path(p) / "project.json").read_text(encoding="utf-8"))
def norm(d):
    d = Project.from_json(json.loads(json.dumps(d))).to_json(); d.pop("rev"); d.pop("next_id"); return d

def ops(rng, proj):
    names = [t["name"] for t in doc(proj)["tracks"]]
    midi = [t["name"] for t in doc(proj)["tracks"] if t.get("kind", "midi") == "midi"]
    buses = [t["name"] for t in doc(proj)["tracks"] if t.get("kind") == "bus"]
    bar = rng.randint(1, 8)
    choices = [
        lambda: api.write(rng.choice(midi), rng.choice(["C4:q E4 G4 C5", "D3:h A3:h", "(C4 E4 G4):w", "E5:e D5 C5 B4 A4 G4 F4 E4"]), at=f"{bar}:1", project=proj, mode=rng.choice(["replace", "merge"])),
        lambda: api.track_update(rng.choice(names), project=proj, volume_db=rng.choice([-12.0, -6.0, -3.0, 0.0]), pan=rng.choice([-0.5, 0, 0.5])),
        lambda: api.track_update(rng.choice(midi), project=proj, mute=rng.choice([True, False])),
        lambda: api.track_add(f"New{rng.randint(1, 999)}", rng.choice(["synth:pad", "synth:drums"]), project=proj),
        lambda: api.track_add(f"Bus{rng.randint(1, 999)}", kind="bus", project=proj),
        lambda: buses and api.track_update(rng.choice(midi), project=proj, sends=[{"to": rng.choice(buses), "db": -10}]),
        lambda: buses and api.track_update(rng.choice(midi), project=proj, output=rng.choice(buses)),
        lambda: len(names) > 2 and api.track_remove(rng.choice(names), project=proj),
        lambda: api.track_duplicate(rng.choice(midi), project=proj),
        lambda: api.track_move(rng.choice(names), rng.randint(0, len(names) - 1), project=proj),
        lambda: api.fx_add(rng.choice(names), rng.choice(["builtin:eq", "builtin:reverb", "builtin:compressor"]), project=proj),
        lambda: api.harmony(rng.choice(["Dm", "F", "Gm7", "A7"]), at=f"{bar}:1", end=f"{bar + 1}:1", project=proj),
        lambda: api.markers(project=proj, add=[{"at": f"{bar}:1", "name": f"S{bar}"}]),
        lambda: api.project_set(project=proj, tempo=rng.choice([90, 100, 110]), key=rng.choice(["D minor", "F major"])),
        lambda: api.project_set(project=proj, loop=f"1:1-{bar + 4}:1"),
        lambda: api.cc(rng.choice(midi), 1, f"{bar}:1", f"{bar + 1}:1", from_value=20, to_value=110, project=proj),
        lambda: api.automation(rng.choice(names), "volume_db", [[0, -6], [8, 0]], project=proj),
        lambda: api.notes_edit(rng.choice(midi), "transpose", project=proj, semitones=rng.choice([-12, 12, 7])),
        lambda: api.notes_edit(rng.choice(midi), "humanize", project=proj),
        lambda: api.arrange("copy", "1:1", "3:1", to=f"{bar + 2}:1", project=proj),
        lambda: api.arrange("delete", f"{bar}:1", f"{bar + 1}:1", project=proj),
        lambda: api.fx_add("master", "builtin:eq", project=proj),
        lambda: api.project_set(project=proj, brief={"mood": rng.choice(["dark", "bright"])}),
    ]
    for _ in range(rng.randint(1, 7)):
        try:
            rng.choice(choices)()
        except Exception as e:  # invalid random op: fine
            pass

bad = 0
for seed in ([int(sys.argv[2])] if len(sys.argv) > 2 else range(int(sys.argv[1]) if len(sys.argv) > 1 else 150)):
    rng = random.Random(seed)
    song = os.path.join(home, f"s{seed}")
    api.project_new(f"S{seed}", folder=song, tempo=96, key="D minor")
    api.track_add("Piano", "synth:pad", project=song); api.track_add("Bass", "synth:pad", project=song); api.track_add("Hall", kind="bus", project=song)
    api.write("Piano", "D4:q F4 A4 D5 | C4:q E4 G4 C5 | Bb3:q D4 F4 Bb4 | A3:q C#4 E4 A4", project=song)
    api.write("Bass", "D2:w | C2:w | Bb1:w | A1:w", project=song)
    api.harmony("Dm C Bb A", at="1:1", project=song)
    ops(rng, song)
    base = doc(song)
    # 1. session unchanged: merged == take
    t = takes.take_new(use=False, session=song)
    ops(rng, t["project"])
    src = doc(t["project"])
    merged, conflicts, notes = takes.merge(base, src, base)
    if conflicts or norm(merged) != norm(src):
        bad += 1; print("SEED", seed, "take-only", conflicts[:2], notes[:2])
        a, b = norm(merged), norm(src)
        for k in a:
            if a[k] != b.get(k):
                if k == "tracks":
                    print("   order", [x["id"] for x in a[k]], [x["id"] for x in b[k]])
                    for x in a[k]:
                        y = next((y for y in b[k] if y["id"] == x["id"]), None)
                        if x != y: print("   track", x["id"], {kk: (x.get(kk), (y or {}).get(kk)) for kk in set(x) | set(y or {}) if x.get(kk) != (y or {}).get(kk) and kk != "clips"}, "clips differ" if x.get("clips") != (y or {}).get("clips") else "")
                else: print("  ", k, a[k], b.get(k))
    # 2. take unchanged: merged == session
    ops(rng, song)
    cur = doc(song)
    merged, conflicts, notes = takes.merge(base, base, cur)
    if conflicts or norm(merged) != norm(cur):
        bad += 1; print("SEED", seed, "session-only", conflicts[:2])
        a, b = norm(merged), norm(cur)
        for k in a:
            if a[k] != b.get(k):
                if k == "tracks":
                    print("   order", [x["id"] for x in a[k]], [x["id"] for x in b[k]])
                    for x in a[k]:
                        y = next((y for y in b[k] if y["id"] == x["id"]), None)
                        if x != y: print("   track", x["id"], {kk: (x.get(kk), (y or {}).get(kk)) for kk in set(x) | set(y or {}) if x.get(kk) != (y or {}).get(kk) and kk != "clips"}, [(c1, c2) for c1, c2 in zip(x.get("clips", []), (y or {}).get("clips", [])) if c1 != c2][:1])
                else: print("  ", k, a[k], b.get(k))
    # 3. both changed: never crashes, result loads, ids unique, refs valid; and resolving with either side works
    for prefer in (None, "src", "dst"):
        merged, conflicts, notes = takes.merge(base, src, cur, prefer)
        p = Project.from_json(merged)
        ids = [x.id for x in p.tracks] + [c.id for x in p.tracks for c in x.clips]
        assert len(ids) == len(set(ids)), (seed, "dup ids")
        tid = {x.id for x in p.tracks}
        for x in p.tracks:
            assert x.output == "master" or x.output in tid, (seed, "output")
            assert all(s["to"] in tid for s in x.sends), (seed, "send")
        assert merged["next_id"] > max([0] + [int(''.join(ch for ch in i if ch.isdigit()) or 0) for i in ids]), (seed, "next_id")
    # 4. sync then accept == accept (when no conflicts)
    m1, c1, _ = takes.merge(base, src, cur)
    if not c1:
        synced, c2, _ = takes.merge(base, cur, src, remap="dst")
        m2, c3, _ = takes.merge(cur, synced, cur)
        if c2 or c3 or norm(m2)["tracks"] != norm(synced)["tracks"]:
            bad += 1; print("SEED", seed, "sync-then-accept", c2[:1], c3[:1])
        a, b = norm(m1), norm(m2)
        strip = lambda d: json.dumps({**d, "tracks": sorted([{**t, "id": 0, "output": 0, "sends": 0, "clips": sorted(json.dumps({**c, "id": 0}, sort_keys=True) for c in t["clips"]), "fx": 0} for t in d["tracks"]], key=lambda t: t["name"])}, sort_keys=True)
        if strip(a) != strip(b):
            bad += 1; print("SEED", seed, "sync path differs from direct accept")
            ja, jb = json.loads(strip(a)), json.loads(strip(b))
            for k in ja:
                if ja[k] != jb[k]:
                    if k != "tracks": print("   ", k, ja[k], jb[k])
                    else:
                        print("   names", [x["name"] for x in ja[k]], [x["name"] for x in jb[k]])
                        for x, y in zip(ja[k], jb[k]):
                            if x != y:
                                for c1, c2 in zip(x["clips"], y["clips"]):
                                    print("     clip", {kk: (c1.get(kk), c2.get(kk)) for kk in c1 if kk != "notes" and c1.get(kk) != c2.get(kk)})
                                    n1, n2 = [tuple(n) for n in c1["notes"]], [tuple(n) for n in c2["notes"]]
                                    print("     only direct", sorted(set(n1) - set(n2))[:8], "only sync", sorted(set(n2) - set(n1))[:8], len(n1), len(n2))
print("bad", bad)
