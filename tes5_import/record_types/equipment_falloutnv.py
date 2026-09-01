"""FO3/FNV weapon animation types, reduced to the ones Skyrim animates.

Skyrim's only ranged animations are Bow and Crossbow, so every firearm becomes
a Crossbow: it aims flat and fires a projectile, where a bow is drawn and arced.
ETYP, BIDS, BAMT, INAM, NAM9 and NAM8 all key off the anim type, so that one
substitution carries the whole record.

See: docs/commentary/tes4_export_falloutnv.md#weapons-guns-become-crossbows
"""

from ..skyrim_overrides import WEAPON_ANIM_CROSSBOW
from .common import get_int

#: FO3/FNV firearm anim types: pistols 3-4, rifles 5-7, launcher 9, thrown 10-13.
_GUN_TYPES = frozenset({3, 4, 5, 6, 7, 9, 10, 11, 12, 13})


def refine_anim_type(rec: dict, anim_type: int) -> int:
    """Crossbow for a FO3/FNV firearm, else the given type unchanged."""
    if get_int(rec, 'DNAM.FalloutAnimType', -1) in _GUN_TYPES:
        return WEAPON_ANIM_CROSSBOW
    return anim_type
