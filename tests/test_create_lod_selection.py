"""Create LOD selection contracts: worldspace ownership and master dependency.

The Create LOD dialog and the run both hang off two questions:

  * Which plugin's records is a worldspace baked FROM? (`worldspace_owner`)
  * Which plugins rest on a given plugin? (`dependents_of`)

The second one decides two visible behaviours: unticking a plugin greys out
everything mastered on it, and a plugin that does not depend on a worldspace's
owner is never overlaid onto it — the gate that keeps standalone Nehrim.esm out
of Oblivion's TES4Tamriel.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asset_convert import sibling_lod
from asset_convert.sibling_lod import (dependents_of, worldspace_owner,
                                       merge_worldspaces, _master_chain)


@pytest.fixture
def masters(monkeypatch):
    """Stub the master lists, keyed by export dir name."""
    table: dict[str, list[str]] = {}

    def _fake(export_dir):
        return table.get(Path(export_dir).name, [])

    monkeypatch.setattr(sibling_lod, '_master_names', _fake)
    return table


@pytest.fixture
def shipped(monkeypatch):
    """Stub which worldspaces each plugin shipped LOD for."""
    table: dict[str, list[str]] = {}

    def _fake(export_dir):
        return [(e, 0) for e in table.get(Path(export_dir).name, [])]

    monkeypatch.setattr(sibling_lod, 'shipped_lod_worldspaces', _fake)
    return table


class TestWorldspaceOwner:
    def test_first_in_load_order_owns_it(self, shipped):
        shipped['Oblivion.esm'] = ['TES4Tamriel']
        shipped['Tamriel.esp'] = ['TES4Tamriel']
        order = ['Oblivion.esm', 'Tamriel.esp']
        assert worldspace_owner('TES4Tamriel', order, Path('.')) == \
            'Oblivion.esm'

    def test_owner_follows_the_given_order(self, shipped):
        """Ownership is positional, so reordering changes the record source."""
        shipped['Oblivion.esm'] = ['TES4Tamriel']
        shipped['Tamriel.esp'] = ['TES4Tamriel']
        order = ['Tamriel.esp', 'Oblivion.esm']
        assert worldspace_owner('TES4Tamriel', order, Path('.')) == \
            'Tamriel.esp'

    def test_unowned_worldspace_is_none(self, shipped):
        shipped['Oblivion.esm'] = ['TES4Tamriel']
        assert worldspace_owner('NoSuchWorld', ['Oblivion.esm'],
                                Path('.')) is None

    def test_a_plugin_that_only_overrides_does_not_own(self, shipped):
        """Shipping no LOD assets means not owning, however many cells it edits.

        Morrowind_ob.esm overrides thousands of TES4Tamriel cells without
        shipping a single TES4Tamriel LOD asset; sourcing records from it would
        drop everything Oblivion.esm holds.
        """
        shipped['Oblivion.esm'] = ['TES4Tamriel']
        shipped['Morrowind_ob.esm'] = ['WrldMorrowind']
        order = ['Morrowind_ob.esm', 'Oblivion.esm']
        assert worldspace_owner('TES4Tamriel', order, Path('.')) == \
            'Oblivion.esm'


class TestDependents:
    def test_direct_dependent(self, masters):
        masters['Tamriel.esp'] = ['Oblivion.esm']
        deps = dependents_of(['Oblivion.esm', 'Tamriel.esp'], Path('.'))
        assert deps['Oblivion.esm'] == {'Tamriel.esp'}
        assert deps['Tamriel.esp'] == set()

    def test_transitive_dependent(self, masters):
        """Dropping Nehrim must grey Translation, which never names Oblivion."""
        masters['Nehrim.esm'] = []
        masters['Translation.esp'] = ['Nehrim.esm']
        masters['Patch.esp'] = ['Translation.esp']
        deps = dependents_of(['Nehrim.esm', 'Translation.esp', 'Patch.esp'],
                             Path('.'))
        assert deps['Nehrim.esm'] == {'Translation.esp', 'Patch.esp'}
        assert deps['Translation.esp'] == {'Patch.esp'}

    def test_unrelated_plugins_do_not_depend(self, masters):
        masters['Nehrim.esm'] = []
        masters['Oblivion.esm'] = []
        deps = dependents_of(['Nehrim.esm', 'Oblivion.esm'], Path('.'))
        assert deps['Nehrim.esm'] == set()
        assert deps['Oblivion.esm'] == set()

    def test_a_master_cycle_terminates(self, masters):
        masters['A.esp'] = ['B.esp']
        masters['B.esp'] = ['A.esp']
        deps = dependents_of(['A.esp', 'B.esp'], Path('.'))
        assert deps['A.esp'] == {'B.esp'}
        assert deps['B.esp'] == {'A.esp'}

    def test_masters_outside_the_selection_are_ignored(self, masters):
        masters['Solo.esp'] = ['NotSelected.esm']
        deps = dependents_of(['Solo.esp'], Path('.'))
        assert deps['Solo.esp'] == set()


class TestOverlayGate:
    """A plugin may only overlay a worldspace whose owner it depends on."""

    def test_standalone_plugin_is_not_an_overlay(self, masters):
        # The real defect this prevents: Nehrim.esm is a different game with
        # its own worldspace, and overlaying it onto Oblivion's TES4Tamriel
        # would merge two unrelated mods' references into one set of tiles.
        masters['Tamriel.esp'] = ['Oblivion.esm']
        masters['Nehrim.esm'] = []
        names = ['Oblivion.esm', 'Tamriel.esp', 'Nehrim.esm']
        assert 'Oblivion.esm' in _master_chain('Tamriel.esp', Path('.'), names)
        assert 'Oblivion.esm' not in _master_chain('Nehrim.esm', Path('.'),
                                                   names)

    def test_transitive_dependent_is_an_overlay(self, masters):
        masters['Tamriel.esp'] = ['Oblivion.esm']
        masters['Patch.esp'] = ['Tamriel.esp']
        names = ['Oblivion.esm', 'Tamriel.esp', 'Patch.esp']
        assert 'Oblivion.esm' in _master_chain('Patch.esp', Path('.'), names)


class TestMergeWorldspaces:
    """The dialog re-flattens this on every plugin toggle, so it must be pure."""

    def test_union_in_first_appearance_order(self):
        by = {'A.esp': ['W1', 'W2'], 'B.esp': ['W2', 'W3']}
        assert merge_worldspaces(['A.esp', 'B.esp'], by) == ['W1', 'W2', 'W3']

    def test_dropping_a_plugin_drops_its_exclusive_worldspace(self):
        by = {'Oblivion.esm': ['TES4Tamriel'], 'Nehrim.esm': ['NehrimWorld']}
        assert merge_worldspaces(['Oblivion.esm'], by) == ['TES4Tamriel']

    def test_a_shared_worldspace_survives_dropping_one_owner(self):
        by = {'A.esp': ['W1'], 'B.esp': ['W1']}
        assert merge_worldspaces(['B.esp'], by) == ['W1']

    def test_unknown_plugin_contributes_nothing(self):
        assert merge_worldspaces(['Ghost.esp'], {'A.esp': ['W1']}) == []


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
