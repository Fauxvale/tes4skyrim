"""Audit CREA -> converted-creature-project coverage for a plugin.

Any CREA whose model folder has no project entry falls back to
skyrim_overrides.resolve_creature_race, i.e. it ships as a BASE SKYRIM
creature (frostbite spider, Nord, ...) instead of the converted actor. This
splits the misses into the two causes that have actually bitten:

  * "folder owned by MASTER project" — the plugin re-uses a master's creature
    meshes, so only the master's creature_projects.json has the entry
    (creature_races._load_projects now inherits these).
  * "in neither" — the folder was never converted at all, usually because it
    is nested somewhere other than meshes\\creatures (creature_pipeline now
    walks the whole mesh tree for skeleton.nif + .kf).

Usage:
    python tools/crea_project_gap.py <plugin> [master ...]
e.g.
    python tools/crea_project_gap.py Morrowind_ob.esm Oblivion.esm
"""
import json
import re
import sys
import collections
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def folders(name):
    txt = (ROOT / "export" / name / "CREA.txt").read_text(
        encoding="utf-8", errors="replace")
    out = []
    for r in txt.split("---RECORD_BEGIN---")[1:]:
        m = re.search(r"^Model\.MODL=(.*)$", r, re.M)
        ed = re.search(r"^EditorID=(.*)$", r, re.M)
        model = (m.group(1) if m else "").replace("/", "\\")
        model = model.replace("\\\\", "\\")
        parts = [p for p in model.lower().split("\\") if p]
        out.append((parts[-2] if len(parts) >= 2 else "",
                    ed.group(1) if ed else "?"))
    return out


def projects(name):
    p = ROOT / "export" / name / "creature_projects.json"
    return set(json.loads(p.read_text())) if p.exists() else set()


plugin = sys.argv[1] if len(sys.argv) > 1 else "Morrowind_ob.esm"
masters = sys.argv[2:] or ["Oblivion.esm"]

recs = folders(plugin)
own = projects(plugin)
mproj = set()
for m in masters:
    mproj |= projects(m)

c = collections.Counter(f for f, _ in recs)
local = sum(n for f, n in c.items() if f in own)
in_master = sum(n for f, n in c.items() if f not in own and f in mproj)
neither = sum(n for f, n in c.items() if f not in own and f not in mproj)

print(f"{plugin}: {len(recs)} CREA records, {len(own)} own projects")
print(f"  mapped by own project                : {local}")
print(f"  folder owned by MASTER project (LOST): {in_master}")
print(f"  in neither                           : {neither}")
print("\nmaster-owned folders referenced by this plugin:")
for f, n in sorted(((f, n) for f, n in c.items()
                    if f not in own and f in mproj), key=lambda x: -x[1]):
    print(f"  {f:24s} {n:4d} CREA")
print("\nfolders in neither (top 15):")
for f, n in sorted(((f, n) for f, n in c.items()
                    if f not in own and f not in mproj),
                   key=lambda x: -x[1])[:15]:
    print(f"  {f:24s} {n:4d}")
