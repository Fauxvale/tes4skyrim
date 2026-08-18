"""Asset conversion pipeline: BSA extraction → mesh/texture conversion → output.

Three separate callable steps:

  extract_bsas(source_file, data_path, extract_dir, force)
      Pull all assets from BSA archives into extract_dir/<source_name>./

  convert_meshes(source_file, extract_dir, output_dir)
      Convert NIFs and copy textures into output/<source_name>/.

  convert_speedtrees(source_file, extract_dir, output_dir)
      Convert SpeedTree `.spt` files into NIFs under
      `output/<source_name>/meshes/tes4/speedtrees`.

  convert_sounds(source_file, extract_dir, output_dir)
      Convert sound files from extracted BSA into XWM and move into output_dir/<source_name>/sound/tes4/.
      Assumes extract_bsas has already been run."""
import os
import shutil
from pathlib import Path

from . import (bsa_extract, grass_profile, landscape_normals, nif_converter,
               spt_converter, texture_prune, wearable_plan)


def extract_bsas(source_file, data_path, extract_dir='export', force=False):
    """Extract BSA archives for a plugin into extract_dir/<source_name>/.

    Args:
        source_file: Plugin filename (e.g. 'Oblivion.esm').
        data_path: Path to Oblivion Data directory.
        extract_dir: Root extraction directory (default: export).
        force: Force re-extraction even if already cached.

    Returns:
        dict from bsa_extract.extract_assets_for_file.
    """
    print("=" * 60)
    print("BSA Extraction")
    print("=" * 60)
    result = bsa_extract.extract_assets_for_file(
        source_file, data_path, Path(extract_dir), force=force
    )
    return result


_PARALLAX_NOTICE = """\
PARALLAX — READ THIS BEFORE PLAYING
===================================

This conversion was built with --parallax, so Oblivion's own parallax surfaces
(dungeon walls, rock, architecture) ship as Skyrim height maps.

YOU NEED ONE OF THESE INSTALLED:

  * Community Shaders  (verified in game with this conversion), or
  * ENB

WITHOUT ONE, THOSE SURFACES RENDER WRONG. Not flat -- wrong: the texture
visibly swims across the geometry as the camera moves. Tested on vanilla SSE,
and the "SSE Parallax Shader Fix" did NOT repair it. If you do not run
Community Shaders or an ENB, rebuild without --parallax; you lose the effect
and everything renders correctly.

WHAT WAS CONVERTED

Oblivion marks a surface for parallax per shape and keeps the height field in
the diffuse texture's alpha channel. Every shape carrying that mark whose
texture actually holds height data was rebuilt as a Skyrim heightmap shape,
with the height written out beside the diffuse as <name>_p.dds (BC4).

Shapes marked for parallax whose texture holds NO height data were left alone.
That is not a gap in the conversion: over half of Oblivion's flagged textures
ship no alpha channel at all, so Oblivion itself renders no parallax on them
either. Building a height map there would have invented data and produced the
swimming surface described above.
"""


def _write_parallax_notice(plugin_dir):
    """Ship the Community-Shaders requirement WITH the mod, not just in a repo
    README nobody downloading the output ever sees."""
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / 'PARALLAX-READ-ME.txt').write_text(
        _PARALLAX_NOTICE, encoding='utf-8')
    print('  Wrote PARALLAX-READ-ME.txt (Community Shaders / ENB required)')


