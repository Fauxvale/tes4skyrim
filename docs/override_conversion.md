# Override Conversion (plugins with masters)

Linked from [CLAUDE.md](../CLAUDE.md).

Converting a plugin that has TES4 masters (Nehrim's `Translation.esp` is ~100%
overrides) follows xEdit's "copy as override" model. Modules:
`overrides.py` (OverrideContext + nested-GRUP emission — the only override
code import_main touches), `export_diff.py`, `master_manifest.py`,
`override_builder.py` (field application), `override_merge.py` (master index).
Audit coverage anytime with `python tools/override_audit.py
export/<Plugin>` — it reports, per record type, what the override path does
with every record and every authored field with no output mapping.

**The rule:** a NEW record takes the normal conversion path. An OVERRIDE is the
master's converted record bytes EXACTLY, with only the fields the author changed
substituted in — and authorship comes from diffing the two TES4 EXPORTS, never
from comparing two conversion runs.

- **Never diff two conversions.** It conflates "the author changed this" with
  "our pass re-derived it differently", and the converter cannot tell them
  apart. That produced 1821 NPC_ `RNAM` races rewritten to vanilla Skyrim ones
  (the authors changed ZERO) and hung the game on load. Diffing the exports
  answers the question directly: a field neither export touches is never
  rewritten, so it cannot drift. No heuristics, no guessing.
- **List diffs must be ORDER-INSENSITIVE.** Oblivion does not preserve list
  order between a master and an overriding plugin: 1166 of 1264 Nehrim NPC_
  inventories differ positionally while only 5 differ as a set.
- **Export every record; never filter by load-order index.** A record whose
  FormID carries a master's index is an OVERRIDE. The old `source_filter`
  dropped 13,890 of Translation.esp's 13,892 records.
- **The FormID shift is the count of NEWLY PREPENDED masters, not
  `len(masters)`.** Using the latter moves overrides onto the plugin's own
  index, turning all 12,177 into duplicate new records.
- **Companion pairings come from a MANIFEST, not inference.** Converting one
  record generates companions (ARMO->ARMA, NPC_->OTFT/VTYP, AMMO->PROJ) whose
  ids come from a bare sequential counter. `writer.converting(source_fid)`
  records the pairing AT CREATION into `<Plugin>.manifest.json`; the plugin's
  run reads it. Re-deriving is impossible — the plugin converts a few thousand
  records, not the master's ~700k, so the counter lands elsewhere.
- **A cell override ships ONLY the references it CHANGES, never the master's
  whole child list.** Verified against BS_DLC_patch.esp: 54 cell overrides, ~5
  REFRs each (278 total), not the master's contents. Copying the full list
  bloated Translation.esp to 34 MB and duplicated 265k records for no benefit.
- **ONAM is what keeps the master's other refs visible.** The header's ONAM
  array lists every record this file overrides in a master's TEMPORARY cell-
  children group (type 9); the engine loads those on demand and, per xEdit's
  docs, "will ignore the override that is missing from ONAM". Persistent refs
  (type 8) are always resident and excluded. BS_DLC_patch's 216 ONAM entries
  are exactly its 216 temporary-group overrides — `writer.py` builds the list
  the same way at save time. WITHOUT ONAM a cell override suppressed the
  master's temporary children and interiors rendered black.
- **A record's children group must directly FOLLOW that record.** The engine
  reads `CELL, GRUP(6,cell), CELL, GRUP(6,cell), ...`; emitting all the records
  first and their groups afterwards pairs each group with the wrong cell.
- **A type-1/6/7 GRUP is bound to its owner ONLY by physical adjacency, so an
  unchanged owner must be pulled in as an ANCHOR.** xEdit states the rule
  exactly (`TwbGroupRecord.InformPrevMainRecord`, wbImplementation.pas ~18023):

      if (grStruct.grsGroupType in [1, 6, 7]) and Assigned(aPrevMainRecord)
         and (aPrevMainRecord.FixedFormID.ToCardinal = GetGroupLabel) then

  1 = world children (under WRLD), 6 = cell children (under CELL), 7 = topic
  children (under DIAL). A group of one of those types that is NOT immediately
  preceded by its owning record attaches to NOTHING, and every record inside it
  is unreachable — **while the file still loads cleanly and still looks correct
  in xEdit**, which is what made this silent. A plugin hits it whenever it
  changes a container's CONTENTS but not the container itself: DLCBattlehornCastle
  overrides Tamriel's exterior cells without touching `WRLD 0000003C`, so its
  type-1 group stood alone and all 473 exterior cell/REFR overrides were dead;
  Translation.esp had 958 orphans, 945 of them type-7 (it retitles INFOs under
  DIALs it never edits). The fix is generic in `emit_nested_overrides`: for any
  owned-type group whose owner this plugin does not emit at that level, the
  owner's converted bytes are pulled from the master VERBATIM and written
  immediately before the group — the same thing xEdit's copy-as-override does.
  Anchoring only the type-6 case (the original code) leaves the WRLD case
  broken. Gate every override plugin with `tools/esm_group_anchors.py`.
