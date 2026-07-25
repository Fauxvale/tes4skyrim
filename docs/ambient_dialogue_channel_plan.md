# Ambient dialogue channels: diagnosis and plan of attack

**Symptom (reported 2026-07-25):** converted NPCs speak a random quip every few
seconds, unprompted. Vanilla Oblivion NPCs do not talk to themselves — they
greet the player on approach, and they occasionally hold conversations *with
each other*.

**Root cause:** Oblivion routes dialogue through three distinct delivery
channels that the converter collapses into two Skyrim ones. Two thirds of what
now plays as ambient chatter was never ambient in Oblivion, and 39% of the
player's dialogue menu is NPC-to-NPC chatter that was never player-selectable.

All numbers below are measured against the current build
(`output/Oblivion.esm/Oblivion.esm`, 2026-07-25) with
`tools/ambient_bark_audit.py`.

---

## The three channels

Oblivion's engine dialogue-type table (recovered by
`tools/oblivion_engine_extract.py` into `tes4_export/oblivion_engine_tables.json`)
lists GREETING and HELLO as **separate channels**, indices 0 and 6. NPC-to-NPC
conversation is a third mechanism — not an engine channel but the DIAL
**category** `Type=1` (Conversation), driven by Oblivion's actor-pairing AI.

| # | Oblivion source | Fires when | Currently converts to | Skyrim then fires it |
|---|---|---|---|---|
| 1 | `GREETING` — DIAL type 0, engine ch. 0 | Player **activates** the NPC; the dialogue menu opens | `HELO` | **Ambient, every 5 s** ❌ |
| 2 | `HELLO` — DIAL type 1, engine ch. 6 | Ambient, on player approach | `HELO` | Ambient, every 5 s ✅ |
| 3 | **Conversation** — DIAL type 1 (`*NQDResponses`, `*RumorResponses`, …) | **NPC-to-NPC only**; never reachable by the player | `CUST` | **Player menu topic** ❌ |

Skyrim by contrast has only two unprompted channels — `HELO` (ambient greeting,
re-armed by `fAIGreetingTimer`) and `IDLE` (idle chatter,
`fIdleChatterCommentTimer`) — plus `SCEN` for multi-actor scenes. There is **no
on-activate greeting channel**: in Skyrim the first thing said on activation is
a topic, not a bark.

### Why the cadence is 5 seconds

`fAIGreetingTimer = 5.0` and `fAIMinGreetingDistance` come from Skyrim.esm.
The converter correctly puts `GMST` in `SKIP_TYPES`
(`tes5_import/constants.py`), and the output contains **0 GMST records**, so
Skyrim's own timers govern. The cadence is therefore *not* a settings bug —
it is correct Skyrim behaviour applied to a pool of lines that should never
have been on that channel. Do not attempt to fix this by writing GMSTs.

---

## Problem 1 — GREETING lines are on the ambient channel

`tes5_import/dialog_converter.py:859-860` maps both channels to one subtype:

```python
'GREETING':       (73, b'HELO', 7),
'HELLO':          (73, b'HELO', 7),
```

Measured result:

```
HELO: topics=293  INFOs=5636
  GREETING   3743   66.4%     <- should not be ambient
  HELLO      1893   33.6%     <- correct
```

**3,743 INFOs** that in Oblivion played only after the player clicked an NPC now
fire on a 5-second ambient timer.

The conditions do not save it. The gates are identity/quest-state predicates
(`GetIsID` 6143 uses, `GetStage` 3217, `GetIsVoiceType` 7291), not
"should I speak unprompted" predicates — so once an NPC matches at the right
stage the line stays permanently eligible and re-fires every tick.

Density is inflated too. Vanilla's Hello pool is deliberately shallow and
broadly shared (one `DialogueRiftenHellos` topic of 315 short lines covers a
whole city); ours is 293 quest-scoped topics of narrative-length lines:

| | Converted | Vanilla |
|---|---|---|
| HELO INFOs | 5,636 | 5,287 |
| Median CTDAs per INFO | 4 | 2 |
| INFOs with ≤2 CTDAs | 165 | 3,570 (68%) |
| Response length (median / max) | 62 / 149 chars | short generic |

Sample of what currently plays as a walk-past bark:

> *"So, you've actually challenged the Gray Prince? Do you really know what
> you've gotten yourself into?"*

That is a conversation opener, not a bark.

### ⚠ A previous fix moved the wrong channel

Memory (`project_hello_channel_split`, 2026-07-19) records a fix setting
`_EDID_SUBTYPE['HELLO'] = (88, b'IDLE', 7)`. **That mapping is not in the
current code** — `git log -S` finds no trace of it, and both entries read
`HELO` today. It was reverted or never landed.

