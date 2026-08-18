# Skyrim ↔ Oblivion Creature Equivalence Map

**Close-to-exact matches only.** A row exists here only when the Skyrim race is
the *same creature* — same body plan, same locomotion class, same combat idiom —
so it could stand in for the Oblivion one without a player noticing a substitute.
Anything that would merely be "the nearest available thing" is deliberately left
**unfilled**. An empty cell is a real answer: it means Skyrim has no equivalent
and the creature must be generated (`creature_races.py`), not aliased.

Sources: `references/Skyrim.esm/RACE.txt` (84 non-vampire/child races) and every
`export/<plugin>/CREA.txt`. **Coverage: Oblivion.esm (909 CREA), Nehrim.esm
(734), Morrowind_ob.esm (307).**

> ## 🛑 The grouping key is (folder, NIFZ body-part set), NOT the folder
>
> An Oblivion mesh *folder* is far too coarse to key a swap on, and using it
> silently mismatches creatures. Measured, in the exports:
>
> | Folder | Actually contains |
> |---|---|
> | `sheep` (Nehrim) | sheep, horned **rams**, and **12 mammoth races** (`Elefant*`, `37Mammut`) |
> | `dog` | dog, wolf, **fox** (Nehrim `fox.nif`), SI skeletal/skinned hounds |
> | `rat` (Nehrim) | rats and a **rabbit** (`rabbit01.nif`) |
> | `hillgiant` (Nehrim) | 13 **giant** races, distinguished only by beard/armour parts |
> | `imp` (Nehrim) | imps, **bats**, and dragon-ish `testdragon.nif` |
>
> The body-part set (`NIFZ`) is the authored signal that separates these, and it
> is already the key `creature_races.py` uses to mint one generated RACE per
> unique `(folder, body set)`. A swap option must key on the same thing — that
> is what "based on race" means here, since **TES4 CREA records have no race
> field at all** (verified: race is a TES5 concept; a CREA carries only a model
> path plus its NIFZ part list).
>
> Race counts per plugin: **Oblivion 229, Nehrim 262, Morroblivion 94.**
> A single race covers many FormIDs, which is exactly why swapping by race
> covers "the many formids that could possibly use one race".

The table is implemented as data in
[vanilla_creature_swap.py](../tes5_import/vanilla_creature_swap.py); inspect
coverage with `tools/creature_swap_report.py`. Every Skyrim.esm FormID below was
verified against the dump.

---

## 0. Intended use: a vanilla-creature swap option

**Status: reference data only. Nothing in the pipeline reads this yet.**

The table exists to back a future converter option that uses a vanilla Skyrim
creature in place of the converted Oblivion one, wherever Skyrim already ships
the same creature. The trade is "looks exactly like the Oblivion original" for
vanilla-quality animation, ragdoll, footsteps, sound and combat AI.

Notes for whoever implements it:

**A swap must replace the RACE *and* the skin ARMO — never the race alone.** A
vanilla race points ANAM/NAM3 at a vanilla skeleton and behavior project, so an
actor left on its Oblivion body mesh gets bone names that do not exist in that
skeleton and T-poses or explodes. Each entry therefore carries both FormIDs.

**Stats should be kept.** Health, level, damage, factions, inventory, AI
packages, scripts and dialogue should still come from the Oblivion CREA, so a
level-40 Oblivion boss stays a level-40 boss wearing a vanilla body. Only the
visual/animation shell is the thing being swapped.

Two tiers, because "close enough" is a judgment call:

| Tier | Meaning |
|---|---|
| `exact` | Same creature in both games. Safe default. |
| `near` | Same archetype, visibly different species (mountain lion → sabre cat). Should require an explicit opt-in. |

Measured coverage (`tools/creature_swap_report.py`):

| Plugin | CREA | exact | exact+near |
|---|---|---|---|
| Oblivion.esm | 909 | 217 (24%) | 396 (44%) |
| Nehrim.esm | 734 | 227 (31%) | 425 (58%) |

```bash
python tools/creature_swap_report.py -f Oblivion.esm          # exact only
python tools/creature_swap_report.py -f Nehrim.esm --near
python tools/creature_swap_report.py -f Oblivion.esm --list   # per record
```

**Resolution order is EditorID first, then folder** — one Oblivion folder can
hold several creatures Skyrim separates. The `dog` folder holds dog, wolf, timber
wolf *and* the SI skinned/skeletal hounds; the first two have exact Skyrim
matches, the hounds have none. An EditorID rule may also map to `None`, meaning
"always generate this one even though its folder swaps" — that is what keeps a
skinned hound from being served as a golden retriever.

