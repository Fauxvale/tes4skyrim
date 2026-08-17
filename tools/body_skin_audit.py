"""Where does the body-skin texture name disagree with the skin bones?

Oblivion bakes the wearer's skin into a wearable. The converter strips it and
splices Skyrim body geometry back, choosing WHICH body by a keyword in the
texture path (skin_replacement._SKIN_TEX_TO_BODY_NIF).

A mod author who textures a torso with a foot texture defeats that. The bones
cannot lie the same way: a shape weighted to Spine/Clavicle/Neck is a torso
whatever its texture is called. Every disagreement here is a wearable that
loses body geometry it should keep, leaving see-through gaps between the
armour plates.

Usage:
  python tools/body_skin_audit.py [plugin] [--max N] [--workers N] [--all]
"""
import os
import sys
from collections import Counter
from multiprocessing import Pool, cpu_count

sys.path.insert(0, '.')

# Only bones NOTHING but a torso is weighted to. A gauntlet legitimately
# reaches the forearm and a boot the calf, so those prove nothing; spine,
# clavicle and neck do. Keeping this narrow is the point — the audit must
# report defects, not naming conventions.
TORSO_BONES = ('spine', 'clavicle', 'neck')

# Mirrors skin_replacement._SKIN_TEX_TO_BODY_NIF, in the same order.
TEX_ORDER = (('upperbody', 'torso'), ('leg', 'torso'), ('hand', 'hand'),
             ('foot', 'foot'), ('underwear', 'torso'))


def _tex_class(tex: str):
    for kw, cls in TEX_ORDER:
        if kw in tex:
            return cls
    return None


def _bone_class(bones):
    b = ' '.join(bones).lower()
    return 'torso' if any(k in b for k in TORSO_BONES) else None


def scan(path):
    from asset_convert import pyffi_monkey_patch      # noqa: F401
    from pyffi.formats.nif import NifFormat
    out = []
    try:
        d = NifFormat.Data()
        with open(path, 'rb') as f:
            d.inspect(f)
            d.read(f)
    except Exception:
        return out
    for b in d.blocks:
        if not isinstance(b, (NifFormat.NiTriShape, NifFormat.NiTriStrips)):
            continue
        sk = getattr(b, 'skin_instance', None)
        if sk is None or b.data is None:
            continue
        tex = ''
        for p in getattr(b, 'properties', []):
            if isinstance(p, NifFormat.NiTexturingProperty):
                bt = p.base_texture
                if bt is not None and bt.source is not None:
                    tex = bytes(bt.source.file_name).decode(
                        'latin-1', 'replace').lower().replace('/', '\\')
                break
        if 'characters\\' not in tex:
            continue                      # not body skin at all
        tc = _tex_class(tex)
        bones = [bytes(x.name).rstrip(b'\x00').decode('latin-1', 'replace')
                 for x in sk.bones if x is not None]
        bc = _bone_class(bones)
        # The defect: a torso spliced from the hands or the feet mesh, which
        # contain no torso, so the geometry is dropped and never replaced.
        if bc == 'torso' and tc in ('hand', 'foot'):
            nm = bytes(b.name).rstrip(b'\x00').decode('latin-1', 'replace')
            out.append((path, nm, b.data.num_vertices, tex, tc, bc))
    return out


def main():
    from asset_convert import wearable_plan as wp
    from pathlib import Path
    plugin = 'Nehrim.esm'
    mx, workers, show_all = 0, max(1, cpu_count() - 1), '--all' in sys.argv
    a = sys.argv[1:]
    for i, x in enumerate(a):
        if x == '--max':
            mx = int(a[i + 1])
        elif x == '--workers':
            workers = int(a[i + 1])
        elif not x.startswith('--') and (i == 0 or a[i - 1] not in ('--max', '--workers')):
            plugin = x

    ed = Path('export') / plugin
    meshes = ed / 'meshes'
    plan = wp.build_plan(ed)
    paths = []
    for rel, mask in sorted(plan.items()):
        if not mask & wp.WORN:
            continue
        p = meshes / rel
        if p.is_file():
            paths.append(str(p))
    if mx:
        paths = paths[:mx]
    print(f'scanning {len(paths)} worn source meshes with {workers} workers',
          flush=True)

    hits = []
    with Pool(workers) as pool:
        for n, res in enumerate(pool.imap_unordered(scan, paths, chunksize=8), 1):
            hits += res
            if n % 200 == 0:
                print(f'  {n}/{len(paths)} ... {len(hits)} mismatches so far',
                      flush=True)

    print(f'\nbody-skin shapes whose TEXTURE and BONES disagree: {len(hits)}')
    print(f'affected meshes: {len({h[0] for h in hits})}')
    print('  ', Counter(f'{t}->{b}' for *_x, t, b in hits).most_common())
    for path, nm, nv, tex, tc, bc in (hits if show_all else hits[:20]):
        print(f'  tex says {tc:5} bones say {bc:5} v={nv:<6} "{nm}" '
              f'{os.path.relpath(path, str(meshes))}')
    if not show_all and len(hits) > 20:
        print(f'  ... ({len(hits) - 20} more, pass --all)')


if __name__ == '__main__':
    main()
