# `script_convert/` architecture — read this BEFORE writing any code here

> **The decision procedure is §3. If you are about to add or change something,
> start there — it routes any change to exactly one file.**
>
> Score every change: `python tools/script/arch_fitness.py --fail-on-regression`
> (0.6s). Correctness is `psc_semantic_diff.py`, never pytest — see §6.

`script_convert/` converts TES4 (Oblivion) script source to Papyrus. It grew as
a line-by-line regex rewriter: it never built a representation of the script, so
every question needing structure — *is this expression balanced? is this
identifier an Actor? does this `If` have its `EndIf`?* — was re-answered by
pattern-matching **text the converter itself had just emitted**. Each new script
exposed a shape the rewriter mangled, the fix was another repair pass, and the
passes began to interact.

A parse tree now exists and is live. This document is the target it is being
rewritten toward, and the rules that keep it there.

---

## 1. The layers

**The pattern is an AST compiler.** Source becomes tokens, tokens become a tree,
the tree is resolved against a symbol table, and only then is Papyrus emitted.
Text flows one way. Nothing ever reads back what was emitted.

```
    TES4 source
        |  tes4/lexer.py          characters -> tokens
        v
     tokens
        |  tes4/parser.py         tokens -> AST   (never raises; degrades to Raw)
        v
      AST  (tes4/nodes.py)
        |  facts.py               tree -> ScriptFacts   (what this script needs)
        |  symbols.py             tree -> types, UDF signatures, ref classes
        v
    resolved tree
        |  emit/expr.py           Expr -> Value
        |  emit/stmt.py           Stmt -> Value
        |  emit/commands.py       Call -> Value   (COMMAND_ROWS, then commands/)
        |  emit/script.py         bodies -> laid-out lines
        v
    assemble/                     events, properties, the .psc file
        v
    pipeline.py                   jobs, batching, VMAD, writing
```

### Layer table — a module may import only from strictly LOWER layers

```
L0  naming.py                      stdlib only
L1  constants.py                   L0
L2  tes4/{lexer,parser,nodes}      stdlib only
    tes5/blocks.py                 stdlib only
    cross_ref.py                   L0, L1
L3  symbols.py, facts.py           L0-L2
    context.py                     L0, L1          (dataclasses only)
L4  resolve.py, emit/api.py        L0-L3
L5  emit/*, commands/*             L0-L4  (commands reach L4 via api ONLY)
L6  assemble/*, udf.py             L0-L5
L7  converter.py                   L0-L6
L8  pipeline.py                    L0-L7
```

🛑 **This includes function-local imports.** A deferred
`from script_convert.emit import expr` inside a method is still an import, and
that pattern is exactly how the current layering violation survives (local-imports counts
them; there are 60 today). If you need a lower layer to reach a higher one, the
design is wrong — pass a value in, do not import upward.

### File responsibilities

| File | Owns |
|---|---|
| `naming.py` | Pure name/type functions: `papyrus_script_name`, `_safe_property_name`, record-type maps. No package imports. |
| `constants.py` | **Data only.** `COMMAND_ROWS` and the TES4/Papyrus vocabulary tables. |
| `tes4/lexer.py` | TES4 source → tokens. Column fidelity is load-bearing (see §2). |
| `tes4/parser.py` | Tokens → AST. **Never raises**; degrades to `Raw`. |
| `tes4/nodes.py` | The AST dataclasses — the contract every emitter reads. |
| `tes5/blocks.py` | The single Papyrus-surface classifier (`classify`/`Kind`/`scan`). |
| `cross_ref.py` | The FormID/EditorID/script graph over exported records. |
| `symbols.py` | Node-based type inference. **Pure; the model to copy.** |
| `facts.py` | `ScriptFacts` derived from the **tree** — never from raw source. |
| `context.py` | `ScriptContext` (per-script state, explicit) + `PluginFacts` (per-run). |
| `resolve.py` | `Resolver`: name → meaning. Owns `xref` and the property registry. |
| `udf.py` | OBSE user-function signatures, built from trees during the run. |
| `emit/api.py` | `EmitCtx` — the **only** surface `emit/` and `commands/` may touch. |
| `emit/value.py` | `Value`: text + Papyrus type + is_comparison + notes. |
| `emit/expr.py` | Expression node → `Value`. TES4→TES5 semantics only. |
| `emit/stmt.py` | Statement node → `Value`. |
| `emit/script.py` | Body walk + layout: indentation, closers, comment re-attach. |
| `emit/commands.py` | The row engine and the one command dispatcher. |
| `commands/*.py` | ~35 handlers that cannot be rows, by domain. |
| `assemble/*.py` | `script`, `fragment`, `events`, `properties` — the .psc file. |
| `converter.py` | **The compatibility façade only** (§5). |
| `pipeline.py` | Jobs, workers, batching, VMAD packing, writing files. |

