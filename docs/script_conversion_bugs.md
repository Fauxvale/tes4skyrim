# Script conversion: known defects found during the parse-tree rewrite

The `script_convert/` rewrite (see the parse-tree plan) reproduces **current
behaviour, bugs included** — the tree path emits the same wrong thing the regex
path emits, and defects are recorded here instead of being fixed inline. Fixing
is separate work, done on a foundation where the fix is one transform rather
than one more repair pass.

Each entry says how it was measured and what is *not* yet known, so nothing here
gets treated as more certain than it is.

---

## 1. Cross-plugin script types are a BUILD-ORDER dependency (measured 2026-08-28)

**Status:** not a runtime defect. Recorded because it is invisible to both
whole-tree repair passes and will matter to stage 5.

A converted script can declare a property typed as a script owned by another
plugin, which its own output directory does not contain:

| Plugin | Distinct missing script types | Property declarations | Files |
|---|---|---|---|
| Translation.esp | 165 | 830 | 179 |
| Knights.esp | 14 | 42 | 25 |
| Morrowind_ob.esm | 5 | 10 | 4 |
| **Nehrim.esm** | **0** | 0 | 0 |
| **Oblivion.esm** | **0** | 0 | 0 |

The split is exact: **every plugin with masters has them; both standalone
plugins have none.** Examples — `TES4_AltaroftheNine`, `TES4_FXDustFall01SCRIPT`
and `TES4_TG03Main` are declared by `Knights.esp` scripts and defined in
`Oblivion.esm`; `TES4_HMSfromFloat24h` is declared by
`Translation.esp/TES4_AAWaitMenuActorScript.psc` (which calls `.TES4Call()` on
it three times, from OBSE `Call HMSfromFloat24h GameHour`) and defined in
`Nehrim.esm`.

**Verified**: every one of the sampled missing types IS generated into its
owning plugin's output (`TES4_AltaroftheNine.psc` etc. are present under
`output/Oblivion.esm/`), and all plugins deploy to the same `Data/Scripts/`
folder, so the `.pex` resolves at runtime. `tools/script/compile_papyrus.py`
already carries `--extra-headers` for exactly this case.

**Not yet known**: whether every one of the 165 Translation.esp types resolves
(only a sample was checked), and whether a *compile* of one plugin in isolation
fails without `--extra-headers`. Neither affects a full-pipeline build.

**Why it matters to the rewrite**: `_comment_undeclared_identifiers` cannot see
this class at all — the property *is* declared, it is just typed as a script
absent from this output tree. A symbol table spanning plugins can check it; the
grep passes cannot.

---

## 2. Shadowed command handlers in `_emit_function` (measured, pre-existing)

Six TES4 command names have two competing branches in the 201-branch chain, one
of them unreachable. Dated with `git log -L`, they split into two opposite
kinds:

**(a) Superseded corpses — safe to delete, zero output change.** The
earlier-in-file branch is the *newer* commit; a better handler was added above
the old one and the corpse left below: `getpcisrace` (L7338 shadows L8166),
`ispcexpelled`/`getpcexpelled` (L6391 shadows L8016), `isexpelled`, `expel`
(L7380 shadows L8008).

**(b) Unreachable NEW code of UNKNOWN correctness.** Commit `fd04769`
(2026-07-28, "handle OBSE extensions") added implementations that an older stub
~1,100 lines above silently defeats:

- **`forceflee`** — the new branch emits `SetActorValue("Confidence", 0)` +
  `EvaluatePackage()`; the April stub at L7299 returns `;NE: ForceFlee` + `0`
  and wins. Its sibling name `flee`, added in the same commit, **does** reach
  the new code — so one commit's two names behave differently today.
- **`positioncell`** — the new branch emits `SetPosition(x,y,z)` + `SetAngle`;
  the 2026-07-20 stub at L6604 wins. `positionworld` works, `positioncell`
  returns `0`.

**This code has never executed.** It was shadowed at birth, so it has never
produced a line of output or been seen in game, and its rationale comment is an
argument rather than evidence — it is *not* known to be better than the stub it
lost to. Enabling it changes output and needs an in-game test; it is out of
scope for the rewrite.

