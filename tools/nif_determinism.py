"""Check that NIF conversion is reproducible across PYTHONHASHSEED values.

PyFFI deduplicated the header string table with ``list(set(...))`` over bytes
objects, whose hash is randomised per process, so the same source NIF converted
to different bytes on every run — same blocks and geometry, different string
order and therefore different NiStringRef indices.  ``pyffi_monkey_patch``
Patch 10 makes that dedupe insertion-ordered; this tool is the check that it is
still working (and the check for any future ordering regression).

    python tools/nif_determinism.py                 # 25 meshes, 2 seeds
    python tools/nif_determinism.py --count 60 --seeds 0,1,999
    python tools/nif_determinism.py --plugin Nehrim.esm

Exit code is non-zero if any mesh differs between seeds, so it works in CI.
"""
import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_CHILD = r'''
import sys, hashlib, json
sys.path.insert(0, r"{root}")
from pathlib import Path
from asset_convert import nif_converter as nc
src = Path(r"{src}")
nc._pyffi_capture_init()
out = {{}}
import tempfile
tmp = Path(tempfile.mkdtemp())
for i, rel in enumerate(json.loads(sys.argv[1])):
    dst = tmp / ("%d.nif" % i)
    try:
        nc.convert_nif(str(src / rel), str(dst), src_meshes_dir=str(src))
    except Exception as exc:
        out[rel] = "EXC:" + exc.__class__.__name__
        continue
    out[rel] = (hashlib.sha1(dst.read_bytes()).hexdigest()
                if dst.exists() else "MISSING")
print(json.dumps(out))
'''


def main():
    import json
    import random

    ap = argparse.ArgumentParser()
    ap.add_argument('--plugin', default='Oblivion.esm')
    ap.add_argument('--count', type=int, default=25)
    ap.add_argument('--seed', type=int, default=7, help='mesh-sampling seed')
    ap.add_argument('--seeds', default='0,12345',
                    help='comma-separated PYTHONHASHSEED values to compare')
    a = ap.parse_args()

    src = ROOT / 'export' / a.plugin / 'meshes'
    if not src.is_dir():
        print(f"no mesh tree at {src}", file=sys.stderr)
        return 2

    from asset_convert import nif_converter as nc
    nifs = []
    for p in src.rglob('*.nif'):
        rel = [x.lower() for x in p.relative_to(src).parts]
        if any(s in rel for s in nc.SKIP_PATHS):
            continue
        nifs.append(str(p.relative_to(src)).replace('\\', '/'))
    nifs.sort()
    rnd = random.Random(a.seed)
    sample = sorted(rnd.sample(nifs, min(a.count, len(nifs))))

    hash_seeds = [s.strip() for s in a.seeds.split(',') if s.strip()]
    print(f"{len(sample)} meshes x {len(hash_seeds)} hash seeds "
          f"({a.plugin})")

    results = []
    for hs in hash_seeds:
        env = dict(os.environ, PYTHONHASHSEED=hs)
        r = subprocess.run(
            [sys.executable, '-c',
             _CHILD.format(root=str(ROOT), src=str(src)), json.dumps(sample)],
            env=env, cwd=str(ROOT), capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  seed {hs}: child failed\n{r.stderr[-2000:]}",
                  file=sys.stderr)
            return 2
        results.append(json.loads(r.stdout.strip().splitlines()[-1]))
        print(f"  seed {hs}: {len(results[-1])} converted")

    base = results[0]
    bad = []
    for rel in sample:
        vals = {res.get(rel) for res in results}
        if len(vals) != 1:
            bad.append((rel, [res.get(rel) for res in results]))

    print(f"\nmeshes differing across seeds: {len(bad)} of {len(sample)}")
    for rel, vals in bad[:15]:
        print(f"  {rel}")
        for hs, v in zip(hash_seeds, vals):
            print(f"    seed {hs}: {v}")
    print("DETERMINISTIC" if not bad else "*** NON-DETERMINISTIC ***")
    return 0 if not bad else 1


if __name__ == '__main__':
    sys.exit(main())
