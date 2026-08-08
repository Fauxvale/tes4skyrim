"""Do the ARMA model paths in a converted plugin resolve to real files?

Every miss is classified, because the two classes need different answers:

  GATE   the source NIF exists and the plugin wears it, but the converter never
         wrote the variant the ARMA names.  A converter bug.
  NOSRC  no source NIF anywhere in the export.  The plugin references a mesh
         that was never shipped — broken in the original game too, nothing to
         convert.

Usage: python tools/arma_mesh_check.py [plugin] [--all]
"""
import os
import sys

sys.path.insert(0, 'tools')
sys.path.insert(0, '.')
from tes5_esm_reader import read_tes5_file          # noqa: E402
from voice_audit import _get, _zstring              # noqa: E402

plugin = 'Nehrim.esm'
show_all = '--all' in sys.argv
for a in sys.argv[1:]:
    if not a.startswith('--'):
        plugin = a

ROOT = f'output/{plugin}/meshes/'
SRC = f'export/{plugin}/meshes/'


def _source_for(rel: str) -> str:
    """Strip the tes4/ output prefix and the _0/_1 suffix to get the source."""
    s = rel[5:] if rel.lower().startswith('tes4/') else rel
    stem, ext = os.path.splitext(s)
    if stem.endswith('_0') or stem.endswith('_1'):
        stem = stem[:-2]
    return stem + ext


h, recs, _ = read_tes5_file(f'output/{plugin}/{plugin}')
missing, present, total = [], 0, 0
halfpair = []
for r in recs:
    if r.type != 'ARMA':
        continue
    for s in r.subrecords:
        if s.type not in ('MOD2', 'MOD3'):
            continue
        rel = _zstring(s.data).replace('\\', '/')
        total += 1
        if os.path.exists(ROOT + rel):
            present += 1
        else:
            e = _get(r, 'EDID')
            src = _source_for(rel)
            kind = 'GATE' if os.path.exists(SRC + src) else 'NOSRC'
            missing.append((kind, _zstring(e.data) if e else '?', rel))
        # The record names <name>_1 and the ENGINE derives <name>_0 — nothing
        # in the plugin references the partner, so a missing _0 is invisible to
        # a path check and the weight lerp has nothing to interpolate from.
        stem, ext = os.path.splitext(rel)
        if stem.endswith('_1'):
            partner = stem[:-2] + '_0' + ext
            if os.path.exists(ROOT + rel) and not os.path.exists(ROOT + partner):
                e = _get(r, 'EDID')
                halfpair.append((_zstring(e.data) if e else '?', partner))

gate = [m for m in missing if m[0] == 'GATE']
nosrc = [m for m in missing if m[0] == 'NOSRC']

print(f'ARMA model paths checked (MOD2+MOD3): {total}')
print(f'  file present: {present}')
print(f'  MISSING:      {len(missing)}   (GATE {len(gate)} / NOSRC {len(nosrc)})')
print(f'  HALF PAIR (_1 present, engine-derived _0 absent): {len(halfpair)}')
for edid, rel in halfpair[:10]:
    print(f'    {edid:32} {rel}')
for kind, edid, rel in (missing if show_all else missing[:15]):
    print(f'    {kind:5} {edid:32} {rel}')
if not show_all and len(missing) > 15:
    print(f'    ... ({len(missing) - 15} more, pass --all)')