Nehrim's creature EditorIDs are **German**, so the table carries German keys
(`hund`, `fuchs`, `hase`, `kuh`, `schaf`, `pferd`). Without them every Nehrim dog
and fox fell through to a generated race despite having exact Skyrim matches.

## 1. Exact matches — same creature in both games

These are the same animal/monster, drawn by both art teams. Safe to treat as
equivalent for animation donation, keyword assignment, and sound routing.

| Oblivion folder | Oblivion creature | Skyrim race | FormID | Notes |
|---|---|---|---|---|
| `bear` | Black / Brown Bear | `BearBlackRace` / `BearBrownRace` | `000131E8` / `000131E7` | Direct colour-for-colour match. `BearSnowRace` `000131E9` has no Oblivion counterpart. |
| `dog` | Dog | `DogRace` | `000131EE` | Same quadruped canine rig; Skyrim's dog is the pipeline's template creature. |
| `dog` | Wolf / Timber Wolf | `WolfRace` | `0001320A` | Oblivion shares one folder for dog+wolf; Skyrim splits them. Match is per-CREA, not per-folder. |
| `horse` | Horse (all coats) | `HorseRace` | `000131FD` | Same rig, same mount role. See [horse_rideability_plan.md](horse_rideability_plan.md). |
| `deer` | Deer Buck / Doe | `DeerRace` | `000CF89B` | `DeerRace` is the true match. `ElkRace` `000131ED` is the *antlered male* and is a closer visual fit for Buck specifically. |
| `mudcrab` | Mud Crab | `MudcrabRace` | `000BA545` | Same creature, same name, both games. |
| `slaughterfish` | Slaughterfish | `SlaughterfishRace` | `00013203` | Same creature, same name, both games. |
| `skeleton` | Skeleton | `SkeletonRace` | `000B7998` | Animated humanoid skeleton, weapon-using, in both. |
| `spriggan` | Spriggan | `SprigganRace` | `00013204` | Same creature, same name, both games. |
| `troll` | Troll | `TrollRace` | `00013205` | Same creature, same cave-brute role in both games. Models differ in detail (Skyrim's is a three-eyed ape) but this is a troll standing in for a troll. Nehrim `nightmaretroll` → `TrollFrostRace` `00013206`; both share `SkinTroll` `00016EE4`. |
| `flameatronach` | Flame Atronach | `AtronachFlameRace` | `000131F5` | Same creature, same name, both games. |
| `frostatronach` | Frost Atronach | `AtronachFrostRace` | `000131F6` | Same creature, same name, both games. |
| `stormatronach` | Storm Atronach | `AtronachStormRace` | `000131F7` | Same creature, same name, both games. |
| `willothewisp` | Will-o-the-Wisp | `WispRace` | `00013208` | Same floating-light creature. `WitchlightRace` `00013209` is the same asset family. |

## 1b. Newly found exact matches (2026-08-17 body-set audit)

These were invisible while the table keyed on folders, because the folder name
does not describe what is inside it. All are **exact** — the same creature.

| Plugin | Folder | Body set | Creature | Skyrim race | FormID |
|---|---|---|---|---|---|
| Nehrim | `sheep` (!) | `mammoth*.nif` | Mammoth (`Elefant`, `37Mammut`) | `MammothRace` | `000131FF` |
| Nehrim | `hillgiant` | `giant.nif` + beard/armour | Giant (13 races) | `GiantRace` | `000131F9` |
| Nehrim | `dog` | `fox.nif` | Fox | `FoxRace` | `00109C7C` |
| Nehrim | `rat` | `rabbit01.nif` | Rabbit/Hare | `HareRace` | `0006DC99` |
| Nehrim | `spinne` | 15 tarantula meshes | Spider | `FrostbiteSpiderRace` | `000131F8` |
| Morroblivion | `horker` | `body.nif` | Horker | `HorkerRace` | `000131FC` |
| Morroblivion | `spherecenturion` | `body.nif` | Dwarven Sphere | `DwarvenSphereRace` | `000131F2` |
| Morroblivion | `steamcenturion` | `body.nif` | Dwarven Centurion | `DwarvenCenturionRace` | `000131F1` |
| Morroblivion | `centspider` | `centurion.nif` | Dwarven Spider | `DwarvenSpiderRace` | `000131F3` |
| Morroblivion | `draugr` | `body.nif` | Draugr | `DraugrRace` | `00000D53` |
| Morroblivion | `skeleton` | `skellie.nif|skull.nif` | Skeleton | `SkeletonRace` | `000B7998` |
| Morroblivion | `spriggan` | `bmspriggan.nif` | Spriggan (Solstheim) | `SprigganRace` | `00013204` |
| Morroblivion | `icetroll` | `body.nif` | Ice Troll | `TrollFrostRace` | `00013206` |
| Morroblivion | `blondbear` | `body.nif` | Snow Bear | `BearSnowRace` | `000131E9` |
| Morroblivion | `a_bear` | `bearbody`/`blackbearbody` | Bear | `BearBrownRace` / `BearBlackRace` | `000131E7` / `000131E8` |
| Morroblivion | `redwolf` / `dog` | `body.nif` / `wolfbody.nif` | Wolf | `WolfRace` | `0001320A` |
| Morroblivion | `mudcrab` | mudcrab parts | Mud Crab | `MudcrabRace` | `000BA545` |
| Morroblivion | `slaughterfish` | `slaughterfish.nif` | Slaughterfish | `SlaughterfishRace` | `00013203` |
| Morroblivion | `flameatronach` / `frostatronach` / `stormatronach` | atronach meshes | Atronachs | `AtronachFlame/Frost/StormRace` | `000131F5`/`F6`/`F7` |

**Requires Dragonborn** (FormIDs NOT yet verified — no dump available; must be
read from the user's installed ESM before use):

| Plugin | Folder | Skeleton | Creature | Dragonborn race | Confidence |
|---|---|---|---|---|---|
| Morroblivion | `iceminion` | `Aa_Blood\IceMinion` | Riekling | `DLC2RieklingRace` | **exact** — UESP confirms Morrowind's Riekling/Ice Minion is the creature Dragonborn reintroduces |
| Morroblivion | `iceraider` | `Aa_Blood\IceRaider` | Mounted Riekling | `DLC2RieklingRace` | **exact** creature, but the *mounted* variant — needs a bristleback to ride or it is a rider with no mount |

### ⚠ Corrections — rows removed after review (2026-08-17)

An earlier draft of this section was **wrong**, built from folder-name
similarity rather than from the meshes or the lore. Recorded here so the same
mistake is not made again:

| Wrongly claimed | Why it is wrong |
|---|---|
| `ashghoul`/`ashslave`/`ashvampire`/`ashzombie` → one "Ash Spawn" row | These are **four different creatures**, each with its own skeleton (`SixthHouse\AshGhoul`, `\AshSlave`, `\AshVampire`, `\AshZombie`). They are Sixth House **corprus** beasts of the Third Era; Skyrim's Ash Spawn are Fourth-Era constructs of volcanic ash raised by a necromancer after the Red Year (UESP). Different creature, different lore, no match. Add Ascended Sleeper to the same family — also distinct. |
| `bullnetch` + `bettynetch` → one "Netch" row | **Two different creatures.** Separate skeletons and different part meshes (`jelly.nif` vs `netchjelly.nif`); UESP: the betty is smaller and more aggressive, the bull larger — they even ship separate images. Dragonborn has both (`DLC2NetchRace` vs a betty variant), so if used they need **two** rows, not one. |
| `boar` (`0FrostBoar`) → Bristleback | Morroblivion's frost boar rides the **Oblivion boar** skeleton (`Creatures\Boar`), not a Solstheim mesh. Whether Dragonborn's bristleback is the same creature is unverified — left out. |
| `udrfrykte`, `frostgiant` → `TrollFrostRace` | Udyrfrykte and Karstaag are **unique named bosses**, not a generic troll race. Left generated. |

## 1c. Deliberately NOT matched (looks close, is not)

| Oblivion/Nehrim | Tempting match | Why rejected |
|---|---|---|
| `sheep` (plain, `sheep.nif`) | `GoatRace` | **A sheep is not a goat.** Different species, different silhouette and horns. Near tier only. |
| `sheep` **ram** (`ramhornl/r.nif`) | `GoatRace` | **A ram is not a goat either** — and it is not the same as a sheep, so it cannot share the sheep row. Horned male sheep; Skyrim has no ram. Near tier at best. |
| `rat` | `SkeeverRace` | Skeever is a *different animal* filling the same niche. Near, not exact. |
| `spiderdaedra` | `FrostbiteSpiderRace` | Only the spider half matches; the Dark Elf torso has no counterpart. |
| `boar` (Oblivion/Nehrim) | `GoatRace` | Skyrim.esm has no boar at all (Dragonborn does — see above). |
| `imp` bats (Nehrim `bat.nif`) | — | Skyrim has no bat creature. |
| Morroblivion `bonewalker` / `greaterbonewalker` | `SkeletonRace` | A bonewalker is a **rotting corpse revenant** (UESP), not an animated bare skeleton. Closer to a draugr, but not that either. |
| Morroblivion `lich` / `lichmw` | `DragonPriestRace` | A Morrowind lich is not a masked Nordic dragon priest. Different creature, different silhouette. |
| Morroblivion `ashghoul`/`ashslave`/`ashvampire`/`ashzombie` | Ash Spawn | Four separate Sixth House corprus creatures; Skyrim's Ash Spawn are unrelated 4th-Era ash constructs. See §1b corrections. |
| Morroblivion `bullnetch` + `bettynetch` | one Netch row | Two different creatures with different meshes and sizes. |
| Morroblivion `udrfrykte`, `frostgiant` | `TrollFrostRace` | Udyrfrykte and Karstaag are unique named bosses. |
| Morroblivion `ascendedsleeper` | any undead race | Half-Dunmer half-beast Sixth House abomination; nothing comparable. |

### 🛑 Method note — how the bad rows got in

Every wrong row above came from reading a **folder name** and matching on the
word, rather than checking the mesh or the lore. Two specific traps:

1. **Generic body-mesh names are not identity.** `body.nif` is used by **20
   different Morroblivion folders** (alit, cliffracer, draugr, dreugh,
   frostgiant, horker, kagouti, riekling, …) and `mesh.nif` by 11. A body set of
   `body.nif` tells you nothing at all.
2. **The full skeleton path is the identity**, not the folder leaf and not the
   body set: `Creatures\Horker\skeleton.nif` vs
   `Morroblivion\Creatures\SixthHouse\AshGhoul\skeleton.nif`. Paths also vary
   in case (`Skellie.NIF` vs `skellie.nif`), so compare case-insensitively or
   identical creatures split into two races.

**Rule: a row is only allowed once the creature has been confirmed from the mesh
path AND from UESP** (`python tools/uesp_lookup.py --page "Morrowind:<X>"`).
Folder-name resemblance is not evidence.


## 2. Near-exact — same archetype, different species

Usable as animation/sound donors; **not** interchangeable on screen.

| Oblivion folder | Oblivion creature | Skyrim race | FormID | Why it is not exact |
|---|---|---|---|---|
| `mountainlion` | Mountain Lion | `SabreCatRace` | `00013200` | Same big-cat quadruped rig and pounce idiom; Skyrim's has sabre tusks and is larger. |
| `sheep` (`sheep.nif`) | Sheep | `GoatRace` `000131FA` / `GoatDomesticsRace` `0006FC4A` | — | Skyrim has no sheep. Same livestock size/gait, different species. |
| `sheep` (`ramhornl/r.nif`) | **Ram** | `GoatRace` | `000131FA` | A horned male sheep — a *separate race* from the plain sheep in every plugin. Not a goat, not a sheep. Kept distinct so a ram is never silently served as a ewe. |
| `rat` | Rat | `SkeeverRace` | `00013201` | Skeever fills the rat niche but is a different, larger animal. |
| `spiderdaedra` | Spider Daedra | `FrostbiteSpiderRace` | `000131F8` | Spider half only; no counterpart for the Dark Elf torso. |
| `ox` / `calf` (Nehrim) | Ox / Calf | `CowRace` | `0004E785` | Cow-class draft animals; Skyrim has only the adult cow. |
| `mrsiikasdonkey` (Nehrim) | Donkey | `HorseRace` | `000131FD` | Donkey on the horse rig; visibly smaller in the original. |
| `pig` (Nehrim) | Pig | — | — | No Skyrim pig. Listed to record that it was considered. |
| `boar` | Boar | — | — | Skyrim has no boar. Nearest quadruped is `GoatRace`; too different to list as a match. |
| `zombie` | Zombie | `DraugrRace` | `00000D53` | Both shambling undead humanoids using a humanoid rig. Draugr are armed, armoured and weapon-using; Oblivion zombies are unarmed maulers. |
| `ghost` | Ghost | `WispShadeRace` | `000F1182` | Both translucent floating humanoid spirits. Skyrim's is a wisp-mother thrall, not a generic ghost. |
| `wraith` | Wraith | `IceWraithRace` | `000131FE` | **Name only.** Skyrim's Ice Wraith is a serpentine ice creature, not a humanoid spectre. Listed because the pipeline's fallback uses it — it is a poor match. |
| `imp` | Imp | — | — | Skyrim has no small flying daedra. No honest match. |
| `minotaur` | Minotaur | — | — | Nearest is `GiantRace`/`TrollRace` by size only; neither is bovine-headed. |
| `ogre` | Ogre | `GiantRace` | `000131F9` | Both large humanoid brutes with club-style melee. Giants are much taller and non-hostile by default. |

## 3. No Skyrim equivalent — must be generated

Skyrim ships nothing close. These are the creatures the generated-race pipeline
exists for; aliasing any of them to a vanilla race produces a visibly wrong
actor.

**Oblivion / Shivering Isles**

| Folder | Creature | Nearest thing in Skyrim (still not a match) |
|---|---|---|
| `clannfear` | Clannfear | — (`DremoraRace` is a humanoid; Clannfear is a raptor) |
| `daedroth` | Daedroth | — (crocodilian biped; nothing comparable) |
| `scamp` | Scamp | — |
| `xivilai` | Xivilai | `DremoraRace` `000131F0` is humanoid daedra but a different creature |
| `landdreugh` | Land Dreugh | — |
| `goblin` | Goblin | `FalmerRace` `000131F4` — small hunched humanoid tribals, different species |
| `lich` | Lich | `DragonPriestRace` `000131EF` — robed undead spellcaster, different lore/model |
| `mehrunesdagon` | Mehrunes Dagon | — (unique boss) |
| `gatekeeper` | Gatekeeper | — (unique SI boss) |
| `jyggylag` | Jyggalag | — (unique SI boss) |
| `grummite` | Grummite | — |
| `gnarl` | Gnarl | — (`SprigganRace` is plant-based but a different creature) |
| `baliwog` | Baliwog | — |
| `elytra` | Elytra | — |
| `hunger` | Hunger | — |
| `shambles` | Shambles | — |
| `murkdweller` | Scalon / Murk Dweller | — |
| `fleshatronach` | Flesh Atronach | — (Skyrim has no flesh atronach) |

**Nehrim-only folders** (no Skyrim equivalent unless noted)

`butcher`, `firedemon`, `fleshgolem`, `ghoul`, `hillgiant` (→ `GiantRace`
`000131F9` is a genuine near-match), `ivellon-lich`, `lichking`, `reaper`,
`spiderspriggan`, `swampstalker`, `thornelemental`, `nightmaretroll` (→
`TrollRace`/`TrollFrostRace` near-match), `wuestegoblin`/`goblin1` (goblin
variants), and the livestock set `calf`, `chicken` (→ `ChickenRace` `000A919D`,
exact), `cow` (→ `CowRace` `0004E507`, exact), `ox` (→ `CowRace`, near),
`pig`, `mrsiikasdonkey` (→ `HorseRace` `000131FD`, near). `spinne` (spider) →
`FrostbiteSpiderRace` `000131F8`, exact-class match.

## 4. Skyrim races with no Oblivion source

Listed so nothing tries to map *into* them. Every one is a Skyrim-original
creature: `DragonRace`, `AlduinRace`, `UndeadDragonRace`, `MammothRace`,
`HorkerRace`, `HagravenRace`, `ChaurusRace`, `ChaurusReaperRace`,
`DwarvenCenturionRace`, `DwarvenSphereRace`, `DwarvenSpiderRace`,
`WerewolfBeastRace`, `FoxRace`, `HareRace`, `BearSnowRace`,
`SabreCatSnowyRace`, `SkeeverWhiteRace`, `TrollFrostRace`, `FalmerRace`,
`DragonPriestRace`, `GiantRace`, `ElkRace`, `WhiteStagRace`.

---

## Reading this against the code

`skyrim_overrides.CREA_RACE_PATTERNS` is a **keyword fallback** used only for
creatures with no converted project. Several of its rows are not real matches by
the standard of this document and are marked as such in-table above — notably
`wraith → IceWraithRace`, `clannfear → DremoraRace`, `scalon → ChaurusRace`,
and `grummite → FalmerRace`. They are acceptable as last-resort fallbacks
(something must be written) but should not be read as equivalences, and any
creature reaching them is a creature whose project failed to convert.
