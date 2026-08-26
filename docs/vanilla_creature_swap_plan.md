# Plan — "Vanilla Creature Swap" ESP generator + GUI

**Status: PLAN ONLY. Nothing here is implemented.**

Goal: a top-bar GUI action that builds a **separate override ESP** which makes
converted creatures use their vanilla Skyrim counterparts — vanilla mesh,
skeleton, behavior graph, ragdoll and sounds — while keeping the converted
record's own FormID and all of its gameplay attributes.

Companion reference: [creature_race_equivalence.md](creature_race_equivalence.md)
(the match table, the tiers, and what was deliberately rejected).

---

## 1. The three constraints, and what they force

The prompt sets three hard constraints. Each one rules out an otherwise
attractive design, so they are worth stating as engineering consequences.

### 1.1 "Shouldn't replace the FormID"

The ESP must **override** each converted `NPC_` record at its existing FormID,
not create new actors. So the ESP masters the converted plugin and re-emits the
same record with a different `RNAM` (race). Every placed `ACHR` reference, quest
alias, script property and leveled-list entry keeps working untouched, because
none of them are edited at all.

### 1.2 "Replace the mesh and behavior, leave applicable attributes the same"

An actor's visual/animation identity lives **entirely in its RACE**, not in the
actor record. That is the whole reason this design is cheap:

```
NPC_.RNAM ──► RACE ──► ANAM  skeleton.nif
                  ├──► NAM3  behavior project .hkx
                  ├──► WNAM  skin ARMO ──► MODL ARMA ──► MOD2  body mesh
                  ├──► GNAM  body part data (ragdoll/dismember)
                  ├──► ATKD/ATKE attack data + events
                  └──► MTNM  movement-type names
```

**Changing one field — `NPC_.RNAM` — swaps mesh, skeleton, behavior, ragdoll,
footsteps and attack animations in a single stroke.** Everything the prompt calls
an "applicable attribute" lives on the `NPC_` record itself and is simply copied
through unchanged: `ACBS` (level, health offset, flags), `DATA`/stats, `CNTO`
inventory, `SPLO` spells, `FACT` factions, `PKID` packages, `VMAD` scripts,
`AIDT`, `CSTY`, `VTCK` voice.

**Corollary — do NOT mint our own ARMA pointing at a vanilla mesh path.** A
vanilla body NIF is skinned to *vanilla bone names* (`NPC Spine [Spn0]`). Our
generated skeleton uses Oblivion names (`Bip01 Spine`). Pointing `MOD2` at a
vanilla mesh while the race still names a converted skeleton yields unresolved
bones — T-pose or crash. The vanilla chain must be taken **whole**, which is why
the swap references the real vanilla RACE FormID and therefore needs the real
master. (This is the point that reversed an earlier draft of this plan.)

### 1.3 "Only EXACT matches" + "based on race"

Only the `exact` tier ships. `near` rows stay in the doc as analysis and are not
offered in the UI — a sabre cat is not a mountain lion.

"Based on race" cannot mean a TES4 race field: **CREA records have no race**
(verified — race is a TES5 concept). It means the *generated* race — the unit
`creature_races.py` mints one RACE for. Keying the swap there gives exactly the
requested property: **one decision covers every FormID sharing that race** (e.g.
one tick swaps all 51 Oblivion skeleton records).

The identity of that race is the **skeleton path**, refined by body set only
where one skeleton serves several creatures — see §2, which corrects an earlier
draft that used the folder. That is what keeps ram ≠ sheep (`ramhornl.nif`) and
wolf ≠ dog ≠ fox on the shared `Creatures\Dog` skeleton.

---

## 2. Rebuilding the match table on race identity

`vanilla_creature_swap.py` is currently keyed on folder + EditorID substrings.
That is the thing the prompt calls "a little broad", and the audit proves it:

| Folder | Actually holds | Folder rule would do |
|---|---|---|
| `sheep` (Nehrim) | sheep, rams, **12 mammoth races** | turn mammoths into goats |
| `dog` | dog, wolf, **fox**, skeletal hounds | one canine for all |
| `rat` (Nehrim) | rats + a **rabbit** | rabbit → skeever |
| `hillgiant` | 13 **giant** races | (missed entirely) |

