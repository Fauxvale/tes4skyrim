"""MOPP bytecode analysis and repair (Oblivion→Skyrim collision pipeline).

MOPP_RL.exe builds its MOPP with Havok's *chunk subdivision* enabled (an
SPU/PS3 streaming feature): the code contains 5-byte `0x70 <abs32>`
chunk-jump instructions that transfer control to 16-byte-aligned sub-trees
stored after the main tree (0xCD alignment filler between them).

Vanilla Skyrim never ships chunked MOPPs (0 of 400 vanilla meshes contain
opcode 0x70), and the PC engine mis-executes them: when a collision query
descends into a 0x70 branch the VM runs away and Skyrim dies with
EXCEPTION_STACK_OVERFLOW inside hkpCollisionDispatcher (the intermittent
"walking over castleint2way.nif" crash — only queries that reach a chunk
jump die, so it looked random).

`dechunk_mopp()` rewrites each 5-byte chunk jump IN PLACE as a 3-byte
0x06 JUMP16 (a vanilla-proven opcode) plus two unreachable pad bytes, so no
other instruction offset moves.  Unreachable bytes (alignment filler and the
dead pad) are then zeroed.  The result is bytecode that only uses opcodes
observed in vanilla Skyrim meshes.

`walk_mopp()` is the underlying verifier: it executes the bytecode
symbolically, following every branch, and reports coverage, triangle shape
keys, and structural errors.  The opcode table is PyFFI's parse_mopp
(niftools reverse engineering) extended with the Skyrim-era commands
(0x52 TERM24, 0x29-0x2B DOUBLE_CUT24, 0x70 CHUNK_JUMP32), validated clean
against 400 vanilla Skyrim SE meshes.
"""

# Walk safety cap
_MAX_STEPS = 2_000_000


