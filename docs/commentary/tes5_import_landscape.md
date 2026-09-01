# tes5_import/record_types/region.py — REGN, LSCR and WATR

**Code:** `tes5_import/record_types/region.py`, `tes5_import/record_types/common.py`

Regions, water types and load screens are worldspace-scoped decoration rather
than placement geometry, which is why they live apart from the CELL/WRLD/REFR
converters in `world.py`.

REGN's own weather and music behaviour is documented where the rest of those
subsystems live:
[weather](tes5_import_weather.md#regn-weather-where-cyrodiils-weather) and
[exterior music](asset_convert_audio.md#exterior-music-comes-from-regnrdmo).

## Contents

- [WATR — the DNAM offset trap](#watr-dnam-offset-trap)
- [WATR — fields deliberately not carried](#watr-fields-not-carried)
- [LSCR — NNAM is mandatory](#lscr-nnam-mandatory)

## WATR — the DNAM offset trap
<a id="watr-dnam-offset-trap"></a>

TES4's `DATA` is prefix-compatible with TES5's 228-byte `DNAM` water-visuals
struct **as far as the colour block, but NOT at the same offsets.** TES4 carries
a Scroll X/Y Speed pair at bytes 28-35 that TES5 dropped, so every field from
Fog Near onward sits **4 bytes earlier** in TES5: colours land at 40/44/48, not
TES4's 44/48/52.

Writing TES4's offsets straight through put the colours in the rain-simulator
region, left the real colour bytes zeroed, and rendered every converted water
surface as undefined near-black. Offsets are derived from the xEdit TES5
definition and verified field-by-field against Skyrim.esm's `DefaultWater`,
`LavaWater` and `DefaultVolcanicWater`.

**Subrecord order matters too.** Vanilla order is
`EDID NNAM* ANAM FNAM [MNAM] [SNAM] DATA DNAM [GNAM NAM0 NAM1]`. This was once
written with the 228-byte struct under the `DATA` tag and a bogus 196-byte block
under `DNAM`, which SSEEdit's background loader flags on **every** WATR record
("unexpected (or out of order) subrecord DATA"/"DNAM").

`DATA` in TES5 is a 2-byte Damage-per-second value. TES4 carries its damage at
the tail of its own `DATA` struct and gates it behind `FNAM` bit 0 ("Causes
Damage") — `OblivionLavaTest01` authors 50/sec that way. The gate is honoured: a
record with a damage value but no flag is not meant to hurt.

### DNAM field groups, and why each is sourced the way it is

| Bytes | Source | Why |
|---|---|---|
| 0-15 | vanilla constants | wind/wave. TES5 marks them unused, but every vanilla record still writes the same four values, so they are mirrored rather than taken from TES4. |
| 16-27 | TES4, renormalised | the surface response TES4 does author. Sun Power is a 0-50ish scale in TES4 and a ~1000 scale in TES5 (vanilla: 1021 default water, 1000 lava), so it is rescaled rather than copied raw. The rest are 0-1 ratios meaning the same in both games. |
| 32-39 | TES4, verbatim | above-water fog distance. |
| 40-52 | TES4, RGB only | the colour block — the whole visual identity of the water, and the reason Oblivion's realms came through as ordinary blue. Alpha is 0 in every vanilla record. |
| 56-227 | Skyrim `DefaultWater` | noise, fog-under, specular and depth. TES4 has no source for any of these — they describe a shader it does not have — so they take the values an unedited record in the CK would carry. Zeroing them gives water with no noise scale and no depth response. |

## WATR — fields deliberately not carried
<a id="watr-fields-not-carried"></a>

| Field | Why not |
|---|---|
| `NNAM` (noise maps) | TES5 takes three (one per noise layer) and every vanilla record points all three at the same file. TES4 authors a single **diffuse surface** texture, not a normal/noise map, so feeding it to Skyrim's noise sampler produces garbage displacement. All 34 vanilla records use `DefaultWater.dds`; so do we, letting the DNAM colours carry the look. |
| `FNAM` bits above 0 | Bit 0 (Causes Damage) means the same in both games. TES4 bit 1 is "Reflective", which TES5 reassigned (bit 3 Enable Flowmap, bit 4 Blend Normals in SSE). Passing the raw byte would set flowmap/normal-blend bits at random, so only bit 0 is carried. |
| `GNAM` / `NAM0` / `NAM1` | Required by the xEdit definition and present on all 34 vanilla records, always zeroed. TES4's Scroll X/Y Speed is NAM0's closest analogue, but the units differ by orders of magnitude — TES4 authors 0.0011 where vanilla NAM0 carries 0.22 — so it is not carried. |
| `MNAM` / `TNAM` | TES5 marks MNAM "Material ID (Unused)" and every vanilla record that writes it writes a zero byte array, not the TES4 material string. TNAM took over as the material reference and is a MATT FormID; Skyrim ships no lava MATT and only 5 of 34 vanilla records set TNAM at all. Neither is emitted. |

## LSCR — NNAM is mandatory
<a id="lscr-nnam-mandatory"></a>

TES5 loading screens use a 3D model, not a 2D texture: `NNAM` is a required
FormID → STAT. TES4 has no 3D model reference, so NULL (0) is written. `ICON` is
omitted for the same reason — it is the 2D path TES5 no longer uses.

## CELL water and music

**Code:** `_cell_water_and_music` in `tes5_import/record_types/world.py`

`XCWT` is the cell's own water type, overriding the worldspace's `NAM2`. This is
how Oblivion authors the lava in its realm interiors — 46 of the 162 cells that
set `XCWT` name a lava record — and dropping it left every one of them on the
worldspace default. It is emitted after `XLCN` and before ownership to match the
xEdit CELL subrecord order.

`XCMO` is the music type. TES4 stores only a 3-value enum (`XCMT`: 0 Default,
1 Public, 2 Dungeon) because Oblivion's engine picks the actual track by scanning
`Data/Music/<Category>/`; Skyrim needs a `MUSC` FormID instead. Measured source:
1,104 Dungeon + 767 Public cells in Oblivion.esm, 386 + 227 in Nehrim.esm.
Vanilla Skyrim.esm sets `XCMO` on 701 cells.

An **interior** with no authored `XCMT` gets the same enum-0 default the engine
applies (see `convert_WRLD`'s ZNAM note): it has no worldspace to inherit from,
so leaving the subrecord off makes it silent. 382 Oblivion and 219 Nehrim
interiors are in this state.

**Exteriors are deliberately left alone** — 33,241 of Oblivion's 33,560 have no
`XCMT`, and stamping every one with an explicit `XCMO` would add ~33k subrecords
to say what the worldspace's single `ZNAM` already says, and would override any
future per-region music with a cell-level value.

## OBND: authored bounds, and the int16 clamp

**Code:** `tes5_import/record_types/common.py` (`_resolve_obnd`),
`tes5_import/writer.py` (`pack_obnd`)

OBND is six **signed 16-bit** ints. Two independent defects met here and
crashed the game.

### Authored bounds win

FO3/FNV write OBND natively; TES4 has no such field. Measured over the exports:

| | records | carry OBND | types |
|---|---:|---:|---:|
| FalloutNV.esm | 465,054 | **15,272** | 23 |
| Oblivion.esm | 1,167,016 | **0** | 0 |

`_resolve_obnd` consulted only the scanned mesh bounds, discarding the authored
value. It now prefers the record's own OBND, then mesh bounds, then the
per-type default. Oblivion authors none, so its fallback chain is untouched.

This also means FO3/FNV need no mesh-bounds scan to get correct OBND — the
plugin already carries the answer the CK computed.

### The int16 clamp

`pack_obnd` used `struct.pack('<6h', ...)` unguarded. A converted mesh larger
than 32,767 units raised `struct.error`, the record was **dropped entirely**,
and every reference to it then pointed at nothing:

```
ERROR converting ACTI 'NVStripLightsPollition': 'h' format requires -32768 <= number <= 32767
ERROR converting ACTI 'NVStripLightsPollitionDim': 'h' format requires -32768 <= number <= 32767
```

Exactly **2 of 1,143** FNV ACTIs were lost this way. The resulting crash:

```
EXCEPTION_ACCESS_VIOLATION   movzx eax, byte ptr [rcx+0x1A]
  rcx = 0x0000000000000000
  RSI: (BGSLocation*) "South Cistern"
  RSP+1A0: (QueuedPromoteLocationReferencesTask*)
```

`QueuedPromoteLocationReferencesTask` walked the location's references, hit a
REFR whose base object had been dropped, and dereferenced null. Both lost ACTIs
were `NVStripLightsPollition*`, referenced 8 times.

**This is a latent TES4 bug too** — nothing about the overflow is FO3/FNV
specific, so bounds are now clamped for every game rather than raising. All
15,272 authored FNV values are already inside int16, so the clamp only ever
catches our own computed bounds.

### Finding a dangling base

`temp`-style census: walk the output ESM, collect every record FormID, then
check each REFR/ACHR `NAME`. After these fixes only the FO3/FNV
engine-hardcoded ids remain unresolved (`0x20`, 851 refs; `0x17`, 71 refs),
which are not records in any plugin.