**Replace both lookup tables with one keyed on the SKELETON PATH.**

The first draft of this plan said "key on `(folder, body set)`". That is still
too weak, and a review caught real errors it would have caused:

- `body.nif` is the body set for **20 different Morroblivion folders** (alit,
  cliffracer, draugr, dreugh, frostgiant, horker, kagouti, riekling, …);
  `mesh.nif` for 11; `mesh2.nif` for the four *different* Sixth House creatures.
  A generic body name carries no identity at all.
- Conversely the **skeleton path** is unique per creature and is exactly what the
  engine binds animation to:
  `Creatures\Horker\skeleton.nif`, `Creatures\Aa_Blood\Draugr\skeleton.nif`,
  `Morroblivion\Creatures\SixthHouse\AshGhoul\skeleton.nif`.

```python
# key: normalised (lowercased, forward-slashed) Model.MODL skeleton path,
# optionally + body set where ONE skeleton legitimately serves two creatures.
BY_SKELETON = {
    'creatures/horker/skeleton.nif':                 HORKER,
    'creatures/aa_blood/iceminion/skeleton.nif':     RIEKLING,   # needs DLC2
    'creatures/dog/skeleton.nif': {                              # split by body set
        'wolfbody.nif|wolfeyes.nif|wolfhead.nif':    WOLF,
        'dogbody.nif|dogeyes01.nif|doghead.nif':     DOG,
        'fox.nif':                                   FOX,
        'houndface.nif|skeletal hound.nif':          None,       # no equivalent
    },
}
```

**Case-insensitivity is mandatory.** The exports contain both `Skellie.NIF` and
`skellie.nif`, and both `Slaughterfish.NIF` and `slaughterfish.nif`, for the same
creature. Comparing raw strings splits one creature into two races.

The body set stays as a **secondary** discriminator only where one skeleton
genuinely serves several creatures (`Creatures\Dog`, `Creatures\Sheep`,
`Creatures\Bear`, `Creatures\Deer`) — that is what keeps ram ≠ sheep and
wolf ≠ dog ≠ fox.

### 2.1 Variants: colours, tack, armour, and when they matter

One Oblivion creature ships many body-part permutations. Keying on the *exact*
part list explodes them into meaningless races: **15 body sets for 5 horse
colours**, 4 for one skeleton, 14 for Nehrim's giants. Keying too loosely merges
creatures that must stay apart. The split is:

| Kind of variation | Example | Same race? | Why |
|---|---|---|---|
| **Colour / texture** | bay / black / grey / paint / chestnut horse | **yes** | Vanilla does exactly this: 5 colour skins (`SkinHorse`, `SkinHorseBlackHide`, `SkinHorseGreyHide`, `SkinHorsePalominoHide`, `SkinHorseBlacknWhiteHide`) all on **one** `HorseRace`. Colour lives in the skin ARMO, not the race. |
| **Tack / equipment** | `saddle.nif`, `bridle.nif`, `bridle_db.nif` | **yes** | Vanilla ships `HorseSaddleAA` / `HorseHarnessAA` as separate ARMA on the same race. |
| **Armour / clothing** | Nehrim giant `giantarmorsteel`, skeleton `bhelmet`/`skdbcuirass` | **yes** | Equipment on an identical body. Oblivion's 51 skeletons are **one** creature in 4 armour configs. |
| **Hair / beard style** | `giantbeardred` vs `giantbeardblond`, `mane` vs `manelong` vs `maneroman` | **yes** | Cosmetic only. |
| **Different base body** | `wolfbody` vs `dogbody` vs `fox`, `sheep` vs `sheep04` | **NO** | A different animal or a different breed mesh. |
| **Structural feature** | unicorn `Horn.NIF`, buck `antlar8point`, mammoth tusks | **NO** | Changes what the creature *is*, not how it is decorated. (Ram horns are NOT in this class — see below.) |