def walk_mopp(mopp, size, start=0, follow_chunks=True):
    """Walk MOPP bytecode from `start`, following all branches.

    mopp: byte sequence (list/bytes/bytearray), size: number of valid bytes.
    follow_chunks: when False, 0x70 chunk jumps are recorded but treated as
    leaves (used to delimit one region without entering others).

    Returns dict with:
      visited      : set of byte offsets executed/consumed
      tris         : set of shape keys (triangle ids) encountered
      errors       : list of strings (OOB jumps, unknown opcodes, ...)
      counts       : dict opcode -> executed instruction sites
      chunk_jumps  : list of (site_offset, target_offset) for 0x70 commands
      max_offset   : highest visited offset
    """
    visited = set()
    tris = set()
    errors = []
    counts = {}
    chunk_jumps = []
    seen_states = set()
    stack = [(start, 0)]  # worklist of (offset, triangle_offset)
    steps = 0

    def oob(i, what):
        errors.append('offset %d: %s runs out of bounds (size %d)' % (i, what, size))

    def result():
        return {'visited': visited, 'tris': tris, 'errors': errors,
                'counts': counts, 'chunk_jumps': chunk_jumps,
                'max_offset': max(visited) if visited else -1}

    while stack:
        i, toffset = stack.pop()
        if (i, toffset) in seen_states:
            continue
        seen_states.add((i, toffset))
        ret = False
        while not ret:
            steps += 1
            if steps > _MAX_STEPS:
                errors.append('step limit exceeded (possible cycle)')
                return result()
            if i < 0 or i >= size:
                oob(i, 'instruction pointer')
                break
            code = mopp[i]
            counts[code] = counts.get(code, 0) + 1

            if 0x30 <= code <= 0x4F:                       # TERM4 compact leaf
                visited.add(i)
                tris.add(code - 0x30 + toffset)
                ret = True

            elif 0x50 <= code <= 0x53:                     # TERM 8/16/24/32 leaf
                n = code - 0x50 + 1                        # operand bytes
                if i + n >= size:
                    oob(i, 'TERM%d operands' % (8 * n))
                    break
                key = 0
                for k in range(n):
                    key = (key << 8) | mopp[i + 1 + k]
                visited.update(range(i, i + n + 1))
                tris.add(key + toffset)
                ret = True

            elif code == 0x05:                             # JUMP8
                if i + 1 >= size:
                    oob(i, 'JUMP8 operand'); break
                visited.update((i, i + 1))
                i = i + 2 + mopp[i + 1]

            elif code == 0x06:                             # JUMP16
                if i + 2 >= size:
                    oob(i, 'JUMP16 operands'); break
                visited.update(range(i, i + 3))
                i = i + 3 + (mopp[i + 1] << 8 | mopp[i + 2])

            elif code == 0x07:                             # JUMP24
                if i + 3 >= size:
                    oob(i, 'JUMP24 operands'); break
                visited.update(range(i, i + 4))
                i = i + 4 + (mopp[i + 1] << 16 | mopp[i + 2] << 8 | mopp[i + 3])

            elif code == 0x09:                             # TERM_REOFFSET8
                if i + 1 >= size:
                    oob(i, 'REOFFSET8 operand'); break
                visited.update((i, i + 1))
                toffset += mopp[i + 1]
                i += 2

            elif code == 0x0A:                             # TERM_REOFFSET16
                if i + 2 >= size:
                    oob(i, 'REOFFSET16 operands'); break
                visited.update(range(i, i + 3))
                toffset += mopp[i + 1] << 8 | mopp[i + 2]
                i += 3

            elif code == 0x0B:                             # TERM_REOFFSET32
                # Full 32-bit big-endian operand SETS the terminal offset.
                # (PyFFI's parse_mopp only read bytes 3-4 — an Oblivion-era
                # guess; Skyrim CMS keys carry the chunk id in the high bytes,
                # e.g. operand 0x00040000 = chunk 0.  Verified: with the full
                # read, walked key sets exactly match the CMS-predicted key
                # sets on vanilla Skyrim meshes.)
                if i + 4 >= size:
                    oob(i, 'REOFFSET32 operands'); break
                visited.update(range(i, i + 5))
                toffset = (mopp[i + 1] << 24 | mopp[i + 2] << 16
                           | mopp[i + 3] << 8 | mopp[i + 4])
                i += 5

            elif 0x10 <= code <= 0x1C:                     # SPLIT8 (13 dop dirs)
                if i + 3 >= size:
                    oob(i, 'SPLIT8 operands'); break
                visited.update(range(i, i + 4))
                stack.append((i + 4 + mopp[i + 3], toffset))
                i = i + 4

            elif 0x20 <= code <= 0x22:                     # SINGLE_SPLIT (X/Y/Z)
                if i + 2 >= size:
                    oob(i, 'SINGLE_SPLIT operands'); break
                visited.update(range(i, i + 3))
                stack.append((i + 3 + mopp[i + 2], toffset))
                i = i + 3

            elif 0x23 <= code <= 0x25:                     # SPLIT16 (X/Y/Z)
                if i + 6 >= size:
                    oob(i, 'SPLIT16 operands'); break
                visited.update(range(i, i + 7))
                jump1 = mopp[i + 3] << 8 | mopp[i + 4]
                jump2 = mopp[i + 5] << 8 | mopp[i + 6]
                stack.append((i + 7 + jump2, toffset))
                i = i + 7 + jump1

            elif 0x26 <= code <= 0x28:                     # DOUBLE_CUT X/Y/Z
                if i + 2 >= size:
                    oob(i, 'DOUBLE_CUT operands'); break
                visited.update(range(i, i + 3))
                i += 3

            elif 0x29 <= code <= 0x2B:                     # DOUBLE_CUT24 X/Y/Z
                if i + 6 >= size:
                    oob(i, 'DOUBLE_CUT24 operands'); break
                visited.update(range(i, i + 7))
                i += 7

            elif 0x01 <= code <= 0x04:                     # RESCALE (4 bytes)
                if i + 3 >= size:
                    oob(i, 'RESCALE operands'); break
                visited.update(range(i, i + 4))
                i += 4

            elif code == 0x70:                             # CHUNK_JUMP32 (chunked mopp)
                if i + 4 >= size:
                    oob(i, 'CHUNK_JUMP32 operands'); break
                visited.update(range(i, i + 5))
                target = (mopp[i + 1] << 24 | mopp[i + 2] << 16 |
                          mopp[i + 3] << 8 | mopp[i + 4])
                chunk_jumps.append((i, target))
                if not follow_chunks:
                    ret = True
                else:
                    i = target

            else:
                ctx = [('0x%02X' % mopp[j]) for j in range(i, min(size, i + 10))]
                errors.append('offset %d: unknown opcode 0x%02X context=[%s] toffset=%d'
                              % (i, code, ' '.join(ctx), toffset))
                break

    return result()
