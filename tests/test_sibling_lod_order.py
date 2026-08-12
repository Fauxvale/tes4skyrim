"""Sibling-LOD overlay ordering: who wins a contested tile.

The LAST overlay applied wins every FormID it shares with an earlier one, so
this order IS the conflict resolution. The invariant: a plugin the user never
positioned must never outrank one they did.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from asset_convert import sibling_lod
from asset_convert.sibling_lod import _load_order


@pytest.fixture
def no_plugins_txt(monkeypatch):
    monkeypatch.setattr(sibling_lod, 'plugins_txt_order', lambda: [])


class TestExplicitOrder:
    """The GUI arrangement is absolute for the plugins it names."""

    def test_user_choice_wins_over_unseen_plugin(self, no_plugins_txt):
        names = ['Tamriel.esp', 'ElsweyrAnequina.esp', 'AAANewMod.esp']
        order = _load_order(names, Path('.'),
                            explicit=['Tamriel.esp', 'ElsweyrAnequina.esp'])
        assert order[-1] == 'ElsweyrAnequina.esp', (
            'the last plugin the user arranged must win contested tiles')
        assert order.index('AAANewMod.esp') == 0, (
            'a plugin the user never saw must not outrank their arrangement')

    def test_explicit_relative_order_preserved(self, no_plugins_txt):
        names = ['Tamriel.esp', 'ElsweyrAnequina.esp']
        order = _load_order(names, Path('.'),
                            explicit=['Tamriel.esp', 'ElsweyrAnequina.esp'])
        assert order == ['Tamriel.esp', 'ElsweyrAnequina.esp']


class TestPluginsTxtOrder:
    """plugins.txt is what the game itself obeys."""

    def test_unlisted_plugin_never_outranks_listed(self, monkeypatch):
        # DLCBattlehornCastle.esp is absent from plugins.txt; appending it
        # LAST gave a 14-cell DLC the final word over Elsweyr's 1,855 cells.
        monkeypatch.setattr(sibling_lod, 'plugins_txt_order',
                            lambda: ['Oblivion.esm', 'Tamriel.esp',
                                     'ElsweyrAnequina.esp'])
        names = ['Tamriel.esp', 'ElsweyrAnequina.esp',
                 'DLCBattlehornCastle.esp']
        order = _load_order(names, Path('.'))
        assert order[-1] == 'ElsweyrAnequina.esp', (
            'plugins.txt puts ElsweyrAnequina.esp last, so it wins tiles')
        assert order.index('DLCBattlehornCastle.esp') == 0, (
            'an unlisted plugin must sort BEFORE every ranked one')

    def test_listed_order_follows_plugins_txt(self, monkeypatch):
        monkeypatch.setattr(sibling_lod, 'plugins_txt_order',
                            lambda: ['ElsweyrAnequina.esp', 'Tamriel.esp'])
        order = _load_order(['Tamriel.esp', 'ElsweyrAnequina.esp'], Path('.'))
        assert order == ['ElsweyrAnequina.esp', 'Tamriel.esp'], (
            'the ranked order must mirror plugins.txt, not the alphabet')

    def test_all_unlisted_falls_through_to_structural(self, monkeypatch):
        """plugins.txt listing NONE of them must not silently return them."""
        monkeypatch.setattr(sibling_lod, 'plugins_txt_order',
                            lambda: ['Skyrim.esm', 'Unrelated.esp'])
        names = ['B.esp', 'A.esp']
        order = _load_order(names, Path('.'))
        assert sorted(order) == sorted(names)


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])
