"""Rebake the LOD tiles that two or more converted SIBLING plugins both claim.

Sibling plugins extending one master (Tamriel.esp, ElsweyrAnequina.esp,
Morrowind_ob.esm and DLCBattlehornCastle.esp all extend Oblivion.esm's
TES4Tamriel) each bake LOD as "master + itself". Right alone, wrong together:
LOD tiles are files on a fixed grid, so two plugins touching one tile ship rival
copies of the same path and whichever the mod manager writes last erases the
other's terrain and distant objects for that entire tile.

This bakes the contested tiles ONCE from the master with every sibling stacked
as an overlay in load order (the existing merge is by FormID, so a REFR one
sibling moved and another left alone resolves as the engine would), into its own
mod folder so it can be installed last and win the overwrite deliberately.

Only contested tiles are baked — a tile a single sibling touches is already
correct in that sibling's own output.

Usage:
  python tools/merge_sibling_lod.py                     # scan output/, bake
  python tools/merge_sibling_lod.py --dry-run           # report conflicts only
  python tools/merge_sibling_lod.py --output-dir PATH
  python tools/merge_sibling_lod.py --worldspace TES4Tamriel
"""

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

# Overwritten files listed per plugin before truncating. A contested worldspace
# runs to thousands of tiles, so the full list belongs behind --list-all.
_MAX_LISTED = 12

from subprocess_flags import configure_multiprocessing
from process_job import create_pool_job

