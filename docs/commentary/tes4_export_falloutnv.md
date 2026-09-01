# FO3/FNV export deltas

**Code:** `tes4_export/record_types/falloutnv.py`, `tes4_export/tes4_reader.py`

Why a Fallout plugin needs its own delta module, and why the field list is
shorter than a signature census predicts.

## The header size is per-file, not a constant

FO3/FNV record headers are 24 bytes against Oblivion's 20, and **the GRUP header
scales with them**. An early note claimed `GROUP_HEADER_SIZE` was fixed at 20 in
both games; that was wrong, and came from a test that set the group size to 24
for *both*, which corrupted the Oblivion parse (63 types collapsed to 31). The
measurement that settles it: an FO3/FNV GRUP's first record signature sits at
+24, Oblivion's at +20.

`detect_header_size` probes both offsets for the `HEDR` literal rather than
trusting a version float, then one detected size drives both headers. Across 60
real plugins the split is unambiguous — 5 TES4 files report HEDR 1.00 at offset
20, and 55 FNV/FO3 files report 1.32-1.34 at offset 24.

`_format_chunk_worker` re-detects from its own mmap instead of receiving the size
in its args tuple. `ProcessPoolExecutor` runs with no initializer, so on Windows
spawn a module global set in the parent never reaches the worker; detecting
locally sidesteps the problem without widening the args tuple.

## A shared signature is not a shared field

The subrecord census that motivated this work compared signature presence and
frequency. That is necessary but not sufficient, and three fields prove it.

**`XCLC` is 12 bytes in FO3/FNV, 8 in TES4** — X, Y, then a land-flags byte and
3 unused. `export_CELL` reads X and Y at offsets 0 and 4 behind a `>= 8` guard,
so it was already correct for both; only the flags byte is new.

**`XCLL` is 40 bytes against TES4's 36.** The extra trailing float is Fog Power.
Everything before it is positionally identical, so the existing reader is safe
and only the tail needed adding.

**`LNAM` looks like a passthrough and is not.** In FO3/FNV it is a `U32` of
lighting-inherit flags; in Skyrim it is `wbByteArray(LNAM, 'Unknown', 0,
cpIgnore)` — leftover, ignored, with the real flags moved into `XCLC`. It is
dropped rather than passed through.

## Format-identical fields whose referents do not exist

The 14.4% "Skyrim-native passthrough" bucket is right about layout and wrong
about five CELL fields. `LTMP`, `XCAS`, `XCIM`, `XCMO` and `XEZN` are byte-for
byte the same in FNV and Skyrim per xEdit, but every one is a FormID pointing at
a record type this converter does not emit:

| Field | Points at | Resolved in FalloutNV.esm |
|---|---|---|
| `LTMP` | `LGTM` | 273 real + 30,224 null |
| `XCAS` | `ASPC` | 17,485 |
| `XCIM` | `IMGS` | 321 |
| `XCMO` | `MUSC` | 297 |
| `XEZN` | `ECZN` | 84 |

None of `LGTM`, `ASPC`, `IMGS`, `MUSC` or `ECZN` is in `IMPORT_DISPATCH`.
Passing these through writes dangling FormIDs, which is a rejection the CK
reports as a broken reference rather than a bad value. They drop until their
target types convert.

`XNAM` survives the same test: FNV and Skyrim both define it as
`wbString(XNAM, 'Water Noise Texture')`, and it carries no FormID. 30,458 of
30,495 are a 1-byte empty string; 36 carry real 35-47 byte paths.

## Measured field coverage

From `FalloutNV.esm` (Tale of Two Wastelands merge), 465,054 records parsed
through the shipped reader:

| Type | Records | Notes |
|---|---|---|
| `REFR` | 307,710 | `NAME`+`DATA`+`XSCL` = 89% of 816k subrecords |
| `CELL` | 30,497 | `DATA`/`LNAM`/`LTMP`/`XCLW` fire on every record |
| `LAND` | 29,363 | 7 subrecord types, all TES4-known |
| `STAT` | 6,795 | `OBND` on all; `BRUS` (3,414) drops |
| `ACHR` | 3,386 | |
| `ACRE` | 2,999 | |
| `DOOR` | 320 | `OBND` on all |
| `WRLD` | 14 | |

`LAND` needs no delta handling at all — every one of its 7 subrecord signatures
is one Oblivion also emits, at the same size.

## Fallout-only base objects

FO3/FNV place references against base-object types Oblivion does not have. The
records are skipped, but the REFRs that point at them are not — so the base
resolves to null and the engine faults dereferencing it. The measured crash:
`EXCEPTION_ACCESS_VIOLATION` reading `[rcx+0x1A]` with `rcx = 0` inside
`QueuedPromoteLocationReferencesTask`, while promoting a persistent REFR into
`BGSLocation \"Mojave Wasteland\"`.

Measured in the first FalloutNV output: **721 base objects, 11,722 references**
with no record behind them. Oblivion's output has 6 such targets, all of them
deliberate Skyrim.esm marker references (`XMarker`, `XMarkerHeading`,
`NorthMarker`, `MapMarker`, `Gold001`), which resolve through the master.

| Type | Bases | Refs | Converted as |
|---|---|---|---|
| `MSTT` movable static | 224 | 7,823 | STAT |
| `TERM` terminal | 171 | 219 | ACTI |
| `IDLM` idle marker | 98 | 1,424 | STAT |
| `SCOL` static collection | 83 | 1,084 | STAT |
| `NOTE` note | 51 | 54 | ACTI |
| `ASPC` acoustic space | 32 | 49 | STAT |
| `TACT` talking activator | 31 | 43 | ACTI |
| `PWAT` placeable water | 28 | 102 | STAT |

Every one carries `MODL` and `OBND`, which is all a Skyrim STAT needs; the
named, scriptable ones (`TERM`, `NOTE`, `TACT`) additionally carry `FULL`
and a script, making them ACTI. `SCOL` loses its `ONAM` part list and renders
as the collection's own model rather than its instanced pieces.


## LTEX moved its texture to a TXST

Oblivion names a landscape texture directly on `LTEX.ICON`. FO3/FNV keep
`ICON` in the record definition but do not use it: the texture is a `TNAM`
FormID pointing at a `TXST` record, whose `TX00` holds the diffuse path.
xEdit's FNV definition declares both, and the shipped data settles which is live.

All 89 of FalloutNV.esm's LTEX records carry a `TNAM` and **zero** carry an
`ICON`, so an ICON-only exporter emits no texture at all and the terrain
renders black or falls back to the Skyrim default.

`index_texture_sets` indexes the 495 TXST records that carry a `TX00`, and
`ltex_icon_path` resolves `TNAM` through it. The TX00 paths are spelled
`Landscape\Asphalt02.dds`, while `convert_LTEX` prepends
`landscape\` itself (Oblivion's ICON convention is relative to that
folder), so the prefix is stripped on the way out rather than teaching the
importer a second form.
