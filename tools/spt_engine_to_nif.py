#!/usr/bin/env python3
"""Convert ground-truth SpeedTree geometry (from native/dist/) to a NIF.

`native/dist/spt_engine_dump.exe` drives the SpeedTreeRT 4.x code
statically linked into Oblivion.exe and writes the engine's own vertex buffers
to a `.bin`.  This turns that dump into a Skyrim-format NIF, so the ENGINE's
branch geometry can be inspected and compared against
`asset_convert/spt_generator.py` output byte-for-byte instead of by eye.

The .bin layout (written by spt_engine_dump.cpp):

    'SPTG'                     4 bytes
    vertexCount   uint32
    coords        float32[n*3]      (engine world units)
    normals       float32[n*3]
    texcoords0    float32[n*2]
    stripCount    uint32
    per strip:  indexCount uint32, indices uint32[indexCount]

Only vertices referenced by a strip carry valid data -- the engine leaves the
tail of each buffer uninitialised -- so the converter re-indexes to the used
set before writing.

Usage:
    python tools/spt_engine_to_nif.py temp/spt_gt/oak.bin out.nif [--texture X]
"""
from __future__ import annotations

import argparse
import io
import struct
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from asset_convert.spt_converter import _make_shape, NifFormat  # noqa: E402


def read_dump(path: Path):
    """Parse a spt_engine_dump .bin into (coords, normals, uvs, strips)."""
    d = path.read_bytes()
    if d[:4] != b'SPTG':
        raise SystemExit(f'{path}: not an SPTG dump')
    n = struct.unpack_from('<I', d, 4)[0]
    off = 8
    co = np.frombuffer(d, np.float32, n * 3, off).reshape(n, 3).copy()
    off += n * 12
    no = np.frombuffer(d, np.float32, n * 3, off).reshape(n, 3).copy()
    off += n * 12
    uv = np.frombuffer(d, np.float32, n * 2, off).reshape(n, 2).copy()
    off += n * 8
    ns = struct.unpack_from('<I', d, off)[0]
    off += 4
    strips = []
    for _ in range(ns):
        cnt = struct.unpack_from('<I', d, off)[0]
        off += 4
        strips.append(np.frombuffer(d, np.uint32, cnt, off).astype(np.int64))
        off += cnt * 4
    return co, no, uv, strips


def strips_to_triangles(strips) -> np.ndarray:
    """Expand triangle strips, dropping the degenerate joins and keeping the
    alternating winding a strip implies."""
    tris = []
    for idx in strips:
        for k in range(len(idx) - 2):
            a, b, c = int(idx[k]), int(idx[k + 1]), int(idx[k + 2])
            if a == b or b == c or a == c:
                continue                      # degenerate: strip stitching
            tris.append((a, b, c) if k % 2 == 0 else (a, c, b))
    return np.asarray(tris, np.int32).reshape(-1, 3)


def build_nif(co, no, uv, tris, texture: str, name: str) -> bytes:
    # Re-index to the vertices the triangles actually reference: the engine
    # leaves unused tail entries uninitialised (NaN), which would otherwise
    # blow up the bounding sphere and render as garbage.
    used = np.unique(tris)
    remap = np.full(len(co), -1, np.int32)
    remap[used] = np.arange(len(used), dtype=np.int32)

    v = co[used]
    nrm = no[used]
    t = uv[used]
    f = remap[tris]

    # Guard against any residual non-finite value.
    bad = ~np.isfinite(v).all(1)
    if bad.any():
        v[bad] = 0.0
    bad_n = ~np.isfinite(nrm).all(1)
    if bad_n.any():
        nrm[bad_n] = (0.0, 0.0, 1.0)
    t[~np.isfinite(t)] = 0.0

    colors = np.ones((len(v), 4), np.float32)
    shape = _make_shape(name.encode(), v, nrm, t, colors, f, texture, '')

    root = NifFormat.NiNode()
    root.name = name.encode()
    root.num_children = 1
    root.children.update_size()
    root.children[0] = shape

    data = NifFormat.Data()
    data.version = 0x14020007
    data.user_version = 12
    data.user_version_2 = 83
    data.header.endian_type = 1
    data.roots = [root]
    buf = io.BytesIO()
    data.write(buf)
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('dump', type=Path)
    ap.add_argument('out', type=Path)
    ap.add_argument('--texture', default='textures/trees/treeenglishoakbark.dds')
    ap.add_argument('--name', default='EngineBranches')
    a = ap.parse_args()

    co, no, uv, strips = read_dump(a.dump)
    tris = strips_to_triangles(strips)
    used = np.unique(tris)
    print(f'{a.dump.name}: {len(co)} verts, {len(strips)} strips, '
          f'{len(tris)} triangles, {len(used)} referenced verts')
    print(f'  bbox min {co[used].min(0).round(2)}  max {co[used].max(0).round(2)}')

    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_bytes(build_nif(co, no, uv, tris, a.texture, a.name))
    print(f'  wrote {a.out}  ({a.out.stat().st_size} bytes)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
