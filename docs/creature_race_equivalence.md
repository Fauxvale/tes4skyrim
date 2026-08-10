# Skyrim ↔ Oblivion Creature Equivalence Map

**Close-to-exact matches only.** A row exists here only when the Skyrim race is
the *same creature* — same body plan, same locomotion class, same combat idiom —
so it could stand in for the Oblivion one without a player noticing a substitute.
Anything that would merely be "the nearest available thing" is deliberately left
**unfilled**. An empty cell is a real answer: it means Skyrim has no equivalent
and the creature must be generated (`creature_races.py`), not aliased.

Sources: `references/Skyrim.esm/RACE.txt` (84 non-vampire/child races) and the
creature mesh folders in `export/<plugin>/CREA.txt` (Oblivion 42, Nehrim 65).
Folder names are the pipeline's grouping key (`_folder_of` in
[creature_races.py](../tes5_import/creature_races.py)) — the same key
`_FOLDER_KEYWORDS` and `creature_projects.json` use.

The table is implemented as data in
[vanilla_creature_swap.py](../tes5_import/vanilla_creature_swap.py) and drives
the opt-in `--vanilla-creatures` conversion option (§0). Every FormID below was
verified against the dump by `tools/creature_swap_report.py`.

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
| `sheep` | Sheep / Ram | `GoatRace` / `GoatDomesticsRace` | `000131FA` / `0006FC4A` | **Near-exact, not exact** — Skyrim has no sheep. Same size/gait/livestock role; the only mismatch is species. |
| `mudcrab` | Mud Crab | `MudcrabRace` | `000BA545` | Same creature, same name, both games. |
| `slaughterfish` | Slaughterfish | `SlaughterfishRace` | `00013203` | Same creature, same name, both games. |
| `rat` | Rat | `SkeeverRace` | `00013201` | Skeever is Skyrim's rat — same ecological + encounter role, larger model. |
| `skeleton` | Skeleton | `SkeletonRace` | `000B7998` | Animated humanoid skeleton, weapon-using, in both. |
| `spriggan` | Spriggan | `SprigganRace` | `00013204` | Same creature, same name, both games. |
| `troll` | Troll | `TrollRace` | `00013205` | Same creature, same cave-brute role in both games. Models differ in detail (Skyrim's is a three-eyed ape) but this is a troll standing in for a troll. Nehrim `nightmaretroll` → `TrollFrostRace` `00013206`; both share `SkinTroll` `00016EE4`. |
| `flameatronach` | Flame Atronach | `AtronachFlameRace` | `000131F5` | Same creature, same name, both games. |
| `frostatronach` | Frost Atronach | `AtronachFrostRace` | `000131F6` | Same creature, same name, both games. |
| `stormatronach` | Storm Atronach | `AtronachStormRace` | `000131F7` | Same creature, same name, both games. |
| `willothewisp` | Will-o-the-Wisp | `WispRace` | `00013208` | Same floating-light creature. `WitchlightRace` `00013209` is the same asset family. |
| `spiderdaedra` | Spider Daedra | `FrostbiteSpiderRace` | `000131F8` | **Partial** — matches the *spider half* only. The Oblivion creature is a centaur-form Dark Elf torso on a spider body; Skyrim has no upper body. |

## 2. Near-exact — same archetype, different species

Usable as animation/sound donors; **not** interchangeable on screen.

| Oblivion folder | Oblivion creature | Skyrim race | FormID | Why it is not exact |
|---|---|---|---|---|
| `mountainlion` | Mountain Lion | `SabreCatRace` | `00013200` | Same big-cat quadruped rig and pounce idiom; Skyrim's has sabre tusks and is larger. |
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
