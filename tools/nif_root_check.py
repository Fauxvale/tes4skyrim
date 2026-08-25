"""Diff the fast header-only NIF root check against the full PyFFI parse.

`lod_gen._lod_mesh_is_safe` decides whether a mesh may be listed for LODGen.
LODGenx64 casts every LOD mesh's root block to NiNode *without checking*, and
one bad root takes down that worldspace's entire object LOD — so the screen is
load-bearing.  It used to answer the question with a full `NifFormat.Data.read`
(~84 ms per mesh, 99.6% of `write_lodgen_input`); it now reads only the NIF
header (~0.3 ms).

A hand-rolled binary header parser is exactly the kind of thing that fails
silently: a wrong "unsafe" drops that object from LOD with no error, a wrong
"safe" can abort a worldspace.  The first version of it omitted
`user_version_2` and the export-info strings and answered False for **600 of
600** meshes — every object would have vanished from LOD.  This tool is what
caught that, so run it after ANY edit to `_root_is_ninode`.

    python tools/nif_root_check.py                       # 1200 meshes
    python tools/nif_root_check.py --count 3000
    python tools/nif_root_check.py --meshes "export/Nehrim.esm/meshes"

Exit code is non-zero on any disagreement, so it works in CI.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import asset_convert.lod_gen as lg  # noqa: E402
from asset_convert.lod_far_gen import NifFormat  # noqa: E402


def slow_root_is_ninode(full: Path) -> bool:
    """The original check: full parse, then test the first root's class."""
    try:
        data = NifFormat.Data()
        with open(full, 'rb') as fh:
            data.read(fh)
        roots = data.roots
        if not roots or roots[0] is None:
            return False
        return isinstance(roots[0], NifFormat.NiNode)
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--meshes', default='output/Oblivion.esm/meshes',
                    help='mesh tree to sample (default: the converted output)')
    ap.add_argument('--count', type=int, default=1200)
    ap.add_argument('--quiet', action='store_true',
                    help='only print the verdict line')
    a = ap.parse_args()

    meshes = Path(a.meshes)
    if not meshes.is_dir():
        print(f"no mesh tree at {meshes}", file=sys.stderr)
        return 2

    nifs = []
    for p in meshes.rglob('*.nif'):
        nifs.append(p)
        if len(nifs) >= a.count:
            break
    nifs.sort()
    if not a.quiet:
        print(f"checking {len(nifs)} meshes under {meshes}")

    bad = []
    t_fast = t_slow = 0.0
    for p in nifs:
        t0 = time.perf_counter()
        fast = lg._root_is_ninode(p)
        t_fast += time.perf_counter() - t0
        t0 = time.perf_counter()
        slow = slow_root_is_ninode(p)
        t_slow += time.perf_counter() - t0
        if fast != slow:
            bad.append((p, fast, slow))

    if not a.quiet:
        print(f"fast {t_fast:7.2f}s   slow {t_slow:7.2f}s   "
              f"speedup {t_slow / max(t_fast, 1e-9):.0f}x")
    print(f"disagreements: {len(bad)} of {len(nifs)}")
    for p, fast, slow in bad[:15]:
        print(f"  {p.relative_to(meshes)}: fast={fast} slow={slow}")
    print("MATCH" if not bad else "*** MISMATCH ***")
    return 0 if not bad else 1


if __name__ == '__main__':
    sys.exit(main())
