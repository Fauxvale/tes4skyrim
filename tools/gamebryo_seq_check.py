#!/usr/bin/env python3
"""Audit generated behavior graphs for BGSGamebryoSequenceGenerator problems.

Why this exists: the generator resolves `pSequence` BY NAME against the NIF's
NiControllerManager sequence map at runtime.  A name that resolves to nothing
gives a NULL sequence, which the engine dereferences the moment that state is
entered -- `movdqu xmm2,[rax]` with rax=0 inside VCRUNTIME140, under
BGSGamebryoSequenceGenerator (crash-2026-08-10-01-08-13).

Because the state machine STARTS in the Rest state, an unresolvable name there
crashes on the object's very first animation.

Three checks, calibrated against vanilla (51 Behavior00.hkx that use a
Gamebryo generator, extracted from Skyrim - Animations.bsa: 0 violations):

  1. every NON-Rest generator names a sequence the sibling NIF declares,
     and never an empty name (that null sequence is deref'd on activation)
  2. the Rest generator's pSequence is EMPTY.  Empty is CORRECT there --
     nothing should play on cell load, and the working doors ship exactly
     this (see "The Rest state is CORRECT" in docs/nif_conversion_notes.md).
     A Rest naming a real sequence animates the object on load (the
     self-opening secret doors)
  3. no sequence in a graph-carrying NIF has an EMPTY text key value.  On
     activation the generator walks NiTextKeyExtraData and `strchr`s each
     value for '.' (GOG exe 0x505130, AddrLib ID 32774); an empty NiString
     loads as a NULL BSFixedString and the strchr crashes — the Spiddal
     Stick / Harrada Root CTDs (crash-2026-08-10-01-41-07 / -01-39-02).
     Vanilla ships empty keys ONLY on graph-less meshes (impjaildoor01,
     ruinscanopicjar02), never beside a behavior graph.

Usage:
    python tools/gamebryo_seq_check.py <output-meshes-dir> [--quiet]

Exit code is 1 if any graph names a sequence its mesh does not provide.
"""
import argparse
import os
import sys
import time
import warnings

warnings.filterwarnings('ignore')
if not hasattr(time, '_original_clock'):
    time.clock = time.perf_counter
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _nif_sequences(nif_path):
    """(declared sequence names, [(seq name, empty text key count), ...])."""
    from asset_convert import sse_nif
    from pyffi.formats.nif import NifFormat
    try:
        data = sse_nif.read_nif(nif_path)
    except Exception:
        return None, []
    names = set()
    empty = []
    for root in data.roots:
        for block in root.tree():
            if isinstance(block, NifFormat.NiControllerManager):
                for seq in (block.controller_sequences or ()):
                    if seq is None:
                        continue
                    sname = bytes(seq.name or b'').decode('latin-1')
                    names.add(sname)
                    tk = getattr(seq, 'text_keys', None)
                    if tk is not None:
                        n = sum(1 for k in tk.text_keys
                                if not bytes(k.value or b'').strip())
                        if n:
                            empty.append((sname, n))
    return names, empty


def _graph_sequences(hkx_path):
    """(generator name, pSequence) pairs from the packed behavior graph.

    Binary hkx pools its strings NUL-separated, so split on NUL rather than
    scanning for printable runs: an EMPTY pSequence leaves no printable bytes
    at all, and a printable-run scan silently skips it -- which is exactly the
    defect this tool exists to catch, so that version reported 0 violations on
    a file built with the bug deliberately injected.
    """
    with open(hkx_path, 'rb') as fh:
        blob = fh.read()
    parts = [p.decode('latin-1', 'replace') for p in blob.split(b'\x00')]
    out = []
    for i, s in enumerate(parts):
        if not s.startswith('GamebryoSequenceGenerator'):
            continue
        # The pool pads with NULs, so the sequence name is the next NON-EMPTY
        # string -- but only within a short window, because a generator whose
        # pSequence really IS empty must report '' rather than running on and
        # picking up the next generator's name.  Vanilla-shaped files put it
        # within ~6 slots (measured: +5 for numbered generators, +3 for Rest).
        nxt = ''
        for j in range(i + 1, min(i + 8, len(parts))):
            cand = parts[j]
            if not cand:
                continue
            if cand.startswith('GamebryoSequenceGenerator'):
                break        # ran into the next generator: this one is empty
            nxt = cand
            break
        out.append((s, nxt))
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('root')
    ap.add_argument('--quiet', action='store_true')
    args = ap.parse_args()

    checked = bad = 0
    for base, dirs, _files in os.walk(args.root):
        for sub in dirs:
            if not sub.endswith('_behavior'):
                continue
            stem = sub[:-len('_behavior')]
            nif = os.path.join(base, stem + '.nif')
            hkx = os.path.join(base, sub, 'Behaviors', 'Behavior00.hkx')
            if not (os.path.exists(nif) and os.path.exists(hkx)):
                continue
            checked += 1
            declared, empty_keys = _nif_sequences(nif)
            if declared is None:
                continue
            problems = []
            for sname, n in empty_keys:
                problems.append(
                    f'sequence {sname!r}: {n} EMPTY text key value(s) '
                    f'(NULL BSFixedString -> strchr crash on activation)')
            for gen, seq in _graph_sequences(hkx):
                is_rest = gen.endswith('Rest')
                if not seq:
                    # Empty is CORRECT on Rest (nothing plays on load; the
                    # in-game-verified doors ship this).  On a numbered
                    # generator it is a null deref on first animation.
                    if not is_rest:
                        problems.append(f'{gen}: EMPTY pSequence (null deref)')
                elif is_rest and seq in declared:
                    # The machine STARTS in Rest, so naming a real sequence
                    # animates the object on cell load: this is what made the
                    # castle/Ayleid secret doors open by themselves.
                    problems.append(
                        f'{gen}: names REAL sequence {seq!r} -- plays on load')
                elif not is_rest and seq not in declared:
                    problems.append(
                        f'{gen}: names {seq!r}, NIF has {sorted(declared)}')
            if problems:
                bad += 1
                print(f'  BAD {nif}')
                for problem in problems:
                    print(f'      {problem}')
            elif not args.quiet:
                print(f'  ok  {stem}: {sorted(declared)}')

    print(f'\nbehavior projects checked: {checked}   violations: {bad}')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
