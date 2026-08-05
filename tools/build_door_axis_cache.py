"""Build the door threshold cache from the ORIGINAL door meshes' CLOSED pose.

The navmesh orients, sizes and positions every Door Triangle from this cache:
per model, the threshold axis, the doorway width, the doorway CENTRE relative
to the REFR pivot, and the closed slab's base height — all measured from the
export (original Oblivion) NIF with the 'Close' controller sequence's final
key values applied to the animated nodes (see
asset_convert.collision_extract.door_closed_geometry).

The closed pose is the only correct source: several doors are STORED mid-open
(idgate01's leaves rest 60+ units from the doorway and swing 90 degrees shut),
and the previously-used converted collision baked those transforms wrong,
rotating or offsetting the Door Triangle in-game.  Frames/arches/static fence
sections carry no animation keys and never widen the doorway.

Usage:
    python tools/build_door_axis_cache.py [--plugin Oblivion.esm] [--workers N]

Writes <export>/<plugin>/door_panel_axis_cache.json:
    { "tes4/<model>.nif": [axis, width, centre_x, centre_y, z_min], ... }
in WORLD units.  Models whose closed pose is thin in Z (trapdoors/hatches —
they swing about a horizontal axis and have no vertical threshold) are
omitted; the navmesh skips non-teleport doors that are missing.
"""

import argparse
import json
import os
import re
import sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def door_models(door_txt):
    """Normalised model paths ('architecture/anvil/x.nif') of every DOOR base."""
    models = set()
    with open(door_txt, encoding='utf-8', errors='replace') as fh:
        txt = fh.read()
    for rec in txt.split('---RECORD_BEGIN---'):
        m = re.search(r'Model\.MODL=(.+)', rec)
        if not m:
            continue
        s = m.group(1).strip().lower()
        s = s.replace('\\', '/')
        s = re.sub(r'/+', '/', s)
        models.add(s)
    return models


def _classify(args):
    path, key = args
    from asset_convert.collision_extract import (door_closed_geometry,
                                                 read_nif_data)
    try:
        return key, door_closed_geometry(read_nif_data(path))
    except Exception:
        return key, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--plugin', default='Oblivion.esm')
    ap.add_argument('--export-dir', default='export')
    ap.add_argument('--workers', type=int, default=None)
    a = ap.parse_args()

    door_txt = os.path.join(a.export_dir, a.plugin, 'DOOR.txt')
    if not os.path.exists(door_txt):
        print(f"DOOR.txt not found: {door_txt}")
        return 1
    # ORIGINAL meshes, extracted from the LE BSAs by the export stage.
    root = os.path.join(a.export_dir, a.plugin, 'meshes')

    models = door_models(door_txt)
    jobs = []
    missing = 0
    for mdl in sorted(models):
        p = os.path.join(root, *mdl.split('/'))
        if os.path.exists(p):
            jobs.append((p, 'tes4/' + mdl))
        else:
            missing += 1

    workers = a.workers or max(1, (os.cpu_count() or 2) - 1)
    out = {}
    skipped = 0
    print(f"Classifying {len(jobs)} door meshes ({workers} workers)...")
    with ProcessPoolExecutor(max_workers=workers) as ex:
        for key, res in ex.map(_classify, jobs, chunksize=8):
            if res is None:
                skipped += 1
            else:
                axis, width, cx, cy, zmin = res
                out[key] = [axis, round(width, 2), round(cx, 2),
                            round(cy, 2), round(zmin, 2)]

    dest = os.path.join(a.export_dir, a.plugin, 'door_panel_axis_cache.json')
    with open(dest, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, indent=0, sort_keys=True)

    ny = sum(1 for v in out.values() if v[0] == 'Y')
    ws = sorted(v[1] for v in out.values())
    print(f"  models={len(models)} classified={len(out)} "
          f"(threshold Y={ny} X={len(out) - ny}) "
          f"no-threshold={skipped} missing-from-export={missing}")
    if ws:
        print(f"  doorway width: min={ws[0]:.0f} median={ws[len(ws) // 2]:.0f} "
              f"max={ws[-1]:.0f}")
    print(f"  wrote {dest}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
