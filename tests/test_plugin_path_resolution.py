"""No module may build a plugin's folder by joining its NAME onto a root.

Plugins imported together from one mod archive share ONE folder named for the
MOD, so `export/<plugin>/`, `output/<plugin>/` and `output/<plugin>/<plugin>`
are all wrong for them. The correct answers come from three resolvers:

    output_layout.record_dir(export_dir, plugin)       records + own caches
    output_layout.asset_root(export_dir, plugin)       shared meshes/textures
    output_layout.plugin_out_root(out_root, plugin, export_dir)
    output_layout.plugin_esm(out_root, plugin, export_dir)
    output_layout.master_record_dir(export_dir, master)

This rule was learned the hard way. The group layout landed, and the same
`root / name` idiom kept surfacing in module after module -- the CLI's missing
-master check, then the GUI's import dialog, then the importer's own master
loader, then LOD, books and half a dozen tools. Each one reported a master that
WAS converted as missing, and the importer's copy silently diffed every
override against nothing. Three separate rounds of user-visible breakage, all
one idiom.

So the ban is enforced here rather than remembered. A new call site that joins
a plugin name onto a root fails this test with the file and line, and the fix
is always the same: call the resolver.

If a genuinely new site needs an exception, add it to ALLOWED with a comment
saying why the plain join is correct there.
"""
import ast
import os
import sys
from pathlib import Path

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Variables that name an export/output ROOT -- a folder that CONTAINS plugin
# folders, and so must never have a plugin name joined onto it directly.
ROOT_NAMES = {
    'export_dir', 'export_root', 'EXPORT_DIR', 'extract_dir', 'export',
    'out_root', 'output_dir', 'OUTPUT_DIR', 'out_dir', 'output_root',
    'output', 'export_base', 'out_base',
    # `root` was missing, and three real defects hid behind that one omission
    # (import_main._master_export_dirs, creature_races._load_projects,
    # overrides.OverrideContext). It is the commonest name for exactly this
    # variable, so it is the LAST one that should have been left out.
    'root', 'export_dir_root', 'exp_root',
}

# Names that are always a FILE inside a plugin's folder, never a plugin. Any
# join whose right-hand side is one of these is fine by construction.
FILENAME_HINTS = {'fname', 'filename', 'basename', 'cache_name'}

# Variables that hold a PLUGIN name (as opposed to a fixed filename).
PLUGIN_NAMES = {
    'name', 'plugin', 'master', 'm', 'n', 'sib', 'source_name', 'file_name',
    'owner', 'cur', 'plugin_name', 'names',
}

# `fname` was in BOTH sets. FILENAME_HINTS wins and is now actually consulted:
# `os.path.join(root, fname)` inside an os.walk loop is the single commonest
# join in this codebase and is always correct.
PLUGIN_NAMES -= FILENAME_HINTS

# Directories that are not shipped pipeline code.
SKIP_DIRS = {'__pycache__', 'references', '.git', 'temp', 'external',
             'node_modules', 'navmesh_cache', 'build', 'dist', 'tests',
             'output', 'export', 'TESGameSelect'}

