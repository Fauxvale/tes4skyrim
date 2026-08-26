#!/usr/bin/env python3
"""Audit NiMultiTargetTransformController targets against sequence blocks.

Why this exists: a NiMultiTargetTransformController holds a POSITIONAL list of
extra targets, and the engine pairs each slot with the NiControllerSequence
controlled-block that drives it.  If a target has no block, that slot resolves
to a null interpolator and BGSGamebryoSequenceGenerator crashes the moment the
object animates -- `movdqu xmm2,[rax]` with rax=0 inside VCRUNTIME140
(crash-2026-08-10-00-42-35, spiddalcloudplant.nif).

The converter used to delete any controlled block whose node name equalled the
ROOT node's name; when the root is also an extra target (Oblivion names the
root and its animated node the same thing), that deleted exactly the block the
target needed.

Usage:
    python tools/validate/mttc_target_check.py <nif-or-dir> [--workers N] [--quiet]

Exit code is 1 if any mesh has a target with no driving block.
"""
import argparse
import os
import sys
import warnings
from concurrent.futures import ProcessPoolExecutor

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def check(path):
    from asset_convert import sse_nif
    from pyffi.formats.nif import NifFormat
    try:
        data = sse_nif.read_nif(path)
    except Exception as exc:
        return path, None, f'read failed: {exc}'
    problems = []
    for root in data.roots:
        root_name = bytes(getattr(root, 'name', b'') or b'')
        blocks = set()
        targets = []
        stated = 0
        for b in root.tree():
            if isinstance(b, NifFormat.NiControllerSequence):
                for cb in (b.controlled_blocks or ()):
                    blocks.add(bytes(cb.node_name or b''))
            elif isinstance(b, NifFormat.NiMultiTargetTransformController):
                stated += int(getattr(b, 'num_extra_targets', 0))
                for t in (getattr(b, 'extra_targets', None) or ()):
                    if t is not None:
                        targets.append(bytes(getattr(t, 'name', b'') or b''))
        if not targets and not stated:
            continue
        # 1. every target needs a driving block (vanilla: 0 violations)
        for t in targets:
            if t and t not in blocks:
                problems.append(f'target with no block: {t.decode("latin-1")}')
        # 2. the root must not be a target at all (vanilla: 0 violations)
        if root_name and root_name in targets:
            problems.append(f'root is an extra target: {root_name.decode("latin-1")}')
        # NOT checked: `num_extra_targets` vs the number of non-null slots.
        # Vanilla disagrees there routinely (spitpotopen01 states 16 and lists
        # 2; 134 of the sampled clutter meshes do the same), so a stated count
        # above the live entries is legal and is NOT the crash condition.
    return path, problems, None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('root')
    ap.add_argument('--workers', type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()

    if os.path.isfile(args.root):
        paths = [args.root]
    else:
        paths = [os.path.join(d, f)
                 for d, _, fs in os.walk(args.root)
                 for f in fs if f.lower().endswith('.nif')]
    print(f'checking {len(paths)} NIFs...')

    bad = errs = 0
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for path, missing, err in ex.map(check, paths, chunksize=16):
            if err:
                errs += 1
                if not args.quiet:
                    print(f'  ERROR {path}: {err}')
            elif missing:
                bad += 1
                print(f'  BAD {path}')
                for problem in missing:
                    print(f'      {problem}')
    print(f'\nmeshes violating an MTTC invariant: {bad}   (read errors: {errs})')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
