"""Generate ALL distant LOD, ONCE, into a standalone AutoConvertLOD mod.

LOD used to be a per-plugin pipeline step (`convert.py --lod-only -f X`) that
wrote into `output/<plugin>/`, followed by a separate "Merge Sibling LOD" pass
that rebaked whatever two plugins both claimed. That is three bakes of the same
ground: LOD tiles are FILES on a fixed grid keyed only by worldspace and
coordinate (`meshes/terrain/<wrld>/Objects/<wrld>.4.-32.-32.bto`), so every
plugin editing a worldspace generates the SAME tile paths. Converting four
siblings produced four rival copies of each shared tile, the mod manager's
install order silently picked a winner, and the merge pass then threw all of
them away and baked a fifth.

There is no per-plugin LOD here. For each worldspace the bake reads records
from the plugin that OWNS it and applies every other selected plugin as an
overlay in load order — which is what the generators have always supported, and
what makes one tile correct for the whole load order. Each tile is written
exactly once, into one mod folder the user installs like any other.

Because the output is a single standalone mod, nothing overwrites anything:
there are no rival copies to resolve, so there is no contested-tile maths, no
`only_cells` restriction and no merge stage. Install it after the plugins it
covers.

Order is lowest priority FIRST — the last plugin listed wins any reference two
of them both change. The default is the user's plugins.txt order, with anything
it does not list appended at the bottom alphabetically but never before its own
masters (see sibling_lod.create_lod_order).

Usage:
  python tools/create_lod.py                                  # everything
  python tools/create_lod.py --plugins Oblivion.esm Tamriel.esp
  python tools/create_lod.py --worldspaces TES4Tamriel
  python tools/create_lod.py --dry-run                        # plan only
"""

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPT_DIR))

from subprocess_flags import configure_multiprocessing
from process_job import create_pool_job