def convert_meshes(source_file, extract_dir='export', output_dir='output',
                   mesh_subdirs=None, parallax=False):
    """Convert extracted NIFs and copy textures into `output_dir/<source_name>/`.
    Assumes BSA extraction has already been run (extract_bsas).

    Args:
        source_file:  Plugin filename (e.g. 'Oblivion.esm').
        extract_dir:  Root extraction directory (default: export).
        output_dir:   Final output root (files placed under output_dir/<source_name>/).
        mesh_subdirs: Optional list of root mesh subfolders to include (e.g.
                      ['architecture', 'clutter']). None means all subfolders.
        parallax:     Carry Oblivion's parallax across as Skyrim height maps.
                      Off by default — see asset_convert/parallax.py; the
                      output needs Community Shaders or ENB.

    Returns a dict with keys: 'mesh_conversion', 'textures_copied', 'other_copied'.
    """
    extract_dir = Path(extract_dir)
    output_dir = Path(output_dir)
    source_name = Path(source_file).name

    stats = {
        'mesh_conversion': {},
        'textures_copied': 0,
        'other_copied': 0,
    }

    plugin_dir = output_dir / source_name

    # -----------------------------------------------------------------------
    # NIF Mesh Conversion
    # -----------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("NIF Mesh Conversion")
    print("=" * 60)
    mesh_src = extract_dir / source_name / 'meshes'
    if mesh_src.exists():
        mesh_dst = plugin_dir / 'meshes' / 'tes4'
        # Which _0/_1/plain variants each wearable is actually referenced as —
        # without this the converter writes all three for every armor and
        # clothing mesh and the plugin only ever loads one or two of them.
        plan = wearable_plan.build_plan(extract_dir / source_name)
        print(f"  Wearable variant plan: {len(plan)} meshes referenced by "
              f"ARMO/CLOT")
        stats['mesh_conversion'] = nif_converter.batch_convert(
            str(mesh_src), output_dir=str(mesh_dst),
            fix_textures=True, remap_skeleton=None,
            subdir_filter=mesh_subdirs,
            wearable_plan=plan,
            parallax=parallax,
        )
        if parallax:
            _write_parallax_notice(plugin_dir)
        # The textures the converted meshes reference, harvested as they were
        # written.  Persisted because the prune runs in a later phase (after LOD
        # and speedtrees add their own meshes), possibly a separate invocation.
        #
        # A --mesh-subdirs run only saw part of the tree, so it must ADD to the
        # manifest rather than replace it: overwriting leaves the prune believing
        # nothing outside those folders uses a texture, and it deletes the rest.
        _used = stats['mesh_conversion'].pop('textures_used', set())
        if mesh_subdirs:
            _used = set(_used) | texture_prune.read_manifest(plugin_dir)
        texture_prune.write_manifest(plugin_dir, _used)
    else:
        print(f"  No meshes found at {mesh_src}")
        stats['mesh_conversion'] = {'converted': 0, 'skipped': 0, 'errors': 0}

    # -----------------------------------------------------------------------
    # Grass models — GRAS NIFs (from the export's GRAS.txt) get the vanilla
    # grass shader profile and a copy under meshes\landscape\grass\, the
    # location every working GRAS record uses (see grass_profile module doc).
    # -----------------------------------------------------------------------
    if mesh_src.exists():
        processed, modified, missing = grass_profile.run(
            extract_dir / source_name, plugin_dir / 'meshes')
        stats['grass_profile'] = {
            'processed': processed, 'modified': modified, 'missing': missing}
        print(f"  Grass models: {processed} placed under landscape\\grass, "
              f"{modified} profiled"
              + (f", {missing} missing" if missing else ""))

    # -----------------------------------------------------------------------
    # Copy Textures
    # -----------------------------------------------------------------------
    print("\n" + "=" * 60)
    print("Copy Textures to Output")
    print("=" * 60)

    tex_src = extract_dir / source_name / 'textures'
    if tex_src.exists():
        tex_dst = plugin_dir / 'textures' / 'tes4'
        stats['textures_copied'] = _copy_tree(tex_src, tex_dst)
        print(f"  Textures: {stats['textures_copied']} files -> {tex_dst}")

        # Landscape normal maps: DXT1 has no alpha channel, which Skyrim's
        # landscape shader reads as a full-strength specular mask (shiny
        # ground).  Re-container as DXT5 with a dark alpha.  Runs after the
        # copy so re-copies don't resurrect the DXT1 versions.
        checked, fixed = landscape_normals.run(tex_dst / 'landscape')
        stats['landscape_normals_fixed'] = fixed
        print(f"  Landscape normals: {checked} checked, {fixed} DXT1->DXT5 fixed")

    return stats

