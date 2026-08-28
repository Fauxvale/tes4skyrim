"""An imported mod resolves textures it does not ship through its base.

Master-export blindness, the asset half of it. A mod ships only the files it
changes; everything else lives in the base game's export tree. Deriving the
texture root from the mesh path -- swapping `\\meshes\\` for `\\textures\\` --
can never reach that tree.

Measured on the author's parallax mod: of the 3357 distinct texture paths its
8665 meshes reference, 1464 were in the mod and **1602 only in Nehrim.esm**.
Unreachable means the shape gets no height map and no specular verdict.

A mod with a plugin names its masters in `_HEADER.txt`; an asset-only mod has
no plugin and no header, so the base is recorded at import time.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asset_convert import nif_converter as nc                    # noqa: E402


def _tree(root, name, textures=(), header_masters=None, base=None):
    """A minimal export/<name>/ with meshes/, textures/ and optional base."""
    d = root / name
    (d / 'meshes').mkdir(parents=True, exist_ok=True)
    for rel in textures:
        p = d / 'textures' / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'DDS ')
    if header_masters is not None:
        (d / '_HEADER.txt').write_text(
            ''.join(f'Master[{i}]={m}\n' for i, m in enumerate(header_masters)),
            encoding='utf-8')
    if base is not None:
        s = d / '_source'
        s.mkdir(parents=True, exist_ok=True)
        (s / nc.BASE_PLUGINS_FILE).write_text('\n'.join(base) + '\n',
                                              encoding='utf-8')
    return d


class TestMasterTextureRoots:

    def test_a_plugin_mod_finds_its_masters_from_the_header(self, tmp_path):
        _tree(tmp_path, 'Base.esm', textures=['rock/stone.dds'])
        mod = _tree(tmp_path, 'Mod.esp', header_masters=['Base.esm'])
        roots = nc.master_texture_roots(mod / 'meshes')
        assert len(roots) == 1
        assert roots[0].endswith(str(Path('Base.esm') / 'textures'))

    def test_an_asset_only_mod_uses_the_recorded_base(self, tmp_path):
        # no plugin means no _HEADER.txt, so --base is the only carrier
        _tree(tmp_path, 'Base.esm', textures=['rock/stone.dds'])
        mod = _tree(tmp_path, 'TexturePack', base=['Base.esm'])
        roots = nc.master_texture_roots(mod / 'meshes')
        assert len(roots) == 1

    def test_no_base_means_no_fallback(self, tmp_path):
        mod = _tree(tmp_path, 'Lonely')
        assert nc.master_texture_roots(mod / 'meshes') == ()

    def test_a_base_without_an_export_is_skipped(self, tmp_path):
        mod = _tree(tmp_path, 'Mod.esp', header_masters=['Absent.esm'])
        assert nc.master_texture_roots(mod / 'meshes') == ()


class TestResolutionThroughTheFallback:

    def test_the_mods_own_copy_still_wins(self, tmp_path):
        base = _tree(tmp_path, 'Base.esm', textures=['rock/stone.dds'])
        mod = _tree(tmp_path, 'Mod.esp', textures=['rock/stone.dds'],
                    header_masters=['Base.esm'])
        got = nc._resolve_source_texture(
            'textures\\tes4\\rock\\stone.dds', str(mod / 'meshes' / 'a.nif'),
            nc.master_texture_roots(mod / 'meshes'))
        assert got is not None
        assert str(mod) in got and str(base / 'textures') not in got

    def test_a_texture_only_the_base_has_is_found(self, tmp_path):
        # the 1602-path case: without the fallback this returns None
        base = _tree(tmp_path, 'Base.esm', textures=['rock/stone.dds'])
        mod = _tree(tmp_path, 'Mod.esp', header_masters=['Base.esm'])
        args = ('textures\\tes4\\rock\\stone.dds',
                str(mod / 'meshes' / 'a.nif'))
        assert nc._resolve_source_texture(*args) is None, \
            'no fallback should still miss it'
        got = nc._resolve_source_texture(
            *args, nc.master_texture_roots(mod / 'meshes'))
        assert got is not None and str(base) in got

    def test_a_texture_nobody_has_is_still_unresolved(self, tmp_path):
        _tree(tmp_path, 'Base.esm')
        mod = _tree(tmp_path, 'Mod.esp', header_masters=['Base.esm'])
        assert nc._resolve_source_texture(
            'textures\\tes4\\rock\\absent.dds',
            str(mod / 'meshes' / 'a.nif'),
            nc.master_texture_roots(mod / 'meshes')) is None


class TestWearablePlanThroughTheBase:
    """An asset-only tree inherits its base's ARMO/CLOT records.

    Without this no mesh in a merge is ever WORN, so no `_0`/`_1` pair is
    written and the armour silently falls back to the base conversion's
    meshes -- decided without any of the mod's textures. Worn gear is exactly
    what benefits most from a specular map.
    """

    def _armo(self, d, model):
        """One ARMO record in the export text format."""
        lines = ['---RECORD_BEGIN---', 'Signature=ARMO',
                 'FormID=0004938C', 'BMDT.BipedFlags=32',
                 f'Male.BipedModel.MODL={model}', '---RECORD_END---']
        (d / 'ARMO.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')

    def test_a_merge_inherits_the_bases_plan(self, tmp_path):
        from asset_convert import wearable_plan as wp
        base = _tree(tmp_path, 'Base.esm')
        self._armo(base, 'armor\iron\cuirass.nif')
        assert wp.build_plan(base), 'base plan should not be empty'

        mod = _tree(tmp_path, 'Merge', base=['Base.esm'])
        assert wp.build_plan(mod) == wp.build_plan(base)

    def test_without_a_base_it_stays_empty(self, tmp_path):
        from asset_convert import wearable_plan as wp
        base = _tree(tmp_path, 'Base.esm')
        self._armo(base, 'armor\iron\cuirass.nif')
        mod = _tree(tmp_path, 'Lonely')
        assert not any(v for v in wp.build_plan(mod).values())

    def test_the_trees_own_records_win(self, tmp_path):
        from asset_convert import wearable_plan as wp
        base = _tree(tmp_path, 'Base.esm')
        self._armo(base, 'armor\iron\cuirass.nif')
        mod = _tree(tmp_path, 'Merge', base=['Base.esm'])
        self._armo(mod, 'armor\steel\cuirass.nif')
        plan = wp.build_plan(mod)
        # both present: the base's entry survives, the mod's is added
        assert len([k for k in plan if not k.startswith('*')]) == 2
