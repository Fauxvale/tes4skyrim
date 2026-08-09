"""The prune's keep-set must speak the same paths the importer writes.

A texture the plugin references but the keep-set spells differently is deleted
as unused and never ships. That failure is invisible offline — the record is
correct, the file was on disk when it was checked — and only shows in game as
untextured terrain.
"""

from asset_convert import texture_prune as tp


def _write(tmp_path, name, body):
    (tmp_path / name).write_text(body, encoding='utf-8')


class TestSharedMapSiblings:
    """A variant diffuse borrows its base name's maps, and nothing records it.

    `brumawoodpost_grey.dds` ships without its own normal map; the engine loads
    `brumawoodpost_n.dds` from the same folder. `_companions` derives only from
    the full name, so it invents `brumawoodpost_grey_n.dds` (which does not
    exist) while the map actually in use gets pruned. Measured on Nehrim: 27,
    including `armor/nehrimsoldier/cuirass_n.dds`, used by `cuirass_b.dds`.
    """

    @staticmethod
    def _tree(tmp_path, names):
        tex = tmp_path / 'textures' / 'armor' / 'nehrimsoldier'
        tex.mkdir(parents=True)
        for n in names:
            (tex / n).write_bytes(b'DDS ')
        return tmp_path

    def test_variant_diffuse_keeps_the_base_normal_map(self, tmp_path):
        self._tree(tmp_path, ['cuirass_b.dds', 'cuirass_n.dds'])
        # only the variant is referenced; the base diffuse is not even shipped
        refs = {'armor/nehrimsoldier/cuirass_b.dds'}

        rescued = tp._shared_maps_on_disk(tmp_path, refs)
        assert 'armor/nehrimsoldier/cuirass_n.dds' in rescued

    def test_an_unrelated_map_is_not_rescued(self, tmp_path):
        """The rescue must not become 'keep every map in a used folder'."""
        self._tree(tmp_path, ['cuirass_b.dds', 'cuirass_n.dds', 'helmet_n.dds'])
        refs = {'armor/nehrimsoldier/cuirass_b.dds'}

        rescued = tp._shared_maps_on_disk(tmp_path, refs)
        assert 'armor/nehrimsoldier/helmet_n.dds' not in rescued

    def test_rescue_only_covers_files_that_exist(self, tmp_path):
        """It is disk-bounded: it can never invent a path."""
        self._tree(tmp_path, ['cuirass_b.dds'])
        refs = {'armor/nehrimsoldier/cuirass_b.dds'}

        assert tp._shared_maps_on_disk(tmp_path, refs) == set()


class TestRecordTexturePrefixes:
    def test_ltex_icon_is_relative_to_the_landscape_folder(self, tmp_path):
        """LTEX ICON omits `landscape\\`; the importer prepends it, so must we.

        Nehrim shipped 252 of 484 referenced landscape texture slots outside
        every BSA because of exactly this: the keep-set held
        `tes4/oblivion/terrainhd...dds` while the plugin asks for
        `tes4/landscape/oblivion/terrainhd...dds`. Nothing matched, the prune
        deleted them, and most of the terrain rendered untextured.
        Mirrors tes5_import/record_types/world.py:111.
        """
        _write(tmp_path, 'LTEX.txt',
               '---RECORD_BEGIN---\n'
               'Signature=LTEX\n'
               'EditorID=TerrainHDOblivionGrass\n'
               'ICON=Oblivion\\\\TerrainHDOblivionLavaRock02.dds\n'
               '---RECORD_END---\n')
        refs = tp.refs_from_records(tmp_path)

        # what the plugin actually asks for
        assert 'tes4/landscape/oblivion/terrainhdoblivionlavarock02.dds' in refs
        # the un-prefixed spellings stay too — harmless, and a plugin that
        # already spells the folder out must keep matching
        assert 'tes4/oblivion/terrainhdoblivionlavarock02.dds' in refs

    def test_a_plain_icon_record_gets_no_folder_prefix(self, tmp_path):
        """Only LTEX is folder-relative; nothing else may gain a prefix."""
        _write(tmp_path, 'BOOK.txt',
               '---RECORD_BEGIN---\n'
               'Signature=BOOK\n'
               'ICON=Clutter\\\\Books\\\\Book01.dds\n'
               '---RECORD_END---\n')
        refs = tp.refs_from_records(tmp_path)

        assert 'tes4/clutter/books/book01.dds' in refs
        assert not any(r.startswith('tes4/landscape/') for r in refs)

    def test_referenced_landscape_texture_survives_the_keep_set(self, tmp_path):
        """End to end: the LTEX texture must be in build_refs, not pruned."""
        export = tmp_path / 'export'
        export.mkdir()
        _write(export, 'LTEX.txt',
               '---RECORD_BEGIN---\n'
               'Signature=LTEX\n'
               'ICON=TerrainMud02.dds\n'
               '---RECORD_END---\n')
        plugin_dir = tmp_path / 'out'
        plugin_dir.mkdir()
        # A non-empty manifest is required: build_refs refuses to prune without
        # one, so that a missing mesh pass cannot wipe textures that are in use.
        tp.write_manifest(plugin_dir, {'tes4/clutter/unrelated.dds'})

        refs = tp.build_refs(plugin_dir, export)
        assert 'tes4/landscape/terrainmud02.dds' in refs
        # the normal map rides along via _companions
        assert 'tes4/landscape/terrainmud02_n.dds' in refs
