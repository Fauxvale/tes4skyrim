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
TODO.txt #16.

**Update 2026-08-07:** the *quest-advancing* subset of these conversations is
no longer absent — 15 chains are replayed by a generated driver quest. See
[The NPC-to-NPC conversation scheduler](#the-npc-to-npc-conversation-scheduler).
The flavor families below remain dropped.

---

## Plan of attack — ordered by ease of fixing

### Step 1 — Drop the NPC-to-NPC topics entirely ✅ IMPLEMENTED 2026-07-25

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

#### ⚠ The trap that nearly broke CharacterGen

A drop keyed on `DATA.Type == 1` alone is **wrong**. Measured against the real
export, **293 of the 535 Type-1 topics are script-driven** — spoken by an
explicit `Say`/`SayTo`/`StartConversation` in a quest script, which converts to
a real Skyrim `Actor.Say()` and works correctly. That set includes:

- every CharGen topic (`CharGenMain`, `CharGenVoice`, `CharGenBaurus`,
  `CharGenEmperor`, `CharGenGlenroy`, `CharGenBlades`, `CharGenRenote`,
  `CharGenTaunt2`) — the Emperor/Blades tutorial intro;
- the Daedric-prince speeches (`DASheogorathSpeech`, `DANamiraSpeech`,
  `DAClavicusSpeech`), the arena/announcer topics (`ICAnnouncer`, `Announcer`),
  `FGD02Insults`, `OblivionGateConv`, the SE and Thieves-Guild scripted scenes.

Dropping by type alone would have deleted all of them and broken the tutorial
outright. The predicate therefore consults `_SAY_TOPIC_DISPOSITIONS` (the
converter's existing Say/SayTo/StartConversation scanner) and keeps anything a
script speaks. Note this detector finds **295** Type-1 topics where a naive
`\bsay\b` regex finds only 12 — it also handles `StartConversation` and
`ref.Say Topic` forms.

**Ordering requirement:** `build_say_topic_dispositions` must run *before* the
first `should_skip_dial` call. It is now built in the `build_dialog_groups`
pre-scan. `dialog_unlocks.build_unlock_plan` also calls `should_skip_dial`, and
runs much earlier (`import_main.py:332` vs `:801`), so the predicate **fails
safe**: an empty map keeps every topic rather than dropping all 293.

#### The script-driven topics still needed unlisting

Keeping them is necessary but not sufficient. They were still emitted with a
**top-level `BNAM` branch** and an EditorID `FULL`, so `CharGenVoice`,
`Dark18TraitorTalk`, `SE11SheogorathFarewell2` … sat in the player's menu —
the same defect, on a different set of topics. Fix: force a **Normal
(non-top-level) branch** for script-driven Type-1 topics. `Actor.Say()` reaches
an INFO through its topic regardless of branch visibility and TCLT links still
resolve, so the scripted lines play exactly as before while the topic leaves the
menu. `INFOGENERAL` is exempt — it is genuinely player-selectable.

#### Measured result

```
Type-1 named-keep    :   3 topics  4891 INFOs   (INFOGENERAL/HELLO/GOODBYE)
Type-1 script-driven : 293 topics  1266 INFOs   <- KEPT, unlisted
Type-1 droppable     : 242 topics  1157 INFOs   <- dropped

converted DIAL 3461 -> 3330
NPC-to-NPC conversation topics dropped: 258
Type-1-sourced CUST topics: TOP-LEVEL(menu)=1 (INFOGENERAL), NORMAL(hidden)=292
dangling TCLT in output: 0
```

(258 > 242 because the count includes topics whose INFOs were already skipped
for other reasons.)

**Verify:** `tools/ambient_bark_audit.py --by-source`; regression tests in
`tests/test_import.py::TestNpcToNpcConversationDrop` (5 tests, incl. the
fail-safe and the CharGen keep).

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

### Step 4 — Restore NPC-to-NPC conversations (partially done 2026-08-07)

Step 1 drops these lines; this step is how they come back. Still tracked as
**TODO.txt "Later Issues" #16** for the *flavor* families. The
**quest-advancing** subset is now restored — not via SCEN, but by a generated
driver quest. See
[**The NPC-to-NPC conversation scheduler**](#the-npc-to-npc-conversation-scheduler)
below, which is the implemented design and supersedes the SCEN sketch that
used to sit here.

**Still deferred (the flavor families):** synthesize `SCEN` records for the
~240 dropped `*NQDResponses` / `*RumorResponses` chatter families, using the
surviving `TCLT` chains (1,753 of 2,881 core INFOs) as phase structure. Cost is
high — vanilla scenes bind 2–7 quest **alias** actors each (1,706 SCEN records)
whereas Oblivion picks the pair at runtime from proximity + AI packages, so
this needs invented actor pairing plus aliases, phases/actions and per-scene
conditions. Every partial version is worse than absence — which is exactly how
these lines came to be labelled `SE` and `FGD02Insults` in the player's menu.
Do not ship a half-version of it.

The driver below does **not** generalize to these: it works precisely because
quest-advancing chains name both actors with `GetIsID`, which the flavor
families do not.

---

## The NPC-to-NPC conversation scheduler

**Oblivion has an ambient conversation SCHEDULER that Skyrim does not have at
all, and no record conversion can substitute for it.** This is the mechanism
behind the dropped families, and it is also why several main-quest chains
stalled.

### How Oblivion runs one

When two NPCs idle near each other, the engine may start a conversation
between them:

1. It picks the pair from proximity + AI packages.
2. The initiator speaks a **`HELLO`** line whose conditions name BOTH actors —
   `GetIsID(<speaker>)` on the subject side and `GetIsID(<listener>)` with the
   **Run-on-Target** bit on the target side. The target-side identity is what
   makes the line mean "say this *to that specific NPC*".
3. The engine then walks the line's **`Choice`/`TCLT`** links, alternating
   speakers per each INFO's `DATA.NextSpeaker`, until a `GOODBYE` ends it.
4. Each INFO's result script runs as it plays — which is where the quest
   payload lives.

Quest authors lean on this. **CharacterGen stage 26→27 IS such a chain:**

```
Baurus  HELLO   0004EA44  "Are you all right, sire? We're clear, for now."
        conditions: GetIsID(Baurus) + GetIsID(UrielSeptim)[Target]
                    + GetStage(CharacterGen)==26
   -> TCLT
Emperor CharGenVoice 0004EA45  "Captain Renault?"
   -> TCLT
Baurus  GOODBYE 0005144A  "She's dead. I'm sorry, sire, but we have to keep
                           moving."          result: setstage charactergen 27
```

The stage-26 result script itself only calls `evp` — **nothing in the plugin
starts this conversation.** The scheduler does. So on Skyrim the three lines
were never spoken, stage 27 never fired, and the intro stopped dead with
everyone standing in position. (`setstage 27` from the console resumed it
normally, which is what isolated the chain as the only broken link.)

### Why the obvious destinations don't work

| Tried | Why it fails |
|---|---|
| Leave the head on `HELO` | Skyrim evaluates HELO **only when greeting the PLAYER**, so `GetIsID(<other NPC>)[Target]` can never pass. Silent. |
| Route the head to `ACAC` (subtype 92) | ❌ **Wrong — do not retry.** `ACAC` is **"ActorCollidewithActor"**, the bump-into-someone bark. It is not a conversation channel. Tried 2026-08-07; still silent in-game. Source: the engine subtype table in `references/xEdit/Tools/xSE/f4se_plugin_xEdit/f4se_plugin_xEdit-20180628.txt` — `108;"ActorCollidewithActor";108;"ACAC";7;0;0`. **Its presence in Skyrim.esm (3 topics) was mistaken for evidence it was the ambient-conversation channel; that inference was wrong.** |
| Full `SCEN` synthesis | Correct in principle but needs actor pairing Oblivion never records — see the deferred Step 4 above. |

### What is implemented: `tes5_import/npc_conversations.py`

These chains are **identity-pinned** — the head names both actors — so the
proven `Actor.Say()` machinery (which already drives every scripted CharGen
conversation) can replay them. Two pieces, built from ONE shared plan:

* **The head INFO is reparented** onto a synthesized hidden topic
  `TES4NPCConv<plugin>Topic<N>` (a Type-1 CUST topic registered say-driven, so
  `Say()` reaches it and the player menu never shows it — the same shape
  `CharGenVoice` already has). Source-space FormIDs come from
  `_CONV_FAKE_FID_BASE = 0x00F40000`, asserted collision-free against the real
  DIALs.
* **A generated start-game-enabled quest** `TES4NPCConv<plugin>` (in the `.seq`)
  polls on a 4 s `RegisterForSingleUpdate` loop and, when a chain's gate
  passes, `Say()`s the sequence with measured waits.

The poll reproduces the scheduler's own preconditions:

```papyrus
Bool Function CanConverse(Actor akA, Actor akB)
    ; both loaded, both alive, neither fighting, within 500 units
```

**Line selection stays with the engine.** The plan fixes only the TOPIC
sequence, each hop's speaker, and the expected line length; which INFO plays
within a topic is decided by that INFO's own converted conditions — exactly
Oblivion's rule. So per-line CTDA fidelity is preserved and the INFO End
fragments (the `setstage` payloads) fire as they do for any other `Say()`.

### The selection rules (and why each exists)

Restored only when ALL hold:

| Rule | Why |
|---|---|
| Head is a `HELLO` INFO whose **every** target-side `GetIsID` names a non-player actor | That is the scheduler's signature. |
| The identity is **positive** (`== 1`, operator `==`) | `GetIsID(x)[Target] == 0` is an *exclusion* on a player greeting, not an address. |
| The chain's TCLT closure carries a quest-advancing result (`setstage`/`startquest`/`stopquest`/`set X.y to`) | Pure flavor stays dropped per "better absent than wrong". |
| Exactly one subject identity, and both actors have a **unique** placed ref | The driver needs concrete `Actor` properties. |
| Every non-identity head condition compiles to a poll gate | See below. |

Supported gates: `GetStage` (58), `GetQuestVariable` (79 → the converted quest
script's property), `GetItemCount` (47 → `Conv<i>A.GetItemCount`). **Anything
else SKIPS the chain** rather than firing it on a looser trigger — a
conversation firing EARLY is worse than one that stays absent. Skips are
logged, not silent.

### Traps this hit, all still live

* **`GOODBYE` hops are bark-grouped.** The bark pass emits one topic per
  (owning quest, subtype), so a chain's GOODBYE line does NOT live at the
  source `GOODBYE` DIAL's FormID. `_build_bark_pass` publishes
  `ctx['bark_topic_fids'][(quest, subtype)]` and the driver binds through that.
* **Hops that ride `HELLO` itself** (MS91 Weebam-Na → Mazoga) must point at the
  *other chain's* synthesized head topic, since the raw HELLO DIAL doesn't
  survive as one record. Chains whose HELLO hop has no restored head are
  dropped.
* **Chain indices name Papyrus properties**, so after any filtering pass the
  chains are **renumbered gapless** — both pipelines must agree on the numbering
  or every property binds to the wrong thing.
* **Overlapping chains are mutually exclusive.** Two heads can open the same
  authored talk (MS91: Weebam-Na's "You want to speak to me?" and Mazoga's
  "You are Weebam-Na?" both walk the MazogaTalk lines). Whichever fires sets
  the other's `_done` flag, or the whole conversation replays.
* **Member topics must be un-dropped.** A chain routing through a Type-1 topic
  the NPC-to-NPC drop would remove registers it say-driven so it survives.

### The mirroring contract

`build_conversation_plan()` is **shared analysis**, in the same family as
`message_menus` and `dialog_unlocks`: `tes5_import` builds the head topics and
the quest VMAD from it, `script_convert.pipeline` generates the matching
`.psc` from it. **Any divergence leaves VMAD properties unbound** — the
generated script guards every property against `None` and disables that chain
rather than aborting the whole poll function (see
`project_unbound_vmad_property_aborts`), but it cannot repair the binding.
Both sides gate on masterless plugins only.

Verify the contract holds after any change:

```bash
# psc-declared Conv* properties must equal the VMAD-bound set, exactly
python temp/verify_conv_vmad.py     # 99/99, dangling: 0
```

### Measured result (Oblivion.esm, 2026-08-07)

```
NPC-to-NPC conversation topics dropped: 243 (TODO.txt #16)
quest-advancing chains restored: 15
NPC conversations: 15 chains on TES4NPCConvOblivion (2 skipped), 99 bound properties
```

By OWNING quest (note this is the QSTI owner, not the quest a chain's gate
tests — MQConversations holds the MQ13 Narina/Martin exchange, whose gate is
`GetStage(MQ13)==20`):

```
Charactergen 1   MQ04 1   MQ15 1   MQ16 3   MQConversations 1
MS91 3           SEConversations 3         TG01BestThief 1   TG03Elven 1
```

All were stalled the same way, not just the reported one — **MQ16's three are
the endgame Ocato/messenger conversations**. The 2 skips are heads whose
speaker cannot be statically resolved (0 or 2 subject identities).

### Known compromise

`_done` latches are script state, so an overlapping *flavor* pair (the SE
spouse chats) plays once per save rather than recurring. The quest-critical
chains all self-terminate via their own `setstage`, so this costs nothing
there.

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
| Conversation-sourced topics in the player MENU | 423 | 1 (`INFOGENERAL`/Rumors) ✅ |
| Pure NPC-to-NPC topics emitted | 242 | 0 (dropped) ✅ |
| Script-driven Type-1 topics kept | 293 | 293, unlisted ✅ |
| Dangling TCLT after the drop | — | 0 ✅ |
| SCEN records | 0 | 0 (flavor families still deferred) |
| Quest-advancing chains restored | 0 | 15, via the driver quest ✅ |

Ground truth is in-game: the fix is confirmed when NPCs stop quipping
unprompted and their menus no longer list EditorIDs.

For the conversation driver specifically, the in-game check is that
CharacterGen advances 26→27 on its own (Baurus and the Emperor speak the
three-line exchange while the player stands in the ambush room) — and the
equivalent beats in MQ16, MS91 and TG03.
