"""Create LOD plugin ordering: plugins.txt first, the rest appended safely.

The order is conflict resolution, not presentation — LOD tiles are files on a
fixed grid, so the LAST plugin applied wins every tile two of them both change.

Two invariants:
  1. Everything plugins.txt lists comes FIRST, in plugins.txt's own order.
  2. Everything else is appended at the bottom, alphabetically, EXCEPT that a
     plugin never sorts before one of its own masters — a master applied last
     would overwrite the very dependent that was built on top of it.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asset_convert import sibling_lod
from asset_convert.sibling_lod import create_lod_order


@pytest.fixture
def no_plugins_txt(monkeypatch):
    monkeypatch.setattr(sibling_lod, 'plugins_txt_order', lambda: [])


@pytest.fixture
def masters(monkeypatch):
    """Stub the master lists, keyed by export dir name."""
    table: dict[str, list[str]] = {}

    def _fake(export_dir):
        return table.get(Path(export_dir).name, [])

    monkeypatch.setattr(sibling_lod, '_master_names', _fake)
    return table


class TestPluginsTxtFirst:
    def test_listed_plugins_lead_in_plugins_txt_order(self, monkeypatch,
                                                      masters):
        monkeypatch.setattr(sibling_lod, 'plugins_txt_order',
                            lambda: ['Oblivion.esm', 'Tamriel.esp'])
        order = create_lod_order(['Tamriel.esp', 'AAAExtra.esp',
                                  'Oblivion.esm'], Path('.'))
        assert order[:2] == ['Oblivion.esm', 'Tamriel.esp'], (
            'plugins.txt order must lead, verbatim')
        assert order[2] == 'AAAExtra.esp', (
            'an unlisted plugin is APPENDED, never interleaved')

    def test_unlisted_sorts_alphabetically_at_the_bottom(self, monkeypatch,
                                                         masters):
        monkeypatch.setattr(sibling_lod, 'plugins_txt_order',
                            lambda: ['Tamriel.esp'])
        order = create_lod_order(['Zeta.esp', 'Alpha.esp', 'Tamriel.esp'],
                                 Path('.'))
        assert order == ['Tamriel.esp', 'Alpha.esp', 'Zeta.esp']


class TestMasterConstraint:
    def test_dependent_never_precedes_its_master(self, no_plugins_txt,
                                                 masters):
        # Alphabetically AAAPatch.esp sorts first, but it is BUILT ON
        # Tamriel.esp: applying Tamriel last would overwrite the patch's tiles
        # with the unpatched original.
        masters['AAAPatch.esp'] = ['Tamriel.esp']
        order = create_lod_order(['AAAPatch.esp', 'Tamriel.esp'], Path('.'))
        assert order.index('Tamriel.esp') < order.index('AAAPatch.esp'), (
            'a master must apply BEFORE the plugin that depends on it')

    def test_transitive_masters_still_rank(self, no_plugins_txt, masters):
        masters['B.esp'] = ['Oblivion.esm']
        masters['A.esp'] = ['B.esp']
        order = create_lod_order(['A.esp', 'B.esp', 'Oblivion.esm'], Path('.'))
        assert order == ['Oblivion.esm', 'B.esp', 'A.esp']

    def test_esm_before_esp_at_equal_depth(self, no_plugins_txt, masters):
        order = create_lod_order(['Zeta.esp', 'Morrowind_ob.esm'], Path('.'))
        assert order == ['Morrowind_ob.esm', 'Zeta.esp'], (
            '.esm before .esp at equal depth, matching the engine split')

    def test_a_master_cycle_does_not_hang(self, no_plugins_txt, masters):
        """A malformed header must degrade, not recurse forever."""
        masters['A.esp'] = ['B.esp']
        masters['B.esp'] = ['A.esp']
        assert sorted(create_lod_order(['A.esp', 'B.esp'], Path('.'))) == [
            'A.esp', 'B.esp']

    def test_masters_outside_the_selection_are_ignored(self, no_plugins_txt,
                                                       masters):
        """Depth counts only masters that are actually being generated.

        A plugin whose master was deselected must not be pushed to the bottom
        for depending on something this run will never apply.
        """
        masters['Solo.esp'] = ['NotConverted.esm']
        order = create_lod_order(['Solo.esp', 'Alpha.esp'], Path('.'))
        assert order == ['Alpha.esp', 'Solo.esp']


def test_every_plugin_appears_exactly_once(monkeypatch, masters):
    monkeypatch.setattr(sibling_lod, 'plugins_txt_order',
                        lambda: ['Tamriel.esp', 'Missing.esp'])
    masters['Patch.esp'] = ['Tamriel.esp']
    names = ['Patch.esp', 'Tamriel.esp', 'Alpha.esp', 'Oblivion.esm']
    order = create_lod_order(names, Path('.'))
    assert sorted(order) == sorted(names)
    assert len(order) == len(set(order))


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
