# The `CREA` record — every field, byte-exact

Sources: xEdit `Core/wbDefinitionsTES4.pas` (record definition, enums, defaults,
required flags) and UESP `Oblivion Mod:Mod File Format/CREA` (byte sizes and
independent flag confirmation). Values are cross-checked against the real
`Oblivion.esm` export of the goblin (`0009661A`) and the Gatekeeper
(`SE02Gatekeeper6`).

Subrecord order below is xEdit's declaration order, which is the order Bethesda
writes them.

---

## 1. Record header flags

TES4 record-header flags meaningful on `CREA`:

| Bit | Value | Meaning |
|---|---|---|
| 10 | `0x00000400` | Quest Item |
| 19 | `0x00080000` | Starts Dead |

---

## 2. Subrecord list

| Req | Sig | Name | Type | Notes |
|---|---|---|---|---|
| + | `EDID` | Editor ID | zstring | |
| − | `FULL` | Name | zstring | Display name |
| − | `MODL` | Model | zstring | **Path to `skeleton.nif`** |
| − | `MODB` | Bound Radius | float | |
| − | `MODT` | Texture Hashes | bytes | |
| * | `CNTO` | Item | struct[8] | formid + u32 count |
| * | `SPLO` | Spell | formid | → `SPEL` / `LVSP` |
| − | `NIFZ` | Model List | zstring[] | **Body-part NIF filenames** |
| **+** | `NIFT` | Model List Textures | bytes | **Required** (`SetRequired`) |
| **+** | `ACBS` | Configuration | struct[16] | see §3 |
| * | `SNAM` | Faction | struct[10] | formid + u8 rank + 3 unused |
| − | `INAM` | Death Item | formid | → `LVLI` |
| − | `SCRI` | Script | formid | → `SCPT` |
| **+** | `AIDT` | AI Data | struct[12] | see §4 |
| * | `PKID` | AI Package | formid | → `PACK` |
| − | `KFFZ` | Animations | zstring[] | **Override .kf names, from `specialanims\`** |
| **+** | `DATA` | Creature Data | struct[20] | see §5 |
| **+** | `RNAM` | Attack Reach | u8 | default 32 |
| − | `ZNAM` | Combat Style | formid | → `CSTY` |
| **+** | `TNAM` | Turning Speed | float | |
| **+** | `BNAM` | Base Scale | float | default 1.0 |
| **+** | `WNAM` | Foot Weight | float | default 3.0 |
| − | `NAM0` | Blood Spray | zstring | |
| − | `NAM1` | Blood Decal | zstring | |
| − | `CSCR` | Inherits Sounds From | formid | → `CREA` |
| * | `CSDT` | Sound Type | u32 | see §6 |
| * | `CSDI` | Sound | formid | → `SOUN` |
| * | `CSDC` | Sound Chance | u8 | |

`+` required · `−` optional · `*` repeating

`CSDT`/`CSDI`/`CSDC` form a repeating **sorted struct** keyed on `CSDT`: one
`CSDT` followed by one or more `CSDI`+`CSDC` pairs.

---

## 3. `ACBS` — Configuration (16 bytes)

| Offset | Field | Type | Default |
|---|---|---|---|
| 0 | Flags | u32 | 576 (`0x240`) |
| 4 | Base spell points | u16 | 50 |
| 6 | Fatigue | u16 | 50 |
| 8 | Barter gold | u16 | 0 |
| 10 | Level (offset) | **s16** | 1 |
| 12 | Calc min | u16 | 0 |
| 14 | Calc max | u16 | 0 |

`Level` is **signed** — a negative value means "PC level offset" (goblin uses
`-2` with calc min 1 / max 3).

### `ACBS` flags

| Bit | Value | Meaning |
|---|---|---|
| 0 | `0x00000001` | Biped |
| 1 | `0x00000002` | Essential |
| 2 | `0x00000004` | Weapon & Shield |
| 3 | `0x00000008` | Respawn |
| 4 | `0x00000010` | Swims |
| 5 | `0x00000020` | Flies |
| 6 | `0x00000040` | Walks |
| 7 | `0x00000080` | PC Level Offset |
| 9 | `0x00000200` | No Low Level Processing |
| 11 | `0x00000800` | No Blood Spray |
| 12 | `0x00001000` | No Blood Decal |
| 15 | `0x00008000` | No Head |
| 16 | `0x00010000` | No Right Arm |
| 17 | `0x00020000` | No Left Arm |
| 18 | `0x00040000` | No Combat in Water |
| 19 | `0x00080000` | No Shadow |
| 20 | `0x00100000` | No Corpse Check |

Bits 4/5/6 (**Swims/Flies/Walks**) are the movement-capability set and pair with
the animation files present in the folder — a creature flagged Swims is expected
to have `swim*.kf` variants (the rat has a full set: `swimforward.kf`,
`swimidle.kf`, `swimbackward.kf`, `swimfastforward.kf`, and swim hand-to-hand
attack/recoil/stagger files).

Bits 15–17 (**No Head / No Right Arm / No Left Arm**) tell the engine the rig
lacks those parts, which suppresses look-at, weapon and shield behaviour.

Default 576 = `0x240` = Walks | No Low Level Processing.

UESP notes the `No Corpse Check` bit behaves opposite to `NPC_`'s — likely a CS
bug, but the bit value is as listed.

---

## 4. `AIDT` — AI Data (12 bytes)

| Offset | Field | Type | Default |
|---|---|---|---|
| 0 | Aggression | u8 | 70 |
| 1 | Confidence | u8 | 50 |
| 2 | Energy Level | u8 | 50 |
| 3 | Responsibility | u8 | 50 |
| 4 | Buys/Sells and Services | u32 | 0 |
| 8 | Teaches | **s8** | 0 |
| 9 | Maximum training level | u8 | 0 |
| 10 | *unused* | 2 bytes | |

**Energy Level directly drives idle frequency** — the Idle Manager consults it,
and low-energy actors idle more often.

### Service flags (`Buys/Sells and Services`)

| Value | Service | | Value | Service |
|---|---|---|---|---|
| `0x00000001` | Weapons | | `0x00000400` | Miscellaneous |
| `0x00000002` | Armor | | `0x00000800` | Spells |
| `0x00000004` | Clothing | | `0x00001000` | Magic Items |
| `0x00000008` | Books | | `0x00002000` | Potions |
| `0x00000010` | Ingredients | | `0x00004000` | Training |
| `0x00000080` | Lights | | `0x00010000` | Recharge |
| `0x00000100` | Apparatus | | `0x00020000` | Repair |

### `Teaches` — skill enum

| | | | | | |
|---|---|---|---|---|---|
| 0 Armorer | 1 Athletics | 2 Blade | 3 Block | 4 Blunt | 5 Hand to Hand |
| 6 Heavy Armor | 7 Alchemy | 8 Alteration | 9 Conjuration | 10 Destruction | 11 Illusion |
| 12 Mysticism | 13 Restoration | 14 Acrobatics | 15 Light Armor | 16 Marksman | 17 Mercantile |
| 18 Security | 19 Sneak | 20 Speechcraft | | | |

---

## 5. `DATA` — Creature Data (20 bytes)

| Offset | Field | Type | Default |
|---|---|---|---|
| 0 | Type | u8 | 0 |
| 1 | Combat Skill | u8 | 50 |
| 2 | Magic Skill | u8 | 50 |
| 3 | Stealth Skill | u8 | 50 |
| 4 | Soul | u8 | 3 |
| 5 | *unused* | 1 | |
| 6 | Health | u16 | 50 |
| 8 | *unused* | 2 | |
| 10 | Attack Damage | u16 | 0 |
| 12 | Strength | u8 | 50 |
| 13 | Intelligence | u8 | 50 |
| 14 | Willpower | u8 | 50 |
| 15 | Agility | u8 | 50 |
| 16 | Speed | u8 | 50 |
| 17 | Endurance | u8 | 50 |
| 18 | Personality | u8 | 50 |
| 19 | Luck | u8 | 50 |

### `Type` enum

| 0 | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| Creature | Daedra | Undead | Humanoid | Horse | Giant |

### `Soul` enum

| 0 | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| None | Petty | Lesser | Common | Greater | Grand |

`Speed` feeds the movement rate the accumulated `Bip01` translation is scaled
against — see the movement-distance guidance in the main SKILL (§3).

---

## 6. `CSDT` — Sound Type enum

| Value | Type | Triggered by |
|---|---|---|
| 0 | Left Foot | text key `Enum: Left` |
| 1 | Right Foot | text key `Enum: Right` |
| 2 | Left Back Foot | text key `Enum: BackLeft` |
| 3 | Right Back Foot | text key `Enum: BackRight` |
| 4 | Idle | text key `Enum: Idle` |
| 5 | Aware | text key `Enum: Aware` |
| 6 | Attack | text key `Enum: Attack` |
| 7 | Hit | text key `Hit` |
| 8 | Death | death |
| 9 | Weapon | weapon impact |

Quadrupeds use all four foot types; bipeds use only 0/1. The goblin maps 0 and 1
to the **same** `SOUN` (`000A653F`).

---

## 7. Worked record — the vanilla Goblin (`0009661A`)

```
EditorID = Goblin
Model.MODL = Creatures\Goblin\Skeleton.nif      ← the SKELETON
ACBS.Level = -2, CalcMin = 1, CalcMax = 3
FactionCount = 2   (00045302 rank 0, 00000013 rank 0)
AIDT.Aggression = 100, Confidence = 70, EnergyLevel = 80, Responsibility = 0
AIPackage[0] = 00050489,  AIPackage[1] = 000362B9
DATA.Type = 0 (Creature)
DATA.CombatSkill = 20, MagicSkill = 50, StealthSkill = 10
DATA.Soul = 1 (Petty),  Health = 15,  AttackDamage = 3
DATA.Strength = 30  Intelligence = 30  Willpower = 30  Agility = 40
DATA.Speed = 12     Endurance = 40     Personality = 10  Luck = 40
RNAM.AttackReach = 52
ZNAM.CombatStyle = 00050534
TNAM.TurningSpeed = 0.0
BNAM.BaseScale = 1.0
WNAM.FootWeight = 6.0
NIFT.Size = 4
NIFZCount = 5
  NIFZ[0] = GobLegs01.NIF        NIFZ[3] = GoblinHandR.NIF
  NIFZ[1] = GoblinChest01.NIF    NIFZ[4] = GoblinHead.NIF
  NIFZ[2] = GoblinHandL.NIF
