# asset_convert/hkx_anim.py — animation and behaviour graphs

**Code:** `asset_convert/hkx_anim.py`, `asset_convert/hkx_animobject.py`, `asset_convert/hkx_behavior.py`, `asset_convert/kf_decode.py`, `asset_convert/kf_writer.py`

## Contents

- [NIF animated mesh conversion](#nif-animated-mesh-conversion)
- [Animated-object behaviour graphs (asset_convert/hkx_animobject.py)](#animated-object-behaviour-graphs)

## NIF animated mesh conversion
<a id="nif-animated-mesh-conversion"></a>
- Oblivion animated doors/activators use keyframed collision (motion_system=6 in Oblivion format)
- Key differences from static collision in Skyrim:
  - bhkCollisionObject.flags = 137 (0x89 = ACTIVE | D_ANIMATED | bit 7)
  - bhkRigidBody.motion_system = 4 (MO_SYS_KEYFRAMED)
  - **bhkRigidBody.mass = 0 AND filter layer = 2 SKYL_ANIMSTATIC** (see below)
  - bhkRigidBody.quality_type = 1 (MO_QUAL_FIXED)
  - bhkRigidBody.unknown_byte = 10 (broadphase type for animated)
  - NiNode flags |= 0x80 (selective update sync for physics)

### Keyframed bodies: layer 2 ANIMSTATIC + mass 0 (2026-08-02, PENDING in-game confirmation)

**History, kept honest.** The earlier note here ("mass 0 is MANDATORY,
implemented") was wrong twice over: (a) the mass write was never in the shipped
code — the attempted `rb.mass = 0.0` inside the keyframed branch changed which
downstream shape path ran (hull decomposition keys on `mass > 0`), collapsed
the collision compound, and was reverted, so **the in-game test that "mass
changed nothing" tested a broken build, not the theory**; (b) the two
"real causes" it then blamed (adjacent `PlayGroup`s cancelling; one-sequence
hold-state dead end) were both fixed and the planks still failed in-game
(2026-08-02: "begins to animate, stops suddenly, doesn't finish" — so the
event DOES reach the graph and the sequence starts; something reclaims the
nodes mid-clip). Do not cite either as the mechanism again.

**The discriminator the earlier session missed is the collision filter
LAYER.** Census of every vanilla motion_system=4 body found
(`farmhouseanimdoor01`, `farmbtrapdoor01`, `rtirongate01`, `orcdoor01`,
`riftenkeepdoor01` ×2, `mrkmarketstalldoor01`, `rifrmsmbasewallgrate01`,
`rifrmsmsecretcabinetdoor01` ×2, `sldjailwallcollapse01`): **layer 2
SKYL_ANIMSTATIC and mass exactly 0.0, no exceptions.** Our one in-game-working
animated object (`prisonSecretWall01`, source-authored OL_ANIM_STATIC + mass 0)
also ships layer 2 / mass 0. The broken ones shipped **layer 10 PROPS**
(Oblivion authored the bricks/planks on OL_PROPS, which the 0-18 identity
remap passes through) with mass 40/100:
- `mwallplankbreakaway01` (Oblivion + Nehrim) — 8 planks × mass 40, layer 10
- `IDCrumbleWall01` (ImperialDungeon01) — 13 bricks × mass 100, layer 10

Fix in `_convert_collision`: the keyframed branch forces layer 2 on both
filters; mass is zeroed at the very END of the function (after the mass-keyed
decompose gate), so the only bytes that change are the two fields themselves —
verified by structural diff (block graph identical; prisonSecretWall01
unchanged in every field). Multiple keyframed bodies per NIF is vanilla-legal
(`riftenkeepdoor01` ships two).

Note `sldjailwallcollapse01`'s own pattern for multi-piece collapses: ONE
keyframed mass-0 helper body (`ColHelper01`) and NO collision on the 22
animated pieces — vanilla never gives each piece its own body. We keep
per-piece keyframed bodies (faithful to the source collision), normalized to
the vanilla per-body contract.

### Constrained trap islands are HELD, not dynamic (2026-08-05, in-game confirmed)

A swinging trap (`ctrapswingmacelong01`'s chain links + mace head,
`ctraplogs01`, `cprollingrock01`) is authored exactly like a breakaway piece:
`ms=6` KEYFRAMED bodies with **real mass** and `Unyielding = 1`, wired together
by constraints. Oblivion's own script states the contract in its header:

> `; On activation havok will turn on and logs will roll` — `CTrapLogs01SCRIPT`

The old rule sent any `ms=6` body with *mass + a constraint* to **DYNAMIC**
(case 2), which is why **every swinging trap swung freely the instant the cell
loaded**, before anything tripped it. The opposite error (mass-0 keyframed)
welds the trap solid forever. Both were wrong for the same reason: the island is
**held rigid until the trap script fires**.

Fix (`_node_is_held_trap`): a constrained island member ships **KEYFRAMED but
keeps its authored mass**, and the converted script releases it with
`SetMotionType(Motion_Dynamic)`. Membership is checked **island-wide, not
per-body** — a chain link routinely carries mass with `num_constraints == 0` and
hangs off a neighbour's constraint (same reason `collision_extract` checks
constraints file-wide).

Vanilla `trapmace01` ships its links dynamic because a *Skyrim* trap has no
script-held phase; ours must reproduce Oblivion's held phase instead. Do not
"correct" ours to match vanilla here.

**But the MOTION TYPE is the only thing the held phase changes — quality_type
and solver_deactivation must be the POST-RELEASE values (2026-08-10, in-game
confirmed hang).** `SetMotionType(Motion_Dynamic)` swaps the motion type and
nothing else, so whatever the NIF ships for collision quality is what the body
simulates with *after* it is let go. The keyframed branch used to give every
animated body `quality_type=1` (MO_QUAL_FIXED, "static body") with
`solver_deactivation=1` (OFF) — right for a door, whose position really is
deterministic and which is never released, but wrong for a held trap. On
release that handed Havok a ring of mass-bearing bodies inside a live
constraint island all still claiming to be static with deactivation disabled;
the solver has no consistent state to converge on and the simulation step stops
completing. The game keeps running and never renders another frame — it reads
as a **freeze on a black loading screen**, not a crash, and nothing appears in
the Papyrus log.

Symptom that isolated it: walking onto the tripwire in Natural Caverns
(`ImperialDungeon05`, `CGTrigTripwire01` → three chained `CTrapSwingMaceLong01`)
hung the game, while the Vilverin tripwire was fine. Vilverin's trap is
`CTrapSwingMaceShort01`, and the two differ in exactly one way that matters:
the long mace hangs on **7 `chainLink` bodies with `bhkRagdollConstraint`**,
the short one had no chain at the point it was compared. Same mesh family,
same script, same `playgroup` — the constraint island was the whole difference.

Vanilla is the reference for the released state: `trapmace01.nif` ships every
`Link01..11` **and** `Mace01` at `quality_type=4` (MO_QUAL_MOVING) with
`solver_deactivation=2` (LOW). So `_convert_collision` now branches on
`breakaway_body`: held/breakaway pieces get 4/2, plain animated bodies keep
1/1. Plain animated doors (`cdoor03`, `ricketyfencegate01`) stay
**byte-identical**, and creature skeletons are untouched because they route
through `_convert_blend_collision` before this branch. Regression test:
`test_held_trap_ships_post_release_quality`.

**The release is keyed on the MESH, never the animation-group name.**
`physics_flags_from_data` bit 1 = "ships a keyframed body that kept a non-zero
mass", which `_convert_collision` writes for held pieces only (36 meshes).
Keying off the group name cannot work: `forward` is **491 of Oblivion's 850**
`playgroup` calls and is overwhelmingly gates, doors and portcullises that must
keep following their clip exactly — yet it is *also* the tripwire's break group.
The mesh knows which is which; the name does not.

**The bounds cache is SCHEMA-VERSIONED — bump the version when you add a field**
(2026-08-14). `mesh_bounds_cache.json` carries a `"__schema__": [N]` entry, and
`collision_extract.bounds_cache_is_current()` treats any cache written at a
lower version — or with no stamp at all — as missing, so it regenerates instead
of being trusted. Bump `BOUNDS_SCHEMA_VERSION` in the same commit as any change
to an entry's fields.

This is not hypothetical tidiness; skipping it shipped a bug. Entries are plain
lists, so a cache written before a field existed parses cleanly and reads as
**zero** for that field, which is indistinguishable from a computed zero. The
scan used to run only when the file was **absent**, so when bit 1 (HELD) shipped
on 2026-08-05 Nehrim simply kept its 2026-08-02 cache: 0 of its 11,946 meshes
carried the bit, `needs_havok_release` answered False for every one, and **no**
converted `playgroup` emitted `TES4Polyfill.ReleaseBreakaway`.
`mwallplankbreakaway01`'s planks stopped falling. Oblivion's cache happened to
be rebuilt an hour after that commit, so the *same mesh* still worked there —
which made it look like a Nehrim mesh bug rather than a stale cache. Diagnostic
that settles it in one step: compare the entry length for the same path key
across two plugins' caches (`[…, 2]` vs a bare 6-element list).

`--scripts-only` cannot rebuild the cache (it runs with no mesh scan), so
`script_convert/pipeline.py` prints a loud warning when the cache is stale
rather than silently emitting scripts with no release. The script stage also
has to load that cache in **both** the parent and the spawned workers (Windows
spawn does not inherit module state), or every lookup silently answers 0.

**Layer 14 (`OL_TRAP`) on the striking body is LOAD-BEARING — never remap it.**
`_remap_world_filter` passes 14 through unchanged and must keep doing so: it is
the layer whose contact raises Skyrim's `OnTrapHitStart`, which is the ONLY
thing that makes a converted trap deal damage (see the trap-damage section of
[papyrus_conversion_notes.md](script_convert.md) — the damage lives
in the script's `fTrapDamage` variables, not in the mesh). Oblivion and Skyrim
agree on the idiom: `ctrapswingmacelong01`'s mace-end link is layer 14 with its
chain on 10, and vanilla `trapmace01` is identical (Mace01 = 14, Link01-11 =
10). Flattening 14 → 10 "for consistency" would silently disarm every trap.

### The Rest state is CORRECT — the animobject crash is elsewhere (2026-08-10)

Recorded so the next session does not re-tread this. `pSequence=''` on
`GamebryoSequenceGeneratorRest` is **right** and must not be changed: nothing
should play on cell load, for doors, rubble AND plants alike (the Spiddal
plant animates when the player approaches, driven by its script, not by the
graph starting in Forward).

Two "fixes" were tried and both were wrong:

| change | crash | door |
|---|---|---|
| `pSequence=sequences[0]`, `fPercent=0` | stopped | **opened on cell load** |
| non-empty sentinel naming no sequence | **still crashed** (in Generator00) | ok |

The decisive evidence: the FIRST crash of this family
(crash-2026-08-10-00-42-35) was already on **`GamebryoSequenceGenerator00`** —
the generator that plays `Forward` — not on the Rest generator. So the empty
Rest name was never the cause, and every Rest-state change merely moved the
symptom.

Also ruled out by census (700 vanilla meshes, 149 sequences): a **dataless
interpolator is legal**. Vanilla ships 72 dataless `NiFloatInterpolator`, 248
`NiBoolInterpolator`, 259 `NiBoolTimelineInterpolator` and even 4 dataless
`NiTransformInterpolator` (fxbatgroup, fxpoisongaswithonoff,
sprigganfxtestunified). Both crashing meshes carry dataless blocks, but so does
working vanilla content — it is not the discriminator.

RESOLVED (same day): the crash was **empty text key values** in the activated
sequence — see "🔴 A graph-bound mesh must ship NO empty text keys" below.
The secret door's `Forward` plays fine because its keys are only
`start`/`end`, both non-empty; the plants shipped Oblivion-authored empty
keys.

### Every state needs a real transitions array — including at ONE sequence (2026-08-01)

`_transitions(exclude_state=i)` gives each motion state "every OTHER sequence",
so a repeated event cannot restart a sequence mid-play. For a **one-sequence**
object that set is EMPTY and the emitter writes `transitions=null` — the exact
dead end the Rest-state comment warns about. `IDCrumbleWall01`'s only sequence
is `Unequip`, so once it played it could never be re-entered and `OnReset` was
inert. Fix: when the exclusion would empty the array, keep the self-transition.
2- and 3-sequence graphs are byte-identical to before, so the working
`prisonSecretWall01` is unaffected.

The regression test now runs at 1, 2 and 3 sequences — it only covered the
2-sequence case, which is why this shipped.

### Sequence controlled-block ID strings are the ENGINE'S LOOKUP KEY (2026-08-02)

The engine resolves each controlled block at sequence activation BY STRING: on
node `<node_name>` find the property whose class is `<property_type>`, then its
controller of class `<controller_type>`, disambiguated by `<variable_1>`.  Our
shader-controller rewrites swapped the controller block + `controller_type` but
left Oblivion's strings — `property_type='NiTexturingProperty'` (a class that
no longer exists in the file) and `variable_1='0-0-TT_TRANSLATE_V'` — so the
lookup failed silently and the interpolator never drove the shader:
palacefont01's fountain shipped a correct V-offset curve that never played.
Vanilla convention (beehive01, blackpool, dweastrolabehub01, every entry
sampled): `property_type` = shader class name, `variable_1` =
`str(type_of_controlled_variable/color)` (`'8'`, `'11'`, …), `variable_2` = `''`.
Fixed generically in `_normalize_shader_cb_strings` (runs at the end of
`_match_seq_shader_types`).  PSys controlled-block strings were already
vanilla-identical (`var1='NiPSysBoxEmitter:0'`, `var2='BirthRate'`) — leave.

**Shared Oblivion properties → one entry per shape (2026-08-18, the Font of
Madness's upper tier).** Oblivion shares one `NiTexturingProperty` /
`NiMaterialProperty` block between several shapes and a sequence entry names
only ONE of them: palacefont01's `Water` entry drives texturing property #71,
which `Water03`, `PalaceWaterL2` and `PalaceWaterR02` also wear, so in TES4 one
entry scrolls all four. Skyrim gives every converted shape its own
`BS*ShaderProperty`, so only the named shape animated (lower tier moving, upper
frozen). `_process_controller_manager` now indexes property controllers →
wearing shapes once per manager (`_property_ctrl_index`) and, for each
retargeted texture-transform / alpha / material-colour entry, appends one entry
per sibling with a cloned controller + interpolator (`_fan_out_shared_entries`,
key data shared; `_attach_seq_shader_controllers` then hangs each off its own
shader). 33 Oblivion.esm meshes / 703 entries (oblivionwargateani02 168,
citadeldeadralordscenterring 106, obeliskenergybox01 102, se01waitingroomwalls
36).

<a id="morph-emulation"></a>
### NiGeomMorpherController does not exist in Skyrim — emulate as a shape swap (⚠ SCALE version REVERTED 2026-08-10, see notice below)

The SSE exe has NO `NiGeomMorpherController` RTTI class (only the orphaned
`NiMorphData` remains) and vanilla ships 0 uses, so morph entries HAD to be
dropped — but the morph IS the visible effect for 18 Oblivion meshes
(ctrigtripwire01's wire snap, se01waitingroomwalls, obliviongate_forming,
gnarlspawner…).  `_emulate_morphs` (fed by a harvest at the drop site in
`_process_controller_manager`) bakes each animated morph target into a sibling
copy of the shape (relative_targets → base verts + deltas) and CUTS from base
to copy where the weight curve crosses 0.5.  A smooth crossfade degrades to a
cut — the closest this engine gets.

**The cut is animated as wrapper-node SCALE, never as a NiVisController on the
geometry.**  Each shape — the base and every baked target — is wrapped in its
own `NiNode` (`"<shape> Swap"`), and the sequence drives that node's scale
1 ↔ 0 through an ordinary `NiTransformController` entry bound to the manager's
`NiMultiTargetTransformController`.  Clone wrappers rest at scale 0 (so the
authored rest pose shows only the base shape) and the clone geometry itself
ships VISIBLE; wrappers are added to the MTC's `extra_targets` and to the
manager's `NiDefaultAVObjectPalette`.  Scale keys are `LINEAR` (1) floats with
a hold key one frame (1/30 s) before each transition, which expresses the step
without touching the bool-key machinery.

> ## 🛑 REVERTED 2026-08-10 — THE SCALE SWAP FREEZES THE GAME
>
> **Everything described above and below about the wrapper-node SCALE swap is
> the state of `90d04a3`, which is NOT what the tree currently builds.**
> `_emulate_morphs` has been reverted to the pre-`90d04a3` **NiVisController**
> implementation because the scale swap hard-freezes Skyrim.
>
> ### The symptom
> Walking onto the tripwire in **Natural Caverns / `ImperialDungeon05`**
> (ref `00051AC9`, base `CGTrigTripwire01` `000CD4CC`) freezes the game: no
> crash, no crash log, nothing in the Papyrus log — the process stays alive
> and never renders another frame.  The **same mesh file** in **Vilverin**
> (ref `0006BF50`, base `CTrigTripwire01` `0004CAD9`) works perfectly, wire
> snap and all.  One `ctrigtripwire01.nif` serves both cells, so the mesh
> alone cannot explain the difference — that contradiction was never resolved.
>
> ### How it was isolated (in-game bisection, user-run)
> Each removed in turn from `output/`, one at a time:
> * long mace `ctrapswingmacelong01.nif` removed → **still froze**
> * tripwire `ctrigtripwire01.nif` removed → **no freeze**
> * tripwire restored, `ctrigtripwire01_behavior/` removed → **froze**
>   (so the animobject graph is innocent)
> * tripwire rebuilt with `_emulate_morphs` disabled → **no freeze**
>
> That last step is the definitive result: **morph emulation ON = freeze,
> OFF = no freeze.**  The trap-damage `OnTrapHitStart` scripts were also
> stripped and rebuilt separately — the freeze persisted, so the scripts are
> innocent too.
>
> ### Four fixes attempted, all failed in-game
> 1. **Move the wrappers off the MTC** (own `NiTransformController` each) —
>    still froze, and the wire stopped breaking.
> 2. **Give the entries full translation+rotation+scale key channels** —
>    still froze.
> 3. **Replace scale with a shader-ALPHA cross-fade**
>    (`BSLightingShaderPropertyFloatController`, variable 12) — no freeze
>    reported, but the wire **did not visually break**, so it is not a fix.
> 4. **Add a constant rotation key channel** so the entry reads `r=3 s=3`
>    like vanilla's `t=0 r=1 s=0` shape — still froze.
>
> ### Verified facts — do NOT re-derive these
> From the **GOG/AE** `SkyrimSE.exe` (the Steam copy is DRM-packed and
> disassembles to garbage — `tools/disasm/skyrim_disasm.py` still defaults to the
> Steam path, pass `--exe` explicitly):
> * `NiMultiTargetTransformController`: interpolator slots at `+0x48`, sized
>   `count * 0x48`, allocated at `0xd0d857`; target pointers at `+0x50`,
>   `count * 8`, zero-filled at `0xd0d91f`; `num_extra_targets` is a **ushort**
>   at `+0x58`.  Both arrays are walked **strictly by index** (`0xd0ca20`,
>   bounded by `cmp bx, word ptr [rdi+0x58]`).
> * Blend bookkeeping at `0xd0b640` walks `0x20`-byte `NiBlendInterpolator`
>   records, reading each slot's interpolator pointer and **priority byte** at
>   `+0x10` to track highest / second-highest contributor.
> * `NiTransformInterpolator`: `+0x18` translation, `+0x24` rotation quat,
>   `+0x34` scale, `+0x38` data pointer.
> * `NiTransformData`: `+0x10`/`+0x18` translation count/keys, `+0x20`/`+0x28`
>   rotation, `+0x30` scale keys, `+0x14`/`+0x24` key types.
> * `NiControllerSequence`: controlled blocks are a 32-byte stride array at
>   `+0x20`, count at `+0x18`, with a priority-ordered insertion pass at
>   `0xd08890`.  Its constructor seeds float fields with `0xff7fffff`
>   (**-FLT_MAX**) at `0xd04549`–`0xd04589`, so that sentinel is
>   **engine-native and correct** — writing real values there is wrong.
>
> Ruled out by measurement, all dead ends:
> * MTC target **count** — vanilla `alduin.nif` ships **246** targets.
> * Targets with **no driving block** — vanilla `fxnocturnalbirdl.nif` has
>   10 targets and 1 block, 9 of them NULL.
> * Missing `NiBlendTransformInterpolator` blocks — vanilla ships **0**; the
>   engine allocates them at runtime.
> * MTC identity / manager-chain shape — ours matches vanilla exactly (one
>   MTC in the chain, all blocks binding to it).
> * Degenerate `scale = 0.0` — replacing it with `1e-4` did not help.
> * Orphaned manager-chain `NiTransformController` — present **identically**
>   in the pre-`90d04a3` build that works, so it is not the cause.
> * `-FLT_MAX` statics, node/child array consistency, palette registration,
>   scene-graph reachability, clone geometry flags, controlled-block
>   priorities, the two maces' NIFs and behaviour graphs (8 bodies + 7
>   constraints each, identical graph file sizes) — all verified equal.
>
> ### The one lead never chased to a conclusion
> The two placements differ in exactly two authored ways: `XSCL` (0.75 in
> ImperialDungeon05 vs 0.71 in Vilverin) and the **persistent** record flag
> (Vilverin's ref is `0x400` persistent, ImperialDungeon05's is not).  A
> non-persistent ref whose 3D unloads/reloads while a sequence holds MTC
> interpolator slots is the only mechanism found that is consistent with
> "same file, different cell, freezes *sometimes*".  Untested.
>
> ### What "reverted" means concretely
> `_emulate_morphs` is the pre-`90d04a3` body (`git show 90d04a3^:asset_convert/nif_converter.py`),
> plus its `_init_blend_interpolator` helper which `90d04a3` had deleted.
> Output for `ctrigtripwire01.nif` is block-for-block identical to that build
> (the only delta is `BSBehaviorGraphExtraData`, added by a later, unrelated
> commit).  **Consequence: the wire does not visibly snap.**  That is the
> accepted trade — a cosmetic loss instead of a hard freeze.  The two tests
> named below still assert the SCALE design and will fail against the
> reverted code; fix them together with the real fix.
>
> **When returning to this:** the scale swap itself is not obviously illegal,
> and it demonstrably works in Vilverin.  Start from the persistence /
> ref-scale difference above, not from the mesh — the mesh has been
> exhaustively compared and is identical in both cells.

**Do NOT "restore" the NiVisController version.**  *(Superseded — see the
revert notice above; the NiVisController version is what currently ships.)*
The first implementation
toggled `NiVisController` entries aimed at the NiTriShapes themselves; it
produced NO visible swap in-game across three rounds of fixes, and the vanilla
census explains why it was never trustworthy: sequence-driven NiVisController
controlled blocks target **NiNode / NiBillboardNode / particle systems in
1852/1852 cases and a NiTriShape in ZERO**.  Meanwhile transform entries on
plain NiNodes carrying scale keys are routine (406 in a 130-file sample), and
converted transform sequences are the one animation path already confirmed
working in-game (CharacterGen's secret wall).  The scale swap therefore reuses
only proven machinery and generates no `NiVisController` /
`NiBlendBoolInterpolator` at all — which also retires both Vilverin CTDs below
for this path.  Two tests in
`tests/test_asset_convert.py::TestAnimationBlockLayout` pin it:
`test_morph_emulation_never_targets_geometry` (no vis entries are synthesized)
and `test_tripwire_morph_ships_a_scale_swap` (converts the real
ctrigtripwire01 and asserts inverse scale curves, wrapper rest scales, MTC
extra-target + palette registration, and zero surviving NiVisController).

> **Note (2026-08-10):** the "406 scale-key transform entries in a 130-file
> sample" claim above **does not reproduce**.  A 250-mesh re-census found 36
> `NiTransformData` total, only 8 with scale keys, and every one of those is
> on `skeleton.nif` at a constant `1.0` — vanilla never animates a node's
> scale, and ZERO sequence entries are scale-only.  Vanilla makes geometry
> appear/disappear mid-sequence with shader float controllers instead:
> `BSEffectShaderPropertyFloatController` (25),
> `BSLightingShaderPropertyFloatController` (17),
> `BSNiAlphaPropertyTestRefController` (4); `NiTransformController` accounts
> for 4 and none drive scale.  Treat the original census as unreliable.

Historical note — **the two CTDs the vis-swap path caused**, kept because
`_normalize_blend_interpolators` still repairs blocks COPIED from Oblivion:

1. **NiBoolData keys must be `CONST_KEY` (5), never `LINEAR` (1).**  Writing 1
   CTD'd on entering Vilverin — an access violation at `0x0` inside
   `NiBoolData::Load`, `RSI/R14 = NiBoolData*`,
   `inputFilePath: ctrigtripwire01.nif`.  Census: **3449/3449 vanilla Skyrim
   and 1296/1296 Oblivion source NiBoolData store 5**; `nif [version].xml`
   documents type 5 as "Step function.  Used for visibility keys in
   NiBoolData".  The two types are byte-identical on disk
   (`{float time, byte value}`), so the file round-trips through PyFFI and
   NifSkope cleanly and nothing but the engine notices — hence check 3 in
   `tools/validate/nif_block_type_audit.py`.
2. The Manager-Controlled flag defect below, which the same crash hunt found.

Both still apply to any NiBoolData / blend interpolator the converter copies
through; they are simply no longer reachable from morph emulation.

### NiBlendInterpolator must be Manager Controlled (2026-08-02)

Fixing the key type above moved the Vilverin CTD one block later, to
`lock inc [rax+0x08]` — an AddRef — with `RDI = NiVisController*` and
`rax = 0xBF800000421BED50`.  That high half is `-1.0f`, i.e. **float data being
dereferenced as a pointer**, which is the signature of a block read at the wrong
length.

`NiBlendInterpolator.Flags` bit 0 is **Manager Controlled**.  nif.xml makes the
next SEVEN fields (Interp Count, Single Index, High Priority, Next High
Priority, Single Time, High Weights Sum, Next High Weights Sum) conditional on
that bit being **clear** — so a manager-driven block is 7 bytes and a
free-standing one is 15.  We were writing `Flags=0` into a 7-byte block, so the
engine read 15 bytes, ran into the following block, and AddRef'd whatever it
found.  `Single Time` defaults to `-1.0f`, which is precisely the `0xBF800000`
in the faulting address.

Vanilla is unanimous: **8779/8779 `NiBlend*Interpolator` blocks store Flags=1,
Array Size=2** (2688 bool, 5520 float, 571 point3).

The underlying cause is a **PyFFI 2.2.3 broken layout** (cf. NiPSysData): it
models this block as `unknown_short` + `unknown_int` + `bool_value` instead of
`byte Flags, byte Array Size, float Weight Threshold, byte Value`.  So
`unknown_short = 0x0201` IS `Flags=1, ArraySize=2`.  These are **not padding** —
the usual "never touch unknown_*" rule does not apply, because they are real
named fields PyFFI failed to describe.  Critically this hits blocks **copied**
from Oblivion as well as synthesized ones: PyFFI reads them under the old
version's layout and rewrites them under Skyrim's, and the flags do not survive.
`_normalize_blend_interpolators` therefore stamps the header onto every blend
interpolator in the tree after all controller passes, and
`tools/validate/nif_block_type_audit.py` checks it (check 4).  261 blocks across 26
Oblivion meshes were affected — gates, magic effects, creatures and the enemy
health bar, not just the morph-swap meshes.

### Oblivion `sound:` text keys are NATIVE in Skyrim — never rewrite them (2026-08-05)
<a id="sound-text-keys-are-native"></a>

**This section previously said the opposite.  The rewrite it described silenced
244 Oblivion meshes** — every animated gate, portcullis and prison door — and
was reverted after the user reported StoneWallGateDoor01 losing its iron creak.
Confirmed fixed in-game 2026-08-05.

SkyrimSE keeps Gamebryo's own text-key sound handler.  At `0x1401db723` (GOG
build) it compares the key against the literal **`"Sound: "`** (`r8d = 7`) with
**`_strnicmp`, which is CASE-INSENSITIVE**, so Oblivion's lowercase `sound: X`
matches, and it plays whatever follows those 7 characters (`lea rcx, [rbx + 7]`
at `0x1401db890`).  The same handler also accepts `"Enum: StopSounds "`.  Both
literals sit at file offsets `0x1635f50` / `0x168d0ec`.

**The trap:** the earlier pass searched the exe for lowercase `sound:`, found
nothing, and concluded the keyword did not exist.  The string is capitalised.
Case-fold before concluding a string is absent from the exe.

`SoundPlay.<SNDR EDID>` is a DIFFERENT, non-interchangeable channel: it is
matched against a behaviour graph's declared event-name table, so it only works
on meshes that have one (38 of the 39 vanilla meshes using it carry
`BSBehaviorGraphExtraData`).  Converted doors deliberately have **no** graph —
attaching one to an Open/Close door CTDs it on cell load — so the rewritten key
matched nothing and was dropped.  Creature/actor sounds still correctly use
`SoundPlay.` because they DO go through a graph (`hkx_behavior.py`).

`_convert_sound_text_keys` is therefore a documented no-op returning 0.

### DOOR sound records: SNAM/ANAM must name an SNDR (2026-08-05)

Separate defect found in the same investigation.  TES5 `DOOR` SNAM (open) /
ANAM (close) / BNAM (loop) reference a sound **descriptor**, not a SOUN — xEdit
declares `wbFormIDCk(SNAM, 'Sound - Open', [SNDR])`, and all 90 sounded vanilla
Skyrim DOORs agree (WRDragonSideDoor01's SNAM `0005AFC9` is the SNDR
`DRSWoodImperialDouble01OpenSD`).  The converter was writing the TES4 SOUN id,
so all 417 sounded Oblivion doors held a wrong-typed reference.

DOORs are written in import Phase 1, before Phase 3 mints the descriptors, so
`convert_DOOR` stores the SOUN id as a placeholder and
`items.patch_door_sounds` resolves it afterwards — the same approach
`actors.patch_actor_sounds` uses for CSDI.  Allocating descriptor ids earlier
would shift every other generated FormID.

Oblivion also lets a door's sound live ONLY in the mesh (the record has no
SNAM/ANAM at all — StoneWallGateDoor01 and 57 other doors).  Skyrim's record
channel is what vanilla relies on, so `asset_convert/door_sounds.py` reads the
model's `Open`/`Close` sequence text keys and `items.load_door_model_sounds`
lifts those names onto SNAM/ANAM.  The sequence NAME decides the slot, so the
NIF is parsed rather than byte-scanned.

### PlayGroup chains: NEVER convert to PlayAnimationAndWait (2026-08-02)

`PlayAnimationAndWait("<seq>", "<event>")` waits on a BEHAVIOR-GRAPH event.  A
BGSGamebryoSequenceGenerator state has no completion event and NIF text keys
are not delivered as anim events — vanilla proof: every gamebryo-sequence
object script (norsarcophagustopanim01script, dunsolitudejailopencelldoor, the
Solitude jail-wall scene) uses plain `PlayAnimation` with a state debounce and
never waits; the scripts that DO wait (`sarcophagusskulllock01script`
"alldone", `dunlabyanimateontrig` "done") drive native-hkx objects whose
events are havok annotations.  The wait blocks its thread forever.  Consecutive
same-frame PlayGroups therefore stay plain `PlayAnimation` calls (last event
wins — which also matches Oblivion's own queue-depth-1 PlayGroup semantics).

- BSXFlags must have bit 0 set (ANIMATED) → value 139 (0x8B) for animated meshes. Detect via NiControllerManager on root.
- Animation data: NiControllerSequence StringPalette offsets MUST be resolved BEFORE version upgrade (UV2=11→83). After upgrade, PyFFI switches to direct-string mode and offsets are ignored → empty node_name → crash.
- **EVERY `NiTimeController` needs "Compute Scaled Time" (flags bit 6, 0x40) or a `PlayAnimation()`d sequence NEVER MOVES — the CharacterGen secret-wall fix (2026-07-26, `_fix_controller_flags`)**: `nif.xml` `TimeControllerFlags` declares bit 6 `default="true"`, and Oblivion's engine computed scaled time unconditionally without ever writing the bit — every controller in the Chargen secret-wall/switch NIFs stores 12 / 40 / 44, always 0x40 **clear**. Skyrim reads the flag: the sequence binds its targets, `ObjectReference.PlayAnimation("Forward")` returns success and logs **no Papyrus error**, but scaled time never advances so the object sits on frame 0 forever. Symptom was maximally misleading — the quest stage said the wall had opened (the switch fired, `secretDoor` flipped 0→1, the timer ran; all visible in the `TES4CharGen` user log) while the wall physically stayed shut. Census: across 62 vanilla animated door/activator meshes (Windhelm animated secret doors, Nordic animated doors, Dwemer doors, Labyrinthian panel, Winterhold anim door) **157/157 `NiMultiTargetTransformController` have flags=108 (0x6C)** and every other controller — `NiTransformController`, `NiControllerManager`, `NiVisController`, `NiFloatExtraDataController` — has **76 (0x4C)**; both set 0x40, and 108 vs our 44 differs *only* in this bit. Fix ORs 0x40 into every `NiTimeController` in the tree before the version upgrade, so activators/doors/traps/levers are all covered. This generalizes the emitter-controller rule below (which had it only for `NiPSysEmitterCtlr`/`NiPSysUpdateCtlr`) and the `0x48` already hardcoded on the flip-book `BSEffectShaderPropertyFloatController`.
  - **Things that were NOT the cause** (all verified fine, don't re-investigate): the Papyrus conversion (`PlayGroup` correctly routed to `PlayAnimation` via base-signature lookup); the dropped `prisonSecretWall01`/`... NonAccum` controlled blocks (genuinely empty — `data=None`, zero translation — the real motion is on the `bed`/`wall` transform tracks, which survive with all 111/21 keys); the missing `NiStringPalette` (correct — Skyrim uses direct strings); sequence names `Forward`/`Backward` (vanilla `VolunruudLeftDoor`/`RightDoor` use exactly these); and the absent ACTI `PNAM`/`FNAM` (marker colour + flags, cosmetic — 1739/1753 vanilla write FNAM=0).
  - **CORRECTION (2026-07-26): the "needs no BGED" claim previously recorded here was WRONG.** The earlier note reasoned that because 227 ACTI + 196 DOOR vanilla records ship `NiControllerManager` meshes, the in-NIF sequence was sufficient. That census is real but does not support the conclusion: `ObjectReference` exposes **two different animation paths** — `PlayGamebryoAnimation` drives an in-NIF `NiControllerSequence`, while **`PlayAnimation`/`PlayAnimationAndWait` drive the BEHAVIOUR GRAPH and require an animation graph manager**, which exists only when the root carries a `BSBehaviorGraphExtraData` naming an hkx project. `PlayGroup` converts to `PlayAnimation`, so without a BGED the call is accepted, returns immediately, logs no Papyrus error, and nothing moves. Fixed by generating the graph (below).

## Animated-object behaviour graphs (`asset_convert/hkx_animobject.py`)
<a id="animated-object-behaviour-graphs"></a>

**ONLY meshes whose sequences carry SCRIPT-DRIVEN group names get a GENERATED graph** — `Forward`, `Backward`, `FastForward`, `FastBackward`, `Left`, `Right`, `Equip`, `Unequip`, `SpecialIdle`, `Stagger` (`_SCRIPT_DRIVEN_SEQUENCES` in `nif_converter.py`; 161 trees on Oblivion.esm). Ambient `AutoPlay`/`AutoLoop` meshes point at vanilla's shared `GenericBehaviors\Autoplay.hkx` instead (next section). Generated by `collect_sequence_names` + `_add_animobject_bged`. Layout, sibling to the mesh so two animated NIFs in one folder never collide:

    <model>_behavior/<model>.hkx            project    (this is what BGED names)
    <model>_behavior/Characters/Character01.hkx
    <model>_behavior/CharacterAssets/Skeleton.hkx      1-bone; transforms live in the NIF
    <model>_behavior/Behaviors/Behavior00.hkx          state machine + Gamebryo generators

The bridge is **`BGSGamebryoSequenceGenerator`**, whose `pSequence` names a NIF `NiControllerSequence`. Each sequence becomes one state AND one same-named event, so `PlayAnimation("Forward")` sends `Forward` and lands on the generator bound to the `Forward` sequence, plus a synthetic **`Rest`** start state. BSX bit 0 (Animated) must also be set or the graph loads and never ticks.

### 🛑 A NIF CANNOT BE HOT-RELOADED — the engine caches it for the whole process

Skyrim parses each NIF once and keeps it in its model cache for the lifetime of
the process. `coc` out and back unloads the CELL, not the model: the reload
hands back the cached copy, so **overwriting the mesh on disk while the game is
running changes nothing.** There is no console command that drops it (`pcb`
purges the cell buffer, not the model cache).

Consequence for debugging: **every in-game observation describes the build that
was on disk when the game LAUNCHED.** Rebuilding a mesh mid-session and
re-entering the cell tests the OLD file and silently produces a "the fix did
nothing" result. On 2026-08-18 this invalidated several rounds of ambient-mesh
testing before it was noticed.

So: deploy the mesh, THEN relaunch, then test. One build per launch. Verify the
DEPLOYED file (`Data\meshes\...`, not just `output/`) before asking for a
relaunch — a wasted launch costs a full play cycle.

Note the deploy is hardlinked here (Vortex): writing `output/...` updates
`Data/...` in place, same inode. That makes deployment instant and is easy to
mistake for a working hot reload. It is not one.

### Ambient (self-playing) meshes: the vanilla AutoPlay/AutoLoop pair (2026-08-18, mechanism read out of the live engine)

Oblivion authors ambient scenery (arena spectator crowds, `palacefont01`'s
fountain, `watersurf01`, candle flames) as a STAT whose NIF holds a self-playing
`Idle` sequence. TES4 starts `Idle` on load; Skyrim starts nothing. What vanilla
does instead, and what `_autoplay_ambient_sequences` now emits:

- BGED → **`GenericBehaviors\Autoplay.hkx`**, the shared graph every one of the
  63 vanilla self-playing meshes points at (`hkx_animobject` returns it for
  meshes whose sequences are only ambient; anything a script drives by name
  keeps its generated project). Its state machine STARTS on `AutoplayState`
  (sequence **`AutoPlay`**), and on that sequence's `End` event hands off to
  `AutoLoopState` (sequence **`AutoLoop`**). Its events are `End`, `StopEffect`,
  `AutoOneOff`, `Reset`, `AutoReset` — `AutoPlay`/`AutoLoop` are sequence names,
  never events.
- The authored `Idle` becomes **`AutoLoop` and keeps its authored cycle type**;
  a full-length **`AutoPlay` clone with cycle type CLAMP** is added for the
  start state (shares the interpolators — the engine binds the same pointers
  from both and plays).
- **CycleType is 0 = LOOP, 1 = REVERSE, 2 = CLAMP.** All 116 Oblivion `Idle`
  sequences are 0. Vanilla: `AutoPlay` CLAMP 53/54, `AutoLoop` LOOP 39/53.
  Looping is the SEQUENCE's — `BGSGamebryoSequenceGenerator` has no looping
  field and `AutoLoopState` has no self-transition.

**Why it took ten builds** (all read back out of the running game with
`tools/live/nif_live.py sequences|nodes` on a loaded arena spectator, then patched
in memory with `set-cycle` / `set-pose` + `sae AutoReset` to prove each fix
before rebuilding):

1. *"Plays one cycle then freezes"* — the converter had `_CYCLE_LOOP = 2`, i.e.
   it wrote CLAMP into `AutoLoop`. Live: `Autoplay` state INACTIVE (finished,
   `End` fired), `AutoLoop` state ANIMATING with lastTime far past its end —
   frozen on the last frame. Flipping the loaded sequence's `cycleType` to 0 and
   `sae AutoReset` made it loop indefinitely.
2. *"Rotated ~90°"* — Bethesda's exporter writes the sequence's **accum root**
   (`Bip01`, `DoorLowerINT01`, `MetalGate`…) as an IDENTITY pose and moves the
   node's real transform onto the **`<accum> NonAccum`** child (crowd:
   NonAccum key 0 = Bip01's authored (−0.34,−1.64,64.07) / 82.5° Z; census of
   464 Oblivion NIFs: 853 accum-root entries, 815 identity poses, 0 that
   move). Both engines apply the identity and NonAccum restores the world
   pose. Our data-less sentinel rule (rotation/scale → −FLT_MAX) left Bip01's
   authored 82.5° in place while NonAccum re-applied its own 82.5° → the crowd
   faced 165° off whenever the sequence actually played, and was correct only
   in builds where nothing played. `_accum_root_mode` now leaves a
   'transferred' accum-root entry exactly as authored (identity applied) and
   sentinels every channel only for an 'orphan' (nothing carries the
   transform). Live: Bip01 → identity, NonAccum → (−0.34,−1.64,~64) at 82.5°,
   crowd at the authored pose, looping.

Ruled out along the way, do not retry: STAT→MSTT promotion (crashed on save
load — 94 vanilla STATs carry a BGED, the record type is not the gate);
`selfTransitionMode` FORCE_TRANSITION_TO_START_STATE (looping is the
sequence's); a generated per-mesh graph instead of the shared one (works, but
the shared one is what vanilla ships and what is verified live).

**`sae <event>` is the fastest first diagnostic** for any "animated object does
nothing": empty reply = the graph is bound and knows the event; *"not processed
by the graph"* = no such event or no graph. Then `python tools/live/nif_live.py
sequences <ref>` (state, cycleType, lastTime per sequence) and `nodes <ref>
--names ... --samples N` (is the bone moving, and where is it) instead of
theorising.

### The four defects that had to be fixed before it worked in-game (2026-07-26, all CONFIRMED)

Every one was invisible to structural inspection **and to NifSkope, which renders and animates the NIF perfectly while never loading the hkx at all** — so "it's fine in NifSkope" tells you nothing about any of these. In symptom order:

1. **BGED must NOT carry a `meshes\` prefix — the object is otherwise NEVER RENDERED.** The engine prepends `Meshes\%s` itself, so `meshes\tes4\…` resolves to `Meshes\meshes\tes4\…`, the project is never found, and the object silently gets no graph and never draws. Vanilla stores `Clutter\BlackPool\BlackPoolSecretDoor\NocturnalsSecretDoor01.hkx`; our own working bow rig stores `Weapons\Bow\BowProject.hkx`. **The path is relative to `meshes\`, not to `data\`.**
2. **The skeleton's bone must be the fixed dummy name `x_SingleBone`, never the model stem.** The rig is a placeholder (the real motion is in the NIF's sequences), and vanilla's `SingleBoneSkeleton.hkx` uses that reserved name precisely so it can never collide with a NIF node. Naming it after the model made the engine bind the graph's identity bind pose onto the object and place it **far from its authored worldspace position**.
3. **`startStateId` must point at a state that plays NOTHING.** Vanilla starts on an idle (`BlackPoolSecretDoor` `startStateId=3` = `AnimIdle01`) and reaches the motion only by event. Oblivion sources have no idle sequence — a converted wall has only `Forward`/`Backward` — so starting on state 0 made the wall **swing open by itself the instant the cell loaded**. Fix: synthesise a `Rest` state whose `pSequence` is empty (it holds the NIF's authored rest pose = closed) and start there. It is the LAST state, so the event→stateId mapping of the real sequences is untouched.
4. **Transitions must live ON EACH STATE, not only in the machine's `wildcardTransitions`.** Vanilla's Gamebryo state machine sets `wildcardTransitions=null` and gives every state its own `hkbStateMachineTransitionInfoArray` (`State00` carries event 0 → state 4). Leaving `Rest.transitions = null` made the start state a **DEAD END**: nothing could open the wall again, from the quest *or* from console `activate`. Each state now reaches every *other* sequence (self-transitions excluded, or a repeated event restarts the sequence mid-play); the global wildcard array is kept as a harmless second route.

**`Open`/`Close` MUST NOT get a graph — attaching one is a CTD (2026-07-26).** They are the engine's own DOOR group names, driven natively through the NIF's `NiControllerManager`; no converted script ever names them (census of 18,566 output scripts: Forward 418, Backward 192, Unequip 45, Equip 27, SpecialIdle 10, FastForward 8, Left 6, FastBackward 6, Right 5, Stagger 1 — **zero Open/Close**). `prisonCellGate01` animated perfectly before the graph existed; giving it one made the engine bind the sequence through the graph instead of natively and crash on cell load (`EXCEPTION_ACCESS_VIOLATION`, `movdqu xmm2,[rax]` with `rax=0`, relevant objects `BGSGamebryoSequenceGenerator "GamebryoSequenceGenerator00"` + `hkbBehaviorGraph "prisoncellgate01"`). Vanilla agrees: the graph-driven `NocturnalsSecretDoor01` uses `AnimIdle01`/`AnimPlay01`, never Open/Close. **A mesh that already animates is not a mesh that needs a graph — check whether a script actually drives it first.**

**Template: vanilla `NocturnalsSecretDoor01`** — `Behaviors/Behavior00.hkx` ships loose at `references/Skyrim Animations/meshes/clutter/blackpool/blackpoolsecretdoor/`, the NIF at `references/Skyrim Meshes/meshes/clutter/blackpool/blackpoolsecretdoor/nocturnalssecretdoor01.nif` (BGED + BSX 0x0B + a 12-object `NiDefaultAVObjectPalette`). Decompile with `hkx_xml.decompile_hkx` and match field-for-field. Traps found the hard way:

- **hkxcmd fails SILENTLY on a malformed packfile**: it prints `Converting '...'`, exits non-zero, and writes **no file and no error text**. A missing or extra param is indistinguishable from any other failure, so bisect against the decompiled vanilla file rather than guessing.
- **`BGSGamebryoSequenceGenerator` takes exactly `pSequence`, `eBlendModeFunction`, `fPercent`.** The class also declares `bLooping`/`bDelayedActivate`/`fTime`/`events`, and they appear in hkxcmd's own field-name table in the exe — but vanilla marks them **`SERIALIZE_IGNORED`** and emitting them breaks the compile. *The exe's class definition lists fields that must not be written; only the decompiled vanilla file distinguishes them.*
- **`hkbBehaviorGraphData` needs `wordMinVariableValues` + `wordMaxVariableValues`** between `eventInfos` and `variableInitialValues`.
- **`eventToSendWhenStateOrTransitionChanges` is a nested `hkobject` (`{id:-1, payload:null}`), not `null`**; likewise `triggerInterval`/`initiateInterval` are nested `hkbStateMachineTimeInterval` structs (all `-1`/`0.0`), not tuple literals — `param_structs` renders values inline and cannot express either, so they are built with `param_raw`.
- **`hkbBlendingTransitionEffect.flags` is the integer `0`**, and `selfTransitionMode` is the full `SELF_TRANSITION_MODE_CONTINUE_IF_CYCLIC_BLEND_IF_ACYCLIC`.
- Class signatures for all of these are registered in `hkx_xml.SIGNATURES`, read off the vanilla file.
- Sequences that `_process_controller_manager` stripped to zero controlled blocks are **excluded** — a state for a dead sequence makes `PlayAnimation()` succeed while animating nothing, reintroducing the original silent failure.
- **The skeleton needs exactly ONE `referencePose` entry per bone, emitted ONCE.** `HkxPackfile` happily writes a duplicate `hkparam` and hkxcmd keeps the **FIRST**, so an empty `referencePose` emitted before the real one yields a skeleton with 1 bone and 0 poses; binding a sequence then indexes past the end and null-derefs (this was the second half of the prisonCellGate01 CTD).
- **hkxcmd compiles the identity pose into a ZERO QUATERNION — patch the bytes (`_fix_identity_quat`).** The XML text `(0 0 0)(0 0 0 1)(1 1 1)` is exactly what every shipped creature skeleton uses, but for this file hkxcmd writes the rotation slot as all zeros. A zero quaternion is not a rotation, so the single bone the graph drives has no valid bind pose and **the entire object renders nothing** — while the graph loads without error and no Papyrus message appears. Havok's **binary** quaternion is **w-first** `(1,0,0,0)`, unlike the XML's xyzw, so the fix rewrites the 48-byte pose block (trans/quat/scale hkVector4 slots) in the compiled WIN32 file, before the AMD64 step. Verified **byte-identical to vanilla `clutter\beehive\characterassets\SingleBoneSkeleton.hkx`** (1104 bytes, 0 diffs) — that file is the reference for any single-bone animated object.
- **`hkbCharacterData`'s field list is not what the name suggests** — copy `clutter\beehive\characters\Character00.hkx`: `characterControllerInfo, modelUpMS, modelForwardMS, modelRightMS, characterPropertyInfos, numBonesPerLod, characterPropertyValues (this is where the hkbVariableValueSet hangs), footIkDriverInfo (null POINTER, not an array), handIkDriverInfo (null), stringData, mirroredSkeletonInfo, scale`. There is **no `variableInitialValues` and no `aiControlDriverInfo`**. Getting it wrong made hkxcmd silently drop the `hkbVariableValueSet` — detectable by diffing the packfile's `__classnames__` string table against vanilla's, which is a fast sanity check for any generated hkx.
- Vanilla lays these files out in the mesh's OWN folder (`clutter\beehive\{behaviors,characters,characterassets}\`), not a `<stem>_behavior\` subfolder; ours nests them so two animated NIFs in one directory cannot collide on `Character01.hkx`. Both work — the paths inside the character file resolve relative to the project file's folder. Our project hkx is byte-identical to vanilla's (880 bytes).
- Final step is `convert_hkx_to_amd64` on every file: SSE loads only 64-bit packfiles (verified pointer-size byte 8 on all 161×4 outputs).
