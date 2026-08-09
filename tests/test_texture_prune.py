"""The prune's keep-set must speak the same paths the importer writes.

A texture the plugin references but the keep-set spells differently is left out
of the BSA and never ships. That failure is invisible offline — the record is
correct, the file is still on disk because the mesh phase re-copies the whole
texture tree every run — and only shows in game as untextured terrain.
"""

from asset_convert import texture_prune as tp


def _write(tmp_path, name, body):
    (tmp_path / name).write_text(body, encoding='utf-8')


class TestSharedMapSiblings:
    """A variant diffuse borrows its base name's maps, and nothing records it.

    `brumawoodpost_grey.dds` ships without its own normal map; the engine loads
    `brumawoodpost_n.dds` from the same folder. `_companions` derives only from
    the full name, so it invents `brumawoodpost_grey_n.dds` (which does not
    exist) while the map actually in use is left out of the archive. Seen on
    Nehrim as `armor/nehrimsoldier/cuirass_n.dds`, used by `cuirass_b.dds`.
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
        `tes4/landscape/oblivion/terrainhd...dds`. Nothing matched, so they
        were left out of the BSA and the terrain rendered untextured.
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
        """End to end: the LTEX texture must be in build_refs, so it ships."""
        export = tmp_path / 'export'
        export.mkdir()
        _write(export, 'LTEX.txt',
               '---RECORD_BEGIN---\n'
               'Signature=LTEX\n'
               'ICON=TerrainMud02.dds\n'
               '---RECORD_END---\n')
        plugin_dir = tmp_path / 'out'
        plugin_dir.mkdir()
        # A non-empty manifest is required: build_refs refuses to build a
        # keep-set without one, so a missing mesh pass cannot strip the
        # textures that are in use out of the archive.
        tp.write_manifest(plugin_dir, {'tes4/clutter/unrelated.dds'})

        refs = tp.build_refs(plugin_dir, export)
        assert 'tes4/landscape/terrainmud02.dds' in refs
        # the normal map rides along via _companions
        assert 'tes4/landscape/terrainmud02_n.dds' in refs


class TestPackTimeFilter:
    """The keep-set is applied when STAGING the BSA, never by deleting.

    An earlier design pruned `output/` in its own phase. That was wrong twice
    over: the mesh phase re-copies the whole texture tree every run, so the
    deletions were silently undone (which is why a broken keep-set went
    unnoticed for so long), and the user tests with loose files, so deleting
    from `output/` removed the assets under test.
    """

    @staticmethod
    def _tree(tmp_path):
        tex = tmp_path / 'textures' / 'tes4' / 'landscape'
        tex.mkdir(parents=True)
        for n in ('kept.dds', 'unreferenced.dds'):
            (tex / n).write_bytes(b'DDS ')
        (tmp_path / 'meshes').mkdir()
        (tmp_path / 'meshes' / 'a.nif').write_bytes(b'NIF')
        return tmp_path

    def test_unreferenced_texture_is_left_out_of_the_archive(self, tmp_path):
        from asset_convert import bsa_pack
        plugin = self._tree(tmp_path)
        keep = {'tes4/landscape/kept.dds'}

        staged = bsa_pack._collect_files(plugin, ['textures'], keep)
        names = {p.as_posix() for _src, p, _sz in staged}
        assert 'textures/tes4/landscape/kept.dds' in names
        assert 'textures/tes4/landscape/unreferenced.dds' not in names

    def test_the_filter_never_deletes_from_output(self, tmp_path):
        """Loose-file testing must keep the full tree."""
        from asset_convert import bsa_pack
        plugin = self._tree(tmp_path)
        bsa_pack._collect_files(plugin, ['textures'],
                                {'tes4/landscape/kept.dds'})

        tex = plugin / 'textures' / 'tes4' / 'landscape'
        assert (tex / 'kept.dds').is_file()
        assert (tex / 'unreferenced.dds').is_file(), \
            'staging must not remove anything from output/'

    def test_no_keep_set_packs_everything(self, tmp_path):
        """Without an export dir the filter is off — never guess."""
        from asset_convert import bsa_pack
        plugin = self._tree(tmp_path)

        staged = bsa_pack._collect_files(plugin, ['textures'], None)
        assert len(staged) == 2

    def test_non_texture_dirs_are_never_filtered(self, tmp_path):
        """The keep-set is about textures/ only; meshes/ passes through."""
        from asset_convert import bsa_pack
        plugin = self._tree(tmp_path)

        staged = bsa_pack._collect_files(plugin, ['meshes'], set())
        names = {p.as_posix() for _src, p, _sz in staged}
        assert names == {'meshes/a.nif'}