configure_multiprocessing()
create_pool_job()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Merge the LOD tiles that sibling plugins both change.")
    ap.add_argument("--output-dir", metavar="PATH",
                    help="Output directory (default: output/ in project root)")
    ap.add_argument("--worldspace", metavar="EDID",
                    help="Only merge this worldspace (default: every "
                         "worldspace with a conflict)")
    ap.add_argument("--order", nargs="+", metavar="PLUGIN",
                    help="Explicit load order for the overlays, lowest "
                         "priority first. The LAST one listed wins a "
                         "contested reference. Default: the user's "
                         "plugins.txt order, then anything unlisted "
                         "appended alphabetically.")
    ap.add_argument("--list-all", action="store_true",
                    help="List every overwritten file rather than the first "
                         f"{_MAX_LISTED} per plugin.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report the conflicts and the tile counts, bake "
                         "nothing")
    args = ap.parse_args()

    from asset_convert.lod_gen import (generate_lod,
                                       _textures_root as _lod_textures_root)
    from asset_convert.terrain_lod import generate_terrain_lod
    from asset_convert.sibling_lod import (find_sibling_groups,
                                           contested_cells,
                                           converted_plugins,
                                           merge_cloud_bank,
                                           overwrite_report,
                                           plugins_txt_order,
                                           MERGED_DIR_NAME)

    out_root = (Path(args.output_dir) if args.output_dir
                else SCRIPT_DIR / "output")
    export_root = SCRIPT_DIR / "export"

    print("=" * 54)
    print("  MERGE SIBLING LOD")
    print("=" * 54)
    print(f"  Output dir: {out_root}")

    plugins_found = converted_plugins(out_root)
    print(f"  Converted plugins: {', '.join(plugins_found) or '(none)'}")
    print()

    if args.order:
        print(f"  Load order: explicit (--order)")
    elif plugins_txt_order():
        print(f"  Load order: plugins.txt, unlisted appended alphabetically")
    else:
        print(f"  Load order: plugins.txt not found - using master depth")
    print()

    groups = find_sibling_groups(out_root, export_root,
                                 explicit_order=args.order)
    if args.worldspace:
        groups = {k: v for k, v in groups.items()
                  if k.lower() == args.worldspace.lower()}
        if not groups:
            print(f"No sibling conflict in worldspace "
                  f"'{args.worldspace}'; nothing to merge.")
            return 0
    if not groups:
        print("No worldspace is changed by two or more converted plugins; "
              "no merged LOD needed.")
        return 0

    merged_dir = out_root / MERGED_DIR_NAME
    ok_all = True

    for edid in sorted(groups):
        g = groups[edid]
        plugins = g['plugins']
        print("-" * 54)
        print(f"Worldspace '{edid}'  (master: {g['master']})")
        # Printed in APPLY order, lowest priority first, because that order is
        # the conflict resolution: the last plugin listed is the one whose
        # version of a shared reference survives into the merged tile.
        for i, n in enumerate(plugins, 1):
            win = "  <- wins conflicts" if i == len(plugins) else ""
            print(f"   {i}. {n}: {len(g['cells'][n])} changed cell(s){win}")

        # The world-map cloud bank is a single file per worldspace, so it has
        # the same overwrite problem as the tiles — but at the worldspace
        # level, independent of which TILES are contested. Merge it whenever
        # siblings share a worldspace, before the tile check below can skip.
        if not args.dry_run:
            cloud_rel = merge_cloud_bank(out_root, merged_dir, edid,
                                         g['master'], plugins)
            if cloud_rel:
                print(f"   Merged world-map cloud bank -> {cloud_rel} "
                      f"(union of {len(plugins) + 1} plugin bounds)")

        hot = contested_cells(g['cells'])
        if not hot:
            print("   No tile is claimed by more than one plugin — each "
                  "plugin's own LOD already wins cleanly. Skipping.")
            continue
        print(f"   {len(hot)} cell(s) fall inside contested tiles.")

        if args.dry_run:
            print("   (dry run — nothing baked)")
            continue

        # Drop THIS worldspace's tiles from a previous run before rebaking.
        # The bake only writes the tiles it produces this time, so a tile that
        # was contested before and is not now — or that an earlier run emitted
        # at the wrong coordinates — survived as an orphan and still shipped,
        # overwriting the correct tile at that path. Observed after the load
        # order fix: 5 .btr + 10 .dds at tile 0.0 left from an earlier run,
        # while the corrected bake produced no 0.0 tile at all.
        #
        # Scoped to this worldspace's own tile names so a run covering several
        # worldspaces never deletes a sibling worldspace's fresh output, and
        # so the merged object .nifs (shared, coordinate-free) are untouched.
        stale = 0
        for sub, pat in (("meshes/terrain", f"{edid}.*"),
                         ("textures/terrain", f"{edid}.*")):
            d = merged_dir / sub / edid
            if not d.is_dir():
                continue
            for f in d.glob(pat):
                if f.is_file():
                    f.unlink()
                    stale += 1
        if stale:
            print(f"   Cleared {stale} tile(s) from a previous run")

        # Assets from the master AND every sibling: a merged tile draws objects
        # from all of them, so a model converted into only one sibling's output
        # still has to be findable here.
        asset_dirs = [out_root / n for n in [g['master']] + plugins
                      if (out_root / n).is_dir()]
        overlays = [out_root / n / n for n in plugins]

        print(f"   Baking merged object LOD -> {merged_dir}")
        ok = generate_lod(
            esm_path=g['master_esm'],
            output_dir=merged_dir,
            worldspace_edid=edid,
            # No tile-ownership skip: these tiles are exactly the ones the
            # siblings ship and that this folder exists to override. Passing
            # the master's dir would skip every tile it already built — all of
            # them — and the merged folder would come out empty.
            master_dirs=None,
            master_mesh_dirs=asset_dirs,
            master_texture_dirs=asset_dirs,
            overlay_paths=overlays,
            only_cells=hot,
        )

        print(f"   Baking merged terrain LOD -> {merged_dir}")
        ok_terrain = generate_terrain_lod(
            esm_path=g['master_esm'],
            output_dir=merged_dir,
            worldspace_edid=edid,
            overlay_paths=overlays,
            only_cells=hot,
            extra_texture_roots=[_lod_textures_root(Path(d))
                                 for d in asset_dirs],
        )
        ok_all = ok_all and ok and ok_terrain

        # What this merge actually takes over, per plugin. Measured from the
        # files on disk rather than predicted from the cell maths, so it
        # reports what shipped and not what was intended.
        print()
        print(f"   Overwrites in '{edid}':")
        rep = overwrite_report(out_root, edid, plugins, MERGED_DIR_NAME)
        for name in plugins:
            r = rep[name]
            n_over = len(r['overwritten'])
            if not n_over:
                print(f"     {name}: nothing overwritten "
                      f"({r['kept']} own LOD file(s) untouched)")
                continue
            print(f"     {name}: {n_over} file(s) overwritten, "
                  f"{r['kept']} untouched")
            shown = r['overwritten'] if args.list_all \
                else r['overwritten'][:_MAX_LISTED]
            for f in shown:
                print(f"        {f}")
            if len(shown) < n_over:
                print(f"        ... and {n_over - len(shown)} more "
                      f"(--list-all prints every file)")
        print()

    print("-" * 54)
    if args.dry_run:
        print("Dry run complete.")
    elif merged_dir.is_dir():
        print(f"Merged LOD written to {merged_dir}")
        print("Install it AFTER the plugins it merges so it wins the tile "
              "overwrite.")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
