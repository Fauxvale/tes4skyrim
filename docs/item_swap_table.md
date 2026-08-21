# Oblivion → Skyrim Item Swap Table (MISC + Ingredients/Food)

Companion to [vanilla_item_swap_plan.md](vanilla_item_swap_plan.md). Scope for
now: **Oblivion.esm `MISC` and `INGR`** (Oblivion files food as INGR; Skyrim
files it as ALCH, so many rows cross record types).

**Measured 2026-08-18.** Sizes are real: the vanilla side is each record's
`OBND` from `references/Skyrim.esm`, the Oblivion side is the pipeline's own
measurement of the converted mesh (`export/Oblivion.esm/mesh_bounds_cache.json`).
`ratio` = vanilla longest-axis ÷ Oblivion longest-axis.

435 unique Oblivion INGR+MISC names; **167 have a vanilla counterpart**.

---

## Swap modes — and why gameplay change is a FEATURE here

The creature swap has one mode. Items have two, and for food the *gameplay*
change is the point:

| Mode | Changes | Use for |
|---|---|---|
| **A — model swap** | `MODL` path only; keeps FormID, name, value, effects | anything where the Oblivion item's behaviour should survive |
| **B — full replace** | the REFR points at the **vanilla base record** | **food and drink**, plain clutter |

**Mode B on food is an upgrade, not a regression.** Oblivion food is inert —
an apple is worth 1 gold and does nothing. Skyrim food restores health/stamina,
participates in cooking recipes, and carries the `VendorItemFood` /
`ClothingRich`-style keywords the economy and crafting systems key off. Full-
replacing `Apple → Red Apple` gives Oblivion a working food system it never had.

The same argument holds for gems (vanilla gems feed the smithing/quest
economy), and for ordinary clutter (correct material, impact sound and havok
mass come free with the base record).

**Where Mode B still needs care** — these are judgement calls, not blockers:
- **Alchemy ingredients**: Skyrim's effects differ from Oblivion's, so recipes
  the player knows change. Defensible either way; recommend Mode B **on** for a
  "play it like Skyrim" profile and **off** for a purist one.
- **Quest items**: a record referenced by a quest alias or script must keep its
  own FormID → force Mode A. This is a hard rule (`Carrot of Seeing`,
  `Poisoned Apple`, `S'jirra's Famous Potato Bread` are quest items).
- **Unique/named items** with custom value or enchantment lose it under B.

---

## Legend

| Verdict | Meaning |
|---|---|
| **OK** | within ±25% — swap as-is |
| **SCALE** | 0.5–0.8× or 1.25–2× — swap with a scale factor |
| **ROT** | sorted extents match but axes are permuted — **reject**, cannot infer rotation sign from an AABB |
| **REJECT** | >2× or <0.5× — different object, or a size no scale should paper over |

Note: the median ratio for identical-name pairs is **1.00** (n=43), so there is
no systematic scale bias between the games — a large ratio is a real per-item
difference, not a units problem.

---

## 1. FOOD & DRINK — recommend **Mode B (full replace)**

The headline group: gives Oblivion a real food system.

