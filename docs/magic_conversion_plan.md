# Magic Conversion: Analysis and Path to Completion

Status as of 2026-07-25. Measured with `python tools/magic_audit.py export/<Plugin>`
(written alongside this doc; re-run it after every change in this area).

## Summary

`MGEF` is in `SKIP_TYPES` (`tes5_import/constants.py:358`). Because no magic
effect is ever converted, every effect on every SPEL/ENCH/ALCH/INGR/SGST is
re-pointed at a **vanilla Skyrim MGEF** through a flat 4-char code table
(`MGEF_CODE_TO_SKYRIM` / `MGEF_AV_CODE_TO_SKYRIM` in `skyrim_overrides.py`),
and anything the table cannot name is **silently dropped**
(`_pack_effects`, `record_types/equipment.py:68`).

Measured fallout:

| | Oblivion.esm | Nehrim.esm |
|---|---|---|
| source MGEF records | 145 | 149 |
| mapped to a vanilla effect | 74 | 75 |
| **unmapped → effect dropped** | **71** | **74** |
| distinct Skyrim targets used | 51 | 51 |
| records losing **ALL** effects → filler | **382** | **356** |
| SPEL effects dropped | 375 / 1856 (20.2%) | 274 / 1138 (24.1%) |
| ENCH effects dropped | 298 / 2411 (12.4%) | 246 / 2745 (9.0%) |

382 Oblivion records (201 SPEL, 154 ENCH, 22 ALCH, 5 INGR) convert to a
zero-magnitude `AlchRestoreHealth` filler — they exist, they are castable, and
they do nothing. **330 NPC spell-list entries point at one of the 201 gutted
spells.** Every summon spell in the game is in that set.

The one thing that is *not* a gap: the summon/bound targets are all real
records this pipeline already converts — of 118 MGEFs carrying an `AssocItem`,
33 resolve to a CREA, 13 to a WEAP, 8 to an ARMO, 4 to an NPC\_ (the remaining
60 are actor-value indices, not FormIDs). And all 22 `meshes/magiceffects/*.nif`
are already converted and sitting unused in `output/`.

## The four defects, in priority order

### 1. Whole effect families are dropped because MGEF is never converted

The flat table can only express "Oblivion effect X behaves like Skyrim effect
Y". That works for value modifiers (Restore Health → `AlchRestoreHealth`) and
fails completely for effects whose behaviour is *parameterised by a FormID the
source record carries*:

- **All 33 summons** (`Z001`–`Z019`, `ZCLA`, `ZDAE`, `ZDRE`, `ZFIA`, `ZFRA`,
  `ZGHO`, `ZLIC`, `ZSCA`, `ZSKE`, `ZSPD`, `ZSTA`, `ZWRA`, `ZXIV`, `ZZOM`, …).
  Each is `AssocItem` = the creature to summon. There is no "summon *anything*"
  vanilla effect to point at, so all 33 are unmapped and dropped.
- **Bound weapons/armor** (`BWSW`, `BACU`, `BW01`–`BW08`, `MYHL`): `AssocItem`
  = the WEAP/ARMO to conjure.
- Effects with a plain Skyrim archetype that simply has no vanilla
  value-modifier stand-in: `OPEN` (16 uses), `DSPL` Dispel (55), `TURN` Turn
  Undead (32), `NEYE` Night-Eye (41), `CHRM` Charm (23), `WABR` Water Breathing
  (48), `WAWA` Water Walking (37), `DIAR`/`DIWE` Disintegrate (57).

