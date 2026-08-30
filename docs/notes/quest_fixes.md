# Quest conversion: bugs found and fixed

From the 2026-07-17 completability audit.

## Bugs found and FIXED on this branch

### 1. `End ;comment` silently dropped whole event blocks — the biggest find
[script_convert/converter.py](../../script_convert/converter.py) `_parse_source` only recognized a bare `end`
line. Shivering Isles scripts end blocks with `End ;OnActivate`, `End GameMode`, etc. — the End line fell
into the block body and the block was **silently discarded** (and a following `Begin` also discarded the
accumulated previous block). 15 scripts were affected, 11 of them quest-critical: the entire OnActivate
of `SE09AddItemsScript` (four `SetStage SE09` calls — **SE09 Ritual of Ascension was uncompletable**),
`SE02OrcCaptainScript` OnDeath, `SEDoorToShiveringIslesScript`, `SE02GatekeeperScript`,
`SERelmynaVerenimScript`, `SEJayredIceVeinsScript`, `SE09AltarScript`, `SE09BodyPartActivatorScript`,
`BejeenScript`, `EyeOfNocturnalScript` (Daedric quest Nocturnal), `SE04FelldewScript`.
**Downstream casualties:** SE10 stage 3 and SEObelisks stage 90 are set by SE09's stage-200 result — three
quests healed by one fix. Fix: recognize `End` + trailing comment/label, close an open block when a new
`Begin` starts, keep an unterminated final block.

### 2. Reserved-EditorID properties never bound in VMAD (MS14 uncompletable)
`_safe_property_name` renames a quest EditorID that collides with a vanilla Skyrim script (`MS14` →
`myMS14`) in the generated Papyrus, but the VMAD binders looked the **sanitized** name up as an EditorID,
found nothing, and silently skipped the binding → `myMS14` was None at runtime and every
`myMS14.SetStage(...)` in 8 dialogue fragments plus the attached scripts did nothing. **MS14 (Nothing You
Can Possess) was uncompletable.** Fix: `resolve_property_formid()` in
[script_convert/constants.py](../../script_convert/constants.py) reverses the `my` rename on lookup miss; used by
both the INFO-fragment binder ([tes5_import/dialog_converter.py](../../tes5_import/dialog_converter.py)) and the
object-script binder ([tes5_import/object_scripts.py](../../tes5_import/object_scripts.py)). Verified: TIF props
now bind `myMS14 → 01017606`, and SE09AddItemsScript's props now include `SE09` + all activator refs.

### 3. `StartConversation target topic` discarded the topic (`Say(None)`)
Scripted NPC↔NPC conversations are how several quests advance: Bejeen/WeebamNa's talk sets DANocturnal
stage 48, the Jauffre/Martin council sets MQ12 stage 26, the Llevana scene sets MS10 stage 79, Kaneh/Mirel
sets SE06 stage 30, plus SE05/SE11 scenes. The converter emitted `ref.Say(None)` — topic gone, result
fragment never fires. Fix: route through the SayTo path — `ref.Say(TopicProp)` with the Topic property
registered for VMAD binding (verified: `BejeenREF.Say(DANocturnalConvo1)` with bound Topic props).

### 4. LIGH records never got their object-script VMAD
`convert_LIGH` was the only script-capable converter that hand-rolls its header and never spliced
`get_object_vmad()` — so `SE06FlameOfAgnonSCRIPT` (sets SE06 stages 9/190, the Flame of Agnon mechanic)
was converted+compiled but attached to nothing. Fixed in
[tes5_import/record_types/items.py](../../tes5_import/record_types/items.py); all other types go through
`_common_header_subs`, which splices it.

### 5. QUST VMAD declared fragments the .psc doesn't define
The importer's fragment filter counted a whitespace-only (`"\r\n"`) stage result script; the psc
generator's filter (`script.strip()`) didn't. `TES4_QF_E3` and `TES4_QF_SEObelisks` VMADs referenced a
`Fragment_Stage_0100_Item_0` that doesn't exist. Fixed by aligning the importer filter
([tes5_import/dialog_converter.py](../../tes5_import/dialog_converter.py) `_quest_stage_fragments`).

### 6. Inherited bark gate dead-ended conversation-revealed choice topics (SE36 froze)
The bark-choice promotion stamps the revealing greeting's timing gate onto the choice topic's INFOs. SE36's
"story" choice is offered **ungated from a conversation topic** and *also* from a `GetStage==15` reminder
greeting — the inherited `GetStage(SE36)==15` gate made the line unspeakable on the conversation path, and
stage 15 is set *by that line*: the quest froze at the start. SE02's stage-60/80 `GetStageDone` self-gates
are the same family. Fix: a choice target that also has a non-bark reveal path is no longer
promoted-and-gated; its TCLT conversation link (Oblivion's own shape) stays authoritative.

### 7. Compile fixes surfaced by restoring the dropped blocks
`GetForceSneak`/`GetKnockedState` had no mapping (now `IsSneaking`/`IsBleedingOut`), and the Say-duration
approximation assigned `0.0` to an Int (TES4 `short`) variable. **All 11030 scripts now compile
(11030/11030, 0 TODO regressions — 2 `;TODO` markers total, same as before).**
