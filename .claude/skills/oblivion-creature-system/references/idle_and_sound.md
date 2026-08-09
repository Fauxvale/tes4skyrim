# Idles, the Idle Manager, AnimObjects and creature sound

Sources: xEdit `Core/wbDefinitionsTES4.pas` (`IDLE`, `ANIO`, `CREA` sound
subrecords, `wbIdleAnam`), the Oblivion CS wiki (`Idle Animations`,
`Forcing Idle Animations`, `Anim Object`, `Animation Tab`), and the vanilla
`Oblivion.esm` export.

---

## 1. The `IDLE` record

```
EDID   Editor ID                                   required
MODL   Model — the .kf file                        required
MODB   Bound radius
MODT   Texture hashes
CTDA   Conditions (repeating)                      standard TES4 condition list
ANAM   Animation Group Section (u8)                required
DATA   Parent (formid → IDLE) + Previous (formid → IDLE)   required
```

`DATA` is what builds the **tree**: `Parent` is the node above, `Previous` is the
prior sibling. Both may be `NULL`.

### 1.1 `ANAM` — animation group section + flag

From xEdit's `wbIdleAnam`. The low 7 bits are the section; **bit `0x80` is a flag
whose sense is inverted**.

| `ANAM & 0x7F` | Section |
|---|---|
| 0 | Lower Body |
| 1 | Left Arm |
| 2 | Left Hand |
| 3 | Right Arm |
| 4 | **Special Idle** |
| 5 | Whole Body |
| 6 | Upper Body |

> If **bit `0x80` is CLEAR**, the idle is **"Must return a file"**.
> (xEdit: `if (aInt and $80) = 0 then Result := Result + ', Must return a file'`.)

Sections 0–3 are the LB / LA / LH / RA channels the CS animation preview exposes.
**One animation can play per section at a time** — this is how Oblivion layers an
upper-body action over lower-body locomotion without a behavior graph. Sections do
*not* restrict which bones the animation touches: `bowattack.kf` runs in the Right
Arm section but animates both arms.

For creature and NPC special idles the section is normally **4 (Special Idle)**,
which the CS wiki describes as: "the game attempts to play the file across the
entire skeleton and uses priorities to determine what bones, if any, of the
animation currently playing are overridden by the Idle Animation."

That is the crucial interaction: a **Special Idle competes bone-by-bone with the
running animation using the `ControlledBlock.Priority` values** described in
`animation_kf.md`. A special idle authored with low priorities will be visibly
overridden by locomotion.

### 1.2 File location requirement

The CS wiki is explicit twice over:

- "Only KF files in the **IdleAnims** directory beneath the directory in which the
  NPC or Creature's skeleton exists can be selected."
- SpecialIdles "must be identified as `SpecialIdle_*` … and must be located in a
  sub-folder of the main creature/character folder named `idleanims`."

So for a goblin: `meshes\creatures\goblin\idleanims\specialidle_flee.kf`.
Putting the file anywhere else produces an **"Invalid Directory"** error in the CS.

Note the *sequence name* inside such a file is still an AnimGroup —
`SpecialIdle_GetUpFaceUp`, `SpecialIdle_Look`, etc. (see `animation_kf.md` §6.1).

---

## 2. The Idle Manager

### 2.1 The tree

Top-level nodes are **skeletons**, added dynamically from the loaded NPCs and
creatures — e.g. `Characters\_Male\` for NPCs and `Creatures\Goblin` for goblins.
Everything beneath a top-level node is a candidate idle.

Deleting an idle deletes all of its children. "Insert Child" adds as the **first**
child; "Insert Sibling" adds **immediately after** the selected node. Order
matters — see the selection algorithm.

### 2.2 Selection algorithm (CS wiki, verbatim behaviour)

1. Find the top-level node matching **the actor's skeleton**.
2. Start at its first child; test that idle's **conditions**.
3. If the actor fails, move to the **next sibling**.
4. If the actor passes, descend into that idle's **first child** and repeat.
5. The chosen idle is the one whose conditions pass while **none of its children's
   conditions pass** (or which has no children).

Then, on the chosen idle:

- If it **has a KF**, the file is queued and played when loaded.
- If it has **no KF**:
  - **"Must Return a File" set** → skip it as though the conditions had failed,
    and continue to its next sibling.
  - **"Must Return a File" clear** → return nothing; **no idle plays**.

The wiki's worked example: an NPC in a chair matches a `SittingIdles` grouping
node that has no KF. With the flag clear, nothing plays (correct — better than a
standing animation). With the flag set, the search moves on and the actor may play
an idle inappropriate for sitting.

### 2.3 When idles fire

- **`PickIdle`** — script/console function; the console prints the chosen file.
- **Knockdown / unconscious** — the Idle Manager picks the get-up animation.
  (Hence `SpecialIdle_GetUpFaceUp` 21 files, `SpecialIdle_GetUpFaceDown` 18,
  `SpecialIdle_GetUpLeft`/`Right` 10 each in vanilla creatures.)
- **Yielding in combat.**
- **Sleep and Eat packages** — idles are requested *constantly*, so any actor that
  can run these packages needs blocking idles for them.
- **Dialogue** — both speaker and listener request an idle on each new line.
- **Randomly**, governed by the actor's **`AIDT.EnergyLevel`**: low energy idles
  more often. The check happens when the lower body is idling and the energy timer
  has run down.

### 2.4 Forcing an idle on one actor

The standard recipe (CS wiki *Forcing Idle Animations*): create a `MiscItem`
token, give it to the actor, put the KF in `idleanims\`, create an IDLE with
section **SpecialIdle** conditioned on `GetItemCount` of that token, and run
`PickIdle` from an `OnLoad` block:

```
scn AnimationActorScript
begin onload
    pickidle
