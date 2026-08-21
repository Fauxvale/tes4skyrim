"""Move imported mods onto the shared GROUP folder layout.

Plugins imported together from one archive used to get a folder each --
`export/TamRes.esm/`, `export/TamRes.esp/`, ... -- with the same meshes and
textures hard-linked (or, when an import was re-run per plugin, copied
outright) into every one of them. They now share ONE folder named for the mod,
with each plugin's records in a subfolder:

    export/Tamriel Resource Pack Full 2.0/
        _source/   <every plugin binary> + the retained archive
        meshes/ textures/ sound/ trees/ misc/    <- shared
        collision_cache.bin, mesh_bounds_cache.json, ...   <- shared, asset-keyed
        TamRes.esm/     <- that plugin's .txt dump and per-plugin caches
        TamRes.esp/

This script performs that move for mods already on disk. It is idempotent: a
mod already laid out this way is reported and skipped.

    python tools/migrate_group_layout.py                 # dry run (default)
    python tools/migrate_group_layout.py --apply         # do it
    python tools/migrate_group_layout.py --apply --mod "Tamriel Resource Pack Full 2.0"

Stale `output/<plugin>/` trees are reported but only deleted with
`--clean-output`: they are reconvertible, and deleting them costs a rebuild.
"""
import argparse
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asset_convert import source_registry  # noqa: E402

# Folders holding SHARED assets: keyed by asset path, identical for every
# plugin in the mod.
ASSET_DIRS = ('meshes', 'textures', 'sound', 'trees', 'misc')

# Caches keyed by ASSET path -- valid for the whole group. Verified rather than
# assumed: collision_extract.scan_mesh_data keys entries on the mesh's relative
# path with no plugin identity, and the door/navmesh caches are located via
# os.path.dirname(collision_cache), so they follow it.
SHARED_CACHES = (
    'collision_cache.bin',
    'mesh_bounds_cache.json',
    'door_centers_cache.json',
    'door_panel_axis_cache.json',
    # Texture manifests list the textures the SHARED meshes reference, and
    # asset_pipeline writes them beside those meshes. Treating them as
    # per-plugin leftovers stranded them in a record folder the prune step
    # never reads, so every texture looked unreferenced.
    'textures_used.txt',
    'overlay_diffuses.txt',
)


def _human(n):
    if n < 1024:
        return str(n) + " B"
    for unit in ('KB', 'MB', 'GB'):
        n /= 1024.0
        if n < 1024 or unit == 'GB':
            return "%.1f %s" % (n, unit)
    return "%.1f GB" % n


def _tree_size(path, seen=None):
    """Bytes in `path`, counting a hard-linked file only once.

    The old layout hard-linked a secondary plugin's assets to the primary's,
    so a naive sum reports the same bytes two or three times over and makes
    the reclaim figure a fiction. `seen` carries inode identity ACROSS calls
    so the second tree to be walked reports (correctly) almost nothing.
    """
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(dirpath, f)
            try:
                st = os.stat(fp)
            except OSError:
                continue
            if st.st_nlink > 1 and seen is not None:
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
            total += st.st_size
    return total


def _merge_into(src, dst, apply):
    """Move every file from `src` into `dst`, keeping whatever `dst` has.

    Same-path files are byte-identical copies of one payload, so an existing
    destination file always wins and the source copy is simply dropped.
    """
    moved = kept = 0
    for dirpath, _dirs, files in os.walk(src):
        rel = Path(dirpath).relative_to(src)
        for fn in files:
            s = Path(dirpath) / fn
            d = dst / rel / fn
            if d.exists():
                kept += 1
                if apply:
                    s.unlink()
                continue
            moved += 1
            if apply:
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(s), str(d))
    return moved, kept


