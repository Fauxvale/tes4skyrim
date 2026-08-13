"""Verify and benchmark the terrain-LOD plugin/LTEX parse caches.

The terrain LOD stage runs once per worldspace, and before these caches each run
re-read the whole plugin (twice inside `_parse_land_records`, once more per
overlay) and rebuilt the worldspace-independent LTEX map from scratch. This tool
proves the cached path returns IDENTICAL data and measures what the repeat calls
now cost.

Correctness is checked first and independently of timing: a cache that is fast
but returns different bytes is a data-corruption bug, not an optimisation.

Usage:
    python tools/lod_parse_cache_bench.py <plugin.esm> [--worldspace NAME]
                                          [--repeat N] [--overlay PATH ...]

Example (small worldspace, cheap):
    python tools/lod_parse_cache_bench.py output/Oblivion.esm/Oblivion.esm \\
        --worldspace MS14World --repeat 3
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asset_convert import terrain_lod as TL              # noqa: E402
from asset_convert import terrain_lod_textures as TLT    # noqa: E402


def _lands_fingerprint(lands):
    """Order-independent digest of the parsed LAND set."""
    import hashlib
    h = hashlib.sha1()
    for key in sorted(lands):
        rec = lands[key]
        h.update(repr(key).encode())
        for field in ('heights', 'colors'):
            v = rec.get(field)
            if v is not None:
                h.update(v.tobytes())
        h.update(repr(sorted(rec.get('layers', {}).items())).encode()[:4096])
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('plugin')
    ap.add_argument('--worldspace', default='MS14World',
                    help='worldspace EditorID (default: MS14World, a small one)')
    ap.add_argument('--repeat', type=int, default=3,
                    help='calls to time after the first (default 3)')
    ap.add_argument('--overlay', action='append', default=[],
                    help='overlay plugin path (repeatable)')
    args = ap.parse_args()

    esm = Path(args.plugin)
    if not esm.exists():
        print(f"ERROR: no such plugin: {esm}")
        return 1
    overlays = [Path(p) for p in args.overlay] or None

    print(f"Plugin:     {esm}  ({esm.stat().st_size/1e6:.0f} MB)", flush=True)
    print(f"Worldspace: {args.worldspace}", flush=True)
    if overlays:
        print(f"Overlays:   {', '.join(p.name for p in overlays)}", flush=True)
    print(flush=True)

    # -- Correctness: cold (cache dropped) vs warm must be byte-identical -----
    print("== correctness ==", flush=True)
    TL._drop_plugin_bytes()
    TLT._LTEX_MAP_CACHE.clear()
    lands_a, water_a, wh_a = TL._parse_land_records(esm, args.worldspace, overlays)
    ltex_a = TLT.build_ltex_texture_map(esm)
    fa = _lands_fingerprint(lands_a)

    lands_b, water_b, wh_b = TL._parse_land_records(esm, args.worldspace, overlays)
    ltex_b = TLT.build_ltex_texture_map(esm)
    fb = _lands_fingerprint(lands_b)

    ok = True
    for label, cond in (
        (f"LAND fingerprint  ({len(lands_a)} cells)", fa == fb),
        (f"cell_water        ({len(water_a)} cells)", water_a == water_b),
        ("default water height", wh_a == wh_b),
        (f"LTEX map          ({len(ltex_a)} entries)", ltex_a == ltex_b),
    ):
        print(f"  {'OK  ' if cond else 'FAIL'} {label}", flush=True)
        ok &= bool(cond)

    # The LTEX map is handed to callers that merge overlays with .update();
    # mutating the returned dict must never reach the cached copy.
    ltex_a['__probe__'] = {'diffuse': 'x', 'normal': 'x'}
    isolated = '__probe__' not in TLT.build_ltex_texture_map(esm)
    print(f"  {'OK  ' if isolated else 'FAIL'} LTEX caller mutation isolated",
          flush=True)
    ok &= isolated
    if not ok:
        print("\nCORRECTNESS FAILED — cache returns different data.", flush=True)
        return 2

    # -- Timing: first call (cold read) vs repeats (cached bytes) -------------
    print("\n== timing ==", flush=True)
    TL._drop_plugin_bytes()
    TLT._LTEX_MAP_CACHE.clear()

    t = time.perf_counter()
    TL._parse_land_records(esm, args.worldspace, overlays)
    cold_land = time.perf_counter() - t
    t = time.perf_counter()
    TLT.build_ltex_texture_map(esm)
    cold_ltex = time.perf_counter() - t
    print(f"  cold  _parse_land_records   {cold_land:7.2f}s", flush=True)
    print(f"  cold  build_ltex_texture_map{cold_ltex:7.2f}s", flush=True)

    warm_land = []
    warm_ltex = []
    for i in range(args.repeat):
        t = time.perf_counter()
        TL._parse_land_records(esm, args.worldspace, overlays)
        warm_land.append(time.perf_counter() - t)
        t = time.perf_counter()
        TLT.build_ltex_texture_map(esm)
        warm_ltex.append(time.perf_counter() - t)
        print(f"  warm #{i+1} land {warm_land[-1]:7.2f}s   "
              f"ltex {warm_ltex[-1]:7.2f}s", flush=True)

    aland = sum(warm_land) / len(warm_land)
    altex = sum(warm_ltex) / len(warm_ltex)
    print(flush=True)
    print(f"  _parse_land_records    {cold_land:6.2f}s -> {aland:6.2f}s"
          f"  ({cold_land/aland if aland else float('inf'):.2f}x)", flush=True)
    print(f"  build_ltex_texture_map {cold_ltex:6.2f}s -> {altex:6.2f}s"
          f"  ({cold_ltex/altex if altex else float('inf'):.0f}x)", flush=True)

    # Per-worldspace saving: the LTEX map is rebuilt once per worldspace (plus
    # once per overlay), and the LAND parse re-reads the file it already read.
    saved = (cold_ltex - altex) + (cold_land - aland)
    print(f"\n  saved per additional worldspace: ~{saved:.2f}s", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