It also moved the wrong channel: HELLO *is* genuinely Oblivion's ambient
approach line and belongs on an ambient Skyrim channel. GREETING is the one
that does not belong there at all. Do not re-apply that mapping.

**Load-hang corollary (still binding):** that attempt hung the game before the
main menu because it carried **TCLT links onto IDLE-subtype INFOs**. Vanilla
Skyrim has TCLT on HELO barks but on **zero** of its 576 IDLE INFOs; the
engine's topic-link init cannot take links from IDLE. Any reroute must census
whether vanilla ever pairs a subrecord with the destination subtype before
emitting it.

---

## Problem 2 — NPC-to-NPC conversation is in the player's menu

Oblivion `Type=1` Conversation topics are the lines NPCs trade with *each
other*. 534 such topics (4,260 INFOs, after skips) convert to `CUST` —
player-selectable menu topics — and **all 423 purely-conversation topics carry a
`BNAM` branch**, so they genuinely render in the menu.

Because these topics never had a player-facing prompt, `FULL` fell back to the
EditorID. The player literally sees menu entries reading:

```
SEMiscQuestResponses
SkingradNQDResponses
ICAllNQDQuestionResponses
FGD02Insults
DASheogorathSpeech
SE
```

Vanilla's structural contrast is unambiguous:

```
VANILLA:    SCEN topics = 7426, with BNAM = 0      <- never selectable
            CUST topics = 6503, with BNAM = 6503   <- always selectable
CONVERTED:  SCEN topics = 0
```

Skyrim puts all NPC-to-NPC dialogue on the `SCEN` subtype with **zero**
branches; that absence of a branch is exactly what makes it non-selectable.
We emit **no SCEN records at all**, so these lines had nowhere to go but the
menu.

`INFOGENERAL` is the one correct case: it maps to `Rumors` (subtype `RUMO`),
which Skyrim does have as a real player topic, and is deliberately special-cased.
It must stay as-is.

### How much source structure survives

Oblivion gives us real pairing signal to work with:

```
Conversation-type INFOs: 7772
  NextSpeaker: Target 7590 / Either 131 / Self 51
  with Choice(TCLT) chaining: 4007

core NPC-to-NPC (excl. HELLO/GOODBYE/INFOGENERAL): 2881
  with Choice(TCLT) chaining: 1753 (61%)
```

So 61% of the core response lines already carry the thread structure a scene
needs. What Oblivion does **not** provide is *which two actors* hold the
conversation — Oblivion picks the pair at runtime from proximity + AI packages.
Vanilla Skyrim scenes are authored against **quest alias actors** (2–7 `ALID`
per scene across 1,706 SCEN records). That gap is the reason a faithful port is
expensive, and it is a genuine design decision, not a mechanical translation.

**Resolution: drop these lines rather than convert them wrong** (Step 1). There
is no cheap correct destination — a topic in the player menu is wrong, and a
solo ambient bark is the very "NPCs talking to themselves" defect being fixed.
Absence is the only honest intermediate state. The content is recorded in
TODO.txt #16 and restored by Step 4 when scenes are built properly.

---

## Plan of attack — ordered by ease of fixing

### Step 1 — Drop the NPC-to-NPC topics entirely (easiest)

**Decision (2026-07-25): better absent than wrong.** These lines have no correct
destination in Skyrim short of full SCEN synthesis (Step 4), and every partial
destination is worse than silence. So they are **skipped at conversion**, not
emitted-but-hidden.

**Change:** add Oblivion `Type=1` Conversation topics to the skip path in
`should_skip_dial` (`tes5_import/dialog_converter.py`), excluding the four that
are genuinely something else:

- `INFOGENERAL` → `Rumors`/`RUMO` — a real Skyrim player topic; already correct.
- `HELLO` → `HELO` — the ambient channel; correct (see Problem 1).
- `GOODBYE` → `GBYE` — a real Skyrim subtype.
- The already-skipped emotion-response families (`Question`, `AnswerNegative`, …)
  stay skipped by the existing `_SKIP_EDIDS` list.

That leaves the ~423 pure NPC-to-NPC response families to drop.

**Effect:** removes 423 garbage entries (`SEMiscQuestResponses`, `FGD02Insults`,
`SE`, …) from every NPC's dialogue menu. Most player-visible defect, cheapest fix.

**Cost:** small and localized — one predicate in the skip path.

**Risk:** low, with one thing to check: dropping a DIAL must not leave dangling
`TCLT` choices pointing at it. The converter already handles exactly this via
`_strip_dead_tclt(infos, skipped_fids)`, which runs over `should_skip_dial`
output — so routing the drop through that same path gets the cleanup for free.
This is the reason to skip properly rather than to suppress `BNAM`.