def convert_speedtrees(source_file, extract_dir='export', output_dir='output'):
    """Convert SpeedTree `.spt` files into NIFs and place them under
    `output_dir/<source_name>/meshes/tes4/speedtrees`.
    """
    extract_dir = Path(extract_dir)
    output_dir = Path(output_dir)
    source_name = Path(source_file).name

    plugin_dir = output_dir / source_name

    spt_stats = {'spt_conversion': {'ok': 0, 'fail': 0, 'skip': 0}}
    spt_src = extract_dir / source_name / 'trees'
    # A dependent plugin authors TREE records for art its MASTER ships, so the
    # masters' trees/ dirs are searched for any .spt this export lacks. Read
    # from the export header rather than a fixed list -- the chain differs per
    # plugin (Valenwood: Oblivion, Tamriel, Anequina).
    master_tree_dirs = []
    header = extract_dir / source_name / '_HEADER.txt'
    if header.is_file():
        for line in open(header, encoding='utf-8', errors='replace'):
            if line.startswith('Master['):
                name = line.partition('=')[2].strip()
                d = extract_dir / name / 'trees'
                if d.is_dir():
                    master_tree_dirs.append(d)
    if spt_src.exists():
        spt_dst = plugin_dir / 'meshes' / 'tes4' / 'speedtrees'
        # tree textures ship via the generic texture copy (textures/tes4/trees/)
        spt_stats['spt_conversion'] = spt_converter.convert_spt_directory(
            spt_src, spt_dst, export_dir=extract_dir / source_name,
            master_tree_dirs=master_tree_dirs)
    else:
        print(f"  No trees/ directory found at {spt_src}")

    return spt_stats


def convert_sounds(source_file, extract_dir='export', output_dir='output',
                   ffmpeg_path='ffmpeg'):
    """Convert extracted sound files to XWM format.  Delegates to audio_converter.

    Args:
        source_file: Plugin filename (e.g. 'Oblivion.esm').
        extract_dir: Root extraction directory (default: export).
        output_dir:  Final output root.
        ffmpeg_path: Path to ffmpeg executable (default: 'ffmpeg' from PATH).

    Returns:
        dict with keys: converted, copied, failed, total.
    """
    from .audio_converter import convert_sounds as _ac_convert
    return _ac_convert(source_file, extract_dir=extract_dir,
                       output_dir=output_dir, ffmpeg_path=ffmpeg_path)


def _copy_tree(src, dst):
    """Copy a directory tree, returning file count."""
    count = 0
    for root, _dirs, files in os.walk(src):
        for fname in files:
            src_file = Path(root) / fname
            rel = src_file.relative_to(src)
            dst_file = dst / rel
            os.makedirs(dst_file.parent, exist_ok=True)
            shutil.copy2(str(src_file), str(dst_file))
            count += 1
    return count


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(
        description='Asset pipeline: extract BSAs and/or convert assets')
    sub = parser.add_subparsers(dest='cmd')

    p_extract = sub.add_parser('extract', help='Extract BSA archives only')
    p_extract.add_argument('source_file')
    p_extract.add_argument('--data-path', required=True)
    p_extract.add_argument('--extract-dir', default='export')
    p_extract.add_argument('--force', action='store_true')

    p_convert = sub.add_parser('convert', help='Convert extracted assets (meshes + speedtrees)')
    p_convert.add_argument('source_file')
    p_convert.add_argument('--extract-dir', default='export')
    p_convert.add_argument('--output-dir', default='output')

    p_sounds = sub.add_parser('sounds', help='Copy sound files to output')
    p_sounds.add_argument('source_file')
    p_sounds.add_argument('--extract-dir', default='export')
    p_sounds.add_argument('--output-dir', default='output')

    args = parser.parse_args()
    if args.cmd == 'extract':
        extract_bsas(args.source_file, args.data_path,
                     extract_dir=args.extract_dir, force=args.force)
    elif args.cmd == 'convert':
        convert_meshes(args.source_file,
                       extract_dir=args.extract_dir, output_dir=args.output_dir)
        convert_speedtrees(args.source_file,
                           extract_dir=args.extract_dir, output_dir=args.output_dir)
    elif args.cmd == 'sounds':
        convert_sounds(args.source_file,
                    extract_dir=args.extract_dir, output_dir=args.output_dir)
    else:
        parser.print_help()

