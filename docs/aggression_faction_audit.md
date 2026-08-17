# Aggression / Ally / Enemy Conversion Audit

**Date:** 2026-08-16
**Scope:** The full TES4→TES5 conversion path for actor aggression (AIDT),
faction relations (XNAM / Group Combat Reaction), crime factions, and the
script-side faction-reaction and combat calls.
**Status:** Analysis only — no code changes were made. Nothing here has been
fixed.

Every number in this document was measured during the audit. The "converted"
figures were read from the **built binary ESMs** in `output/`, not simulated.
Ground truth for vanilla is `references/Skyrim.esm`; TES4 source is
`export/<plugin>/`.

---

## Contents

| # | Finding | Severity |
|---|---|---|
| 1 | [Master-export blindness in the faction loader](#1) | Critical |
| 2 | [`_faction_reaction_call` contradicts its own docstring](#2) | Critical |
| 3 | [Assistance tier 2 is unreachable](#3) | Critical |
| 4 | [Morality is 43–48 points off vanilla](#4) | Critical |
| 5 | [`_predator_attack_radius` does not exist](#5) | Significant |
| 6 | [Nehrim gets zero crime factions](#6) | Significant |
| 7 | [TES4 RACE `Relation[]` is dropped entirely](#7) | Significant |
| 8 | [Aggression tier 3 unreachable; creatures over-shoot tier 2](#8) | Minor |
| 9 | [Oblivion XNAM Neutral is 4.3× vanilla](#9) | Minor |
| 10 | [FACT override merge gap](#10) | Latent |
| 11 | [Confidence calibration](#11) | Minor |
| 12 | [Mood is hardcoded 0](#12) | Known-empty |

An appendix records a [separate defect found while investigating the
CharacterGen assassin](#appendix), which is **not** an aggression/faction bug
and is not counted above.

---

## Reference census

Baseline distributions used throughout. Vanilla Skyrim NPC_ AIDT, n=5118 (all
have AIDT, size 20):

| Field | Distribution |
|---|---|
| Aggression | 0: 2488 (48.6%) · 1: 739 (14.4%) · 2: 1889 (36.9%) · 3: 2 (0.0%) |
| Confidence | 0: 292 (5.7%) · 1: 90 (1.8%) · 2: 1730 (33.8%) · 3: 393 (7.7%) · 4: 2613 (51.1%) |
| Morality | 0: 1723 (33.7%) · 1: 99 (1.9%) · 2: 4 (0.1%) · 3: 3292 (64.3%) |
| Mood | 0: 4703 (91.9%) · 1–7: 415 (8.1%) |
| Assistance | 0: 2019 (39.4%) · 1: 1026 (20.0%) · 2: 2073 (40.5%) |
| AggroRadiusBehavior | 0: 4957 (96.9%) · 1: 161 (3.1%) |
| Energy | 50 in 4931 (96.3%) |

Warn / Warn+Attack / Attack U32 are nonzero in 113 (2.2%) / 215 (4.2%) /
226 (4.4%).

Vanilla Skyrim FACT XNAM — 1084 FACT records, 388 (35.8%) carry XNAM,
1036 relations total:

| Reaction | All 1036 | Self (200) | Non-self (836) |
|---|---|---|---|
| 0 Neutral | 69 (6.7%) | 3 (1.5%) | 66 (7.9%) |
| 1 Enemy | 302 (29.2%) | 8 (4.0%) | 294 (35.2%) |
| 2 Ally | 317 (30.6%) | **160 (80.0%)** | 157 (18.8%) |
| 3 Friend | 348 (33.6%) | 29 (14.5%) | 319 (38.2%) |

XNAM Modifier is nonzero in 1 of 1036 (0.1%).

TES4 source, for comparison:

- **Oblivion.esm** — NPC_ 2482, CREA 914, FACT 476. FACT relations 660:
  ≤−50 → 153 (23.2%), −49..49 → 189 (28.6%), ≥50 → 318 (48.2%); self-relations
  216 (32.7%).
- **Nehrim.esm** — NPC_ 1737, CREA 734, FACT 204. FACT relations 277:
  ≤−50 → 67 (24.2%), mid → 18 (6.5%), ≥50 → 192 (69.3%); self-relations 69,
  all ≥50.

`DATA.Personality` is present on all 3396 Oblivion and all 2471 Nehrim actors
(NPC_ and CREA alike), so the `get_int(..., 50)` default in `_player_disposition`
is never exercised.

---

<a id="1"></a>
## 1. Master-export blindness in the faction loader — Critical

`tes5_import/import_main.py:1459-1460`:

```python
from .record_types.actors import load_faction_player_reactions
load_faction_player_reactions(by_type)
```

`by_type` only. `load_faction_player_reactions` (`actors.py:553`) reads
`by_type.get('FACT', [])` at line 569 and nothing else. `ctx.master_export`
appears **nowhere** in `actors.py`.

This is an oversight rather than a design choice — the loaders immediately
around it are master-aware:

- `import_main.py:1452` — `load_item_index(by_type, ctx.master_export if ctx else None)`
- `import_main.py:1358` / `1370` — same pattern
- `outfits.py:113-114` documents master_export as "REQUIRED for a plugin with masters"

`load_faction_player_reactions` is the only Phase-0 loader in that block missing
it. `_load_crime_factions` (line 539, called at 568) is blind the same way.

The worker path is not a factor: `convert_worker.py` handles only
CELL/REFR/ACHR/LAND (lines 63-68) and has no faction code — NPC_/CREA convert in
the parent, so the module globals are live. The defect is purely the missing
master data.

### Measured impact

| Plugin | Actors w/ factions | Hitting master-only factions | Distinct missing FACTs | Resolvable from master |
|---|---|---|---|---|
| Morrowind_ob.esm | 3426 | **598** | 11 | 11 (100%) |
| ElsweyrAnequina.esp | 823 | **265** | 38 | 38 (100%) |
| Chargen/Transport.esp | 2 | 2 | 5 | 5 (100%) |
| Nehrim.esm | 1924 | 0 | 0 | — |
| Oblivion.esm | 3060 | 0 | 0 | — |

Nehrim and Oblivion are standalone, so they are clean as expected. Every missing
faction is resolvable from `export/Oblivion.esm/FACT.txt` — the data is present
and simply not consulted.

Two distinct failure modes:

**Prey misclassification.** `_is_prey()` (`actors.py:603`) matches EditorIDs
containing "prey". Vanilla `Prey` (`0x05D556`) exists only in Oblivion.esm.
**53 ElsweyrAnequina actors** belong to it and are silently classified
non-prey. Per the function's own docstring, prey members carry aggression up to
100, so aggression alone cannot exclude them — these horses/deer/sheep convert
as hostile.

**Lost disposition.** `_player_disposition()` (`actors.py:617`) sums
`_FACTION_PLAYER_DISP`. Actors whose missing faction carries a real
PlayerFaction relation: **11** (Morrowind_ob), **18** (ElsweyrAnequina). Ten
Morrowind_ob and twelve Elsweyr actors are in `PlayerFaction` (`0x01DBCD`)
itself, and one in `AdventurerFaction` — both have player relations in the
master. Given `_ONSIGHT_MARGIN = 10` (`actors.py:600`), a lost negative faction
term shifts `margin = (aggression - 5) - disposition` and flips the tier.

### Fix sketch

One-line omission at `import_main.py:1460` — pass `ctx.master_export` the way
line 1452 does, and extend `load_faction_player_reactions` /
`_load_crime_factions` to iterate master FACT records.

---

<a id="2"></a>
## 2. `_faction_reaction_call` contradicts its own docstring — Critical

`script_convert/converter.py:5263-5271` states plainly:

> Positive amounts stop at Friend and never reach Ally. […] promoting its
> positive amounts to Ally wired bystanders into other people's fights. Ally is
> reserved for a faction's relation to itself.

The code three lines below does the opposite:

```python
line 5312:  return f'{f1}.SetAlly({f2}, true, true)'   # is_mod,  amount > 0
line 5319:  return f'{f1}.SetAlly({f2}, true, true)'   # literal, amount > 0
```

Neither branch can ever emit Friend. Measured in the built output:
**247 `SetAlly` calls vs 80 `SetEnemy`**, of which 220 are
`SetAlly(PlayerFaction, true, true)`.

Most-common generated calls:

```
220  SetAlly(PlayerFaction, true, true)
 29  SetEnemy(PlayerFaction, false, false)
 14  SetEnemy(PlayerFaction, true, true)
 10  SetAlly(d0Nerevarine, true, true)
  5  SetEnemy(MythicDawnCG, false, false)
```

The mirror path inherits the same bug:
`script_convert/static_scripts/TES4Polyfill.psc:280` — `aiMode == 2` is
documented as "friend" in the comment at line 264 and calls `SetAlly`.

This is the exact regression the **record** path was fixed for
(`tes5_import/record_types/actors.py:1577-1594`, which explains how a +100
BladesCG→MythicDawnCG relation converted to Ally turned the Emperor's guards
against the player). The script path never received the same fix.

---

<a id="3"></a>
## 3. Assistance tier 2 is unreachable — Critical

`tes5_import/record_types/actors.py:777`:

```python
tes5_assist = 1 if resp >= 30 else 0
```

Tier 2 (HelpsFriendsAndAllies) is never emitted.

| Assistance | Skyrim.esm | Oblivion built | Nehrim built |
|---|---|---|---|
| 0 HelpsNobody | 39.4% | 51.2% | 48.3% |
| 1 HelpsAllies | 20.0% | 48.8% | 51.7% |
| 2 HelpsFriendsAndAllies | **40.5%** | **0** | **0** |

Assistance is half of the "who joins a fight" decision (UESP Skyrim:Factions —
reaction combines with aggression *and assistance*). With tier 2 absent, no
converted actor ever assists a **Friend**. This interacts directly with finding
2: the script path writes Ally everywhere, which is the only reaction the
record-side actors can respond to.

Underlying TES4 responsibility distribution:

- Oblivion: 0 → 1254, 50 → 912, 100 → 634, 10 → 170, 90 → 97, 80 → 74
- Nehrim: 0 → 1146, 50 → 850, 100 → 313, 80 → 83, 30 → 26

---

<a id="4"></a>
## 4. Morality is 43–48 points off vanilla — Critical

`tes5_import/record_types/actors.py:775`:

```python
tes5_moral = 3 if resp >= 80 else (2 if resp >= 50 else (1 if resp >= 30 else 0))
```

| Morality | Skyrim.esm | Oblivion built | Nehrim built |
|---|---|---|---|
| 0 AnyCrime | 33.7% | 51.2% | 48.3% |
| 1 ViolenceAgainstEnemies | 1.9% | 1.0% | 1.2% |
| 2 PropertyCrimeOnly | **0.1%** (4 recs) | **26.3%** | **34.6%** |
| 3 NoCrime | **64.3%** | **21.5%** | **15.9%** |

The single largest deviation in the audit. Tier 2 is effectively unused by
Bethesda — 4 records out of 5118 — yet receives a third of converted actors,
while NoCrime (vanilla's clear majority) is undersupplied by roughly 3×.

Cause: TES4 responsibility 50 is the modal value (912 Oblivion / 850 Nehrim
actors) and the `>= 50` threshold routes all of them to the one tier vanilla
never uses.

---

<a id="5"></a>
## 5. `_predator_attack_radius` does not exist — Significant

`tes5_import/record_types/actors.py:713-718` explains that Skyrim discriminates
a passive-but-territorial predator using AIDT's Aggro Radius fields (EncWolf is
Aggression 0 but carries `aggroRadiusBehavior=1`, attack radius 1500), that
TES4's AIDT has no such field, and concludes:

> The discriminator therefore has to be reconstructed; see
> `_predator_attack_radius` below.

**There is no such function.** Grep across `tes5_import/` and `script_convert/`
returns exactly one hit — that comment. The AIDT tail is hardcoded
(`actors.py:782-783`):

```python
0, 0,        # aggro radius, unused
0, 0, 0)     # warn, warn/attack, attack
```

All three U32s and AggroRadiusBehavior are zero across **3838 Oblivion** and
**2537 Nehrim** built records. Vanilla writes AggroRadiusBehavior on 3.1% and
the Attack radius on 4.4%.

This is the CLAUDE.md caution about docs describing fixes that were never
implemented — here the stale description lives in a source comment rather than
a doc.

---

<a id="6"></a>
## 6. Nehrim gets zero crime factions — Significant

`tes5_import/record_types/actors.py:535-536` derives the crime-faction set by
regexing scripts for `Get/SetPCFaction{Murder,Attack,Steal}`:

```python
_CRIME_FN_RE = re.compile(
    r'\b(?:get|set)pcfaction(?:murder|attack|steal)\s+([A-Za-z0-9_]+)', re.I)
```

Measured results:

| Plugin | Crime factions found |
|---|---|
| Oblivion.esm | 6 (`blackwoodcompanyfaction`, `darkbrotherhood`, `fightersguild`, `icwaterfrontresident`, `magesguild`, `thievesguild`) |
| Morrowind_ob.esm | 9 |
| **Nehrim.esm** | **0** |

Nehrim never uses the `PCFaction` family. But it has **204 FACT records
including ~15 `…Wachen` (guard) factions** — `CitySarnorWachen`,
`CityCahbaetWachen`, `CityDarlanWachen`, `CityErothinWachen`,
`CityFurtsandenWachen`, `MQ29Tempelwachen`, … — and its scripts make **46
crime-gold calls**:

```
25  SetCrimeGold
 9  ModCrimeGold
 6  GetCrimeGold
 5  Setcrimegold
 1  setcrimegold
 1  SetCrimegold
```

Track Crime (DATA bit 6) gates the CRVA branch at `actors.py:1656`, so every
Nehrim faction receives the all-zeros CRVA. `GetCrimeGold()` therefore returns 0
forever and no guard ever arrests.

This is the same defect the CRVA comment at `actors.py:1638-1654` says was
already fixed, reached through a different door: the detector's vocabulary is
too narrow. Widening it to the crime-gold family (`SetCrimeGold`,
`ModCrimeGold`, `GetCrimeGold`) would cover Nehrim generically.

---

<a id="7"></a>
## 7. TES4 RACE `Relation[]` is dropped entirely — Significant

TES4 RACE records carry faction relations that feed disposition. No converter
reads them: `Relation` / `XNAM` appears in no record-type module except
`actors.py`, and `convert_RACE` emits none.

| Plugin | RACE relations | Values |
|---|---|---|
| Oblivion.esm | 1 | negligible |
| **Nehrim.esm** | **67** | −5 (×26), +2 (×16), +5 (×7), −12 (×6), +10 (×6), −10 (×5), 0 (×1) |

TES5 RACE has no XNAM slot, so these cannot transfer directly — but they are
also not folded into `_player_disposition()`, which reads only `Faction[i]`. For
Nehrim that is a systematically missing disposition term feeding the tier
decision in finding 8.

---

<a id="8"></a>
## 8. Aggression tier 3 unreachable; creatures over-shoot tier 2 — Minor

The `aggr >= 106` branch (`actors.py:737`) never fires — the maximum observed
TES4 aggression is 100. Zero Frenzied actors in either build; vanilla has 2.
Harmless, but it is dead code.

More substantive is the split by record type:

| | Skyrim | Oblivion built | Nehrim built |
|---|---|---|---|
| Aggression 0 | 48.6% | 56.7% | 39.3% |
| Aggression 1 | 14.4% | 11.5% | 15.1% |
| **Aggression 2** | **36.9%** | **31.8%** | **45.6%** |
| Aggression 3 | 0.04% | 0 | 0 |
| …NPC_ only, tier 2 | — | 25.9% | 37.0% |
| …**CREA only, tier 2** | — | **63.2%** | **70.2%** |

Overall Oblivion is well-calibrated (within ~5 points); Nehrim runs ~9 points
high. The excess is almost entirely **creatures**.

The cause is visible in the source: **685 of 914 Oblivion CREA carry
`Personality=10`** (211 at 50, 8 at 90). With `disp` that small,
`(aggr-5) - disp >= 10` passes for nearly any nonzero aggression.
`_ONSIGHT_MARGIN` was calibrated across a mixed population but effectively only
binds on NPCs.

---

<a id="9"></a>
## 9. Oblivion XNAM Neutral is 4.3× vanilla — Minor

| Reaction | Skyrim (1036) | Oblivion built (662) | Nehrim built (279) |
|---|---|---|---|
| 0 Neutral | **6.7%** | **28.5%** | 6.5% |
| 1 Enemy | 29.2% | 23.4% | 24.7% |
| 2 Ally | 30.6% | 22.2% | 24.7% |
| 3 Friend | 33.6% | 25.8% | 44.1% |

Driven by the 189 Oblivion relations in the −49..49 dead band, of which **69 are
self-relations** that fall to Neutral. Vanilla self-relations are 80% Ally
(160/200) and only 1.5% Neutral.

A faction that is Neutral to itself will not assist its own members — which
compounds finding 3. Converted self-relations land on Ally in 147/216
(Oblivion) and 69/69 (Nehrim); Oblivion's remaining 69 become Neutral.

Nehrim is unaffected because all 69 of its self-relations are ≥50.

XNAM Modifier is 0 in 100% of converted relations, correctly matching vanilla's
1035/1036.

---

<a id="10"></a>
## 10. FACT override merge gap — Latent

FACT has no entry in `_REBUILDERS`, `_PATCHERS`, or `_RUN_REBUILDERS`
(`override_builder.py`: zero hits for `'FACT'`), and is not in
`_NO_GENERIC_CONVERT` (lines 485-494). So `Relation[]` falls through to
`generic` → `generic_substitutions` (line 566) → `_apply_generic` (line 1208),
which replaces the entire XNAM run as a unit — per its own docstring, "Each
signature is replaced as a UNIT". `diff_records` (`export_diff.py:114`) reports
only `{'Relation[]': True}`, a boolean flag rather than content, and the
substitution comes from `convert_FACT(plugin_rec)` alone. **There is no merge
with the master's relations.**

Running the real code path (`diff_records` + `apply_changes` + `convert_FACT`):

- Full-list override (plugin repeats master's 3, adds 1) → 4 XNAMs, correct
- Delta-only override (plugin lists only the 1 new) → **1 XNAM; the master's 3
  are lost**

A census of all plugins against Oblivion.esm's 476 FACTs found exactly **one**
real override — `Translation.esp` `0004B90C NecromancerDungeon`:

```
master:  0000A951/100  0009DB1F/100  0000C0F2/100  0004B90C/100   (4)
plugin:  0009DB1F/100  00194B59/100  0004B90C/100                 (3)
```

`0000A951` and `0000C0F2` are dropped. Whether that is a defect depends on
Oblivion's override semantics: TES4 override records normally carry the complete
list, so this specific case is likely correct authoring.

All other plugins have 0 overrides of master FACTs (Morrowind_ob 66 own / 0
overrides; ElsweyrAnequina 136 / 0; Pelletine 11 / 0; TWMP_Valenwood 19 / 0).

**Classified as latent**: the code path has no merge capability at all, so any
plugin exporting a delta would lose data silently — but no shipped plugin
currently does.

---

<a id="11"></a>
## 11. Confidence calibration — Minor

| Tier | Skyrim | Oblivion built | Nehrim built |
|---|---|---|---|
| 0 Cowardly | 5.7% | 3.1% | 3.0% |
| 1 Cautious | 1.8% | 1.5% | 0.2% |
| 2 Average | 33.8% | 19.1% | 19.6% |
| 3 Brave | 7.7% | 24.0% | 8.2% |
| 4 Foolhardy | 51.1% | 52.3% | 68.9% |

Tier 4 is nearly exact for Oblivion (52.3 vs 51.1); Nehrim runs 18 points high.
Tier 2 is ~14 points low in both.

Separately: one Nehrim NPC has `Confidence=255`, which the `>= 100` branch
silently maps to Foolhardy. Plausibly an authoring error in the source, but
nothing validates the input range.

---

<a id="12"></a>
## 12. Mood is hardcoded 0 — Known-empty

`actors.py:781` writes `mood=0` unconditionally. Vanilla writes nonzero Mood on
8.1% of NPCs. TES4 has no Mood field, so there is no direct source. Recorded as
a known-empty axis rather than a defect.

---

<a id="appendix"></a>
## Appendix — CharacterGen final assassin (NOT an aggression bug)

Investigated in response to "the assassin that is supposed to kill the emperor
does not kill the emperor". **None of findings 1–12 explain it.** The cause is
in package flag conversion, outside this audit's scope, and is recorded here
only so the analysis is not lost.

### Source data

`CGMythicDawnAssassinFinal` (`0001E6FF`):

```
AIDT.Aggression=0      DATA.Personality=47
Faction[0].FormID=000227BC   (MythicDawnCGAssassin, DATA.Flags=7)
AIPackage[0]=0000ABB1  CGAssassinFinalMoveTowardsPlayer
AIPackage[1]=0005415E  CGAssassinFinalSpeakToEmperor
AIPackage[2]=00017D81  CGAssassinsGenericWait
```

The kill is driven by CharacterGen stage 48
(`CGAssassinFinal.startcombat UrielSeptimRef`), converted to
`Fragment_Stage_0074_Item_1` → `TES4Polyfill.ForceCombat(...)`.

### Mechanism

Both approach packages carry authentic Oblivion conditions — decoded from
`Condition[0].Raw`, function 58 `GetStage`, param1 `0002466E` (CharacterGen):

- `CGAssassinFinalSpeakToEmperor` → `GetStage >= 76`
- `CGAssassinFinalMoveTowardsPlayer` → `GetStage >= 80`

These convert faithfully. At stage 74 neither qualifies, so the only package
that passes is `CGAssassinsGenericWait`: a Travel package to `PLDT.Type=3`
("Near Editor Location"), radius 0, duration 0 — "stand where you already are" —
carrying TES4 flags `528388` = Must Complete (0x04) | Always Sneak (0x1000) |
No Idle Anims (0x80000).

`T4_MUST_COMPLETE (0x04)` maps straight to `T5_MUST_COMPLETE (0x04)` via
`_FLAG_MAP` in `tes5_import/pack_converter.py:136`. The two flags share a bit
and a name but not a contract:

| | Meaning |
|---|---|
| TES4 Must Complete | Finish this package before advancing the schedule. A scripted `startcombat` still **forcibly overrides** it (UESP Oblivion:StartCombat — aggression, disposition and packages are all ignored). |
| TES5 Must Complete | The actor may not **leave** the package until it completes. Skyrim's `StartCombat` is only a nudge to the combat AI, so it does not win the tie. |

On a package that can never complete, the TES5 reading is an inescapable pin.
`StartCombat` fires against a package the actor is forbidden to leave: he never
swings, the Emperor never dies, stage 76 never runs, and the `>= 76` / `>= 80`
gates never open. Permanent deadlock.

### Evidence it is systemic

| Corpus | Must Complete rate |
|---|---|
| TES4 Oblivion.esm | 1079 / 7209 (**15.0%**) |
| Vanilla Skyrim.esm | 141 / 5961 (**2.4%**) |
| Vanilla, Travel template `00016FAA` (the one all three assassin packages use) | 37 / 1988 (**1.9%**) |

A 6× over-application. Of our 1079, **106** have the never-completes shape
(`PLDT.Type==3 and PLDT.Radius==0 and PSDT.Duration==0`).

This is the third same-name/different-meaning trap in the same flag table, after
TES4 0x8 "lock doors at start" vs TES5 0x8 "maintain speed" and Defensive
Combat vs Ignore Combat (`pack_converter.py:436`).

### Candidate fixes (not implemented)

1. Withhold `T5_MUST_COMPLETE` from open-ended packages — generic, addresses all
   106, restores TES4 ordering semantics without touching packages that
   genuinely complete.
2. Have `ForceCombat` stand the running package down so a scripted `startcombat`
   wins regardless — reproduces the TES4 override contract, which is currently
   honoured nowhere.

Both were prototyped and reverted; neither is in the tree. Neither has been
confirmed in-game.

---

## Method notes

- Vanilla census: `references/Skyrim.esm/{NPC_,FACT}.txt`.
- TES4 source: `export/{Oblivion.esm,Nehrim.esm,Morrowind_ob.esm,…}/`.
- Converted figures: read from the built binary ESMs in `output/` via
  `tools/tes5_esm_reader.py`, not simulated from the rules.
- CRVA and XNAM layouts verified against
  `references/xEdit/Core/wbDefinitionsTES5.pas:5176` (`wbStruct(CRVA,…)`) and
  `wbFactionRelations`.
- Package conditions decoded from the export's `Condition[N].Raw` field (note:
  the export does **not** use a `CTDA` line prefix — grepping for `CTDA` in
  `export/*/PACK.txt` returns nothing and will produce a false "no conditions"
  reading).
