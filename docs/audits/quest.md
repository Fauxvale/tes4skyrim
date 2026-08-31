# Quest Completability Audit — Oblivion.esm conversion

**Date:** 2026-07-17 · **Branch:** `quest-audit` (worktree off `papyrus-extension-and-speedup` @ 666faeb)
**Scope:** all 390 QUST records in Oblivion.esm (GOTY, incl. Shivering Isles)

## How the audit works

A new tool — the **quest walkthrough emulator** ([tools/dialog/quest_walkthrough.py](../../tools/dialog/quest_walkthrough.py) +
[tools/dialog/quest_walkthrough_tes5.py](../../tools/dialog/quest_walkthrough_tes5.py)) — symbolically "plays" every quest in the
converted plugin. It collects every stage-advancement edge that survived conversion (dialogue TIF
fragments, QF stage fragments, attached quest scripts, object-script VMAD attachments), gates each edge
by real Skyrim rules, and runs a fixpoint from new-game state until nothing more can fire. A quest is
completable when a TES4 complete-flag stage is reachable. The same optimistic engine runs over the TES4
export as a baseline, so stages that were already unreachable in Oblivion (orphaned scripts, commented-out
`setstage`) don't count as regressions.

Unlike the old `dialog_emulator.py`, the reachability rules follow the engine's actual behavior
(grounded in the skyrim-dialog-system reference):

- an INFO fires only if its DIAL has a QNAM to a **running** quest (start-game-enabled, `Start()`ed, or
  auto-started by SetStage);
- a topic is player-reachable via **top-level branch**, **transitive TCLT chains** (choice chains run 5+
  links deep — CGBaurusA→E), **bark subtypes** (HELO/GRET/ATCK/… fire without a menu), or **`Actor.Say(topic)`
  calls in any reachable script**;
- CTDA gates are evaluated statically: `GetStage`/`GetStageDone`/`GetQuestRunning` against the growing
  reached-set, `GetGlobalValue(TES4Unlock_*)` against fired revealer fragments, `GetVMQuestVariable`
  against declared `Conditional` variables in the attached script, and **any FormID param that exists in
  neither the output nor Skyrim.esm marks the condition permanently dead**;
- every Papyrus edge is checked end-to-end: psc generated → pex compiled → fragment function present →
  quest/topic **property actually bound in the VMAD** (an unbound property is None at runtime and the
  call is lost).

Run it with:

```
python tools/dialog/quest_walkthrough.py --export export/Oblivion.esm \
    --esm output/Oblivion.esm/Oblivion.esm --scripts output/Oblivion.esm/scripts \
    --seq output/Oblivion.esm/seq/Oblivion.seq --md temp/quest_audit_raw.md
```

`--quest <EditorID>` audits one quest; the `--md` report lists every issue with the exact blocking record.

## Headline result

| | count |
|---|---|
| Quests audited | 390 |
| Completable as converted | **375** |
| Broken (cannot finish) | 2 — SE09, MS14 |
| Degraded (main path OK, stages/side content lost) | 13 |

Every one of the 15 problem quests traces to one of **seven root-cause bugs — six of which are fixed on
this branch** — plus two known conversion gaps that need design work. The full machine-generated
per-quest list is `temp/quest_audit_raw.md`.

## Cross-cutting checks that came back clean

- **SEQ file** contains every start-game-enabled quest (no "dialogue never initializes" cases).
- **No dangling CTDA FormID params** in quest dialogue (the CK "Unable to find TESForm" class) — the
  RACE_MAP/condition-translation work is holding.
- **Journal quests all have QOBJ objectives** and their stage fragments call
  SetObjectiveDisplayed/Completed.
- **Papyrus coverage:** every generated .psc has a compiled .pex (after fix 7); every VMAD fragment name
  resolves to a psc function (after fix 5); the AddTopic unlock-gate invariant holds — every
  `TES4Unlock_*` gate reachable in the walkthrough has a firing revealer.
- The infamous flaky parallel-compile failures (7 scripts, no error text) are just papyrus.exe races —
  they compile clean individually.

## Verification status / how to re-check

Scripts-side fixes are verified end-to-end (scripts phase re-run in this worktree: fragment bodies,
compile 11030/11030). ESM-side fixes (2, 4, 5, 6 — VMAD/record changes) are verified at unit level; the
full import wasn't re-run here (the navmesh phase crashed its worker pool in this worktree and per your
call I didn't chase it). **After the next full `python convert.py -f Oblivion.esm`, re-run the walkthrough
tool** — expected outcome: SE09, MS14, SE02, SE06, SE10, SE36, SEObelisks, DANocturnal, MQ12, MS10 all
clear; remaining flags should be exactly the SEFF quests (SE04/MS47/MS40/FGD08) and MS05 until gaps A/B
are designed.

## Caveats on emulator fidelity

The emulator is optimistic where the engine is dynamic: unknowable runtime conditions (distance checks,
faction ranks, random rolls, ref-walking script logic inside `if` bodies) are assumed satisfiable, so it
can miss a break hidden behind unconverted *conditional logic inside* a fragment body (only lost/unbound
*calls* are detected there). It does not model packages/scenes as advancement sources (TES4 quests advance
via dialogue/scripts, so coverage is high), and voice-file presence is out of scope (silent lines still
advance quests; `tools/dialog/voice_audit.py` covers that).