def migrate(export_dir, only_mod=None, apply=False, clean_output=False,
            out_root=None, log=print):
    export_dir = Path(export_dir)
    groups = source_registry.groups(export_dir)
    if not groups:
        log('No imported mods.')
        return 0

    reclaimed = 0
    # Inode identity, shared across every tree walked in this run.
    seen_inodes = set()
    for gid, label, plugins in groups:
        if only_mod and only_mod.lower() not in (label.lower(), gid.lower()):
            continue
        entry = source_registry.get(export_dir, plugins[0]) if plugins else None
        if not entry or not entry.get('plugin'):
            log('-- %s: asset-only mod, nothing to move' % label)
            continue

        group_dir = export_dir / (source_registry._sanitize_folder(label)
                                  or plugins[0])
        log('')
        log('== %s' % label)
        log('   target: %s' % group_dir)

        legacy = [export_dir / n for n in plugins
                  if (export_dir / n).is_dir()
                  and (export_dir / n) != group_dir]

        # The group folder can BE one of the plugin folders: an archive named
        # `A.esm.zip` holding two plugins yields the label `A.esm`. That
        # plugin's records are then already sitting in the group root, where
        # `record_dir` (which nests every member once a mod has >1 plugin)
        # will not look for them. Move them down into their own subfolder.
        self_named = next((n for n in plugins
                           if (export_dir / n) == group_dir), None)
        if self_named and source_registry.record_dir(
                export_dir, self_named) != group_dir:
            stray = [f for f in sorted(group_dir.iterdir())
                     if f.name != source_registry.SOURCE_SUBDIR
                     and f.name not in ASSET_DIRS
                     and f.name not in SHARED_CACHES
                     and f.name not in plugins]
            if stray:
                log('   %s: %d record entries -> %s/'
                    % (group_dir.name, len(stray), self_named))
                if apply:
                    rec = group_dir / self_named
                    rec.mkdir(parents=True, exist_ok=True)
                    for f in stray:
                        dest = rec / f.name
                        if dest.exists():
                            if f.is_dir():
                                shutil.rmtree(f, ignore_errors=True)
                            else:
                                f.unlink()
                        else:
                            shutil.move(str(f), str(dest))

        if not legacy and group_dir.is_dir():
            log('   already migrated')
            continue

        src_sub = group_dir / source_registry.SOURCE_SUBDIR
        if apply:
            group_dir.mkdir(parents=True, exist_ok=True)
            src_sub.mkdir(parents=True, exist_ok=True)

        for old in legacy:
            plugin = old.name
            log('   from %s/' % plugin)

            # 1) Plugin binary + retained archive -> the ONE _source/.
            old_src = old / source_registry.SOURCE_SUBDIR
            if old_src.is_dir():
                for f in sorted(old_src.iterdir()):
                    if not f.is_file():
                        continue
                    dest = src_sub / f.name
                    if dest.exists():
                        sz = f.stat().st_size
                        reclaimed += sz
                        log('       drop duplicate %s (%s)'
                            % (f.name, _human(sz)))
                        if apply:
                            f.unlink()
                    else:
                        log('       keep %s' % f.name)
                        if apply:
                            shutil.move(str(f), str(dest))

            # 2) Shared assets -> group root.
            for cat in ASSET_DIRS:
                d = old / cat
                if not d.is_dir():
                    continue
                moved, kept = _merge_into(d, group_dir / cat, apply)
                log('       %s: %d moved, %d already present'
                    % (cat, moved, kept))

            # 3) Asset-keyed caches -> group root.
            for cache in SHARED_CACHES:
                f = old / cache
                if not f.is_file():
                    continue
                dest = group_dir / cache
                if dest.exists():
                    if apply:
                        f.unlink()
                    log('       drop duplicate %s' % cache)
                else:
                    if apply:
                        shutil.move(str(f), str(dest))
                    log('       keep %s' % cache)

            # 4) Everything else is PER PLUGIN: the .txt dump, navmesh geom
            #    cache, creature projects, voice durations, animdata.
            #
            #    Nest ONLY when the mod ships more than one plugin -- that is
            #    exactly `source_registry.record_dir`'s rule, and the two must
            #    agree or the pipeline looks for records where they are not.
            #    A one-plugin mod keeps them in the group root.
            #    Ask the RESOLVER, never re-derive the rule: it keys on the
            #    archive's shipped plugin list, and a second copy of the test
            #    here would be one refactor away from disagreeing with it.
            rec = source_registry.record_dir(export_dir, plugin)
            leftovers = [f for f in sorted(old.iterdir())
                         if f.name != source_registry.SOURCE_SUBDIR
                         and f.name not in ASSET_DIRS
                         and f.name not in SHARED_CACHES]
            if leftovers:
                log('       -> %s/ : %d record/cache entries'
                    % (rec.relative_to(export_dir).as_posix(), len(leftovers)))
                if apply:
                    rec.mkdir(parents=True, exist_ok=True)
                    for f in leftovers:
                        dest = rec / f.name
                        if dest.exists():
                            if f.is_dir():
                                shutil.rmtree(f, ignore_errors=True)
                            else:
                                f.unlink()
                        else:
                            shutil.move(str(f), str(dest))

            # 5) Whatever is left is duplicate payload.
            if old.is_dir():
                left = _tree_size(old, seen_inodes)
                reclaimed += left
                log('       remove %s/ (%s not already accounted for)'
                    % (plugin, _human(left)))
                if apply:
                    shutil.rmtree(old, ignore_errors=True)

        # 6) Registry: point every member at the group folder.
        if apply:
            for name in plugins:
                e = source_registry.get(export_dir, name)
                if not e:
                    continue
                e['group_dir'] = group_dir.name
                e['group_plugins'] = sorted(plugins, key=str.lower)
                e['plugin_path'] = os.path.relpath(
                    src_sub / name, export_dir.parent).replace('\\', '/')
                arch_name = Path(e.get('archive_retained') or '').name
                if arch_name and (src_sub / arch_name).is_file():
                    e['archive_retained'] = os.path.relpath(
                        src_sub / arch_name,
                        export_dir.parent).replace('\\', '/')
                source_registry.put(export_dir, name, e)

        # 7) Stale output trees.
        if out_root is not None and Path(out_root).is_dir():
            for name in plugins:
                stale = Path(out_root) / name
                if stale.is_dir():
                    sz = _tree_size(stale, seen_inodes)
                    log('   output/%s/ is stale (%s)%s'
                        % (name, _human(sz),
                           '' if clean_output else '  [use --clean-output]'))
                    if clean_output:
                        reclaimed += sz
                        if apply:
                            shutil.rmtree(stale, ignore_errors=True)

    log('')
    log('Reclaimed: %s%s' % (_human(reclaimed),
                             '' if apply else '  (DRY RUN -- nothing changed)'))
    return 0


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--export-dir', default='export')
    ap.add_argument('--output-dir', default='output')
    ap.add_argument('--mod', help='only this mod (group label)')
    ap.add_argument('--apply', action='store_true',
                    help='actually move/delete (default is a dry run)')
    ap.add_argument('--clean-output', action='store_true',
                    help='also delete stale output/<plugin>/ trees')
    args = ap.parse_args()
    return migrate(Path(args.export_dir), only_mod=args.mod, apply=args.apply,
                   clean_output=args.clean_output,
                   out_root=Path(args.output_dir))


if __name__ == '__main__':
    raise SystemExit(main())