**Rule: group by BASE BODY MESH; ignore attachments — with an explicit
structural-parts allowlist that is never ignored.**

```python
# Cosmetic: strip before comparing (colour is in the texture, not the mesh name)
COSMETIC = ('saddle', 'bridle', 'packsaddle', 'cargo', 'harness',
            'mane', 'tail', 'eye', 'hair', 'beard', 'moustache',
            'helmet', 'helm', 'armor', 'armour', 'greaves', 'cuirass',
            'boots', 'pauldron', 'gauntlet', 'forearm', 'upperarm',
            'skirt', 'sporran', 'belt', 'amulet', 'robe', 'fleece')

# STRUCTURAL: never strip. Presence changes the creature's identity.
STRUCTURAL = ('horn.nif',    # UNICORN vs grey horse (exact filename, see below)
              'antlar',      # buck vs doe
              'tusk',        # mammoth variants
              'jelly',       # bull netch vs betty netch
              'wing', 'fluegel')
# NOT structural: 'ramhorn*'. See the ram note below — it is decoration, and
# it is shared with goblins, so a bare 'horn' substring is actively wrong.
```

Measured effect of the rule (base-body grouping vs raw part sets):

| Group | Raw body sets | Base bodies | Result |
|---|---|---|---|
| Oblivion `skeleton` | 4 | **1** | 51 records, one race, 4 armour configs collapsed |
| Oblivion `sheep` | 2 | **1** | Oblivion has no ram creature — all 16 are FULL="Sheep" |
| Oblivion `horse` | 15 | **5** | one per colour — matching vanilla's 5 skins |
| Nehrim `hillgiant` | 14 | **~3** | beard/armour collapsed, dark-skin variant kept |
| Nehrim `dog` | 3 | **3** | wolf / dog / fox all preserved |

#### Two traps this rule exists to avoid

1. **`DAHircineUnicorn` is a grey horse plus `Horn.NIF`.** Strip "horn" as
   cosmetic and Hircine's unicorn silently becomes an ordinary horse. `horn` is
   on the structural list for this reason (and the ram, below).
2. **`horn` must be matched as the exact filename `horn.nif`, NOT as a
   substring.** A substring match also catches `ramhornl/r.nif`, and those are
   not what they look like — see the ram correction below.

#### ⚠ Correction: THE RAM IS NOT A CREATURE (2026-08-18)

An earlier draft made "ram vs sheep" the headline example of a structural
variant. **That was wrong**, and the export proves it:

- Every Oblivion ram record — `CreatureSheepRam`, `SakeepaSheepRam`,
  `UurasSheepRam`, `WeynonPriorySheepRam` — has `FULL` = **"Sheep"**. Not "Ram".
  Oblivion has no ram creature; UESP has no ram page for the same reason.
- `ramhornl.nif` / `ramhornr.nif` are **decoration**, and they are not even
  sheep-specific: they are worn by **goblins** (`CGGoblinThiefShaman`, and the
  `GoblinTribeLeaderA–G` war chiefs) as headgear.

So ram horns behave exactly like a helmet, and Oblivion's 16 sheep are **one**
creature. A `'horn'` substring rule would have split the sheep pointlessly *and*
split the goblin war chiefs off from ordinary goblins — which is why the
structural list now matches the exact filename `horn.nif` (the unicorn) instead.