# (file, symbol) pairs where joining a name onto a root IS correct.
ALLOWED = {
    # The resolvers themselves -- they ARE the fallback for a plugin with no
    # registry entry, which is every game-Data plugin.
    ('output_layout.py', 'plugin_out_root'),
    ('output_layout.py', 'asset_root'),
    ('output_layout.py', 'record_dir'),
    ('asset_convert/source_registry.py', 'asset_root'),
    ('asset_convert/source_registry.py', 'record_dir'),
    ('asset_convert/source_registry.py', 'source_dir'),
    ('asset_convert/source_registry.py', 'plugin_binary'),
    ('asset_convert/sibling_lod.py', '_record_dir'),
    ('asset_convert/sibling_lod.py', '_out_root'),
    ('asset_convert/asset_pipeline.py', '_asset_root'),
    ('asset_convert/asset_pipeline.py', '_record_dir'),
    ('asset_convert/asset_pipeline.py', '_out_root'),
    ('asset_convert/audio_converter.py', '_asset_root'),
    ('asset_convert/audio_converter.py', '_out_root'),
    ('asset_convert/book_inam.py', '_record_dir'),
    ('asset_convert/book_inam.py', '_asset_root'),
    ('asset_convert/book_inam.py', '_out_root'),
    ('asset_convert/bsa_pack.py', '_out_root'),
    ('asset_convert/terrain_lod.py', '_master_record_dir'),
    ('tes5_import/overrides.py', '_master_export_dir'),
    ('tes5_import/master_manifest.py', '_master_export_dir'),
    ('convert.py', 'record_dir'),
    ('convert.py', 'plugin_out_root'),

    # mod_ingest BUILDS the group folder, so it necessarily joins a name it
    # computed itself onto the export root.
    ('asset_convert/mod_ingest.py', 'ingest'),
    ('asset_convert/mod_ingest.py', 'remove'),

    # The migration tool's whole job is walking the OLD per-plugin folders.
    ('tools/esm/migrate_group_layout.py', 'migrate'),
}


def _py_files():
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and
                  not d.startswith('.')]
        for fn in fns:
            if fn.endswith('.py'):
                yield os.path.join(dp, fn)


def _enclosing_functions(tree):
    """{lineno: function_name} for every line inside each function."""
    owner = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, 'end_lineno', node.lineno)
            for ln in range(node.lineno, end + 1):
                # Innermost wins: a nested def overwrites its parent's claim.
                owner[ln] = node.name
    return owner


