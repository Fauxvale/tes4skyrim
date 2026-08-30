# CK warnings: the 2026-07 fix sweep

What the earlier sweep changed.

## Fixed in the 2026-07 sweep

Recorded here because the detail previously lived only in machine-local memory.
Do not re-diagnose these.

- **Persistence-location leak (CK hang)** — `locations.py` door-linking claimed
  door *destination* cells as interiors without checking. A city/Oblivion gate
  leads out to an exterior; XTEL destination doors are persistent and a
  worldspace stores all persistent refs in one dummy cell (Tamriel `00023777`),
  so one poisoned entry gave every persistent ref in Tamriel a single gate's
  location. Guard: only cells with `DATA.Flags & 1` (interior) may be
  door-claimed.
- **LCTN group order** — the CK resolves LCEC worldspace + MNAM marker when the
  LCTN top group loads. Vanilla order is `… NAVI CELL WRLD DIAL QUST … LCTN …
  DLBR DLVW`. LCTN before WRLD = 512× "Could not find worldspace in load" and
  undiscoverable markers.
- **PlayerRef remap** — `get_formid()` must not offset `0x14` (PlayerRef, in no
  data file). Nearly every *other* low FormID (Tamriel WRLD `0x3C`, gold `0xF`,
  Player NPC_ `0x7`, marker STATs, DIALs `0xAA+`) is a real Oblivion.esm record
  (~195 below `0x800`) and must remap. Pass-through set is exactly `{0x14}`
  (`_ENGINE_FIXED_FORMIDS` in `text_reader.py`).
- **Aimed magic needs projectiles** — an AIMED ENCH/SPEL whose effects have no
  projectile MGEF casts nothing. `magic_effects.py` synthesizes companion MGEFs
  (clone vanilla DATA from `vanilla_mgef_data.py`, regen via
  `tools/generators/gen_vanilla_mgef_table.py`; patch cast=FF/delivery=aimed/projectile).
  MGEF DATA offsets: proj `0x48`, arch `0x40`, AV `0x44`, cast `0x50`,
  delivery `0x54`.
- **SPEL cast type** — `convert_SPEL` packed CastType=2 (Concentration) for every
  spell; FF is 1 (0=Constant 1=FF 2=Conc 3=Scroll; scrolls use 3).
- **SGST→SCRL had zero effects** (dead sigil stones); scrolls also need ETYP
  (EitherHand `0x13F44`).
- **TES4 negative inventory counts** = merchant restock semantics → `abs()` for
  CNTO/LVLO.
- **8-byte LVLO** — the TES4 LVLO Count+pad tail is optional (xEdit
  `wbStructExSK` optional-from-element-3); 8-byte = Level(2)+pad(2)+FormID(4),
  count=1.
- **Orphaned topics** — Oblivion.esm ships ~856 zero-INFO placeholder DIALs;
  emitting them = one "Orphaned topic" each. Skip, and drop TCLT choices into
  them (`_EMPTY_DIAL_FIDS`).
- **Vanilla PTDA null target** = type 6 (Self), never type-0-fid-0.
- **Footstep sets** — FSTArmorLight `0x21486`, FSTBarefoot `0x21468` (the older
  `0x24238`/`0x24237` do not exist).
- **Race VTCK** — vanilla creature races fill *both* voice slots (DogRace:
  CrDogVoice ×2); nulls produce per-race CK warnings.

Re-verify with `python tools/validate/verify_ck_fixes.py <esm>`.
