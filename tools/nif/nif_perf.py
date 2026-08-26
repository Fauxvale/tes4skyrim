"""Benchmark + byte-equality harness for NIF conversion optimisations.

Mesh conversion is one process per core, so wall-clock is set by PER-MESH CPU
cost, and almost all of that is inside PyFFI's generic XML object model rather
than our conversion code.  This harness makes an optimisation measurable and
safe: it converts a fixed, seeded sample of real NIFs, records a SHA-1 per
output, and compares against a stored baseline.

    # record the baseline (do this BEFORE changing anything)
    python tools/nif/nif_perf.py --save-baseline temp/nif_base.json --count 40

    # after a change: same sample, compares hashes and reports the speedup
    python tools/nif/nif_perf.py --baseline temp/nif_base.json --count 40

    # find the hot kernel instead of timing
    python tools/nif/nif_perf.py --profile --count 12 --top 25

`--seed` picks a different sample; `--plugin` selects the export tree.  Sampling
is seeded and sorted, so the same --seed/--count always yields the same files.

IMPORTANT — NIF conversion output depends on ``PYTHONHASHSEED``.  Converting the
same mesh twice in ONE process is stable, but two separate runs disagree: some
part of the converter iterates a set/dict whose order varies with the per-process
hash randomisation.  This harness therefore re-executes itself with
``PYTHONHASHSEED=0`` so a baseline comparison measures the CODE CHANGE and not
the seed.  Without that pin, every comparison reports a spurious mismatch.
"""
import argparse
import cProfile
import hashlib
import io
import json
import os
import pstats
import random
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# Conversion output varies with PYTHONHASHSEED (see module docstring), so pin it
# before anything imports the converter.  Re-exec is the only way: the seed is
# consumed by the interpreter at start-up and cannot be set from inside.
if os.environ.get('PYTHONHASHSEED') != '0':
    os.environ['PYTHONHASHSEED'] = '0'
    os.execv(sys.executable, [sys.executable] + sys.argv)

from asset_convert import nif_converter as nc  # noqa: E402


def collect(src_root: Path, count: int, seed: int, include_lod: bool):
    nifs = []
    for p in src_root.rglob('*.nif'):
        rel = [x.lower() for x in p.relative_to(src_root).parts]
        if any(s in rel for s in nc.SKIP_PATHS):
            continue
        if not include_lod and p.stem.lower().endswith('_far'):
            continue
        nifs.append(p)
    nifs.sort()
    rnd = random.Random(seed)
    if count < len(nifs):
        nifs = rnd.sample(nifs, count)
    return sorted(nifs)


def convert_all(sample, src_root, out_dir):
    """Convert every sampled NIF; return {relpath: sha1|marker} and elapsed."""
    hashes = {}
    t0 = time.perf_counter()
    for i, p in enumerate(sample):
        rel = str(p.relative_to(src_root)).replace('\\', '/')
        dst = out_dir / f"{i}.nif"
        try:
            nc.convert_nif(str(p), str(dst), src_meshes_dir=str(src_root))
        except Exception as exc:
            hashes[rel] = f"EXC:{exc.__class__.__name__}"
            continue
        hashes[rel] = (hashlib.sha1(dst.read_bytes()).hexdigest()
                       if dst.exists() else "MISSING")
    return hashes, time.perf_counter() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--plugin', default='Oblivion.esm')
    ap.add_argument('--count', type=int, default=40)
    ap.add_argument('--seed', type=int, default=23)
    ap.add_argument('--include-lod', action='store_true',
                    help='also sample *_far.nif (excluded by default)')
    ap.add_argument('--save-baseline', metavar='PATH')
    ap.add_argument('--baseline', metavar='PATH')
    ap.add_argument('--profile', action='store_true')
    ap.add_argument('--top', type=int, default=25)
    ap.add_argument('--sort', default='tottime')
    a = ap.parse_args()

    src_root = Path('export') / a.plugin / 'meshes'
    if not src_root.is_dir():
        print(f"no mesh tree at {src_root}", file=sys.stderr)
        return 2

    sample = collect(src_root, a.count, a.seed, a.include_lod)
    total_kb = sum(p.stat().st_size for p in sample) / 1024
    print(f"{len(sample)} NIFs, {total_kb:,.0f} KB "
          f"(plugin={a.plugin} seed={a.seed})")

    nc._pyffi_capture_init()
    out_dir = Path(tempfile.mkdtemp(prefix='nifperf_'))

    # Warm-up: the first conversion pays one-time import/XML costs.
    try:
        nc.convert_nif(str(sample[0]), str(out_dir / 'warm.nif'),
                       src_meshes_dir=str(src_root))
    except Exception:
        pass

    if a.profile:
        pr = cProfile.Profile()
        pr.enable()
        hashes, dt = convert_all(sample, src_root, out_dir)
        pr.disable()
        print(f"\n{dt:.2f}s total, {dt / len(sample) * 1000:.0f} ms/mesh\n")
        s = io.StringIO()
        pstats.Stats(pr, stream=s).sort_stats(a.sort).print_stats(a.top)
        print(s.getvalue())
        return 0

    hashes, dt = convert_all(sample, src_root, out_dir)
    print(f"{dt:.2f}s total, {dt / len(sample) * 1000:.0f} ms/mesh, "
          f"{total_kb / dt:,.0f} KB/s")

    if a.save_baseline:
        Path(a.save_baseline).write_text(
            json.dumps({'seconds': dt, 'hashes': hashes}, indent=1),
            encoding='utf-8')
        print(f"baseline saved to {a.save_baseline}")
        return 0

    if a.baseline:
        base = json.loads(Path(a.baseline).read_text(encoding='utf-8'))
        bh = base['hashes']
        diff = [k for k in sorted(set(bh) | set(hashes))
                if bh.get(k) != hashes.get(k)]
        print(f"\nbaseline {base['seconds']:.2f}s -> now {dt:.2f}s "
              f"= {base['seconds'] / dt:.2f}x")
        print(f"outputs differing: {len(diff)} of {len(hashes)}")
        for k in diff[:10]:
            print(f"  {k}: {bh.get(k)} -> {hashes.get(k)}")
        print("BYTE-IDENTICAL" if not diff else "*** MISMATCH ***")
        return 0 if not diff else 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
