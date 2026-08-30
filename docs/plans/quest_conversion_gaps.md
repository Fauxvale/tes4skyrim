# Quest conversion: gaps needing design

**Status: PLAN, unimplemented.**

From the 2026-07-17 completability audit.

## Remaining conversion gaps (need design, not quick fixes)

### A. Scripted magic effects (SEFF) are never attached — 4 quests degraded
TES4 script-effect spells/poisons/ingredients (`SCHR.Type=0x100`, referenced by `ScriptEffect[i].FormID`
on SPEL/ENCH/INGR) are converted to `extends ActiveMagicEffect` psc and compile — but **nothing in the
output references them**: there's no carrier MGEF, so the script never runs when the effect applies.
Casualties found by the walkthrough: **SE04** stage 40 (Felldew withdrawal), **MS47** stages 40–60
(reverse-invisibility counterspell + its AddTopic unlock chain), **MS40** stage 60 (dagger blessing),
**FGD08** stage 40 (the Hist-sap potion). Suggested design: for each SEFF script generate one MGEF
(archetype Script, matching casting/delivery), attach the AME script via MGEF VMAD (MGEF supports VMAD),
and splice that effect into the converted SPEL/ENCH/INGR effect lists — the condition-side polyfill
(`HasMagicEffectByID`) already exists, this is the effect-side counterpart.

### B. MenuMode blocks are commented out (by design) — MS05 degraded
The converter deliberately preserves `Begin MenuMode` bodies as comments (Papyrus has no per-menu event;
naive conversion caused the "MQ01 starts-then-fails" bug). Collateral: **MS05 (Through a Nightmare,
Darkly)** — entering the Dreamworld happens in a MenuMode block gated on `IsPCSleeping` (sleep menu +
teleport + `setstage MS05 50`). Suggested design: MenuMode blocks whose body is gated on `IsPCSleeping`
map cleanly onto an `OnSleepStart`/`RegisterForSleep` handler (player-alias quest script or TES4Polyfill).

### C. Not a regression: MS14 stage 200
`MS14TivelaScript` is attached to nothing **in Oblivion.esm itself** (orphaned SCPT) — its stage-200 edge
never ran in the original game either. The baseline now ignores orphaned scripts, so only real
regressions are reported.