- **The plugin's TES4 master NAMES come from the export `_HEADER.txt`, not the
  source binary.** `convert.py` derives its master list from the binary in the
  configured Oblivion data folder, but that file may not be there at all (a
  Nehrim plugin against a Steam Oblivion install), so the list came back as
  just `['Skyrim.esm']` while `_HEADER.txt` still correctly reported 1 TES4
  master. Taking the count from one source and the names from the other made
  `masters[len(masters) - count:]` slice the tail off `['Skyrim.esm']` and
  demand a converted `output/Skyrim.esm/Skyrim.esm` — Translation.esp aborted
  with "convert the master first: Skyrim.esm" and could not be built at all.
  `import_main` now reconciles the two and trusts the header.
- **NEVER synthesize an empty children group.** xEdit deletes them:
  `if Assigned(ChildGroup) and (ChildGroup.ElementCount < 1) then
  ChildGroup.Remove` (wbImplementation.pas ~5607).
- **Interior cells bucket by the last two DECIMAL digits of the OBJECTID**
  (`fid & 0xFFFFFF`): block = ones digit, sub-block = tens digit (xEdit
  wbImplementation CheckPosition; verified against Skyrim.esm). Bucketing by
  the full FormID (master-index byte included) put every Nehrim interior in
  the wrong block/sub-block. The master ALONE still played fine — the engine
  reads a winning cell's children from the offset recorded at load — but when
  a plugin overrides the CELL record, the engine re-locates the MASTER's copy
  via the bucket walk to demand-load its temporary children; wrong bucket =
  refs silently missing, only the eagerly-loaded persistent refs survive
  (renamed Translation.esp cells showed only Nehrim.esm persistent refs, and
  an xEdit copy-as-override reproduced it because xEdit buckets correctly
  while the master didn't). Exterior labels were verified CORRECT as written:
  `('<hh', Y, X)` with FLOOR division (Skyrim.esm grid x=7,y=-41 sits in
  sub-block low=-6=y//8, so Pascal-style truncation would be wrong).
- **Persistent-flagged refs inside temporary groups are vanilla-legal** —
  Skyrim.esm itself has 0x400-flagged REFR/ACHRs in type-9 groups
  (DragonBridgeFarm). DLC ESMs keep it clean (persistent overrides in type 8),
  which our source-flag routing already matches. Don't "fix" this.
- **But the record must still sit in the master's GRUP NESTING.** Interior:
  `CELL -> type 2 -> type 3`. Exterior: `WRLD -> type 1 -> type 4 -> type 5`.
  INFO: `DIAL -> type 7`. A record written flat under its top-level group is
  never indexed by the engine — as invisible as a missing one, and the second
  cause of black cells. Copy the nesting from `MasterIndex.group_path()`
  instead of recomputing it; there is then no block-number formula to get wrong.
  (Note the GRUP header layout: label is at offset 8, type at 12.)
- **Skip support-record creation for a plugin with masters.** VTYP/LCTN/vendor
  factions/TES4 globals are created by the master's run; recreating them in a
  dependent plugin duplicates master content (27 VTYP, 35 GLOB, 27 FACT, 265
  LCTN) and the duplicates compete with the originals the overrides reference.
- **INJECTED records** (Oblivion let a plugin ADD records at a master's index;
  Translation.esp does it 3x) move into OUR index. Detect against the master's
  **export**, never its converted output: conversion re-keys DIAL/INFO and skips
  whole types, so judging by output called 1693 records injected instead of 3.
- An export key `override_builder` cannot map is REPORTED, never approximated —
  the master's value stays and the run prints a summary. Mapped changes are
  applied three ways: translated-string substitution, SUBRECORD REBUILD (the
  converter's own builder — `_npc_acbs`, `build_cell_xcll`, `build_armo_bod2`,
  … — re-run against the PLUGIN's export and swapped in whole; drift-free
  because unchanged fields are identical in both exports), and run rebuilds
  for repeated-subrecord families (CNTO/SPLO/PKID) that preserve
  converter-ADDED entries (vendor gold, quest-package filtering) by deriving
  them from master-export-vs-master-output. Effect-list changes (SPEL/ENCH)
  RECONVERT the whole record instead — clone companions can't be spliced.
- **TES4 QSTA 'Flags' is a u8 + 3 bytes of uninitialized CS garbage** — diff
  it masked (`export_diff._LIST_FIELD_NORMALIZERS`) or 58 quests report
  phantom Target[] changes. Expect more TES4 fields like this; the fix
  belongs in the DIFF, never the export (which stays a pure dump).
- **LAND terrain overrides: VNML/VHGT/VCLR are hex blobs with IDENTICAL TES4
  and TES5 layout**, so `convert_LAND` copies them straight through and the
  authored value is directly substitutable — no re-derivation, no drift. They
  had no `_REBUILDERS` entry at all, so every terrain edit was reported
  "no output mapping" and kept the master's terrain: DLCBattlehornCastle
  authored `VHGT` on all 16 of its LAND overrides (plus VNML×10, VCLR×8,
  Layer[]×5) and the castle sat on Oblivion's untouched hillside. `Layer[]`
  needs a `_RUN_REBUILDERS` entry instead — the whole BTXT/ATXT/VTXT run is
  replaced through `world.build_land_layers` (extracted from `convert_LAND` for
  exactly this). **Reuse that builder, never reimplement it**: the mapping is
  lossy and order-dependent (same-texture merge, coverage sort, 6-alpha-per-
  quadrant cap), so a second implementation would disagree with the master's
  for layers the author never touched. Verified the extraction is pure —
  4,000/4,000 master LAND records still convert byte-identically.
  - **The last 3 bytes of VHGT are `wbUnused(3)`** (wbDefinitionsCommon.pas
    `wbLandHeights`: `wbFloat Offset` + 33×33 `itS8` + `wbUnused(3)`). Vanilla
    settles it: a census of 15,410 Skyrim.esm LAND records finds arbitrary junk
    there — `000000` is merely the most common of many values (`3e9e23`,
    `b57086`, `ea5b25` … each in the hundreds-to-thousands). Comparing them
    reported phantom VHGT changes on 6 of the 16 whose real terrain was
    identical, emitting override records with zero authored content. Normalised
    in `export_diff._SCALAR_NORMALIZERS`, and `_Rebuild(keep_tail=3)` preserves
    the MASTER's pad when the terrain genuinely did change, so an override
    diverges only where the author sculpted.
- **A NEW record nested in a master's GRUP tree** (Translation.esp injects a
  map-marker REFR into a Nehrim cell) is converted normally and placed under
  the master parent's children group; if the parent record isn't already
  overridden, its converted bytes are pulled in VERBATIM as the anchor —
  the engine pairs a children GRUP with the record preceding it, so a group
  can never stand alone (same as xEdit's copy-as-override of a reference).
- **LOD: a plugin can ship LOD ASSETS for a worldspace it does not DEFINE.**
  The GOTY `DLCShiveringIsles.esp` is an 85-byte header-only stub — every SI
  record was merged into `Oblivion.esm` — yet its BSA carries all of SEWorld's
  LOD tiles, so the export has `meshes\landscape\lod\40728.*` and no
  `WRLD.txt` at all. Two consequences, both in `phase_lod`:
  - `shipped_lod_worldspaces` must resolve the decimal-FormID prefix to an
    EditorID through the MASTERS' `WRLD.txt` as well as the plugin's own, or
    it falls back to a raw hex id (`00009F18`) that no downstream EDID match
    can resolve and every stage reports "worldspace not found".
  - The generators read WRLD/CELL/LAND out of ONE esm, so point them at the
    master's converted output when the plugin doesn't define the worldspace.
    Records move; **assets and generated output stay in the plugin's own dir**
    — writing them into the master's tree makes the master ship content that
    belongs to the plugin.
- **An override plugin must not re-bake its masters' LOD.** Pass the masters'
  output dirs as `generate_lod(master_dirs=...)`: any model whose `_far.nif`
  a master already ships is dropped from both billboard generation and the
  LODGen input. Without it, SI would regenerate all of Oblivion's object LOD
  to gain the ~370 models it actually introduces. Note this filters by MODEL,
  not by worldspace — SEWorld's terrain tiles are still generated in full,
  because the master's LOD run never covered SEWorld at all.
- **Only list meshes that exist under the single `PathData` root.** LODGen
  resolves every path in its input against that one directory and aborts with
  `file not found` / exit 404 — baking NO tiles — if one resolves only in
  another plugin's tree. A cross-tree search may decide whether a mesh needs
  GENERATING; it must never widen what gets LISTED.
- Verify with: zero non-text diffs vs the master (only FULL/NAM1/DESC/CNAM/NNAM
  should differ), zero dangling refs, zero records at undefined master ids, and
  every override nested exactly as the master nests it.