**Nehrim is the genuine exception.** It authored real rams with their own names:
`01SchafBock` (FULL "Schafbock" = ram) and `01SchafSchwarzBock` (FULL "Schwarzer
Schafbock" = black ram), plus `28TollwuetigerWidder` ("Widder" = ram). There the
distinction is authored in the NAME, not inferred from a mesh part — so **use the
FULL name as the tiebreak when a plugin actually names its variants**, rather
than reading meaning into part files.

#### The STRICT rule: swap only the variant that actually matches

When a creature has several variants and vanilla has a counterpart for only
some, **swap only those; leave the rest generated.** Do not flatten the
unmatched variants onto the nearest vanilla skin.

Worked example — Nehrim's chickens. It ships 11 meshes on one skeleton: hens
(`standardchicken`, `…1`, `…2`), roosters (`rooster`, `…1`, `…2`,
`roosterwhite`) and chicks (`littlechicken`, `…1`, `…2`), and the FULL names
distinguish them (`Huhn` / `Hahn` / `Küken`). Vanilla Skyrim has exactly **one**
chicken skin (`SkinChicken` `0x000A919C`) — no rooster, no chick. So:

| Variant | Records | Action |
|---|---|---|
| hen (`standardchicken*`) | 6 | **swap** → `ChickenRace` |
| rooster (`rooster*`) | 5 | keep generated |
| chick (`littlechicken*`) | 4 | keep generated |

**Measured cost of the strict rule across both plugins: 9 records** (Nehrim's
roosters and chicks). Nothing else is affected, because everywhere else vanilla
has either all the variants or none:

| Group | Swapped | Left generated |
|---|---|---|
| bear (Obl 14 / Neh 10) | all — vanilla has brown + black | 0 |
| deer (Obl 9 / Neh 7) | all — buck→`ElkRace`, doe→`DeerRace` | 0 |
| dog (Obl 31 / Neh 28) | dog, wolf, fox | 10 SI hounds (already excluded) |
| chicken (Neh) | 6 hens | **9 roosters + chicks** |
| sheep (Neh) | 13 mammoths | 12 sheep/ram/black sheep (never matched) |
| rat | 1 rabbit → `HareRace` | 36 rats (`SkeeverRace` is `near`, excluded) |

Two consequences worth stating:

1. **There is no "lossy but exact" category.** A row either has a true vanilla
   counterpart or it does not, so no row needs to default OFF and nothing is
   silently degraded.
2. **Mixed-asset flocks are accepted.** A vanilla hen will stand beside a
   converted rooster with different animation and footstep quality. They are
   different birds, so this reads as intentional; flattening them into identical
   hens would not.

#### How the swap then handles colour

Since colour is not part of race identity, a swapped horse must not lose it.
Two options, to decide at implementation time:

- **A (preferred): map colour → vanilla skin ARMO.** All five Oblivion coats have
  a plausible vanilla counterpart (bay→`SkinHorse`, black→`SkinHorseBlackHide`,
  grey/white→`SkinHorseGreyHide`, chestnut→`SkinHorsePalominoHide`,
  paint→`SkinHorseBlacknWhiteHide`). The `NPC_` override carries `WNAM`, so the
  actor can name its own skin while sharing `HorseRace`. **Verify that
  `NPC_.WNAM` overrides the race skin before relying on this.**
**Option B ("one colour for everything") is rejected** — a stable of identical
horses is a visible regression, and the user has ruled it out. Colour must be
carried whenever vanilla has a counterpart skin.

Where vanilla has **no** counterpart (Nehrim's black sheep vs the goat, which has
no black variant; the 11 chicken meshes vs one vanilla hen), the row is **lossy**
and must be flagged as such in the UI rather than silently flattened — see the
chicken note above. A lossy row should default to OFF.

Bears are the easy case: vanilla ships brown, black and snow, so all of
Oblivion's and Nehrim's bear colours map one-to-one.

### 2.1 Evidence bar for adding a row

The review found rows that were wrong on **lore**, not just on mesh (Sixth House
ash creatures ≠ Skyrim Ash Spawn; bonewalker ≠ skeleton; Morrowind lich ≠ dragon
priest). So a row may be added only when **both** hold:

1. The skeleton path shows it is one distinct creature, and
2. UESP confirms the creature identity —
   `python tools/misc/uesp_lookup.py --page "Morrowind:<name>"`.

Folder-name resemblance is not evidence. When in doubt, leave it unfilled — an
empty cell is a valid answer and the whole table is opt-in.

## 3. Assets: which masters are actually required

Three tiers, and the distinction matters for the UI divider:

| Tier | Source | FormIDs | Master needed | Availability |
|---|---|---|---|---|
| A | Skyrim.esm | **verified** against the dump | Skyrim.esm | always |
| B | Dawnguard / Dragonborn | **NOT verified — no dump** | yes | usually (AE/legendary) |
| C | **Creation Club** (`ccbgssse*.esl/.esm`) | **NOT verified — no dump** | yes | **only if the user owns it** |
| D | **Beyond Skyrim** (`BSAssets.esm`, `BSHeartland.esm`) | **NOT verified — no dump** | yes | **only if installed** |
| E | no equivalent in any source | — | — | stays generated |

**Tier C gating.** CC plugins have fixed filenames, so availability is a plain
`os.path.isfile(Data/<name>)` check. A CC row whose file is absent is **greyed
out in the UI, cannot be ticked, and is never written into the ESP** — it must
not become a dangling master. The same treatment applies to tier B when a user
lacks the DLC. Files that matter here:

| File | Gives |
|---|---|
| `ccbgssse040-advobgobs.esl` | Goblin — covers **95 Oblivion records** |
| `ccbgssse003-zombies.esl` | true Zombie — ~120 records across Oblivion+Nehrim |
| `ccbgssse025-advdsgs.esm` | Golden Saint, Dark Seducer, **Elytra (35)** — the only source for the SI cast |
| `ccbgssse036-petbwolf.esl` | Bone Wolf — Morroblivion's undead wolves |
| `ccbgssse067-daedinv.esm` | Ayleid Lich — better than the rejected `DragonPriestRace` |
| `BSAssets.esm` | **Goblin (122), minotaur (38), scamp (34), ogre (27), imp (22)** |
| `BSHeartland.esm` | **Daedroth (20)**, Cyrodiil river troll, CYR skeleton/wisp |

Tiers C and D are worth supporting precisely because they reach creatures **no
other source can**: goblins, elytra, minotaurs, scamps, ogres and the Shivering
Isles daedra are among the largest "no equivalent" groups in the whole table.

**Beyond Skyrim outranks the others where it overlaps.** BS: Cyrodiil is a
deliberate recreation of *Cyrodiil's* creatures in the Skyrim engine — the same
animals our plugins ship — so for goblin/minotaur/imp/scamp/ogre/daedroth it is
the truest match available. Preference order when several tiers offer one
creature: **Beyond Skyrim → Skyrim.esm → CC/DLC.** The exception is creatures
vanilla genuinely has (skeleton, wisp): `SkeletonRace`/`WispRace` are exact,
verified and dependency-free, so they stay the default and the `CYR*` equivalents
are offered only as alternatives.

Tier B is now only **Riekling** (Morroblivion `iceminion`/`iceraider`) — the
Netch, Ash Spawn and Bristleback rows were removed as wrong; see the corrections
table in the equivalence doc. Two netch rows *could* be added if Dragonborn's
bull and betty races are confirmed separately. **No Dawnguard/Dragonborn FormID
may be written from memory.**

> **Implementation step 0 (blocking for tier B only):** read the RACE/ARMO
> FormIDs out of the user's installed `Dawnguard.esm` / `Dragonborn.esm` with the
> existing export reader, cache them under `export/skyrim_assets/`, and verify
> the same way §5 verifies the Skyrim.esm rows. Tier A ships without waiting on
> this.

**Declaring the master is required, not cosmetic.** A swap that references
`Dragonborn.esm`'s race by FormID is a hard dependency: without the master the
reference dangles and the actor breaks. Declaring it turns a silent T-pose into a
load-order error the user sees immediately. The writer already supports arbitrary
masters (`pack_tes4_header`).

---

## 4. The generated ESP

**Name:** `<Plugin> - Vanilla Creatures.esp`, written next to the converted
output. A separate file (not baked into the main ESM) so the user can toggle the
whole feature by ticking one plugin, and so re-running the converter never has to
redo it.

**Masters:** `Skyrim.esm`, the converted plugin, plus `Dragonborn.esm` /
`Dawnguard.esm` only if a selected row needs one.

**Contents:** exactly one `NPC_` override per affected actor —

```
NPC_ (same FormID as converted record, master-indexed to the converted plugin)
  ...every subrecord copied verbatim from the converted NPC_...
  RNAM = vanilla race FormID          ← the only changed field
```

Nothing else is emitted. No new RACE, ARMA, ARMO, MOVT, IDLE or BPTD — the whole
point is to reference Bethesda's existing chain.

**Bounded scope, measured:** at the exact tier that is ~217 records for Oblivion
and ~227 for Nehrim — small, fast, and reviewable in xEdit.

### 4.1 Known behavioural consequences (must be listed in the UI, not hidden)

Taking the vanilla chain whole means taking its *record-level* data too, and a
few converted attributes stop applying:

- **Scale/size.** A vanilla race has its own height. An Oblivion creature scaled
  via `NAM6`/`MODB` may change apparent size. *Mitigation: the `NPC_` override
  can still carry its own scale — verify during implementation.*
- **Attack data.** `ATKD`/`ATKE` live on the RACE, so the creature gains the
  vanilla attack set. Usually desirable (it is what makes combat animate), but a
  creature whose Oblivion attacks were unusual will fight differently.
- **Movement speed.** `MOVT` records are race-linked, so the carefully derived
  Oblivion speed formula (`_movt_sped`) no longer applies. The creature moves at
  vanilla speed.
- **Body part data.** Dismemberment/ragdoll targeting becomes vanilla's.

None of these are bugs to fix — they are the *definition* of using the vanilla
creature. They belong in the modal's description text so the trade is explicit.

---

## 5. Verification (before any build)

Reuse the method that already caught ~30 wrong FormIDs in the current table —
**never** trust a naming pattern:

1. Every race FormID resolves in `references/Skyrim.esm/RACE.txt` and its
   EditorID matches the table's claim.
2. Skip the skin ARMO entirely — this design no longer writes one. (The current
   table's `skin` column becomes dead weight and should be dropped.)
3. Round-trip the emitted ESP: re-read it and assert every `NPC_` FormID is
   unchanged from the source and only `RNAM` differs.
4. `tools/creature/creature_swap_report.py --by-race` shows per-race counts and which
   races are left generated.

Traps already confirmed the hard way and worth re-checking: `BearBlackRace` uses
`SkinBearCave`; `FoxRace` shares `SkinWolf`; `DeerRace`/`ElkRace` have crossed
skin names; `TrollRace`/`TrollFrostRace` share one skin.

---

## 6. GUI

### 6.1 Top bar

Add to the existing **Tools** menu (`gui.py` ~line 1080), matching how
`Convert to Master` is wired:

```
Tools ▸ Create Vanilla Creature ESP…
```

Disabled with a tooltip when nothing is converted yet, exactly like the LOD and
master entries.

### 6.2 The modal

Follow `_open_create_lod_panel` (gui.py ~2519) — it is already the requested
two-column shape, so this is a variation on an existing widget rather than a new
one. Same dark `CLR[...]` palette, same centred `tk.Frame` card, same
`Apply`/`Cancel` footer.

```
┌─ Create Vanilla Creature ESP ─────────────────────────────────────┐
│ Uses Bethesda's creature for creatures Skyrim already has.        │
│ FormIDs, stats, inventory, factions and scripts are unchanged —   │
│ only the mesh, skeleton, behavior and ragdoll are swapped.        │
│ Vanilla speed, attacks and dismemberment come with it.           │
├──────────────────────────┬────────────────────────────────────────┤
│ Plugins          [All][None] │ Creatures to swap     [All][None]  │
│ ☑ Oblivion.esm           │ ── Skyrim.esm ───────────────────────  │
│ ☑ Nehrim.esm             │  ☑ Skeleton      → SkeletonRace    51  │
│ ☐ Morrowind_ob.esm       │  ☑ Horse         → HorseRace       55  │
│                          │  ☑ Dog           → DogRace         24  │
│                          │  ☑ Wolf          → WolfRace         7  │
│                          │  ☑ Mammoth       → MammothRace     12  │
│                          │ ── Dragonborn.esm ───────────────────  │
│                          │  ☐ Riekling      → DLC2RieklingRace 5  │
│                          │  ☐ Boar          → DLC2BoarRace    13  │
│                          │ ── Beyond Skyrim ────────────────────  │
│                          │  ☐ Goblin        → BSKGoblinRace  122  │
│                          │  ☐ Minotaur      → CYRMinotaur     38  │
│                          │  ☐ Scamp         → BSKScampRace    34  │
│                          │ ── Creation Club ────────────────────  │
│                          │  ☐ Elytra        → CC025 Elytra    35  │
│                          │  ☒ Zombie  (requires ccbgssse003)   60  │ ← greyed
│                          │ ── No equivalent (always generated) ──  │
│                          │    Grummite, Gnarl, Clannfear …        │
├──────────────────────────┴────────────────────────────────────────┤
│ ⚠ Requires Dragonborn.esm, BSAssets.esm — added as masters.       │
│                                    [ Create ESP ]  [ Cancel ]     │
└───────────────────────────────────────────────────────────────────┘
```

Behaviour:

- **Left column** — converted plugins, checkboxes, `All`/`None`. Ticking a
  plugin repopulates the right column with *that* plugin's races.
- **Right column** — one row per swappable **race** (not per FormID), showing the
  creature, its vanilla target, and the count of records it covers, so the user
  sees that one tick moves 51 skeletons. `All`/`None` per the existing pattern.
- **Dividers** — group by required master (`Skyrim.esm`, `Dawnguard.esm`,
  `Dragonborn.esm`), as suggested. A trailing non-interactive "No equivalent"
  group makes the unfilled entries visible rather than merely absent.
- **DLC and Creation Club groups start unticked** and show the warning line when
  ticked. A row whose plugin file is **not present in `Data/`** is rendered
  greyed with "requires <filename>", cannot be ticked, and is excluded from the
  build — never write a master the user does not have. Detection is a plain
  `os.path.isfile` on the fixed filename (see §3). This covers Creation Club
  (`ccbgssse*`) and Beyond Skyrim (`BSAssets.esm`, `BSHeartland.esm`) alike.
- **Where two tiers offer the same creature**, show the preferred one (Beyond
  Skyrim first for Cyrodiil natives, Skyrim.esm for creatures vanilla has) and
  list the other as a sub-choice rather than a second competing row.
- Selection persists in the GUI settings dict alongside `master_plugins` /
  `lod_plugins`.

### 6.3 Backend entry point

New `tools/make_vanilla_creature_esp.py`, argument-driven per the repo's tools
rule, so it is usable headless and by the GUI:

```bash
python tools/make_vanilla_creature_esp.py -f Oblivion.esm \
    [--races skeleton,horse,dog] [--all-exact] [--dry-run]
```

`--dry-run` prints the record count and master list without writing — the GUI
calls it to populate the modal, so the panel never shows a row the backend would
not actually emit.

---

## 7. Work order

1. Read Dawnguard/Dragonborn RACE FormIDs → cache. *(tier B only; non-blocking)*
2. Rebuild the table keyed on `(folder, body set)`; drop the `skin` column;
   add the `--emit-table` helper. Verify per §5.
3. `tools/make_vanilla_creature_esp.py` with `--dry-run`.
4. Round-trip test: FormIDs unchanged, only `RNAM` differs.
5. GUI modal + Tools entry.
6. Build `--import-only` for Oblivion, Nehrim and Morroblivion; user tests
   in-game before any doc claims it works.

## 8. Open questions for the user

1. **Scale.** Should a swapped creature keep its Oblivion scale (a "giant rat"
   stays giant) or take vanilla proportions? Recommend keeping scale.
2. **One ESP per plugin, or one combined?** Recommend per-plugin — it keeps
   masters minimal and lets the user disable one conversion's swaps.
3. **Morroblivion tier B.** Worth pulling in Dragonborn as a master for the
   Riekling (the one row that survived review), or keep the ESP Skyrim.esm-only?
   Recommend keeping it Skyrim.esm-only for a first pass — one creature does not
   justify a DLC dependency, and the netch rows need verification first.
