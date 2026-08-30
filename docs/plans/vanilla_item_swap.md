# Plan — "Vanilla Item Swap" (ingredients, food, clutter) + preview renderer

**Status: PLAN ONLY. Nothing here is implemented.**

Sibling of [vanilla_creature_swap_plan.md](vanilla_creature_swap.md). Same
idea — use Bethesda's asset where Skyrim already has the same thing — but items
are *static clutter*, so almost none of the creature plan's complexity applies:
no skeleton, no behavior graph, no ragdoll, no ARMA/race chain.

The two hard problems are different ones: **size/orientation mismatch** and
**deciding what actually matches**.

**Covers three categories**, all sharing one modal and one output ESP:

| Category | Table | Mechanism |
|---|---|---|
| Items (MISC / INGR / food) | [item_swap_table.md](../reference/item_swap_table.md) — measured here | override record or REFR (§1) |
| Creatures | [creature_race_equivalence.md](../reference/creature_race_equivalence.md) | override `NPC_.RNAM` |
| **Weather** | [weather_climate_conversion.md](../notes/weather_climate.md#vanilla-substitution-map-oblivion-wthr---vanilla-skyrim-wthr) — **already written and verified** | FormID redirect at every referrer (§4b) |

---

## 1. Two swap modes

The creature plan has exactly one mode. Items want two, because an item is both
an *appearance* and a *gameplay object*.

| | **Mode A — model swap** | **Mode B — full reference replace** |
|---|---|---|
| What changes | the record's `MODL` path only | the REFR's `NAME` points at the vanilla base record |
| Keeps | FormID, name, value, weight, effects, scripts | nothing from the Oblivion record |
| Gains | vanilla mesh + texture | vanilla mesh, texture, material, sound, havok, keywords |
| Risk | size/orientation mismatch (§2) | inherits vanilla gameplay data — usually a WIN, see below |
| Good for | anything; quest/scripted records | **food & drink**, clutter, gems |
| Bad for | — | quest items and scripted records (hard rule) |

**Mode B is the point for food and clutter, not a hazard.** An earlier draft of
this plan said Mode B "changes gameplay data" and should be blocked for most
things. That was wrong-headed: **Oblivion food is inert** — an apple is worth 1
gold and does nothing — while Skyrim food restores health/stamina, feeds cooking
recipes, and carries the keywords the economy and crafting systems key off.
Full-replacing `Apple → Red Apple` *gives Oblivion a working food system it
never had*. The same holds for gems (they feed smithing/quest economies) and
plain clutter (correct material, impact sound and havok mass arrive free with
the base record).

Mode B judgement calls, in order of confidence:

| Category | Mode B? | Why |
|---|---|---|
| Food & drink | **yes** | the upgrade above |
| Plain clutter (cutlery, bowls, brooms) | **yes** | a fork is a fork |
| Gems / pelts | **yes** | plug into vanilla economy |
| Alchemy ingredients | **optional** | Skyrim's effects differ, so known recipes change. Offer as a "play it like Skyrim" profile toggle, defaulting off for purists |
| Books | Mode A only | adopting Skyrim's *text edition* is a content change |
| **Quest items / scripted records** | **Mode A — hard rule** | a record named by a quest alias, `SCRI`, or VMAD must keep its own FormID or the quest breaks. Affects `Carrot of Seeing`, `Poisoned Apple`, `S'jirra's Famous Potato Bread` |
| Unique/named items | Mode A | custom value/enchantment is lost under B |

The full per-item recommendation is in
[item_swap_table.md](../reference/item_swap_table.md).

---

## 2. Size and orientation — the thing that makes items look wrong

This is the real failure mode, and it is measurable **before** shipping anything.

### 2.1 Both sides already have measured bounds

- **Vanilla**: every Skyrim record carries `OBND` (an AABB) in the dump.
- **Oblivion**: TES4 has *no* OBND — but the pipeline already measures every
  converted mesh into `export/<plugin>/mesh_bounds_cache.json`
  (`asset_convert.collision_extract.scan_mesh_data` →
  `tes5_import/mesh_bounds.py`). That is the Oblivion-side size, already on disk.

So the comparison is free. **Measured across the 54 name-matched items that have
both sizes:**

| Band | Count | Action |
|---|---|---|
| within ±25% | ~24 | swap as-is |
| 0.5–0.8× or 1.25–2× | ~20 | swap **with a scale factor** |
| >2× or <0.5× | ~10 | **reject — not the same object** |

Worked examples from the real data:

| Item | Oblivion | Skyrim | Ratio | Verdict |
|---|---|---|---|---|
| Skull | 12x16x18 | 10x16x16 | 0.89 | OK |
| Ectoplasm | 13x10x3 | 12x12x4 | 0.92 | OK |
| Taproot | 7x7x17 | 13x15x25 | 1.47 | rescale |
| Potato | 5x5x9 | 11x10x20 | 2.22 | reject/rescale |
| **Wolf Pelt** | 3x19x24 | 46x140x11 | **5.83** | **reject** |
| **Bear Pelt** | 14x23x5 | 70x34x18 | **3.04** | **reject** |
| **Bonemeal** | 29x27x14 | 12x12x5 | **0.41** | **reject** |

The pelts are the proof the gate is needed: Skyrim's are *draped hides*,
Oblivion's are *rolled bundles*. That is not a scale difference, it is a
different object, and no scale factor fixes it.

### 2.2 Orientation is detectable, and should reject rather than rotate

Look at the cutlery: Lockpick `2x15x2` → `20x0x0`; Fork `4x22x2` → `18x2x0`;
Spoon `4x20x2` → `18x2x0`. The **axes are permuted** — Oblivion lays cutlery
along Y, Skyrim along X.

An AABB detects this reliably (sort the three extents; if the *sorted* extents
match but the unsorted ones do not, the object is the same shape in a different
orientation). But an AABB **cannot recover the sign** of the rotation — 90° and
270° are indistinguishable from bounds alone.

**Recommendation: detect axis-permutation and reject with a distinct reason
("orientation differs"), rather than guessing a rotation.** A wrongly-rotated
fork embedded in a table is worse than an Oblivion fork. If a rotation is
wanted later it must be authored per-row in the table, not inferred.

### 2.3 Where the scale actually goes

Two options, to settle at implementation time:

- **On the placed REFR (`XSCL`)** — correct for Mode B, since the base record is
  Bethesda's and must not be edited.
- **Baked into the swap record** — simpler for Mode A, one edit covers every
  placement.

Mode A should prefer baking; Mode B must use `XSCL`.

---

## 3. What actually matches — curate, do not compute

**An earlier pass of this analysis was wrong and is worth recording as a
warning.** Matching by name found only 15/163 ingredients; the true number is
much higher. Two specific errors:

1. **Record types do not correspond.** Skyrim files food as **ALCH** (70 food
   items); Oblivion files the same food as **INGR**. Comparing INGR↔INGR hides
   Apple, Carrot, Potato, Leek, Bread, Cheese, Beef, Ham outright. Cross-type
   matching immediately recovered Carrot, Leek, Potato, Tomato, Venison and
   Sweetroll.
2. **Names are decorated.** "Fly Amanita **Cap**" vs "Fly Amanita", "Lavender
   **Sprig**" vs "Lavender", "**Wheat Grain**" vs "Wheat".

Even after fixing both, string matching plateaus around 57 — and it produces
false positives ("White Seed Pod" → "White Cap" is not the same plant). The
remaining real matches are **visual/botanical equivalences** that no string
algorithm will find: Oblivion's mushroom-cap family onto Skyrim's mushrooms,
Mutton → Leg of Goat, Cheese Wedge/Wheel, Bread Loaf → Bread.

**So the table is curated, exactly like the creature one**, with the same
evidence bar:

> A row is added only when the item is the same object **and** the size gate
> (§2) passes. Name similarity alone is not evidence — it produced both the
> undercount and the false positives above.

String matching is still useful as a *candidate generator* — it should populate
a review list, not the shipping table.

### 3.1 Where the value is (measured)

| Type | Oblivion recs | Placed refs | Notes |
|---|---|---|---|
| **MISC** | 407 | **28,762** | best ratio — gems, cutlery, bowls, tools; Skyrim's gems are far better meshes |
| **BOOK** | 887 | 8,791 | ~176 name matches — Bethesda reused the in-universe library wholesale |
| **INGR** | 173 | 8,534 | the herbal/fungal/food set |
| **ALCH** | 253 | 6,053 | 253 records share only **17** bottle meshes — few meshes, wide effect |
| STAT | 6,013 | **667,536** | out of scope here; enormous and mostly Cyrodiil-specific architecture |

**BOOK carries a content caveat:** pointing at a vanilla book adopts Skyrim's
*edition* of that text, which sometimes differs from Oblivion's. Books should be
**Mode A only** (mesh/cover), never Mode B.

---

## 4. Preview renderer

**Feasibility: confirmed by prototype. No new dependencies.**

PIL (already imported by `gui.py` for the banner via `ImageTk`), numpy and pyffi
are all present. A software renderer producing a small textured thumbnail was
built and measured:

- geometry + UVs + diffuse texture path read straight out of the NIF
- flat-shaded triangles, painter's-algorithm depth sort, diffuse sampled at each
  triangle's UV centroid
- **0.08 s per mesh at 200 px; 25/25 converted clutter meshes rendered with zero
  failures**

Centroid UV sampling is not a correct rasteriser (no perspective-correct
interpolation), and it does not need to be: at thumbnail size the question is
"is this the same object, roughly this shape and colour", which it answers.

### 4.1 ⚠ Known gap: the vanilla side does not render yet

The prototype renders **converted** meshes reliably. Reading vanilla meshes
straight out of the Skyrim BSAs through `skyrim_assets.get_asset_bytes()` →
`sse_nif.read_nif()` **failed** in testing (`array too long (2147483648)`, and
`unpack requires a buffer of 4 bytes`) — including on the dog skeleton, which the
creature pipeline reads successfully elsewhere. The returned bytes are a valid
NIF header, so this is a parse-path problem, not a fetch problem, and it was not
resolved by loading `pyffi_monkey_patch` first.

**This must be solved before a side-by-side preview is promised**, since half the
comparison is the vanilla half. Options, cheapest first:
1. Find how the creature pipeline reads these successfully and reuse that exact
   call path (it demonstrably works — `references/Skyrim Meshes` is populated).
2. Render the vanilla side from `references/Skyrim Meshes` / the extracted
   `export/skyrim_assets/` cache instead of parsing BSA bytes in-memory.
3. Fall back to a static thumbnail cache shipped alongside the table.

Until then the preview can still show the **Oblivion side + the measured size
delta**, which is the part that actually drives the accept/reject decision.

### 4.2 UI

Extend the swap modal (same card/`CLR[...]` styling as
`_open_create_lod_panel`). Selecting a row shows:

```
┌─ Vanilla Item Swap ───────────────────────────────────────────────┐
│ Plugins        │ Items to swap          │  Preview                │
│ ☑ Oblivion.esm │ ── Ingredients ──────  │   ┌────────┐ ┌────────┐ │
│ ☑ Nehrim.esm   │  ☑ Nirnroot     x2.26  │   │Oblivion│ │ Skyrim │ │
│                │  ☑ Daedra Heart  OK    │   │  [img] │ │ [img]  │ │
│                │  ☒ Wolf Pelt    x5.83  │   └────────┘ └────────┘ │
│                │      ↳ rejected: size  │   8x23x6  →  26x26x52   │
│                │  ☑ Skull         OK    │   scale x2.26 applied   │
│                │ ── Clutter ──────────  │                         │
│                │  ☑ Fork    orientation │   Mode: (•) model swap   │
│                │      ↳ rejected: axes  │         ( ) full replace │
├────────────────┴────────────────────────┴─────────────────────────┤
│                                    [ Create ESP ]  [ Cancel ]      │
└───────────────────────────────────────────────────────────────────┘
```

- Rejected rows render **greyed with the reason** ("size x5.83", "orientation")
  rather than being hidden — the user should see what was considered.
- Thumbnails render lazily on selection (0.08 s) and cache to
  `temp/preview_cache/`; a full-table pre-render is ~50 items ≈ 4 s.
- The Mode A/B radio is per-row and disabled (forced to A) for INGR/ALCH/BOOK
  and anything quest- or script-referenced (§1).

---

## 4b. Weather — a third swap category, already mapped

[weather_climate_conversion.md](../notes/weather_climate.md#vanilla-substitution-map-oblivion-wthr---vanilla-skyrim-wthr)
already contains a **complete, verified substitution map** for exactly this
feature: 18 Oblivion weathers mapped to vanilla Skyrim WTHR records, derived from
the authored discriminators on both sides (`DATA.Classification`, `WindSpeed`,
`ThunderFrequency`, `SunGlare`, `FNAM` fog distances) read from the real dumps.
**Do not re-derive it** — read that section and drive the option from it.

It belongs in this plan because it is the same feature wearing a different hat:
one more category in the same modal, emitted into the same override ESP.

### What is different about weather

| | Creatures / Items | **Weather** |
|---|---|---|
| Mechanism | override the record, repoint one field | **redirect every referrer; do not emit the record at all** |
| Referrers | placed REFR/ACHR | `CLMT.WLST`, `REGN.RDWT`, script `Weather` properties, and the WTHR itself |
| Size gate | OBND ratio (§2) | not applicable — weather has no geometry |
| Extra win | better art | **skips the whole NAM0 luminance-normalisation and IMGS-minting path** |

Four consequences worth carrying into the implementation, all already stated in
the weather doc:

1. **It is a FormID redirect, not a record override.** A vanilla weather is
   *referenced*, never copied, so the substituted WTHR is simply not emitted and
   all four referrer sites are repointed.
2. **Script properties bind by EditorID**
   (`script_convert/converter.py`), so a substituted weather must resolve to the
   *vanilla* EditorID or the property silently fails to bind. This is the one
   failure mode that produces no error — it just stops working.
3. **The four IMGS companions must not be minted** for a substituted weather.
   This costs no FormID drift because companion ids are
   [hashed from the source weather](../../CLAUDE.md#formid-drift), so skipping them
   leaves every other id untouched.
4. **New-game-only setting.** Toggling it changes which records exist, exactly
   like enabling weather conversion at all.

### Deliberate non-matches (already decided — do not "improve" these)

The weather doc rejects, with reasons: all the **Oblivion-realm skies**
(`OblivionStormTamriel`, `OblivionStormOblivion`, `OblivionElectrical`,
`OblivionSigil`, …) — the red/black Deadlands sky is the most recognisable
weather in the game and vanilla has nothing near it, *and* `OblivionStormTamriel`
is what the gate scripts `ForceActive`, so substituting it changes what the
scripted sequence puts over the sky. Also rejected: quest/set-piece skies
(`CamoranWeather`, `MS14Sky`, `SE09SummoningWeather`) and `DefaultWeather`.

`SkyrimStormSnow` `0x000C8221` is listed there as a *reference only* — Oblivion
authors no thundering snow, so nothing maps to it. It must not become a
substitution target for the plain `Snow` rows.

### UI

Weather is a category in the same modal, gated the same way — but with **no size
column** (nothing to measure) and **no Mode A/B choice** (redirect is the only
mechanism). The preview is a swatch of the weather's sky/fog colours rather than
a mesh render, which the NAM0 colour table supplies directly.

## 5. Work order

0. **Weather first — it is the cheapest.** Its map already exists and is
   verified, it needs no renderer, no size gate and no new table work: wire the
   redirect at the four referrer sites (§4b). Highest ratio of finished-design to
   remaining-work in the whole plan.
1. `tools/item_swap_report.py` — cross-type candidate generator + size/orientation
   gate; prints accept / rescale / reject-with-reason. Argument-driven, per the
   tools rule.
2. Curate the table from its output (MISC first — best ratio, lowest risk).
3. Fix the vanilla-side NIF read (§4.1), then wire the two-up preview.
4. `tools/make_vanilla_item_esp.py --dry-run` → the ESP writer.
5. Build `--import-only` and have the user verify in-game before any doc claims
   it works.

## 6. Open questions

1. **Scale on the record or the REFR?** Recommend baking for Mode A, `XSCL` for
   Mode B (§2.3).
2. **Should Mode B ever apply to ingredients?** Recommend no — effects differ.
3. **Books: mesh-only?** Recommend yes; adopting Skyrim's text edition is a
   content change, not a graphics option.
4. **Do we want STAT at all later?** 667k placements is the biggest visual win in
   the project, but Cyrodiil architecture has few honest Skyrim counterparts —
   probably a separate, much more selective effort.