configure_multiprocessing()
create_pool_job()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Generate every plugin's distant LOD once, into a "
                    "standalone AutoConvertLOD mod.")
    ap.add_argument("--output-dir", metavar="PATH",
                    help="Output directory (default: output/ in project root)")
    ap.add_argument("--plugins", nargs="+", metavar="PLUGIN",
                    help="Plugins to include, LOWEST PRIORITY FIRST. The last "
                         "one listed wins a contested reference. Default: "
                         "every converted plugin, in plugins.txt order with "
                         "unlisted plugins appended.")
    ap.add_argument("--worldspaces", nargs="+", metavar="EDID",
                    help="Only generate these worldspaces (default: every "
                         "worldspace the source shipped LOD for)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the plan and generate nothing")
    args = ap.parse_args()

    from asset_convert.lod_gen import (generate_lod,
                                       _textures_root as _lod_textures_root)
    from asset_convert.terrain_lod import generate_terrain_lod
    from asset_convert.sibling_lod import (converted_plugins, create_lod_order,
                                           lod_worldspaces, owner_map,
                                           merge_cloud_bank, _master_chain,
                                           touched_worldspace_fids,
                                           LOD_DIR_NAME)
    from asset_convert.terrain_lod import _find_worldspace_fid
    from asset_convert.lod_gen import _formid_remap_table

    out_root = (Path(args.output_dir) if args.output_dir
                else SCRIPT_DIR / "output")
    export_root = SCRIPT_DIR / "export"
    lod_dir = out_root / LOD_DIR_NAME

    print("=" * 54)
    print("  CREATE LOD")
    print("=" * 54)
    print(f"  Output dir: {out_root}")
    print(f"  LOD mod:    {lod_dir}")

    available = converted_plugins(out_root)
    if args.plugins:
        # Honour the caller's order verbatim — it IS the conflict resolution
        # and the GUI dialog let the user arrange it by hand. Names with no
        # converted output are dropped rather than failing the run, so a stale
        # saved selection never blocks the plugins that ARE built.
        plugins = [p for p in args.plugins if p in available]
        missing = [p for p in args.plugins if p not in available]
        if missing:
            print(f"  Not converted, skipping: {', '.join(missing)}")
    else:
        plugins = create_lod_order(available, export_root)

    if not plugins:
        print("  No converted plugin to generate LOD for.")
        return 0

    print(f"  Plugins ({len(plugins)}, lowest priority first):")
    for i, name in enumerate(plugins, 1):
        win = "  <- wins contested references" if i == len(plugins) else ""
        print(f"    {i}. {name}{win}")

    wanted = (list(args.worldspaces) if args.worldspaces
              else lod_worldspaces(plugins, export_root, out_root))
    print(f"  Worldspaces ({len(wanted)}): {', '.join(wanted) or '(none)'}")
    print()

    # Resolve every worldspace to its owner and overlay stack before generating
    # anything, so a bad selection is reported as a plan rather than discovered
    # halfway through an hour of baking.
    # WRLD-FormID lookups, memoised per (owner, edid). Several worldspaces share
    # an owner — Oblivion.esm owns 18 — and resolving each one independently
    # meant re-reading its 613 MB. The jobs below are built in worldspace order,
    # not owner order, so the file's bytes are held only for the duration of one
    # owner's lookups and the resolved FormIDs are what persist.
    _fid_cache: dict = {}

    def _worldspace_fid(esm: Path, edid: str):
        key = (str(esm).lower(), edid.lower())
        if key in _fid_cache:
            return _fid_cache[key]
        # NORMALISED, because it is compared against ids from OTHER plugins
        # (`touched_worldspace_fids`), and a raw id is only meaningful inside
        # the file it came from.
        gmap = _formid_remap_table(esm)
        raw = esm.read_bytes()
        try:
            # Resolve every worldspace this owner is responsible for while its
            # bytes are in hand, so one read serves all of them.
            for w in wanted:
                k = (str(esm).lower(), w.lower())
                if k not in _fid_cache:
                    f = _find_worldspace_fid(raw, len(raw), w)
                    _fid_cache[k] = (None if f is None
                                     else gmap[f >> 24] | (f & 0x00FFFFFF))
        finally:
            del raw
        return _fid_cache.get(key)

    # Which worldspaces does each plugin actually put records in? Depending on a
    # worldspace's owner is not the same as editing that worldspace, and an
    # overlay contributing nothing still costs a full ESM parse per worldspace
    # in BOTH generators. Scanned once per plugin here, then reused below.
    touched = {}
    for name in plugins:
        esm = out_root / name / name
        if esm.is_file():
            try:
                touched[name] = touched_worldspace_fids(esm)
            except OSError:
                touched[name] = None      # unreadable -> never filter it out
        else:
            touched[name] = None

    # Every worldspace's owner in ONE pass over the load order, rather than
    # `worldspace_owner` per worldspace re-listing every plugin each time.
    owners = owner_map(wanted, plugins, export_root, out_root)

    jobs = []
    for edid in wanted:
        owner = owners.get(edid)
        if owner is None:
            print(f"  '{edid}': no selected plugin supplies terrain for it; "
                  f"skipping")
            continue
        owner_esm = out_root / owner / owner
        if not owner_esm.is_file():
            print(f"  '{edid}': {owner} has no converted ESM; skipping")
            continue
        # Overlays: every OTHER plugin that actually depends on the owner,
        # in load order. Records merge by FormID, so a REFR one plugin moved
        # and another left alone resolves exactly as the engine would.
        #
        # The dependency gate is not optional. A plugin that does not rest on
        # the owner cannot legally touch its worldspace, and overlaying one
        # anyway merges two unrelated games: Nehrim.esm is standalone with its
        # own worldspace, and stacking it onto Oblivion's TES4Tamriel would
        # pull its FormIDs into Cyrodiil's tiles.
        contributors = [n for n in plugins if n != owner
                        and owner in _master_chain(n, export_root, plugins)]

        # ...and of those, only the ones that actually have records in THIS
        # worldspace. The dependency gate above is about what a plugin is
        # ALLOWED to touch; this is about what it DOES touch. Morrowind_ob.esm
        # rests on Oblivion.esm and so passes the gate for all 18 of its
        # worldspaces, while placing nothing in any of them — 18 parses of a
        # 206 MB file to merge zero records.
        #
        # A plugin whose ESM could not be read is kept rather than dropped: the
        # cost of an unnecessary overlay is time, the cost of a missing one is
        # lost LOD.
        wrld_fid = _worldspace_fid(owner_esm, edid)
        if wrld_fid is not None:
            scoped = [n for n in contributors
                      if touched.get(n) is None or wrld_fid in touched[n]]
            if len(scoped) != len(contributors):
                skipped = [n for n in contributors if n not in scoped]
                print(f"  '{edid}': {len(skipped)} dependent(s) place nothing "
                      f"here, not overlaid ({', '.join(skipped)})")
            contributors = scoped

        overlays = [out_root / n / n for n in contributors
                    if (out_root / n / n).is_file()]
        jobs.append((edid, owner, owner_esm, overlays, contributors))

    if not jobs:
        print("Nothing to generate.")
        return 0

    print("  Plan:")
    for edid, owner, _esm, overlays, contributors in jobs:
        print(f"    {edid}: records from {owner}, "
              f"{len(overlays)} overlay(s) on top"
              + (f" ({', '.join(contributors)})" if contributors else ""))
    print()

    if args.dry_run:
        print("Dry run - nothing generated.")
        return 0

    ok_all = True
    for edid, owner, owner_esm, overlays, contributors in jobs:
        print("-" * 54)
        print(f"  {edid}  (records: {owner})")
        print("-" * 54)

        # Clear this worldspace's tiles from a previous run before rebaking.
        # The bake writes only the tiles it produces THIS time, so a tile an
        # earlier run emitted — at coordinates this selection no longer covers,
        # or from a plugin since deselected — would otherwise survive as an
        # orphan and still ship. Scoped to this worldspace's own tile names so a
        # run covering several worldspaces never deletes a sibling's fresh
        # output, and so the shared, coordinate-free object .nifs are untouched.
        stale = 0
        for sub in ("meshes/terrain", "textures/terrain"):
            d = lod_dir / sub / edid
            if not d.is_dir():
                continue
            for f in d.rglob(f"{edid}.*"):
                if f.is_file():
                    f.unlink()
                    stale += 1
        if stale:
            print(f"  Cleared {stale} tile(s) from a previous run")

        # Assets come from the owner AND every contributor: one tile draws
        # objects from all of them, so a model converted into only one plugin's
        # output still has to be findable. Scoped to this worldspace's
        # contributors rather than every selected plugin, for the same reason
        # the overlays are — an unrelated plugin's meshes are not in this world.
        asset_dirs = [out_root / n for n in [owner] + contributors
                      if (out_root / n).is_dir()]

        cloud_rel = merge_cloud_bank(out_root, lod_dir, edid, owner,
                                     contributors)
        if cloud_rel:
            print(f"  World-map cloud bank -> {cloud_rel}")

        print(f"  Generating object LOD...")
        ok = generate_lod(
            esm_path=owner_esm,
            output_dir=lod_dir,
            worldspace_edid=edid,
            # No tile-ownership skip: this mod is the ONLY place these tiles
            # are generated now, so there is no master output already shipping
            # them and nothing may be skipped. Passing any plugin dir here
            # would skip every tile it once built and leave holes.
            master_dirs=None,
            master_mesh_dirs=asset_dirs,
            master_texture_dirs=asset_dirs,
            overlay_paths=overlays,
            # The WHOLE worldspace, every time. `only_cells` existed to rebake
            # just the tiles an override touched, on top of a master's own LOD
            # run. There is no separate master run to sit on top of any more.
            only_cells=None,
            # Derived _far.nif meshes stay with the plugin that ships the full
            # model; only tiles and LODSettings land in the LOD mod.
            far_nif_dirs=asset_dirs,
        )

        print(f"  Generating terrain LOD...")
        ok_terrain = generate_terrain_lod(
            esm_path=owner_esm,
            output_dir=lod_dir,
            worldspace_edid=edid,
            overlay_paths=overlays,
            only_cells=None,
            extra_texture_roots=[_lod_textures_root(Path(d))
                                 for d in asset_dirs],
        )
        ok_all = ok_all and ok and ok_terrain
        print()

    print("-" * 54)
    if ok_all:
        print(f"LOD written to {lod_dir}")
        print("Install it AFTER the plugins it covers.")
    else:
        print("Create LOD finished with errors (see above).")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