| Oblivion (INGR) | → Skyrim (ALCH) | Oblivion size | Skyrim size | ratio | Verdict |
|---|---|---|---|---|---|
| Apple | Red Apple | 9x9x9 | 9x9x9 | 1.00 | **OK** |
| Poisoned Apple ⚠quest | Red Apple | 9x9x9 | 9x9x9 | 1.00 | **OK** — Mode A only |
| Orange | Red Apple | — | — | 1.20 | **OK** |
| Pear | Green Apple | — | — | 0.80 | **OK** |
| Cheese Wedge | Goat Cheese Wedge | — | — | 1.00 | **OK** |
| Beef | Raw Beef | — | — | 1.14 | **OK** |
| Tomato | Tomato | 10x10x9 | 8x8x6 | 0.80 | **OK** |
| Shepherd's Pie | Apple Pie | — | — | 1.14 | **OK** |
| Carrot | Carrot | 22x4x4 | 6x9x33 | 1.50 | SCALE |
| Carrot of Seeing ⚠quest | Carrot | 22x4x4 | 6x9x33 | 1.50 | SCALE — Mode A only |
| Leek | Leek | 8x22x5 | 13x12x30 | 1.36 | SCALE |
| Cheese Wheel | Goat Cheese Wheel | — | — | 1.67 | SCALE |
| Ham | Cooked Beef | — | — | 0.75 | SCALE |
| Boar Meat | Raw Beef | — | — | 1.79 | SCALE |
| Lettuce | Cabbage | — | — | 1.64 | SCALE |
| Sweetroll | Sweet Roll | 10x10x9 | 18x18x15 | 1.80 | SCALE |
| Sweetcake | Honey Nut Treat | — | — | 1.94 | SCALE |
| Grummite Eggs | Chicken's Egg | — | — | 0.59 | SCALE |
| Rat Meat | Skeever Tail | — | — | — | **ROT** — reject |
| Potato | Potato | 5x5x9 | 11x10x20 | 2.22 | REJECT |
| Jumbo Potato | Potato | — | — | 2.22 | REJECT |
| Onion | Leek | — | — | 2.73 | REJECT |
| Radish | Cabbage | — | — | 7.67 | REJECT |
| Mutton | Leg of Goat | — | — | 4.22 | REJECT |
| Bread Loaf | Bread | — | — | 0.40 | REJECT |
| S'jirra's Potato Bread ⚠quest | Bread | — | — | 0.40 | REJECT |
| Strawberry | Snowberries | — | — | 4.83 | REJECT |
| Blackberry | Snowberries | — | — | 7.25 | REJECT |
| Grapes | Jazbay Grapes | — | — | 0.50 | SCALE (borderline) |
| Corn / Pumpkin / Watermelon | Gourd | — | — | — | ? no size |
| Venison | Venison | — | — | — | ? no size |
| Crab Meat | Clam Meat | — | — | — | ? no size |

**Potato/Bread/Mutton reject on size only** — they are the right item. Worth
re-checking with a scale factor before discarding; the plan's ±2× cutoff is
conservative.

## 2. ALCHEMY INGREDIENTS — recommend **Mode A** (Mode B optional)

**OK (swap as-is):** Dragon's Tongue, Ectoplasm, Glow Dust, Mort Flesh→Human
Flesh, Scamp Skin→Falmer Ear, Refined Frost Salts→Frost Salts, Rumare
Slaughterfish Scales→Slaughterfish Scales, Scales→Slaughterfish Scales, Bone
Marrow→Bone Meal, Aloe Vera Leaves→Tundra Cotton, Lady's Mantle Leaves→Tundra
Cotton, Arrowroot→Canis Root, Ginseng→Canis Root, Black Tar→Dwarven Oil, Imp
Fluid→Dwarven Oil, Emetic Russula Cap→Scaly Pholiota, Green Stain Shelf
Cap→White Cap, Summer Bolete Cap→Mora Tapinella, Hydnum Azure Giant
Spore→Glowing Mushroom, Red Kelp Gas Bladder→Nordic Barnacle, Water Root Pod
Pit→Grass Pod, Tobacco→Hanging Moss.

**SCALE:** Daedra Heart 0.72, Human Heart 0.67, Vampire Dust 0.60, Void Salts
0.71, Fire Salts 0.50, Garlic 0.67, Taproot 1.47, Fly Amanita Cap 1.62, Wheat
Grain 1.89, Imp Gall→Imp Stool 1.73, Human Skin→Human Flesh 1.56, Unicorn
Horn→Powdered Mammoth Tusk 0.55, Worm's Head Cap→Namira's Rot 0.72, Wormwood
Leaves→Deathbell 0.75, Dreugh Wax→Nordic Barnacle 0.70, Daedroth Teeth→Hagraven
Claw 0.77, plus the mountain-flower and mushroom families (0.6–1.9×).

**ROT (reject):** Lavender Sprig→Lavender, Harrada→Deathbell, Alocasia
Fruit→Juniper Berries, Green Stain Cup Cap→White Cap.

**REJECT (size):** Nirnroot 2.26, Nightshade 2.33, Troll Fat 2.25, Painted Troll
Fat 2.25, Frost Salts 0.45, Bonemeal 0.41, Ashen Remains 0.41, Bone Shard 0.32,
Clannfear Claws 0.29, Spiddal Stick 0.25, Minotaur Horn 2.16, Ogre's Teeth 3.40,
Wisp Core 2.33, Bloodgrass 0.48, Somnalius Frond 0.47, Redwort Flower 0.44,
Primrose Leaves 0.36, Milk Thistle Seeds 2.80, Flour 2.21, Rice 4.42, Ironwood
Nut 3.67, Elf Cup Cap 2.60, Cinnabar Polypore Red/Yellow 0.42/0.45, and the
seed→Grass Pod family (3.20).