**A precedence inversion, not a duplicate**: `getbookread` is in `_NO_OP_FUNCS`
(L7477) but `bookread` is not, so the membership test at L7500 claims
`getbookread` and the later L8503 branch is reachable only for `bookread`.
Deleting that branch wholesale would change `bookread`'s emitted comment text;
only the `'getbookread'` tuple entry may be removed.

---

## 3. Two latent scanner bugs — both FIXED in stage 3 (measured 2026-08-28)

Found while replacing the hand-rolled scanners with the lexer. Both were cases
where the old character-level code did the wrong thing on input the corpus
happens not to contain, so neither changed emitted output — verified by a
zero-diff rebuild of all 38,612 scripts across four plugins after each fix.

**`_split_logical` was not quote-aware.** It tracked parenthesis depth but not
string state, so `MessageBox "a || b"` split into two parts. Verified over
413,210 comparisons: no corpus script has `||` or `&&` inside a string literal.
**Fixed** — the parser-based replacement is quote-aware by construction.

**Two regexes missed digit-leading EditorIDs.** An Oblivion EditorID may start
with a digit (`"1TrapFireMineWorldRef"`, `"2akulaSdoorSa"` — 118 lines, 16
distinct ids, across Nehrim, Morrowind_ob and Translation), but both
`_QUOTED_MEMBER_RE` and `_QUOTED_NAME_RE` required a letter or underscore
first, so those kept their quotes. Only `_safe_property_name` saved them: it
strips the quotes *and* prefixes the `d` that makes the name legal Papyrus, so
quoted and unquoted spellings happened to normalise to the same property
(verified for all 12 sampled ids). Anything reading the name without going
through it would have hit the `_MQ01Tate_` failure that
`_QUOTED_NAME_RE`'s own comment describes — a property bound to nothing,
throwing at every use.

**Fixed** — both name classes widened to `\w+`, and `_unquote_identifiers`
now delegates to `parser.unquote_member_names`, where "a quoted name touching
a `.`" is a structural test rather than a lookahead/lookbehind pair with a
second character class to keep in sync. Verified identical on all 206,612
source lines before the substitution.

**A non-finding, recorded so it is not re-investigated:**
`d1TrapFireMineWorldRef.MoveTo(d1TrapFireMineWorldRef)` looks like an object
being moved to itself, but it is the deliberate conversion of TES4
`Reset3DState` (`converter.py`, `fname_low == 'reset3dstate'`) — `MoveTo(self)`
is the Skyrim idiom for forcing a 3D reset.

---

## 4. Authored typos in source scripts (measured, not our bug)

The parser degrades an unparseable line to a `Raw` node rather than failing the
script — Oblivion's own compiler was permissive, and a script that fails to
convert takes down every other script declaring a property of its type. Across
all 19,013 script bodies in 10 plugins there are **17** such lines, every one an
authored typo:

