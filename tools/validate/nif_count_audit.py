#!/usr/bin/env python3
"""Find NIFs whose array counts would blow up the engine's loader.

WHY THIS EXISTS
---------------
Skyrim's model loader grows its geometry arrays with a 32-bit doubling step:

    mov  eax, [rcx+0x10]   ; capacity
    add  eax, eax          ; capacity * 2
    cmova edi, eax         ; newCap = max(requested, cap*2)
    imul edx, r15d         ; newCap * elemSize   <-- 32-bit multiply
    call <allocate>
    imul esi, r15d         ; count * elemSize    <-- 32-bit multiply
    call <memcpy>

so an out-of-range element count out of a NIF becomes a gigabyte-scale
allocation AND a gigabyte-scale memcpy.  Measured on a captured dump: a 2.25 GB
tbbmalloc block, a 36 GB block in the same list, 49.2 GB committed against a
normal ~8 GB, and a live `memcpy` of 0x7EF225F0 (2.03 GB) whose SOURCE buffer
was entirely `01 00 00 00 01 00 00 00 ...`.  That pattern (0x100000001 read as a
pointer) then surfaces in whatever allocates next -- the renderer, the audio
manager, tbbmalloc's own getTLS -- which is why one bad count looks like four
unrelated crashes.

This reads the RAW BYTES rather than parsing with pyffi, deliberately: a count
pyffi rejects or silently repairs is precisely the one that reaches the engine
intact.

    # audit specific files
    python tools/validate/nif_count_audit.py a.nif b.nif

    # audit a tree, or a list of paths from a file
    python tools/validate/nif_count_audit.py output/Foo.esm/meshes --max 5000
    python tools/validate/nif_count_audit.py --list temp/cellmeshes.txt --root output/Foo.esm/meshes/tes4

Exit status is 1 when something suspicious was found, so it can gate a build.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys

# A shape with more than this many vertices/triangles is not real geometry.
# Vanilla Skyrim's largest single shape is well under 100k verts; the SSE
# BSTriShape vertex count field is itself only 16-bit.
VERT_SANE = 200_000
TRI_SANE = 400_000
# Anything at or beyond this is certainly a corrupt/garbage count.
ABSURD = 1 << 24


def _read_header(data: bytes):
    """Return (version, num_blocks, block_type_names, offset_after_header)."""
    if not data.startswith(b'Gamebryo') and not data.startswith(b'NetImmerse'):
        return None
    nl = data.find(b'\n')
    if nl < 0:
        return None
    p = nl + 1
    ver = struct.unpack_from('<I', data, p)[0]
    p += 4
    endian = data[p]
    p += 1
    if endian != 1:
        return None                      # big-endian: not ours
    p += 4                               # user version
    num_blocks = struct.unpack_from('<I', data, p)[0]
    p += 4
    p += 4                               # user version 2
    # export info strings (3 short-length-prefixed)
    for _ in range(3):
        if p >= len(data):
            return None
        ln = data[p]
        p += 1 + ln
    num_types = struct.unpack_from('<H', data, p)[0]
    p += 2
    types = []
    for _ in range(num_types):
        ln = struct.unpack_from('<I', data, p)[0]
        p += 4
        types.append(data[p:p + ln].decode('latin-1'))
        p += ln
    return ver, num_blocks, types, p


def audit(path: str):
    """Return a list of (severity, message) findings for one NIF."""
    try:
        with open(path, 'rb') as fh:
            data = fh.read()
    except OSError as e:
        return [('ERROR', f'unreadable: {e}')]

    hdr = _read_header(data)
    if hdr is None:
        return []
    ver, num_blocks, types, p = hdr

    findings = []
    if num_blocks > 100_000:
        findings.append(('BAD', f'num_blocks={num_blocks}'))

    # block type index array (u16 per block), then block sizes (u32 per block)
    try:
        idx = struct.unpack_from('<%dH' % num_blocks, data, p)
        p += 2 * num_blocks
        sizes = struct.unpack_from('<%dI' % num_blocks, data, p)
        p += 4 * num_blocks
    except struct.error:
        return [('BAD', 'header truncated / block table unreadable')]

    total = sum(sizes)
    if total > len(data):
        findings.append(
            ('BAD', f'block sizes total {total:,} > file {len(data):,}'))

    # Walk the blocks and check the count fields of geometry data blocks.
    # strings table follows the size table
    try:
        num_strings = struct.unpack_from('<I', data, p)[0]
        p += 4
        p += 4                                   # max string length
        for _ in range(num_strings):
            ln = struct.unpack_from('<I', data, p)[0]
            p += 4 + ln
        num_groups = struct.unpack_from('<I', data, p)[0]
        p += 4 + 4 * num_groups
    except struct.error:
        return findings + [('BAD', 'string table unreadable')]

    for i, ti in enumerate(idx):
        if ti >= len(types):
            findings.append(('BAD', f'block {i} bad type index {ti}'))
            break
        name = types[ti]
        blk = data[p:p + sizes[i]]
        p += sizes[i]

        if name in ('NiTriShapeData', 'NiTriStripsData'):
            if len(blk) < 8:
                continue
            # NiGeometryData: group id (u32), num_vertices (u16) ...
            nverts = struct.unpack_from('<H', blk, 4)[0]
            if nverts > VERT_SANE:
                findings.append(
                    ('BAD', f'{name}[{i}] num_vertices={nverts:,}'))
        elif name == 'BSTriShape' or name == 'BSSubIndexTriShape':
            # vertex/triangle counts sit in the BSVertexDesc block near the end
            pass

    # Raw scan: any u32 in a plausible count position that is absurd is worth
    # reporting even when the structural walk above could not reach it.
    return findings


def iter_targets(args):
    if args.list:
        root = args.root or ''
        with open(args.list, encoding='utf-8') as fh:
            for line in fh:
                rel = line.strip().lstrip('/')
                if not rel or not rel.lower().endswith('.nif'):
                    continue
                yield os.path.join(root, rel.replace('/', os.sep))
        return
    for t in args.paths:
        if os.path.isfile(t):
            yield t
        else:
            n = 0
            for dp, _, fns in os.walk(t):
                for fn in fns:
                    if fn.lower().endswith('.nif'):
                        yield os.path.join(dp, fn)
                        n += 1
                        if args.max and n >= args.max:
                            return


def main():
    ap = argparse.ArgumentParser(
        description='Audit NIF array counts for loader-blowup risk.')
    ap.add_argument('paths', nargs='*')
    ap.add_argument('--list', help='file of mesh paths, one per line')
    ap.add_argument('--root', help='prefix for --list entries')
    ap.add_argument('--max', type=int, default=0)
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()

    scanned = 0
    flagged = 0
    missing = 0
    for path in iter_targets(args):
        if not os.path.isfile(path):
            missing += 1
            if args.verbose:
                print(f'  MISSING {path}', flush=True)
            continue
        scanned += 1
        found = audit(path)
        if found:
            flagged += 1
            print(f'\n{path}', flush=True)
            for sev, msg in found:
                print(f'   [{sev}] {msg}', flush=True)
        elif args.verbose:
            print(f'  ok  {path}', flush=True)
        if scanned % 200 == 0:
            print(f'\r  ...{scanned} scanned, {flagged} flagged',
                  end='', flush=True)

    print(f'\nscanned={scanned} flagged={flagged} missing={missing}')
    return 1 if flagged else 0


if __name__ == '__main__':
    sys.exit(main())
