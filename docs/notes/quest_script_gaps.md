# Quest script conversion: known gaps

Gaps the audit found and deliberately did not fix.

## Known gaps (round 9, not fixed here)

* **No MGEF carries a VMAD, so all 97 `extends ActiveMagicEffect` scripts are
  compiled but never attached.** TES4 MGEFs are restructured wholesale
  (`magic_effects.py` writes no VMAD and MGEF is in `SKIP_TYPES`), so every
  converted script-effect script is dead. This is a structural conversion gap,
  not a script-conversion defect, and it is why R9-2's *writers* cannot
  currently maintain the shadow global even though its reader binds correctly.
  Worth its own pass.
* **`GetCurrentAIProcedure` is a `;NE` no-op returning `0`** (9 sites across 6
  scripts, e.g. `MS91Script`'s `!= 4`, permanently true). Unlike package *type*,
  the AI procedure is pure runtime engine state with no per-actor record to
  reconstruct from, and Skyrim exposes no Papyrus native (checked vanilla
  `Actor.psc` and SKSE). Genuinely unreachable.
* **`BravilGuardJailorScript` is attached to no record in the export** — an
  orphan script in Oblivion.esm itself, so its unbound `Package` property is
  inert in both games. Not a pipeline bug.

---

## Known gaps (round 6, not fixed here)

* **`GetRestrained` is a `;NE` no-op returning `0`**, so `FGD07AjumScript`'s
  three tests are constant (`(0 == 0)` always true, `(0 == 1)` always false) —
  the Fighters Guild kidnap quest's "is Ajum tied up" checks. Skyrim keeps
  `GetRestrained` as condition function 4340 but exposes **no Papyrus native**
  (checked vanilla `Actor.psc` and SKSE), so there is genuinely nothing to call.
  The writer works (`SetRestrained` → `SetDontMove`), so a script-tracked shadow
  flag is the plausible fix — a design change, not a function mapping.
* **`ModDisposition <target> -100` beside an explicit `StartCombat`** emits
  `StartCombat` twice (`DarkExiledScript`). Idempotent and harmless, but it is
  the same shape round 2 deduped for `Say`.

---

## Known gaps (round 3, not fixed here)

* **`AddScriptPackage` → `EvaluatePackage()`** bites hardest in `MartinScript`:
  the player statue is given `MQStatuePose` to hold, and re-running AI selection
  instead means it does not hold the pose. Same structural limitation recorded in
  round 2 — forcing a package is a quest alias with a package stack.
* **`ShowBirthsignMenu`** is a no-op (`CharGenQuest` stage 43), so the birthsign
  is never chosen. Skyrim has no birthsign menu at all; this needs a replacement
  UI or an auto-assignment, not a function mapping.

---

## Known gap (not fixed here)

* **`AddScriptPackage` → `EvaluatePackage()`** drops the requested package
  (`SE05QuestScript`'s `SE05TortureHoldPosition`). Skyrim has no Papyrus
  equivalent: forcing a specific package is a quest **alias with a package
  stack**, a structural conversion rather than a function mapping. The current
  emission at least re-runs AI selection. Out of scope for a script audit.

---