⚠ **Nirnroot and Nightshade are exact-name matches that reject on size** —
Skyrim models them much larger. These are prime candidates for a scale
override rather than a discard.

## 3. MISC CLUTTER — recommend **Mode B (full replace)**

Best value/risk ratio: 28,762 placed references in Oblivion.esm.

| Oblivion | → Skyrim | ratio | Verdict |
|---|---|---|---|
| Diamond | Diamond | 1.00 | **OK** |
| Flawless Diamond | Flawless Diamond | 1.00 | **OK** |
| Sapphire | Sapphire | 1.00 | **OK** |
| Flawless Sapphire | Flawless Sapphire | 1.00 | **OK** |
| Flawless Ruby | Flawless Ruby | 0.86 | **OK** |
| Gold | Gold | 1.00 | **OK** |
| Skull | Skull | 0.89 | **OK** |
| Skeleton Key | Skeleton Key | 0.89 | **OK** |
| Inkwell | Inkwell | 0.89 | **OK** |
| Quill | Quill | 1.25 | **OK** |
| Plate | Plate | 1.07 | **OK** |
| Broom | Broom | 1.02 | **OK** |
| Shovel | Shovel | 1.13 | **OK** |
| Pearl (MISC→INGR) | Pearl | 1.25 | **OK** |
| Ruby | Ruby | 0.71 | SCALE |
| Emerald | Emerald | 0.62 | SCALE |
| Flawless Emerald | Flawless Emerald | 0.62 | SCALE |
| Basket | Basket | 1.58 | SCALE |
| Lockpick | Lockpick | 1.33 | SCALE |
| Flawless Pearl → Pearl | Pearl | 1.67 | SCALE |
| Cheddar Cheese → Goat Cheese Wedge | | 1.40 | SCALE |
| Repair Hammer → Tongs | | 0.79 | SCALE |
| **Fork / Spoon / Knife / Tongs** | same | 0.82–0.90 | **OK by size, but see below** |
| Blue Cheese / Marble Cheese → Eidar Cheese Wedge | | — | **ROT** — reject |
| Bear Pelt | Bear Pelt | 3.04 | **REJECT** |
| Wolf Pelt | Wolf Pelt | 5.83 | **REJECT** |
| Olroy Cheese → Goat Cheese Wheel | | 2.73 | REJECT |
| Flawed Pearl → Small Pearl | | 0.33 | REJECT |
| Bowl / Pitchfork | same | — | ? no size |

⚠ **Cutlery caveat.** Fork/Spoon/Knife/Tongs pass the *ratio* test but their
raw extents show an axis swap (Fork `4x22x2` → `18x2x0`; Knife `4x24x2` →
`20x0x0`). Oblivion lays cutlery along Y, Skyrim along X. The ratio gate alone
does not catch this — **the orientation check must run first**, and these should
land in ROT, not OK. Fix the gate ordering before shipping these rows.

⚠ **Pelts are not a scale problem.** Skyrim's are draped hides, Oblivion's are
rolled bundles. Different object; reject permanently.

---

## Summary

| Verdict | Count | Action |
|---|---|---|
| OK | 48 | ship |
| SCALE | 56 | ship with scale factor |
| ROT | 7 | reject (plus the cutlery once the gate is reordered) |
| REJECT | 49 | reject, or revisit with per-item scale |
| no size | 7 | needs a mesh measurement first |

**Recommended first slice:** the 48 OK rows plus the food group, in Mode B.
That is the largest visible win (working food + better clutter) at the lowest
risk, and it exercises both modes before anything harder is attempted.

## Known gaps

- 7 rows have no measured Oblivion size (mesh missing from the bounds cache) —
  Bowl, Pitchfork, Corn, Pumpkin, Watermelon, Venison, Crab Meat.
- The curated map is **hand-built and unverified in-game**. Some pairings are
  judgement calls (Tobacco→Hanging Moss, Scamp Skin→Falmer Ear) and should be
  reviewed against the preview renderer before shipping.
- Nehrim and Morroblivion are **not** covered here — Oblivion.esm only.