---

## 2. What the tree already answers — do not re-derive it

The parser owns these. Asking them again in text is the defect this whole
architecture exists to remove.

- **Nesting and balance.** An `If` node owns its body, its elseif chain and its
  else. Emitted blocks are closed by construction; there is nothing to repair.
- **Comment attachment.** A trailing `; ...` rides on `Stmt.comment`, so it can
  never be emitted mid-expression and comment out the rest of the line.
- **The author's parentheses.** `Expr.parenthesised` is recorded so output can
  echo them. Recorded, never interpreted — the tree already encodes precedence.
- **Receiver vs argument.** `Call.leading_comma` says the argument list opened
  with a comma, which is the only thing distinguishing `StopCombat, Player`
  (Player's combat) from an argument. Dropping it acted on the wrong actor.
- **Argument adjacency.** `SetFactionRank X -1` (a negative argument) versus
  `GetPos Y - 10000` (subtraction) is decided by **token column adjacency**.
  This is why lexer column fidelity is load-bearing and why source text must not
  be reflowed before parsing.

### The typed emission contract

An emitter returns a **`Value`**, never a bare `str`:

```python
@dataclass(frozen=True)
class Value:
    text: str                    # Papyrus source
    ptype: str = ''              # Bool | Int | Float | ObjectReference | ...
    is_comparison: bool = False  # a top-level ==/!=/</>/&&/||
    notes: tuple[str, ...] = ()  # ;NE: / ;TODO: markers
```

This exists because a `Call` node's emission can **become** a comparison —
`GetInCell X` is one node that emits `GetParentCell() == X` — and nothing on the
node records that. Without `Value`, the emitter has to re-read its own output to
find out what it just produced.

`is_comparison` is derived from the row template **at import** (443 templates,
once), never from output, so it cannot drift.

🛑 **`notes` is the ONLY channel for `;NE:` / `;TODO:` markers.** Never a mutable
list on the converter that a caller clears and reads back.

---

## 3. The decision procedure

Run this before writing code. It terminates at exactly one file.

```
0. Is the change a repair to EMITTED PAPYRUS TEXT?
     -> FORBIDDEN. Ask instead: what NODE produced that text? Fix it there.
        Missing information -> add a Value field or a ScriptFacts field.
        NEVER a pass over lines.

1. A TES4 COMMAND?
   1a. Fits ONE Papyrus template over the placeholders
       {ref} {aN} {sN} {bN} {pN} {iN} {cN} {fN} {fmt} {?n}?
         -> constants.COMMAND_ROWS.  Add a ROW.  WRITE NO CODE.
   1b. Needs real logic (branches on argument VALUES, emits several lines,
       consults PluginFacts, synthesises a helper)?
         -> commands/<domain>.py, one @command handler, (ctx, call) -> Value.
            Domains: quest dialogue actor faction world ui av anim obse misc.
            If none fits it is misc.  NEVER an 11th module for one command.
   1c. No Papyrus equivalent?  -> a row with note=.  Still 1a.

2. A new STATEMENT kind the parser must recognise?
     -> tes4/nodes.py + tes4/parser.py + emit/stmt.py.
        EXACTLY three files.  A fourth means you got it wrong.

3. An EXPRESSION rule (operators, comparisons, casts, null-tests)?
     -> emit/expr.py.  A comparison rewrite is a row in _CMP_RULES,
        never a new `if` in _binop.

4. "What does this BARE NAME mean?" -- local vs form vs command vs global
   vs actor value vs cross-script member?
     -> resolve.py.  Nowhere else may ask.

5. A RECORD or TYPE lookup?
     -> cross_ref.py   a graph query over exported data
        naming.py      a pure string/type mapping
        resolve.py     needs the current script's context to decide
        NEVER constants.py.

6. A new PER-SCRIPT fact ("this script uses X, so emit Y")?
     -> facts.py, derived from the PARSE TREE.
        NEVER a regex over `source`.  If the tree cannot answer it, the parser
        is missing a node -> go to 2.

7. A new PER-RUN fact (something the pipeline scans once)?
     -> pipeline builds it, context.PluginFacts carries it, emit reads
        ctx.plugin.  NEVER a mutable class attribute.

8. A CONSTANT -- a set of names, a map, a threshold, a template?
     -> constants.py  TES4/Papyrus vocabulary
        naming.py     a naming rule
        A module-private tuning value used by exactly ONE function may sit
        beside it.  Used twice = a constants.py entry.
        NEVER rebuilt inside a function body.
        EXCEPTION, and the layer table outranks this step: `tes4/*` is
        stdlib-only, so a table it shares with a HIGHER layer goes in the
        LOWEST module both legally reach -- never in constants.py.
        `lexer.PRECEDENCE` is the worked case (bugs ledger 18).

9. File layout, event headers, property declarations, the poll loop?
     -> assemble/{script,fragment,events,properties}.py

10. Jobs, workers, batching, VMAD, writing files?
     -> pipeline.py

11. Changes ScriptConverter's public shape, _property_refs type strings,
    emitted property/fragment/script NAMES, or a derive_formid key?
     -> STOP.  That is the compatibility boundary (§5).  Find another way.
```

### Self-check — where misplaced work belongs

| Symptom | Wrong home | Right home |
|---|---|---|
| "`GetInCell` emits a comparison, so `== 1` collapses" | reading emitted text | `Value.is_comparison` |
| "this script uses a timer" | regex over `source_low` | `facts.py` |
| "two `Say` calls duplicate" | a post-pass over lines | `commands/dialogue.py` — don't emit the second |
| "a Float went into an Int variable" | `_coerce_*` over lines | `Value.ptype` at the assignment |
| "a ref compared to 0" | `_fix_ref_zero` over lines | `emit/expr.py` `_binop` |
| "UDF arg types are wrong" | re-reading `.psc` files | `udf.py` |
| "a condition got commented out" | a repair pass | impossible — the comment rides on the node |

---

## 4. Invariants

Measured by `tools/script/arch_fitness.py`; the metric id is in brackets.

1. **Imports point strictly downward**, at any nesting depth. [local-imports]
2. **No function in `emit/`, `commands/`, `assemble/` or `converter.py` takes
   emitted Papyrus.** No parameter named `line`/`lines`/`text`/`emitted`/`psc`;
   no `_fix_*`, `_repair_*`, `_coerce_*`, `_postprocess*`. [text-repair-fns]
3. **One parse.** Exactly one non-test call site of `parser.parse`; zero
   references to `emit_source`, `_convert_expression`, `_tree_expression`,
   `USE_TREE_EXPRESSIONS`. [reparse-round-trip]
4. **`constants.py` is data**: no parameter named `xref`/`conv`/`ctx`, no
   `open(`, at most a handful of pure `def`s. [logic-in-constants]
5. **No module-level I/O at import.** [logic-in-constants]
6. **No mutable class-level `dict`/`list`/`set`.** [mutable-class-state]
7. **Zero private reach-in**: `conv._` / `ctx._` absent from `emit/`,
   `commands/`, `assemble/`. [private-reach-ins]
8. **One out-channel**: `_line_comments` appears nowhere; emitters return
   `Value`. [text-repair-fns]
9. **Per-script state is declared**: `ScriptContext` is a dataclass and there is
   **no `_reset`** — a new script gets a new instance. [mutable-class-state]
10. **No source re-scanning after parse** in `assemble/`, `emit/`, `commands/`,
    `converter.py`. [source-rescans]
11. **Command knowledge in one table**: `set(COMMAND_ROWS) & set(registry)` is
    empty; their union defines "known command". [satellite-cmd-sets]
12. **No satellite per-command flag sets** — flags are fields on the row. [satellite-cmd-sets]
13. **No `.py` over 1,000 code lines.** [oversized-files]
14. **The compatibility surface is frozen** (§5).
15. **Comments compress, knowledge does not.** [`stray-comments`/`inline-comments` down, the anchors kept]

---

## 5. The compatibility boundary — do NOT change

Measured from every importer, tool and test that touches this package.

- `ScriptConverter(xref)` with `convert_standalone(name, source, extends,
  editor_id)` and `convert_fragment(source, extends) -> list[str]`, which
  **must preserve `_property_refs` across calls**; `get_property_refs()`;
  `get_cell_family_helpers()`; `set_scro_aliases()`; `set_music_cues()`.
- **`._property_refs` is a LIVE attribute.** `tes5_import/dialog_converter.py`
  reads the private directly (`:222`, `:1352`) and `pipeline._add_scro_ref`
  both reads and writes it (`:1652`, `:1661`). Implement it as a property over
  `Resolver.property_refs`.
- Class attributes `say_durations`, `say_topics`, `topic_unlock_globals`,
  `message_menus`, `chargen_menus`; module constant `SAY_START_WAIT`;
  `sctx_onactivate_consumes`.
- **The `_property_refs` type-string vocabulary, verbatim.** Consumers compare
  literals (`== 'ActorBase'`, `.startswith('TES4_')`) to pick base-vs-reference
  FormIDs.
- Emitted shapes: `Type Property Name Auto` one per line (parsed back by
  `tools/validate/vmad_property_typecheck.py`); **both** `Fragment_0` and
  `Fragment_1` per INFO; `Fragment_Stage_NNNN_Item_M`; `TES4Polyfill.<Fn>`
  matching the hand-written polyfill; `papyrus_script_name(edid)` as ScriptName
  **and** filename **and** VMAD name.
- `pipeline.py`'s surface used by `tes5_import/` and
  `tools/script/convert_scripts_subset.py` (which also reaches six private
  names).

🛑 **FormID drift is a hard stop.** `derive_formid(site, key)` keys on authored
data. Changing which properties a command registers can move ids, and moving one
id breaks every existing save. Measure before shipping; if even one moves, stop
and ask.

---

## 6. Verification

**The semantic diff is the test of correctness. Not pytest.**

```bash
# every edit -- 0.6s, no build
python tools/script/arch_fitness.py --fail-on-regression

# at every stage boundary -- one build at a time
python convert.py -f Oblivion.esm     --scripts-only
python convert.py -f Nehrim.esm       --scripts-only     # standalone, OBSE-heavy
python convert.py -f Knights.esp      --scripts-only
python convert.py -f Morrowind_ob.esm --scripts-only     # masters, largest corpus

python tools/script/psc_semantic_diff.py compare \
    -f Oblivion.esm -f Nehrim.esm -f Morrowind_ob.esm -f Knights.esp --show 0
python tools/validate/vmad_property_typecheck.py
```

`psc_semantic_diff` compares what each script **does** — properties, locals,
events, calls-with-arity, literals, writes, `extends` — and ignores spacing,
temp names, declaration order and parenthesisation. That is what makes it usable
as the safety net for a rewrite: text is expected to change, behaviour is not.

### What the baseline IS

`temp/psc_semantic/` matches **HEAD**. The working tree is not expected to
match it: the current output is *HEAD plus the intentional bug fixes* recorded
in [script_conversion_bugs.md](script_conversion_bugs.md), which is the ledger
for this rewrite — 17 numbered, dated entries marked FIXED / LATENT / REPLACED,
each naming what was measured and in which scripts.

So the changed-count is **not noise and not debt**. It is the footprint of
those deliberate fixes, and every script in it should be traceable to a
numbered entry.

🛑 **If the count GROWS, you had better have a very good reason.** It is a
justification threshold, not a hard cap — a refactor legitimately uncovers real
defects, and §14, §16 and §17 of the ledger were all found exactly that way,
by the tree and the text scan disagreeing. What is forbidden is growth that
nobody looked at. When the number moves up:

1. Name every newly-changed script (`--show`).
2. Decide, per script, whether it is a fix or a regression.
3. A fix gets **a new numbered entry in `script_conversion_bugs.md`** in the
   existing format: what was measured, how many sites in which plugins, the
   authored-vs-shipped-vs-correct comparison, and how it was found.
4. A regression gets reverted.

Growth without steps 1-3 is how a rewrite quietly ships twelve behaviour
changes because "the number only moved a little".

A change is acceptable when **all four** hold:

1. **No CharacterGen script changed.** The tutorial dungeon is the most
   play-tested content in the project and every new game passes through it.
   `psc_semantic_diff` exits 1 and names the file. **Any gated diff is a
   regression until proven otherwise: revert first, diagnose second** — never
   explain it away in the pass that introduced it.
2. **Every semantic change is accounted for** in
   `script_conversion_bugs.md` — see the threshold rule above.
3. **All scripts compile, 0 failures.**
4. **No fitness metric moved away from its target.**

`Morrowind_ob.esm` is non-negotiable: the only plugin exercising
`ctx.master_export`, and at ~18,000 scripts the largest corpus.

🛑 **A stale baseline makes the whole guarantee meaningless.** Re-snapshot
(`psc_semantic_diff.py snapshot --all`) from a clean build before starting, and
confirm `compare` reports **0 changed** — that zero is what proves the net is
measuring the tree you are actually working from.

---

### S3 closed the round trip (2026-08-29)

`emit_call` flattened its parsed argument nodes back into a TES4 source string
and handed it to `_emit_function`, which re-split and re-parsed it. Measured
before cutting, since the cut depends on the two channels agreeing:

| Check | Result |
|---|---|
| Calls reaching `_emit_function` with nodes | 44,322 |
| Where `args_str` differed from the rebuilt node text | **0** |
| Expression conversions through the tree | 30,315 |
| Falling back to the string scanner | **0** |
| `parser.parse` calls per script, after | **1.000** |

`args_str` is gone from `_emit_function`, from all four argument accessors and
from the row engine's `_Args`. `_convert_expression` and `_tree_expression` are
deleted -- 38 call sites became `arg_expr(n)`, which emits from the node.

**reparse-round-trip measures the ROUND TRIP, not `emit_source`.** The old pattern counted every
`emit_source` mention, which was right while it fed the re-parse. It is now a
node->text formatter for `;NE:`/`;TODO:` markers and for keying a lookup on the
authored spelling -- neither re-enters the parser. The pattern was narrowed to
`_convert_expression(` / `_tree_expression` / `parse(emit_source` /
`tokenize(emit_source`, and reparse-round-trip is 0.

**One real regression, caught by the semantic diff and fixed.** Routing the
printf helpers at `_format_string_call` onto the node list made them ignore the
trimmed argument string their callers had built: `message "Rank %.0f Fireball",
SpellRank, 10` emitted the trailing display-duration as text
(`+ (10 as String)`), 73 scripts. Fixed by passing argument INDEXES down instead
of a rebuilt string, so the trim happens on nodes and both helpers stop
reconstructing text at all.

### Why `long-functions` is 60, not 80

Measured across 7,320 first-party functions: **p50 = 12 lines, p75 = 25,
p90 = 49, p95 = 75.** A 60-line cap fires on 534 (7.3%); 80 fires on 320
(4.4%) and 40 on 978 (13.4%).

The deciding argument is not the percentile but the OVERLAP with
`god-functions`. Median complexity by length runs **10 at 40-59, 14.5 at
60-79, 19.5 at 80-99 and 25 at 100-119** — so a complexity-25 rule does not
fire until roughly 100 lines. At an 80-line cap the two rules nearly coincide
and the length rule adds little; at 60 it catches the shape complexity cannot
see, a **long straight-line function** (70 lines of sequential assignment at
complexity 3 — what `measure` was before it was split into phases).

### ms-per-script is noisy; sample it before believing it

ms-per-script is a median of 5 runs x 40 iterations, which is not enough to reject
background load. It read 0.75-0.82 ms three times during S2 -- an apparent
1.8x regression -- while six clean samples on the same code gave 0.452-0.495
(median 0.465 against a 0.443 baseline, i.e. 1.05x). Every high reading
coincided with another job on the machine.

So a single ms-per-script flag is not evidence. Re-sample it 5-6 times with nothing else
running before acting; a real regression holds its value across samples.

### The baseline is not a scratchpad

`--update-baseline` used to write whatever it measured. That makes the whole
fitness suite advisory: add a violation, refresh the baseline, and
`--fail-on-regression` compares the file against itself and reports
`no regressions`. It happened -- 5 plain-`#` comments were added to
`psc_semantic_diff.py` and enshrined in the same session.

`--update-baseline` now REFUSES when any metric moved away from its target and
prints which. `--accept-regression` overrides it, and the override is the point:
accepting one becomes a deliberate, visible act rather than a side effect of
running the tool.

🛑 **Refresh the baseline only at a stage EXIT, never mid-edit.** The metrics are
package-wide totals, so a new violation in one file nets out against unrelated
improvements elsewhere in the same run and the guard never sees it. Order:
fix, verify with `--fail-on-regression`, THEN refresh.

## 7. Why it is this way — the failure modes to not repeat

- **A stage's exit criterion must be a STRUCTURAL FACT, never a deletion list.**
  "Delete these helpers" was satisfied by *moving* them: every named function
  went away, the corpus stayed byte-identical, and the package shrank 54 lines
  while `parse()` still had zero callers. Write it as a property that cannot be
  faked — which is what the fitness metrics are.
- **Build the foundation and USE it in the same change.** An unwired foundation
  is indistinguishable from dead code, and the next change routes around it.
- **Count lines, not branches.** "98 of 197 branches are identical" sounds like
  thousands; it was 356.
- **A metric that cries wolf gets deleted.** The naive form of invariant 2 fires
  32 times and flags `tes4/parser.py` (which legitimately takes TES4 source) and
  `tes5/blocks.py` (which legitimately is the Papyrus classifier). Scope it, and
  give every exemption a comment saying why.
- **Comment volume is not comment value.** The comment-to-code ratio correlates
  *inversely* with code quality here: `converter.py` sits at 0.59 and the clean
  AST layer at 0.13, because prose was compensating for code that could not
  express its own intent. Compression is the comment counts down with every anchor kept — the same
  knowledge in fewer characters, never fewer facts.
- 🛑 **Inline comments are a SYMPTOM OF COMPLEXITY, not a style choice — so
  there is no comment rule, only god-functions.** Measured across the package: inline
  comments per 10 code lines run **1.16** at complexity 1-5, 2.18 at 6-10,
  2.80 at 11-25 and **7.42 at 26+** — a 6.4x spread. `_emit_function`
  (complexity 295) carries 869 inline comments against 882 code lines, roughly
  one per line, because a function that large cannot be navigated without a
  map. The 193 simple functions need about one apiece.

  So do **not** add a "docstring only" rule: it would ban the ~92 inline
  comments that carry real evidence (a measured date, a census count, a
  reverted attempt) pinned to the line they justify, while doing nothing about
  the cause. Drive god-functions to 0 and the narration becomes unnecessary on its own.

  What IS forbidden, at any complexity: a comment that narrates the next line.
  If a function of complexity <10 needs inline signposts, the docstring is
  missing, not the comments.