Skyrim's MGEF `Archtype` enum covers essentially all of these — `18 Summon
Creature`, `17 Bound Weapon`, `16 Open`, `2 Dispel`, `24 Turn Undead`,
`12 Light`, `35 Cloak`, `21 Paralysis`, `1 Script`
(`wbDefinitionsTES5.pas:8145`). The conversion is *possible*; it just requires
emitting MGEF records instead of aliasing to vanilla ones.

### 2. 17 of the 100 table entries are codes that do not exist

The map was written against effect names rather than against the export.
These keys match **no MGEF in either Oblivion or Nehrim** and can never fire:

```
BACT RFDG SMAC SMBO SMCL SMDM SMFL SMFR SMGH SMLI SMSK SMSP SMZB
TNUN WBUA WKFW WKSK
```

The real Oblivion codes for those concepts are `ZFIA`/`ZFRA`/`ZSTA`/`ZDRE`
(summons), `TURN` (turn undead), `WABR` (water breathing), `WKFI`/`WKSH`
(weaknesses) — all of which are in the *unmapped* list. So the summon entries
in the table look like coverage but contribute nothing, while the codes the
game actually uses fall through to 0. `BACU` (Bound Cuirass, 8 uses) is
unmapped while the phantom `BACT` is mapped.

**Every new entry must be validated against `export/*/MGEF.txt`**, not against
a name. `tools/magic_audit.py` reports phantom keys.

### 3. ~~The vanilla DATA blobs are truncated — 96 bytes where 152 are required~~ — FIXED 2026-07-25

`tes5_import/vanilla_mgef_data.py` claimed "152-byte DATA hex" in its
docstring. Every one of its 80 blobs was **96 bytes**.

Cause: `tools/gen_vanilla_mgef_table.py:read_dump` did
`line[9:].split('...')[0]`, and the generic hex fallback in the dump writer
truncates at 192 chars (`tools/tes5_esm_reader.py`) — so the `...` split
silently discarded the tail instead of failing.

Consequence: every MGEF synthesized by `magic_effects.aimed_variant()` was
written with a 96-byte DATA, missing the 14 fields from offset 96 to 151:
`Hit Effect Art`, `Impact Data`, `Skill Usage Multiplier`, `Dual Casting
Art/Scale`, `Enchant Art`, `Hit Visuals`, `Enchant Visuals`, `Equip Ability`,
`Image Space Modifier`, `Perk to Apply`, `Casting Sound Level`, and `Script
Effect AI Score/Delay`.

Resolved by Phase 0 below — all 80 blobs are now full 152-byte structs,
byte-verified against the dump they are generated from.

### 4. All per-effect art, sound and counter-effect data is discarded

Per MGEF, the source carries and the conversion drops:

| field | Oblivion MGEFs carrying it |
|---|---|
| `Model.MODL` (cast art) | 141 |
| casting/bolt/hit/area sounds | 144 |
| `EffectShader` / `EnchantEffect` | 83 |
| `AssocItem` | 118 |
| `Light` | 87 |
| counter effects (ESCE) | 41 |

`EFSH` *is* converted (`record_types/world.py:856`) but nothing references the
result for magic, because no MGEF is written. The 22 magic-effect meshes are
converted and unused. Since the mesh pipeline is now solid, this data is
recoverable — it just has nowhere to land today.

Note also that `EFSH` conversion itself is partial: it writes a 128-byte DATA
populated only through offset 44 (flags, fill colour, 6 fill-alpha floats) and
zeroes the rest.

Supporting record types with **no writer at all**: `ARTO`, `RFCT`, `IPDS`,
`EXPL`, `PROJ`, `HAZD`, `SCRL`. `ARTO` (art object) is what Skyrim uses for
casting/hit art — it is the destination for those 141 `Model.MODL` paths.

## Path to complete conversion

Ordered so each phase is independently shippable and testable. Phase 0 is a
prerequisite for everything after it.

### Phase 0 — Fix the truncated DATA table (prerequisite) — DONE 2026-07-25

1. **Typed MGEF DATA decoder** (`_dec_mgef_data` in `tools/tes5_esm_reader.py`,
   registered as `('MGEF', 'DATA')`). It emits the hex **untruncated** plus all
   38 named fields with the archetype enum resolved, so the dump is both
   lossless for the generator and readable for analysis. Adding a typed decoder
   rather than raising the global 96-byte hex cap keeps every other record
   type's dump size unchanged.
2. **Regenerated** `references/Skyrim.esm/MGEF.txt` from the real
   `Skyrim.esm` — 950 records, every `DATA.hex` now 152 bytes.
3. **Generator now fails loudly** — `read_dump` no longer does
   `split('...')`; a blob that is not `MGEF_DATA_SIZE` raises `SystemExit`
   naming the offending effects and the exact regeneration command. The
   "wanted FormID not in Skyrim.esm" case is called out as a mapping table
   pointing at an effect that does not exist.
4. **Consumer asserts too** — `magic_effects.aimed_variant()` raises rather
   than writing a short DATA, so a bad table can never reach the output ESM.
5. The generated module now exports `MGEF_DATA_SIZE` alongside the table.

Verified: all 80 blobs are 152 bytes and match the dump **byte-for-byte**
(80/80); a synthesized clone writes a 152-byte DATA with Casting Type=1,
Delivery=2 (Aimed), a real projectile, zeroed counter count, and an intact
tail (`DualCastScale == 1.0` at offset 112 — past the old 96-byte cut).
Re-truncating the dump reproduces the original bug and the generator now exits
1. Regression tests: `TestVanillaMgefDataSize` in `tests/test_import.py`.

Immediate benefit: the aimed-variant clones stop shipping a malformed DATA.

### Phase 1 — Convert MGEF as a real record type

Remove `'MGEF'` from `SKIP_TYPES` and add a `convert_MGEF` in a new
`tes5_import/record_types/magic.py`. Emit `EDID`, `FULL`, `MDOB`, `DATA` (152
bytes, FormVersion 44), `ESCE` array, `SNDD`, `DNAM`.

Field derivation (TES4 offsets from `tes4_export/record_types/equipment.py:219`,
TES5 from `wbDefinitionsTES5.pas:8195`):

| TES5 field | off | source |
|---|---|---|
| Flags | 0 | remap TES4 flag bits (they differ — TES4 0x8 = MagnitudePercent, TES5 0x8 = Snap to Navmesh) |
| Base Cost | 4 | `DATA.BaseCost` |
| Assoc. Item | 8 | `DATA.AssocItem`, load-order remapped, **only when the archetype uses it** |
| Magic Skill | 12 | `DATA.School` → Skyrim AV (Alteration/Conjuration/Destruction/Illusion/Restoration; Mysticism has no equivalent — fold to Alteration) |
| Resist Value | 16 | `DATA.ResistValue` → Skyrim AV |
| Counter Effect Count | 20 | `len(ESCE)` — must match the array exactly |
| Casting Light | 24 | `DATA.Light` |
| Hit/Enchant Shader | 32/36 | converted `EFSH` from `DATA.EffectShader` / `DATA.EnchantEffect` |
| **Archtype** | 64 | from the effect code (table below) |
| Actor Value | 68 | from the effect's TES4 attribute/skill AV |
| Projectile | 72 | needed for aimed delivery (see Phase 3) |
| Casting Type / Delivery | 80/84 | from the TES4 flag bits Self/Touch/Target |

The archetype table replaces the current FormID table as the primary mapping.
Sketch, by family:

- summons (`Z0xx`, `Zxxx`) → `18 Summon Creature`, AssocItem = converted CREA/NPC\_
- bound weapon/armor (`BW*`, `BA*`, `BACU`, `MYHL`) → `17 Bound Weapon`, AssocItem = converted WEAP/ARMO
- `OPEN` → `16 Open`; `LOCK` → `15 Lock`
- `DSPL` → `2 Dispel`; `TURN` → `24 Turn Undead`
- `CHRM`/`CALM` → `6 Calm`; `DEMO` → `7 Demoralize`; `FRNZ` → `8 Frenzy`; `RALY` → `38 Rally`
- `PARA` → `21 Paralysis`; `REAN` → `22 Reanimate`; `STRP` → `23 Soul Trap`
- `INVI`/`CHML` → `11 Invisibility`; `LGHT` → `12 Light`; `NEYE` → `0` + Night-Eye AV
- `TELE` → `20 Telekinesis`; `DTCT` → `19 Detect Life`
- `FISH`/`FRSH`/`LISH` → `35 Cloak`
- damage/restore/fortify/drain/absorb → `0 Value Modifier` / `4 Absorb` / `5 Dual Value Modifier`, with the AV carrying the meaning
- `SEFF` → `1 Script` (see Phase 4)

Keep the vanilla-alias table **only** as a fallback for codes with no sensible
archetype, and delete the 17 phantom keys.

Verification: `magic_audit.py` unmapped count → 0; no record falls back to
filler; dump a converted summon MGEF and diff its DATA field-by-field against
`SummonFlameAtronach` from the Skyrim.esm dump.

### Phase 2 — Wire the art back in

1. **`ARTO` writer** — one art object per distinct MGEF `Model.MODL`; point
   `Casting Art` (92) / `Hit Effect Art` (96) at it. The meshes are already
   converted in `output/.../meshes/tes4/magiceffects/`.
2. **Sounds** — emit `SNDD` from the four TES4 sound FormIDs (casting/bolt/hit/
   area) via the existing SOUN→SNDR conversion.
3. **`ESCE`** — counter effects are MGEF→MGEF references; trivially convertible
   once MGEF records exist (41 effects carry them). The count field at offset 20
   must match.
4. Complete the `EFSH` DATA beyond offset 44.

### Phase 3 — Projectiles and delivery

With real MGEFs, the aimed-variant hack in `magic_effects.py` becomes
unnecessary for effects we author: set `Delivery`/`Casting Type` and a
`Projectile` directly from the TES4 flags and `DATA.ProjectileSpeed`. That
needs a **`PROJ` writer** (and ideally `EXPL` for area effects, which the
current pipeline ignores entirely — TES4 `Area` is packed into EFIT but no
explosion is ever created). Retire `aimed_variant` once every aimed item's own
effects carry a projectile.

### Phase 4 — Script effects (`SEFF`)

The single most-used dropped code: **143 uses in Oblivion, 176 in Nehrim**.
Today it maps to 0, so a script-effect spell converts to an inert filler that
merely holds the original duration so `HasMagicEffectByID` polling still sees
it (`_pack_effects`, the `filler_dur` path).

Skyrim's `1 Script` archetype plus a `VMAD` on the MGEF is the real
destination, and the script converter already exists. This is the natural
follow-on once Phase 1 lands: emit archetype 1 and attach the converted
Papyrus fragment, rather than discarding the effect and faking its duration.

### Phase 5 — Cleanup

Remove the filler-effect machinery and the `_FILLER_EFFECTS` padding once no
record loses all its effects; keep the INGR `pad_to=4` requirement.

## Rules for working in this area

- **Validate every effect code against `export/*/MGEF.txt`.** 17 of 100
  existing entries are codes no Oblivion or Nehrim record uses. A plausible-
  looking 4-char code is not evidence.
- **A hand-built TES5 MGEF DATA is 152 bytes, FormVersion 44.** Verified
  against both `wbDefinitionsTES5.pas:8195` and the Skyrim.esm dump.
- **`Counter Effect Count` (offset 20) must equal the number of `ESCE`
  subrecords** or the CK reads garbage counter slots.
- **`AssocItem` is archetype-dependent** (`wbMGEFAssocItemDecider`): it is a
  LIGH/WEAP+ARMO/NPC\_/HAZD/SPEL/RACE/ENCH/KYWD depending on the archetype.
  Writing a creature FormID under a value-modifier archetype is meaningless.
- Never introduce a null `EFID` — it crashes the inventory menu as soon as the
  item card is shown. That constraint is why filler effects exist and it
  survives this plan.
- Re-run `python tools/magic_audit.py export/Oblivion.esm` **and**
  `export/Nehrim.esm` after each change; Nehrim exercises 16 mod-authored
  effect codes (`BA01`–`BA10`, `BW09`, `BW10`, `DISE`, `DUMY`, `RSWD`, `Z020`)
  that Oblivion does not, and its strings are German (the tool forces UTF-8
  output for this reason).