def _root_name(node):
    """The ROOT variable `node` denotes, if any.

    `Path(export_dir)` counts -- it is still the root. A CALL to a resolver
    (`_out_root(...) / name`) does NOT: that has already resolved the group
    folder, and joining the plugin file onto it is exactly right.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        fn = node.func
        fname = getattr(fn, 'id', None) or getattr(fn, 'attr', None) or ''
        if fname in ('Path', 'str'):
            inner = node.args[0] if node.args else None
            return getattr(inner, 'id', None)
        return None          # a resolver's result, not a root
    return None


def _walk_bound_names(tree) -> set:
    """Names bound as the first target of a `for ... in os.walk(...)` loop.

    Such a name is whatever directory the walk is currently in -- it is NOT an
    export or output root, and joining a filename onto it is the correct and
    ubiquitous idiom. Without this, adding `root` to ROOT_NAMES flags every
    os.walk in the project.
    """
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.For, ast.AsyncFor)):
            continue
        it = node.iter
        fn = getattr(it, 'func', None)
        if not (isinstance(it, ast.Call) and getattr(fn, 'attr', None) == 'walk'):
            continue
        tgt = node.target
        elts = tgt.elts if isinstance(tgt, ast.Tuple) else [tgt]
        if elts and isinstance(elts[0], ast.Name):
            out.add(elts[0].id)
    return out


def _is_plugin_expr(node) -> bool:
    """Does `node` evaluate to a PLUGIN NAME (as opposed to a filename)?

    A bare `ast.Name` in PLUGIN_NAMES is the obvious case. The subtle one is a
    CALL: `os.path.join(root, val.strip())` is the exact idiom this test bans,
    and reading only `ast.Name` let it through in
    `import_main._master_export_dirs` for a whole release. So a method call is
    followed to the value it is called ON -- `val.strip()` is judged by `val`.

    Deliberately NOT flagged: a string constant (a fixed subfolder like
    'meshes'), and any name outside PLUGIN_NAMES. `os.path.join(root, fname)`
    inside an os.walk loop is the overwhelmingly common shape of a join onto a
    variable called `root`, and it is always correct.
    """
    if isinstance(node, ast.Name):
        return node.id in PLUGIN_NAMES
    if isinstance(node, ast.Call):
        fn = node.func
        # `x.strip()` / `x.lower()` -- judge by `x`, the value being cleaned.
        if isinstance(fn, ast.Attribute) and fn.attr in (
                'strip', 'lstrip', 'rstrip', 'lower', 'upper'):
            return _is_plugin_expr(fn.value)
    return False


def _violations_in(path):
    rel = os.path.relpath(path, ROOT).replace(os.sep, '/')
    try:
        src = open(path, encoding='utf-8').read()
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return []
    owner = _enclosing_functions(tree)
    walked = _walk_bound_names(tree)
    lines = src.splitlines()
    out = []

    def flag(node, right):
        if not _is_plugin_expr(right):
            return
        fn = owner.get(node.lineno, '<module>')
        if (rel, fn) in ALLOWED:
            return
        # Per-line opt-out for a join that is genuinely not a plugin folder
        # (a record FILE, a bare-file fallback). Must say why.
        if 'noqa: plugin-path' in lines[node.lineno - 1]:
            return
        out.append((rel, node.lineno, fn,
                    lines[node.lineno - 1].strip()))

    for node in ast.walk(tree):
        # ROOT / name
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            r = _root_name(node.left)
            if r in ROOT_NAMES and r not in walked:
                flag(node, node.right)
        # os.path.join(ROOT, name)
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == 'join'
                and len(node.args) >= 2):
            r = _root_name(node.args[0])
            if r in ROOT_NAMES and r not in walked:
                flag(node, node.args[1])
    return out


def test_no_module_joins_a_plugin_name_onto_a_root():
    found = []
    for path in _py_files():
        found.extend(_violations_in(path))
    if found:
        report = "\n".join(
            f"  {rel}:{ln}  in {fn}()\n      {txt}"
            for rel, ln, fn, txt in sorted(found))
        pytest.fail(
            "A plugin name is joined onto an export/output root.\n"
            "An imported mod's plugins share ONE folder named for the MOD, so\n"
            "this path does not exist for them and the lookup silently fails.\n"
            "Use output_layout.record_dir / asset_root / plugin_out_root /\n"
            "plugin_esm / master_record_dir instead.\n\n" + report)


def test_the_resolvers_agree_on_a_plugin_with_no_registry_entry(tmp_path):
    """A game-Data plugin must resolve exactly as it always did."""
    from output_layout import (asset_root, record_dir, plugin_out_root,
                               plugin_esm)

    exp = tmp_path / 'export'
    exp.mkdir()
    assert record_dir(exp, 'Oblivion.esm') == exp / 'Oblivion.esm'
    assert asset_root(exp, 'Oblivion.esm') == exp / 'Oblivion.esm'
    out = tmp_path / 'output'
    assert plugin_out_root(out, 'Oblivion.esm', exp) == out / 'Oblivion.esm'
    assert (plugin_esm(out, 'Oblivion.esm', exp)
            == out / 'Oblivion.esm' / 'Oblivion.esm')


# ---------------------------------------------------------------------------
#  Asset files must not be looked up inside a RECORD directory
# ---------------------------------------------------------------------------

# Files and folders that live in the SHARED asset tree. Reaching for one of
# these off a variable that holds a plugin's RECORD directory finds nothing --
# silently. The mesh-bounds cache was missed exactly this way: the first sweep
# only matched `root / <variable>`, so `export_dir / 'mesh_bounds_cache.json'`
# was invisible to it, and every converted script lost its havok releases.
ASSET_NAMES = {
    'collision_cache.bin', 'mesh_bounds_cache.json',
    'door_centers_cache.json', 'door_panel_axis_cache.json',
    'navmesh_geom_cache', 'meshes', 'textures', 'sound', 'trees',
}

# Variables that hold a plugin's RECORD directory (the .txt dump folder).
RECORD_DIR_NAMES = {'export_dir', 'export_subdir', 'export_root',
                    'extract_dir', 'rec', 'record_root'}

# (file, function) pairs allowed to join an asset name onto a record dir.
ASSET_ALLOWED = {
    # assets_for IS the mapping from a record dir to its asset tree.
    ('output_layout.py', 'assets_for'),
    # These receive an already-resolved asset root, not a record dir.
    ('asset_convert/collision_extract.py', 'scan_mesh_data'),
    ('tes5_import/pgrd_to_navm.py', '_door_axis_map'),
    ('tes5_import/pgrd_to_navm.py', '_bounds_map'),
    # animdata_base is a PER-PLUGIN cache and correctly sits in the record dir.
    ('asset_convert/creature_pipeline.py', 'merge_animdata_singlefiles'),
}


def _asset_violations_in(path):
    rel = os.path.relpath(path, ROOT).replace(os.sep, '/')
    try:
        src = open(path, encoding='utf-8').read()
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return []
    owner = _enclosing_functions(tree)
    lines = src.splitlines()
    out = []

    def check(node, root_node, literal):
        if _root_name(root_node) not in RECORD_DIR_NAMES:
            return
        if literal not in ASSET_NAMES:
            return
        fn = owner.get(node.lineno, '<module>')
        if (rel, fn) in ASSET_ALLOWED:
            return
        if 'noqa: plugin-path' in lines[node.lineno - 1]:
            return
        out.append((rel, node.lineno, fn, lines[node.lineno - 1].strip()))

    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            r = node.right
            if isinstance(r, ast.Constant) and isinstance(r.value, str):
                check(node, node.left, r.value)
        elif (isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)
              and node.func.attr == 'join' and len(node.args) >= 2):
            a1 = node.args[1]
            if isinstance(a1, ast.Constant) and isinstance(a1.value, str):
                check(node, node.args[0], a1.value)
    return out


def test_no_asset_is_looked_up_inside_a_record_directory():
    """`export_dir / 'meshes'` is wrong when export_dir is a RECORD dir.

    An imported mod keeps records one level below the assets, so this join
    lands in a folder that has no meshes, no caches and no sound -- and every
    consumer treats "not found" as "nothing to do" rather than an error.
    Use `output_layout.assets_for(export_dir)` to cross from records to
    assets.
    """
    found = []
    for path in _py_files():
        found.extend(_asset_violations_in(path))
    if found:
        report = "\n".join(
            f"  {rel}:{ln}  in {fn}()\n      {txt}"
            for rel, ln, fn, txt in sorted(found))
        pytest.fail(
            "A shared-asset file is looked up inside a plugin's RECORD "
            "directory.\n"
            "For an imported mod the assets are one level up, so this finds "
            "nothing and the caller silently degrades.\n"
            "Wrap the root in output_layout.assets_for().\n\n" + report)


# ---------------------------------------------------------------------------
#  A plugin's IDENTITY never comes from a folder name
# ---------------------------------------------------------------------------

def test_manifest_source_is_the_plugin_not_the_mod(tmp_path):
    """`<plugin>.manifest.json` must record the PLUGIN, not its mod.

    The writer took `os.path.basename(export_dir)`. For a single-plugin
    imported mod the records live in the MOD's folder, so the manifest claimed
    a plugin named "Black Marsh" -- and the GUI, which trusts that field,
    offered a plugin that does not exist and cannot be re-run.
    """
    import json
    from tes5_import.master_manifest import write_manifest, manifest_path

    out = tmp_path / 'Black Marsh'
    out.mkdir()
    esm = out / 'TWMP_BlackMarsh.esp'
    esm.write_bytes(b'x')

    write_manifest(str(esm), os.path.basename(str(esm)), {})
    got = json.loads(open(manifest_path(str(esm)), encoding='utf-8').read())
    assert got['source'] == 'TWMP_BlackMarsh.esp'


def test_the_two_output_scanners_agree(tmp_path):
    """`converted_plugins` and `gui.scan_converted` must return the same set.

    They read the same folders by different rules -- one looks for the plugin
    file, the other trusts the manifest's `source` -- so a wrong identity in
    either makes the GUI and the LOD load-order disagree about what exists.
    """
    import json
    import gui
    from asset_convert.sibling_lod import converted_plugins

    out = tmp_path / 'output'
    # A group folder holding two plugins, plus a solo conversion.
    grp = out / 'Some Mod'
    grp.mkdir(parents=True)
    for n in ('A.esp', 'B.esp'):
        (grp / n).write_bytes(b'x')
        (grp / f'{n}.manifest.json').write_text(
            json.dumps({'source': n}), encoding='utf-8')
    solo = out / 'Oblivion.esm'
    solo.mkdir()
    (solo / 'Oblivion.esm').write_bytes(b'x')
    (solo / 'Oblivion.esm.manifest.json').write_text(
        json.dumps({'source': 'Oblivion.esm'}), encoding='utf-8')

    assert set(converted_plugins(out)) == set(gui.scan_converted(str(out)))


def test_texture_manifests_live_beside_the_meshes_they_describe(tmp_path):
    """`textures_used.txt` / `overlay_diffuses.txt` are ASSET-scoped.

    They list textures the SHARED meshes reference, and asset_pipeline writes
    them beside those meshes. Treating them as per-plugin records stranded them
    in a folder the prune never reads, which made `build_refs` abort the whole
    BSA pack with "no mesh texture manifest".
    """
    from asset_convert import texture_prune
    from output_layout import assets_for

    # A record dir nested inside a mod folder.
    root = tmp_path / 'export'
    mod = root / 'Some Mod'
    rec = mod / 'A.esp'
    rec.mkdir(parents=True)
    (root / 'sources.json').write_text('{}', encoding='utf-8')

    texture_prune.write_manifest(assets_for(rec), {'tex/a.dds'})
    # Written at the MOD root, not in the record dir.
    assert (mod / texture_prune.MANIFEST_NAME).is_file()
    assert not (rec / texture_prune.MANIFEST_NAME).exists()
    assert texture_prune.read_manifest(assets_for(rec)) == {'tex/a.dds'}


def test_migration_treats_texture_manifests_as_shared():
    """The migration must not file them as per-plugin leftovers."""
    import sys
    sys.path.insert(0, str(Path(ROOT)))
    import tools.esm.migrate_group_layout as mig

    assert 'textures_used.txt' in mig.SHARED_CACHES
    assert 'overlay_diffuses.txt' in mig.SHARED_CACHES


def test_one_mod_one_zip_named_for_the_mod(tmp_path):
    """A mod imported as ONE archive ships as ONE archive.

    Every plugin of an imported mod converts into that mod's folder, so the
    zip is named for the MOD. Naming it after whichever plugin was the -f
    argument produced N identical archives under N different names.
    """
    import inspect
    import convert

    src = inspect.getsource(convert.phase_pack_zip)
    # The zip name comes from the resolved folder, not the -f argument.
    assert 'src_root.name' in src, 'zip is not named for the mod folder'
    assert 'f"{file_name}.zip"' not in src, 'zip still named per plugin'


def test_every_plugin_of_a_mod_lands_in_one_folder(tmp_path):
    """All of a mod's plugins convert into the same output folder."""
    from output_layout import plugin_out_root
    import json

    exp = tmp_path / 'export'
    exp.mkdir()
    reg = {'version': 1, 'sources': {
        n: {'kind': 'archive', 'plugin': n, 'group_id': 'g1',
            'group_label': 'My Pack', 'group_plugins': ['A.esm', 'B.esp']}
        for n in ('A.esm', 'B.esp')}}
    (exp / 'sources.json').write_text(json.dumps(reg), encoding='utf-8')

    out = tmp_path / 'output'
    folders = {plugin_out_root(out, n, exp) for n in ('A.esm', 'B.esp')}
    assert len(folders) == 1
    assert folders.pop().name == 'My Pack'
