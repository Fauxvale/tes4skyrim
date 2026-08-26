"""Audit converted Skyrim NIFs for NiTriStrips, which Skyrim cannot render.

Skyrim has no NiTriStrips renderer: a converted mesh that still contains one
fails to load and the engine draws the red missing-mesh triangle in its place.
Vanilla census backs this up -- across `references/Skyrim Meshes` the only
NiTriStrips-family blocks are `bhkNiTriStripsShape` (collision, a different
type), and all 107 `NiPSysMeshEmitter.emitter_meshes` targets are NiTriShape.

The usual cause is a SECOND reference to a geometry block that the strips ->
NiTriShape rewrite did not follow (mesh emitters, AV object palettes), leaving
the original strips block reachable and therefore re-serialized.

Usage:
    python tools/validate/nif_strips_audit.py output/Oblivion.esm/meshes
    python tools/validate/nif_strips_audit.py <dir> --max 500 --workers 8

Exits non-zero when any offending file is found, so it can gate a build.
"""
import argparse
import os
import sys
from concurrent.futures import ProcessPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import asset_convert.pyffi_monkey_patch  # noqa: F401,E402
from pyffi.formats.nif import NifFormat  # noqa: E402

# Only the header block-type table is needed to decide whether a file is worth
# a full parse, and that keeps a whole-tree sweep affordable.
HEADER_BYTES = 65536


def _quick_has_strips(path):
    try:
        with open(path, 'rb') as f:
            head = f.read(HEADER_BYTES)
    except OSError:
        return False
    # bhkNiTriStripsShape is collision and legal; require the bare type name.
    return b'NiTriStrips' in head


def _name_of(block):
    n = getattr(block, 'name', b'')
    return n.decode('latin-1', 'replace') if isinstance(n, bytes) else str(n)


def check_one(path):
    """Return (path, strip_names, bad_emitters) -- empty lists when clean."""
    if not _quick_has_strips(path):
        return None
    try:
        data = NifFormat.Data()
        with open(path, 'rb') as f:
            data.read(f)
    except Exception as exc:
        return (path, ['<unreadable: %s>' % type(exc).__name__], [])

    strips, bad_emit = [], []
    for block in data.blocks:
        if isinstance(block, NifFormat.NiTriStrips):
            strips.append(_name_of(block))
        elif isinstance(block, NifFormat.NiPSysMeshEmitter):
            for mesh in block.emitter_meshes:
                if mesh is not None and not isinstance(mesh, NifFormat.NiTriShape):
                    bad_emit.append('%s -> %s' % (_name_of(block),
                                                  mesh.__class__.__name__))
    if strips or bad_emit:
        return (path, strips, bad_emit)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('root', help='converted NIF file or directory')
    ap.add_argument('--max', type=int, default=0, help='stop after N files (0=all)')
    ap.add_argument('--workers', type=int, default=8, help='process count')
    args = ap.parse_args()

    if os.path.isfile(args.root):
        files = [args.root]
    else:
        files = []
        for dirpath, _dirnames, filenames in os.walk(args.root):
            for fn in filenames:
                if fn.lower().endswith('.nif'):
                    files.append(os.path.join(dirpath, fn))
        files.sort()
    if args.max:
        files = files[:args.max]

    print('scanning %d NIFs...' % len(files))
    bad = []
    workers = max(1, args.workers)
    if workers == 1:
        results = (check_one(f) for f in files)
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(check_one, files, chunksize=32))
    for res in results:
        if res:
            bad.append(res)

    for path, strips, emit in bad:
        print('  %s' % path)
        if strips:
            print('      NiTriStrips: %d %s' % (len(strips), strips[:5]))
        if emit:
            print('      bad emitter targets: %s' % emit[:5])

    print('\n%d/%d files contain NiTriStrips or a non-NiTriShape emitter target'
          % (len(bad), len(files)))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