end
```

---

## 3. `ANIO` — AnimObjects

AnimObjects are props attached to the skeleton for the duration of an idle.

```
EDID   Editor ID
MODL   NIF file — attached to the actor's skeleton when the idle plays
MODB   Bound radius
MODT   Texture hashes
DATA   formid → IDLE   (the associated idle animation)
```

Rules from the CS wiki *Anim Object*:

- The mesh needs **no BSX flags and no Havok collision data**.
- It is normally attached to the **`Weapon`** node; reference meshes live in
  `Data\meshes\idleobjects`.
- A separate *clutter* mesh (with collision) is the AI-package target.
- The idle needs at least one **`GetIsItemUsed`** condition naming that clutter
  object.
- A **`UseItemAt`** package directs the actor to use it.

The `Attach` (51) and `Detach` (47) **text keys** in the KF are what actually bind
and release the AnimObject during the animation — see `animation_kf.md` §5.2.

---

## 4. Creature sound

### 4.1 The record side

Sounds are stored on the `CREA` record as a repeating, `CSDT`-keyed group:

```
CSDT   Sound Type   u32
CSDI   Sound        formid → SOUN
CSDC   Sound Chance u8        (percent)
```

Both `CSDI` and `CSDC` are marked required within each group; a group may hold
several `CSDI`+`CSDC` pairs, from which the engine picks one.

`CSCR` ("Inherits Sounds from", formid → `CREA`) reuses another creature's whole
sound set — used heavily by creature variants.

### 4.2 `CSDT` sound types, and what fires them

| `CSDT` | Type | Fired by |
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

**This is the join between the animation and the record.** The `.kf` text key
names only a *category*; the CREA record supplies the actual `SOUN`. Therefore:

- Footstep text keys **with no `CSDT` 0–3 entries** → the creature walks silently.
- `CSDT` entries **with no matching text keys** → the sound never plays.
- Quadrupeds use all four foot types; bipeds use only 0 and 1.

A `Sound: <SoundEditorID>` text key **bypasses this table entirely** and plays the
named `SOUN` record directly (249 occurrences in vanilla creature animations,
e.g. `Sound: NPCLichFootFloat`).

### 4.3 Real goblin sound set (`0009661A`)

| Type | Sound | Chance |
|---|---|---|
| 0 Left Foot | `000A653F` | 100 |
| 1 Right Foot | `000A653F` | 100 |
| 4 Idle | `000A6540` | 75 |
| 5 Aware | `000A653D` | 100 |
| 6 Attack | `000A653C` | 80 |
| 7 Hit | `000A6541` | 80 |
| 8 Death | `000A653E` | 100 |

Both feet share one sound. There is no `CSDT` 2/3 (biped), and no 9 (Weapon) —
goblins use their equipped weapon's own sounds.

`WNAM` (Foot Weight, goblin = 6.0) scales the footstep's audible weight; the
default is 3.0.

---

## 5. Diagnostic notes

| Symptom | Cause |
|---|---|
| CS says "Invalid Directory" when picking a KF | file is not under `<skeleton folder>\idleanims\` |
| Idle never plays | conditions fail; or a parent has no KF and *Must Return a File* is clear |
| Wrong idle plays for a sitting/sleeping actor | *Must Return a File* set on a grouping node that should stop the search |
| Special idle visibly overridden by walking | `ControlledBlock.Priority` too low vs the locomotion animation |
| Creature idles constantly / never | `AIDT.EnergyLevel` (low = more idling) |
| Silent footsteps | text keys present but `CSDT` 0–3 missing (or vice versa) |
| Quadruped only makes 2 footstep sounds | `CSDT` 2/3 missing, or the KF lacks `Enum: BackLeft`/`BackRight` |
| AnimObject doesn't appear | `ANIO.DATA` not pointing at the idle, or no `Attach` text key |
