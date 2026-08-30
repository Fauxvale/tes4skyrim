# script_convert: measurements and failure modes

The working log behind the architecture contract.

### What the baseline IS

`temp/psc_semantic/` matches **HEAD**. The working tree is not expected to
match it: the current output is *HEAD plus the intentional bug fixes* recorded
in [script_conversion_bugs.md](../notes/script_conversion_bugs.md), which is the ledger
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
- 🛑 **Inline comments track COMPLEXITY, but the comment rules stand.**
  Measured across the package: inline comments per 10 code lines run **1.16**
  at complexity 1-5, 2.18 at 6-10, 2.80 at 11-25 and **7.42 at 26+** — a 6.4x
  spread. `_emit_function` (complexity 295) carried 869 inline comments against
  882 code lines. The correlation shows prose compensating for code that cannot
  state its own intent — `converter.py` sits at a 0.59 comment-to-code ratio
  against 0.13 for the clean AST layer — so it argues for KEEPING the pressure
  on, not for exempting the comments.

  An earlier revision of this section concluded the opposite and told future
  agents not to add a docstring-only rule. That is superseded: `inline-comments`
  and `stray-comments` are gated at 0 by `tools/validate/code_rules.py`, and
  evidence that must survive (a measurement, a census count, a reverted attempt)
  goes to a `docs/` file cited by a `See:` line, which the gate verifies
  resolves. Compression keeps every fact and drops the narration.

  What is forbidden at any complexity: a comment that narrates the next line.
  If a function of complexity <10 needs inline signposts, the docstring is
  missing, not the comments.