**Do not lose the content.** Recorded as TODO.txt "Later Issues" #16 so the
dropped families can be restored via Step 4.

**Verify:** `tools/ambient_bark_audit.py --by-source` — expect
`Conversation(NPC-to-NPC)` to fall from 4,260 INFOs to ~0 in the CUST
attribution, and zero dangling TCLT in `tools/dialog_validator.py`.

---

### Step 2 — Take GREETING off the ambient channel

**Change:** route `GREETING`-sourced INFOs to a player-activated topic instead
of `HELO`. `HELLO` stays on `HELO` (it is correct).

**Effect:** removes ~3,743 INFOs (66%) from the 5-second ambient timer,
leaving the 1,893 genuinely-ambient HELLO lines. This is the direct fix for
the reported symptom.

**Cost:** moderate. The destination needs deciding (see below) and ~3,743
records re-parent, which moves group structure.

**Risk:** moderate, with a known trap — re-read the load-hang corollary above
before choosing a destination subtype, and census vanilla for every
subrecord/subtype pairing emitted.

**Open decision — where GREETING lands.** Two candidates:

- **(a) A top-level `CUST` topic entered on activation.** Faithful to
  Oblivion's "opening line when you click them", and Skyrim's natural shape.
  Costs a branch + prompt per quest, and risks cluttering the menu if the
  prompt is wrong — Problem 2 is precisely what that failure looks like, so
  prompts must come from real `FULL` text, never an EditorID fallback.
- **(b) Drop the ones that duplicate a HELLO line, keep the rest as HELO.**
  Much cheaper, no structural change, but discards Oblivion's on-activate
  greeting entirely and leaves NPCs opening dialogue silently.

Recommendation: **(a)**, because it preserves content, and (b)'s silence was
already an accepted-but-disliked trade-off in the reverted 2026-07-19 attempt.

---

### Step 3 — Thin the ambient pool to vanilla density (optional, do after 2)

**Change:** after Step 2, re-measure. If 1,893 HELLO lines across quest-scoped
topics still feel too frequent, consolidate toward vanilla's shape — fewer,
broader topics of short lines, rather than many narrow quest-scoped ones.

**Effect:** brings perceived cadence in line with vanilla even where the channel
routing is now correct.

**Cost:** low-to-moderate, and purely additive tuning.

**Risk:** low. Defer until Steps 1–2 are in-game — the cadence may already be
acceptable once two thirds of the pool is gone, which would make this
unnecessary.

---

### Step 4 — Restore NPC-to-NPC conversations via SCEN (deferred)

**Not part of this fix.** Step 1 drops these lines; this step is how they come
back. Tracked as **TODO.txt "Later Issues" #16** so the content is not
forgotten.

**Change:** synthesize Skyrim `SCEN` records for the dropped conversation
families, using the surviving `TCLT` chains (1,753 of 2,881 core INFOs) as phase
structure.

**Effect:** restores the behaviour originally described — NPCs holding
occasional conversations with each other.

**Cost:** high. Requires inventing actor pairing that Oblivion does not record:
vanilla scenes bind 2–7 quest **alias** actors each (1,706 SCEN records), whereas
Oblivion selects the pair at runtime from proximity and AI packages. Also needs
quest aliases, phases/actions, and per-scene conditions.

**Risk:** high, and partly a design question rather than an engineering one.

**Why it is deferred rather than attempted now:** Steps 1–3 remove reported
defects; this one adds back a feature. Every partial version of it is worse than
absence — which is exactly how these lines came to be labelled `SE` and
`FGD02Insults` in the player's menu. Do not block the first three steps on it,
and do not ship a half-version of it.

---

## Verification

- `python tools/ambient_bark_audit.py output/Oblivion.esm/Oblivion.esm --by-source export/Oblivion.esm`
  — per-subtype ambient INFO counts attributed to the originating Oblivion channel.
- `--compare "<SSE>/Data/Skyrim.esm"` — side-by-side against vanilla density.
- `--topics HELO` — largest ambient topics, with conditionless counts.

Target end state after Steps 1–2:

| Metric | Now | Target |
|---|---|---|
| HELO INFOs from GREETING | 3,743 (66%) | 0 |
| HELO INFOs total | 5,636 | ~1,893 |
| Conversation-sourced CUST topics | 423 | 0 (dropped) |
| Conversation-sourced CUST INFOs | 4,260 | 0 (dropped) |
| Dangling TCLT after the drop | — | 0 |
| SCEN records | 0 | 0 (Steps 1–3) / >0 (Step 4, deferred) |

Ground truth is in-game: the fix is confirmed when NPCs stop quipping
unprompted and their menus no longer list EditorIDs.
