"""Exterior block/sub-block GRUPs are ordered by unsigned (X, Y), X major.

The engine builds a worldspace's cell grid by walking its type-4 block list
while PARSING the plugin, before any cell loads. The list must be monotonic in
vanilla's key; a run where X descends and re-ascends never terminates, so the
game hangs on the main menu with no crash and no log.

The GRUP label packs the coordinates as `struct.pack('<hh', Y, X)` -- Y in the
LOW word. Sorting on the label's own word order therefore yields (Y, X), the
TRANSPOSE of what the engine wants, and that is what shipped.

Measured on TWMP_ValenwoodImproved.esp: Tamriel's blocks came out as
(-1,0), (-2,-3), (-2,-2), (-1,-2), (-2,-1), (-1,-1) -- three separate
ascending runs under the correct key. Deleting exterior blocks in xEdit made
the game load again, because each deletion shortens the list until what
remains happens to be monotonic; resaving in xEdit did NOT help, because xEdit
re-sorts on its own key and writes the same order back.

Authority is the real Skyrim.esm: all 168 blocks of worldspace 0000003C are
sorted by unsigned (X, Y) and by no other candidate key, as are the sub-blocks
inside every one of them and all 37 worldspaces in the file. Never census our
own converted output for this -- it carried the same bug, which is exactly how
the transposed key was mistaken for vanilla's.
"""

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tes5_import.import_main import _grid_sort_key
from tes5_import.overrides import _group_sort_key


def _label(x, y):
    """An exterior block/sub-block GRUP label: Y in the low word, then X."""
    return struct.pack('<hh', y, x)


# The order Skyrim.esm uses: X ascends as an unsigned 16-bit value (so every
# non-negative X precedes every negative one), and Y does the same within it.
VANILLA_ORDER = [(0, 0), (0, 3), (0, -5), (0, -1),
                 (3, 0), (3, -1),
                 (-4, 0), (-4, -1),
                 (-1, 0), (-1, 17), (-1, -1)]


def test_grid_sort_key_reproduces_vanilla_order():
    shuffled = [VANILLA_ORDER[i] for i in
                (7, 2, 10, 0, 5, 9, 3, 6, 1, 8, 4)]
    got = sorted((_label(x, y) for x, y in shuffled), key=_grid_sort_key)
    assert [struct.unpack('<hh', b)[::-1] for b in got] == VANILLA_ORDER


def test_group_sort_key_agrees_with_grid_sort_key():
    """The override path and the own-hierarchy builder must not disagree.

    Both write type-4 groups into the SAME world-children group, so two
    different orderings concatenate into two ascending runs -- which is a
    descent at the seam, exactly the shape that hangs the parse.
    """
    labels = [_label(x, y) for x, y in VANILLA_ORDER]
    shuffled = list(reversed(labels))

    via_grid = sorted(shuffled, key=_grid_sort_key)
    via_group = sorted(((4, b) for b in shuffled), key=_group_sort_key)

    assert [b for _t, b in via_group] == via_grid


def test_negative_x_sorts_after_every_positive_x():
    """Unsigned comparison, not signed: -1 is 65535 and comes LAST.

    Sorting these signed would put the whole negative half first and split
    vanilla's runs down the middle.
    """
    got = sorted((_label(x, 0) for x in (-1, -2, 0, 1, 2)), key=_grid_sort_key)
    assert [struct.unpack('<hh', b)[1] for b in got] == [0, 1, 2, -2, -1]


def test_y_is_the_minor_key_not_the_major_one():
    """The regression itself: transposing the key reorders the list.

    (0,-1) and (-1,0) are the discriminating pair -- X major puts (0,-1)
    first, Y major puts (-1,0) first.
    """
    got = sorted((_label(*t) for t in ((-1, 0), (0, -1))), key=_grid_sort_key)
    assert [struct.unpack('<hh', b)[::-1] for b in got] == [(0, -1), (-1, 0)]


def test_owned_groups_still_lead_the_world_children():
    """The persistent cell's type-6 group precedes the exterior blocks.

    Vanilla opens a worldspace's type-1 group with the persistent CELL and its
    children, then the type-4 blocks; sorting by group type alone put it last
    because 6 > 4.
    """
    steps = [(4, _label(0, 0)), (6, struct.pack('<I', 0x01023777)),
             (4, _label(-1, -1))]
    got = sorted(steps, key=_group_sort_key)
    assert got[0][0] == 6, "the persistent cell group must come first"
    assert [struct.unpack('<hh', b)[::-1] for t, b in got[1:]] == [(0, 0),
                                                                  (-1, -1)]