SoundTypeCount = 7
  Type 0 (L Foot)  000A653F  chance 100
  Type 1 (R Foot)  000A653F  chance 100
  Type 4 (Idle)    000A6540  chance  75
  Type 5 (Aware)   000A653D  chance 100
  Type 6 (Attack)  000A653C  chance  80
  Type 7 (Hit)     000A6541  chance  80
  Type 8 (Death)   000A653E  chance 100
```

Note the goblin declares **no `KFFZ`** — it uses the folder's default animation
set. Goblin *shamans* are a separate CREA that adds `KFFZ` entries pointing at
`specialanims\idle_sharman.kf` and `specialanims\forward_sharman.kf`.

---

## 8. Creature folders in vanilla Oblivion

34 folders under `meshes\creatures\` in `Oblivion - Meshes.bsa`:

```
bear  boar  boxtest  clannfear  daedroth  deer  dog  endgame  flameatronach
frostatronach  ghost  goblin  horse  imp  landdreugh  lich  mehrunesdagon
minotaur  mountainlion  mudcrab  ogre  rat  scamp  sheep  skeleton
slaughterfish  spiderdaedra  spriggan  stormatronach  troll  willothewisp
wraith  xivilai  zombie
```

Shivering Isles adds (in `DLCShiveringIsles - Meshes.bsa`): `baliwog`, `elytra`,
`fleshatronach`, `gatekeeper`, `gnarl`, `grummite`, `hunger`, `jyggylag`,
`murkdweller`, `shambles`, and others.

`boxtest` and `endgame` are development/cinematic assets, not playable creatures.