- `MG09Script` line 132 (Oblivion.esm): a stray `` ` `` after `endif`.
- `SE09BodyPartActivatorScript` (Oblivion.esm): a bare `:` where the author
  meant `;`, so a comment line lexes as code.
- `AkarusScript`, `MelvinScript`, `AchievementsQuestScript` (Nehrim.esm): bare
  `-----` / `:= == ==` separator lines with no leading `;`.

No action needed; recorded so a future session does not re-investigate them.

---

## 5. Divergent block scanners in the repair passes — FIXED in stage 4 (measured 2026-08-28)

Four post-emit passes each re-derived Papyrus block structure from text, with
their own keyword spellings, and disagreed. The disagreements were invisible
because they only bite on shapes the emitter does not currently produce —
exactly the class of latent defect §3 records.

**`_remove_dead_code_after_return` did not know `If(`.** `_balance_if_endif`
matched `if ` *and* `if(`; the dead-code pass matched only `if `. So a `Return`
inside an `If(x)` block counted as top-level, and **every statement after that
block was rewritten to `;  <line>  ;dead code after Return`** — live code
silently commented out, including the `EndIf` itself:

    Event A()          old ->  Event A()
    If(x)                      If(x)
    Return                     Return
    EndIf                      ;  EndIf  ;dead code after Return
    foo                        ;  foo  ;dead code after Return
    EndEvent                   EndEvent

**A third divergence, same shape**: `_hoist_quest_start_above_writes` carried
its own barrier list (`_HOIST_STOP_RE`) which matched a bare `Function` but
not a typed `Int Function` header — so a `Quest.Start()` could in principle
hoist ACROSS a function boundary into an unrelated body. Also unreachable:
walking back from all 40,586 files' `Start()` sites crosses a typed header
**0 times**. A 200,000-case randomised differential found the loop rewrite
byte-equivalent to the old cursor loop once the barrier was held constant,
and every divergence with it unpinned was this hardening.

**Not currently reachable**: censused all 40,586 generated `.psc` — **0 lines**
begin `If(`, `While(` or `ElseIf(`; the emitter always writes a space. The
pass also missed `While(` openers and typed `Int Function` headers (which
`_balance_if_endif` did handle), both harmless for the same reason.

**Fixed** — `script_convert/tes5/blocks.py` classifies an emitted line once
(`classify`) and resolves depth/stack once (`scan`); the passes consume `Line`
records and no longer mention a keyword. A future emitter change to `If(` now
lands on every pass at once instead of on one of them. Verified: the
scan-based rewrite is byte-identical to the old logic on all 40,586 files, and
a 24-case adversarial suite of unbalanced input agrees everywhere except this
bug.

---

## 6. Two divergent boolean-function lists (measured 2026-08-28)

`_BARE_BOOL_FUNCTIONS` (constants.py, 21 names) and the `_BOOL_FUNC_NAMES`
regex inside `_convert_expression` (34 names) both answer *"does this TES4
function return a boolean?"* — and agree on only **10**. Which collapse a call
receives depends on which list happens to name it: `ref.IsDisabled == 1`
collapses to `ref.IsDisabled()`, `ref.GetDetected == 1` does not.

**Reachable, and wide**: 24 names are in the regex only, used **3,577 times
across 1,944 scripts** (`isactionref` 1,597, `getincell` 688, `getstagedone`
447). 11 names are in `_BARE_BOOL_FUNCTIONS` only.

**Deliberately NOT fixed during the parse-tree rewrite.** Unifying the lists
changes ~1,944 scripts, which would swamp the rewrite's semantic-diff gate and
make an emitter bug indistinguishable from this fix. The rewrite's expression
emitter reads ONE table (`_BARE_BOOL_FUNCTIONS`), so the union lands as a
one-line table edit once the rewrite is verified — at which point the diff is
attributable and reviewable on its own.

---

## 7. `this` → `Self` substitution leaked INTO string literals — FIXED by the tree emitter (2026-08-28)

`_convert_expression`'s terminal substitution pass rewrites the TES4 keyword
`this` to Papyrus `Self` with a regex over the whole expression **text**, so it
also fires inside a quoted string:

```
authored:  "... Almalexia.esp detected. This file is deprecated ..."
shipped:   "... Almalexia.esp detected.Self file is deprecated ..."
```

Both the space and the word are destroyed, in **player-facing** message text.

**Measured**: 9 lines, all in `TES4_mwFnCheckInstallation.psc` (Morroblivion's
installation-warning banner). Narrow only because few converted scripts build
long English sentences.

**Fixed** by the parse-tree emitter, structurally rather than by a better
regex: a `Literal` node with `is_string` set is emitted verbatim and no
substitution pass can reach inside it. This is the class of defect the rewrite
exists to make unrepresentable — the same shape as the `;NE:`-inside-an-
expression family.

---

## 8. A local variable named like a built-in was shadowed by the FUNCTION — FIXED by the tree emitter (2026-08-28)

`fbmwMercCalvusScript` declares `short isdead` and later tests `if isdead == 1`.
Oblivion resolves that to the VARIABLE — a local always wins over a command
name. The string path checked its boolean-function tables before its local
table, so it emitted the call instead:

```
authored:  short isdead   ...   if isdead == 1
shipped:   If (Self as Actor).IsDead()      ← reads the ACTOR, not the variable
correct:   If isdead                        ← reads the declared property
```

The script also declares `Int Property isdead Auto Conditional`, so the
emitted call ignored a property the quest actually writes.

**Fixed** by the parse-tree emitter, which resolves a bare `Ident` against the
script's own declarations before consulting any command table. Found by the
semantic diff (`calls[isdead/0]: 1 -> None`), not by a compile failure — the
old output compiled perfectly and simply did the wrong thing.

**Scope**: 1 script measured across the four verified plugins. Narrow because
few TES4 authors name a variable after a command.

---

## 9. `SetPos <axis>, <value>` wrote the WRONG AXIS — FIXED (2026-08-28)

TES4 separates arguments with whitespace, a comma, or both, so
`Ref.SetPos Z, PlacePosZ` is as legal as `Ref.SetPos Z PlacePosZ`. The
handler split on whitespace only:

```python
parts = args_str.split(None, 1)      # 'Z, PlacePosZ' -> ['Z,', 'PlacePosZ']
axis  = parts[0].upper()             # 'Z,'  -- not in {X, Y, Z}
```

`'Z,'` fails the axis test and the lookup falls back to its X default, so the
value is written to the **X** coordinate:

```
authored:  Ref.SetPos Z, PlacePosZ
shipped:   Ref.SetPosition(PlacePosZ, Ref.GetPositionY(), Ref.GetPositionZ())
correct:   Ref.SetPosition(Ref.GetPositionX(), Ref.GetPositionY(), PlacePosZ)
```

**Measured**: 27 sites in 10 scripts across the four verified plugins,
including Morroblivion's `JDLevitate` and `mwRotationFix` and Nehrim's
`1MarkFxEffectScript`. `SetAngle` shares the handler and the defect.

**Fixed** by splitting on `,`-or-whitespace. Found by the statement
differential — the tree path joins arguments with `", "` and produced the
correct axis, which made the old path's output the outlier.

---

## 10. `GetLOS` was listed as taking no arguments — FIXED (2026-08-28)

`_ZERO_ARG_REF_FUNCTIONS` exists so that `StopCombat, Player` resolves to
`Player.StopCombat()` — for a command that takes nothing, the token after a
leading comma is the RECEIVER, not an argument.

`getlos` was in that set, and it takes a TARGET: `GetLOS, Player` asks
whether **Self** can see the player. Promoting the argument inverted the
question and dropped it:

```
authored:  if ( GetLOS, Player == 1 )
wrong:     Game.GetPlayer().HasLOS()      ← the PLAYER's line of sight, to nothing
correct:   (Self as Actor).HasLOS(Game.GetPlayer())
```

It does not even compile ("function takes 1 parameters not 0"), which is how
it surfaced: **9 Nehrim scripts** failed once the parse tree started
preserving the leading comma. Before that the comma was discarded upstream,
so the promotion never fired and the bad table entry was inert.

**Fixed** by removing `getlos` from the set. Audited the other 61 entries
against their argument counts; it was the only one wrong.

---

## 11. Multi-button `MessageBox` degraded to a plain text box — FIXED (2026-08-28)

`_convert_function_call` split a command line with two regexes:

```python
ref_m = re.match(r'^(\w+)\.(\w+)\s*(.*)', stripped, re.IGNORECASE)
func_m = re.match(r'^(\w+)\s*(.*)', stripped, re.IGNORECASE)
```

The argument tail then reached `_emit_function` as raw TEXT, and every handler
re-split it — on whitespace, on commas, or on both. That tears a quoted
argument apart at the first separator inside it, and a `MessageBox` is
mostly quoted arguments:

```
authored:  messagebox "Do you steer by the stars of the Lover?", "No", "Yes"
shipped:   Debug.MessageBox("Do you steer by the stars of the Lover?")
correct:   TES4_MsgButton = TES4_ShowMsg(TES4Msg_DoomstoneLoverScriptNEW_01)
```

The buttons were dropped, so the box became a notification the player could
only dismiss — the Doomstone asks a question that could never be answered.

**Measured**: 39 button menus restored and 81 `Message` properties added
across the four verified plugins. The same split also ate the space after a
sentence-ending period (`"...the crowd.He screams for help."`) in 232 strings,
because the tail was re-joined with single spaces after being split.

**Fixed** by PARSING the line instead: `_convert_function_call` now builds a
`Call` node and hands `_emit_function` the parsed `args`, so arguments are
separated once, by the parser, and a quoted literal is one token.

---

## 12. `pms <shader>, <n>` created a second, unbound property — FIXED (2026-08-28)

Branches read their first argument as `args_str.strip().split()[0]`, which
keeps the SEPARATOR on the token when the source uses the comma form. The
name then went through `_safe_property_name`, which sanitises the comma to an
underscore:

```
pms effectDrain 5   ->  property `effectDrain`
pms effectDrain, 5  ->  property `effectDrain_`     ← a different property
```

Both spellings mean the same shader, so a script using both declared two
properties for one record and only one of them was ever bound.

**Fixed** by the `arg_src()` / `arg_srcs()` accessors, which read the parsed
argument nodes; the separator is gone before the name is seen. Affects
`pms`, `sms`, `pme`, `sme` and `showmap`.

---

## 13. Twelve commands were treated as unknown by the node path — FIXED (2026-08-28)

`_is_known_command` gates whether `name <args>` is a call at all; an unknown
name becomes `;TODO:` over the whole line. It tested a hand-kept list of
tables, and the branch chain in `_emit_function` had grown twelve names that
appeared in none of them — `setforcerun`, `resethealth`, `setgamesetting`,
`getcrosshairreference` and nine others.

While only the string path reached the command layer this was invisible: that
path never asked the question. Routing statements through the node path made
it live, and `setforcerun 1` — the SpeedMult write — became `;TODO:` in 62
statements.

**Fixed** by deriving `_BRANCH_ONLY_COMMANDS` from the chain itself rather
than maintaining a parallel list. `foreach` is deliberately excluded: it is a
statement keyword intercepted before the command layer.

---

## 14. `GetDayOfWeek` had two conversions and the worse one won — FIXED (2026-08-28)

The command was converted in two places that did not agree:

| Path | Emitted |
|---|---|
| `FIXED_PROPERTY_CALLS` (a call) | `(GameDaysPassed.GetValueInt() % 7)` |
| a branch in the bare-identifier path | `(GameDaysPassed.GetValue() as Int) % 7` |

`GetValue()` returns Float, so the second form typed the whole expression
Float. Assigning it to a TES4 `short` then attracted a SECOND cast:

```
DayofLastUse = (GameDaysPassed.GetValue() as Int) % 7 as Int
```

Which spelling a script got depended only on whether the author wrote the
command bare or as a call — the same command, two answers.

**Measured**: 42 call sites across Knights.esp and Morrowind_ob.esm.

**Fixed** by deleting the duplicate branch and routing both spellings
(`getdayofweek`, `getdayoftheweek`) to the table. Found by the S1 typing
harness: `symbols.type_of_expr` typed the expression Int from the tree while
the old text scan typed it Float, and the disagreement was the bug.

---

## 15. `FUNCTION_MAP` silently drops 20 entries — LATENT (2026-08-28)

The literal has **537 keys but evaluates to 517**: 17 keys are written more
than once and Python keeps only the last. Four of them carry *different*
values, so a working mapping is overwritten by `(None, ...)`:

| Key | Earlier | Later (wins) |
|---|---|---|
| `getcontainer` | `('GetContainer', True, None)` | `(None, True, None)` |
| `setdoordefaultopen` | `('SetOpen', True, None)` | `(None, True, None)` |
| `setdisplayname` | `('SetDisplayName', True, None)` | `(None, True, None)` |
| `getinfame` | `(None, False, None)` | `(None, True, None)` |

**Not currently a live defect**: three of the four are rescued by an explicit
branch in `_emit_function` (which is *why* those branches exist), and
`setdisplayname` correctly degrades because Skyrim needs SKSE for it. But the
duplicates are invisible, and a future edit to the earlier entry would have no
effect. To be resolved when the command tables merge into one row per command,
where a duplicate key is detectable.

---

## 16. Two disagreeing lists of Bool-returning Papyrus names — FIXED (2026-08-28)

The same fact — "does this Papyrus function return Bool" — was recorded twice:

| Where | Form |
|---|---|
| `constants.PAPYRUS_BOOL_FUNCTIONS` | a `set` of 53 names |
| `converter._BOOL_FUNC_NAMES` | a regex alternation of 33 names |

They disagreed by **twelve names** — `IsDetectedBy`, `HasLOS`, `CanSee`,
`GetDetected`, `IsAnimPlaying`, `IsRidingMount`, `IsHostileToActor`,
`IsWeaponDrawn`, `IsChild`, `IsAlarmed`, `IsCompleted`, `IsObjectiveCompleted`
— so whether a Bool got its `as Int` depended on which list the code path
happened to consult. `Temp = Player.IsDetectedBy(x)` reached the set (which
lacked it), got no cast, and failed to compile:

```
Checker error: value with type `Bool` cannot be assigned to a variable with type `Int`
```

**Fixed** by merging the twelve into `PAPYRUS_BOOL_FUNCTIONS` and DERIVING the
regex from it, so there is one list. A side effect, and the intended one: a
`GetLOS Player == 0` now knows its left side is Bool and collapses to
`!(...HasLOS(Player))` instead of comparing a Bool to `0` — 21 scripts.

**Also fixed in the same pass**: `RETURN_TYPES` is keyed by the bare Papyrus
method, but `FUNCTION_MAP` maps some commands to a QUALIFIED name
(`rand` -> `Utility.RandomFloat`). Without stripping the class prefix,
`set randint to Rand 1 5` looked untyped and lost its cast in 4 Morroblivion
scripts.

---

## 17. Type coercion guessed from emitted text — REPLACED (2026-08-28)

`_coerce_float_to_int` decided whether an assignment needed `as Int` by
running four scans over the ALREADY-EMITTED Papyrus: a Float-function regex,
a `\d+\.\d+` literal probe, an identifier sweep looking up each name, and a
Bool-function regex. All four re-derive the value's type from its rendering,
where a command name inside a string literal counts as a call and the shape of
the arithmetic is invisible.

Replaced by `symbols.type_of_expr`, which types the value from its PARSE TREE
before any text exists. Verified by differential harness over the corpus:

| Plugin | Assignments to Int targets | Disagreements |
|---|---|---|
| Oblivion.esm | 1,076 | 0 |
| Nehrim.esm | 1,133 | 0 |
| Morrowind_ob.esm | 1,414 | 0 |
| Knights.esp | 570 | 0 |

Getting to zero is what surfaced §14 and §16 — both were cases where the tree
and the text scan disagreed, and the tree was right.

---

## 18. Operator precedence encoded twice, and the copies disagreed — LATENT (2026-08-29)

`tes4/parser._PRECEDENCE` (six tiers, which the parser BINDS by) and
`emit/expr._PRECEDENCE_RANK` (five ranks, which the emitter PARENTHESISES by)
were written out separately and did not match: the parser gives `==` and `<`
their own tiers, the emitter collapsed both to rank 2.

The emitter parenthesises a child only when it binds LOOSER than its parent, so
the disagreement drops the parens on an equality nested under a relational
operator. `(a == b) < c` emitted as `a == b < c`, which Papyrus re-reads as
`a == (b < c)` — a different expression. Twelve operator pairs changed
parenthesisation once the tables were unified.

**Not observed in any script.** Censused all 6,364 exported TES4 scripts, 4,010
of which use a relational operator: **zero** occurrences of the shape. It cannot
change current output, which is why the semantic diff is unmoved.

Fixed by deriving both from `tes4/lexer.PRECEDENCE` — the lowest layer the
parser and the emitter both reach (`tes4/*` is stdlib-only, so `constants.py`
was not available).

---

## 19. `Activate` drops its arguments when the caller passes nodes — LATENT (2026-08-29)

The `activate` branch read its arguments as

```python
parts = self.arg_srcs(args_str) if args_str else []
```

`arg_srcs` reads the parsed argument NODES, but the guard tests the parallel
SOURCE-TEXT channel. A caller supplying nodes with an empty `args_str` would
have had every argument silently discarded — the activator and the run-flag
both lost, emitting a no-argument `Activate()`.

**Not reachable today**: measured 0 occurrences over 6,082 scripts, because the
only caller that supplies nodes also built the text. It was one caller away, and
removing the second channel (both are now derived from the nodes) makes it
unreachable by construction rather than by luck.

## 20. Feature flags scanned from raw source matched COMMENTS — FIXED (2026-08-29)

Six per-script flags were set by scanning the lowercased source as text:

```python
self._uses_timer = bool(re.search(r'\btimer\b', source_low))
self._uses_say   = bool(re.search(r'\bsay(?:to)?\b', source_low))
```

A text scan cannot tell a call from the same letters inside a comment or a
string literal. Measured over 6,082 exported scripts, tree-derived facts against
the text scans:

| Flag | Scripts the text scan got wrong |
|---|---|
| `uses_timer` | **122** |
| `uses_say` | 8 |
| `uses_getsecondspassed` | 7 |
| `elapsed_is_realtime` | 6 |
| `uses_say_timer` | 1 |

Every difference is the same direction — the scan says true, the tree says
false — and every sample is a COMMENT: `;Timer for pirate placement`,
`;Float Timer`, whole commented-out `;Begin GameMode` blocks.

`_uses_timer` picks the poll interval in `_get_update_interval`, so **122
scripts polled every 0.25s when they should poll every 0.5s** — twice the VM
load, forever, because of a word in a comment. `DLCOrreryConsoleScript`,
`DLC06FletcherScript` and `ND02BattleControlSCRIPT` are among them.

Replaced by `script_convert/facts.py`, which derives all six from the parse
tree. Semantic diff 475 -> 515: 40 scripts whose interval is now correct.

---

## 18. Nehrim's 161 compile failures — three causes, all generic

`--scripts-only` on Nehrim failed 161 of 3,488 scripts. All four plugins now
compile 100%: Oblivion 16,519/16,519, Nehrim 3,488/3,488, Morrowind_ob
17,970/17,970, Knights 635/635.

### `;/` opens a Papyrus BLOCK comment — 159 of the 161

TES4 has only line comments, so a German divider written `;/////` is inert
there. Papyrus reads `;/` as the start of `;/ ... /;` and swallows the rest of
the file. Because the compiler parses every script on the header path, ONE such
line in `TES4_BergklosterZugbrueckeSCRIPT` failed **159 unrelated scripts** with
"unexpected end of file" at a line past the end of a 206-line file.

Master emitted `; /////` (a space after the semicolon) and never hit it.
`emit/script._safe_comments` now breaks the pair on every emitted comment.
Measured: 86 occurrences across 14 Nehrim scripts; zero in the other 3 plugins.

### A bare `GetInWorldspace` means the PLAYER

`Player.GetInWorldspace X` parses with no receiver in a `ScriptEffectStart`
block, and the `RAW` row defaulted `{ref}` to `Self` — an ActiveMagicEffect,
which has no `GetWorldSpace`. A `Cmd` row may now name its bare-receiver
subject as `defaults={'ref': ...}`, reusing the argument-default vocabulary
rather than adding a field for one command.

### An int-assigned `ref` that also holds a base record must be `Form`

`resolve_ref_types` dropped any variable with `int_assign` and no `form_type`,
leaving it `ObjectReference`. TES4's clear idiom (`let r := 0`) sits in the same
variable as a real record, and Papyrus accepts neither in the other's type:
`AAGeneralUpdateQuest.rCrosshairsLast` holds both `0` and the ARMO `JailShoes`.
`Form` is the one handle that takes every record, so the mixture WIDENS rather
than narrowing. (`ref_as_base_form` could not see it: the cross-ref scanner
matches `set X to Y`, and this script uses OBSE's `let X := Y`.)

### A trailing cast is only "already cast" if it covers the WHOLE expression

`_cast` returned early on any text ending in `as Int`. `100.0 -
X.GetValue() as Int` ends that way while casting only the right operand, so the
subtraction stayed Float and would not assign to an Int. The early return now
also requires `not _needs_parens(text)`.

---

## 19. Semantic drift vs master: 686 scripts, every cause classified

Master-head (`e96bb7b`) baselines for all four plugins are in `temp/psc_master`.
Every difference was bucketed by its exact `_diff_model` signature (71 causes)
and every LOSS of an event, call or write was listed individually.

**No regressions remain.** The drift splits three ways.

### Master bugs this rewrite FIXES

| Scripts | Defect in master |
|---|---|
| 165 | `Set EPWert to 15` DROPPED — the value feeds Nehrim's XP-award call, so 165 creatures awarded 0 XP |
| 76 | Sentence spacing stripped from message text (`skill.Anyone`) |
| 34 | An unused `Actor Property mySelf Auto` emitted, shadowing nothing |
| 18 | `;NE: no converted music` where a real `MusicType` resolves — music now plays |
| 9 | `TES4_CGRopeBucketScript` and 8 Nehrim scripts had a whole body commented out: a `begin OnHitWith <weapon>` filter was judged unconvertible because the body binds the weapon property FIRST. A base record compares to a `Form` parameter fine. Shooting the CharacterGen rope bucket now advances MQ01 to stage 58 again |
| 8 | `If True` where one term of a `&&` was unconvertible — the freeze spell fired on every target. `_logical` keeps the convertible terms |
| 1 | `SEObeliskNewSCRIPT`: `setDestroyed 0` and `setDestroyed 1` BOTH became `DestroyAfterAnimation`, so the "safety net" flipped nothing |

### Deliberate improvements, semantically identical

- `GetValue() as Int` -> `GetValueInt()` (173), dropping a redundant `as Int`
- Empty events dropped: `OnEffectFinish` (15), `OnEffectStart` (2),
  `OnPackageEnd`, `OnDeath` — an empty Papyrus event does nothing
- Redundant `(X as Actor)` removed where the method is on ObjectReference
  (`GetParentCell`, 23) — includes 6 of the 7 gated CharacterGen diffs
- `HasLOS(...) == 1` -> `HasLOS(...)`; `Self.GetAngleZ()` vs `GetAngleZ()`
- Correct nesting: master emitted every nested body flat
- The measure-then-deliver `Say` pair collapsed (8), which master played twice

### Regressions found and FIXED during this pass

| Was | Fix |
|---|---|
| An OnActivate with an EMPTY body was dropped, losing the door-relock preamble AND the activation consume | `events()` keeps a block that `_consumes_activation` reports, body or not |
| `TES4Polyfill.EnterOblivionGate` was never emitted — the gate's identity is known only on the line that discards it | `assemble.gate_capture`, keyed on the authored `set MQ00.nearOblivionGate to 0` |
| `ModPCSkill`/`AdvancePCSkill` emitted `(Self as Actor).ModActorValue(...)` — `None` on the Quest scripts that call it, so 12 scripts' skill gains did nothing | `_AV_PLAYER_ONLY` routes both to `Game.GetPlayer()`. (`Game.AdvanceSkill` is NOT the right target: per the CK wiki it adds skill-USAGE progress and "won't necessarily change the Skill itself", where TES4's `ModPCSkill Blade 10` raises the skill by 10) |
| `TES4Polyfill.RestoreFallDamage` was never emitted — `suppressed_fall_damage` had no setter after the rewrite | derived in `_load_facts` from the tree; `fall_damage` now receives the assembled body so it MERGES into the script's existing `OnEffectFinish` instead of declaring a second one |

### Gated CharacterGen scripts

7 changed, each verified: 6 are the redundant-cast and bool-collapse
improvements above; the 7th (`CGRopeBucketScript`) is a body master had
disabled entirely.
