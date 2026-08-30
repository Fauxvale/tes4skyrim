#!/usr/bin/env python3
"""Fitness functions for `script_convert/` -- the architecture, as numbers.

`script_convert/` is being rewritten from a line-by-line regex rewriter into an
AST compiler (docs/script_convert_architecture.md).  A refactor that large
cannot be policed by review: the failure mode of the last attempt was that
every named helper was DELETED from its old file and RECREATED in a new one,
which reads as progress in a diff and is none.

So the architecture is measured instead.  Each metric below is one number with
a target; a change is scored before and after, and "did that help?" is answered
by arithmetic rather than judgement.

    python tools/script/arch_fitness.py                        # the table
    python tools/script/arch_fitness.py --legend               # what each means
    python tools/script/arch_fitness.py --why stray-comments   # the file:lines
    python tools/script/arch_fitness.py --fail-on-regression   # the edit loop
    python tools/script/arch_fitness.py --update-baseline      # after a stage

`--fail-on-regression` compares against `docs/script_convert_fitness.json` and
exits non-zero ONLY when a metric moved the wrong way.  It never blocks
progress toward a target, which is what keeps it from being switched off.

Every metric is NAMED, not numbered.  `F10: 32` needs a legend to act on and
reads as noise in a report; `satellite-cmd-sets: 32` says what to go fix.  The
`group` column ties one back to the anti-pattern it scores (AP1-AP8) or marks
it `doc` for the documentation rules.

Three families:

  AP1-AP8  one per anti-pattern in the architecture doc -- the ABSENCE of a
           known disease.
  (blank)  simple / organised / deduplicated / maintainable / fast -- the GOAL,
           so the package cannot rot in a way the original audit missed.
  doc      the documentation rules: docstrings carry the prose, comments do not.

🛑 EVERY RULE IS PACKAGE-SCOPED.  The doc rules once ran repo-wide, which made
them dead weight here: `stray-comments` read 26,952 with only 2,857 of them in
`script_convert/`, so no amount of work on this package could move the number.

This is a FITNESS FUNCTION, not the correctness gate.  It says the code is
SHAPED right; only `psc_semantic_diff.py` says it still DOES the same thing,
and only that tool protects the play-tested CharacterGen scripts.  Both are
required and neither substitutes for the other.

🛑 THE SUITE IS BUILD-FREE AND MUST STAY UNDER A SECOND.  A conversion-fidelity
metric (counting `;NE:` markers over generated output) was prototyped and
deliberately left out: it needs a build to exist and takes ~2.8s to scan 40,586
scripts.  That job belongs to `psc_semantic_diff.py`, which runs on the same
artifact.  Sampling to make it cheap was tested and rejected too -- 500/1500/
3000-script samples gave 0.184/0.125/0.154 markers per script, noise that
swamps any real regression.

🛑 EVERY STATIC METRIC SHARES ONE AST PARSE PER FILE.  Parsing per-metric is
the difference between 0.25s and several seconds, and a slow fitness function
stops being run.
"""

import argparse
import ast
import json
import re
import statistics
import subprocess
import sys
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

PKG = ROOT / 'script_convert'
BASELINE = ROOT / 'docs' / 'script_convert_fitness.json'

#: Scratch and vendored trees, excluded from every rule.
REPO_SKIP = ('temp', 'references', 'external', 'output', 'export', 'build')

#: Roughly seven lines of prose -- a contract, not a narrative.
MAX_DOC_CHARS = 480

#: Measured p90 of 7,320 functions is 49 lines; see the architecture doc.
MAX_FUNCTION_LINES = 60

#: 'le': lower is better.  'ge': higher is better.
LOWER, HIGHER = 'le', 'ge'

#: key, target, direction, group -- in printed order; the key IS the name.
METRICS = [
    ('text-repair-fns', 0, LOWER, 'AP2'),
    ('god-functions', 0, LOWER, ''),
    ('branch-points', 1500, LOWER, ''),
    ('private-reach-ins', 0, LOWER, 'AP4'),
    ('code-lines', 6500, LOWER, ''),
    ('oversized-files', 0, LOWER, ''),
    ('reparse-round-trip', 0, LOWER, 'AP1'),
    ('source-rescans', 0, LOWER, 'AP3'),
    ('mutable-class-state', 0, LOWER, 'AP4'),
    ('stray-constants', 10, LOWER, 'AP5'),
    ('satellite-cmd-sets', 0, LOWER, 'AP6'),
    ('logic-in-constants', 0, LOWER, 'AP7'),
    ('psc-readback', 0, LOWER, 'AP8'),
    ('duplicate-literals', 0, LOWER, ''),
    ('long-functions', 0, LOWER, ''),
    ('multi-return-fns', 0, LOWER, ''),
    ('deep-nesting', 0, LOWER, ''),
    ('local-imports', 10, LOWER, ''),
    ('regex-ops', 25, LOWER, ''),
    ('ms-per-script', 0, LOWER, ''),
    ('duck-typed-nodes', 0, LOWER, ''),
    ('return-annotations', 95, HIGHER, ''),
    ('comment-blocks', 0, LOWER, 'doc'),
    ('inline-comments', 0, LOWER, 'doc'),
    ('fat-docstrings', 0, LOWER, 'doc'),
    ('unsectioned-defs', 0, LOWER, 'doc'),
    ('fat-sections', 0, LOWER, 'doc'),
    ('missing-docstrings', 0, LOWER, 'doc'),
    ('bloated-docstrings', 0, LOWER, 'doc'),
    ('fat-attr-docs', 0, LOWER, 'doc'),
    ('stray-comments', 0, LOWER, 'doc'),
]

#: One line each, printed by `--legend`.  A name says WHAT; this says WHY.
EXPLAIN = {
    'text-repair-fns': 'takes emitted Papyrus back as input (fix the NODE)',
    'god-functions': 'cyclomatic complexity over 25',
    'branch-points': 'total branches; splitting a function cannot move it',
    'private-reach-ins': 'emit/commands/assemble touching conv._ / ctx._',
    'code-lines': 'package implementation lines (docstrings excluded)',
    'oversized-files': 'files over 1000 code lines',
    'reparse-round-trip': 'a node flattened to TES4 text and re-parsed',
    'source-rescans': 'feature flags regexed from raw source after parse',
    'mutable-class-state': 'class-level dict/list/set as a global channel',
    'stray-constants': 'data tables living outside constants.py',
    'satellite-cmd-sets': 'per-command flag sets shadowing COMMAND_ROWS',
    'logic-in-constants': 'constants.py taking a graph, or reading at import',
    'psc-readback': 'a symbol table rebuilt by grepping generated .psc',
    'duplicate-literals': 'redundant copies of one string-literal collection',
    'long-functions': 'over %d physical lines' % MAX_FUNCTION_LINES,
    'multi-return-fns': 'over 10 return points -- a dispatch chain',
    'deep-nesting': 'if/for/while/with/try nested over 4 deep',
    'local-imports': 'function-local imports (they hide layering breaks)',
    'regex-ops': 'the string-era footprint in one number',
    'ms-per-script': 'median ms on the frozen fixture, vs baseline',
    'duck-typed-nodes': 'getattr on an AST node instead of a real field',
    'return-annotations': 'pct of public functions with one',
    'comment-blocks': 'module-level comment block over %d chars'
                      % MAX_DOC_CHARS,
    'inline-comments': 'a comment inside a function body',
    'fat-docstrings': 'docstring over %d chars' % MAX_DOC_CHARS,
    'unsectioned-defs': 'a def above the first heading in a sectioned file',
    'fat-sections': 'section heading over %d chars of prose'
                    % MAX_DOC_CHARS,
    'missing-docstrings': 'function with no docstring at all',
    'bloated-docstrings': 'docstring out of proportion to its body',
    'fat-attr-docs': 'a `#:` doc running past one 120-char line',
    'stray-comments': 'a plain `#` that is not a section heading',
}

#: The doc-rule keys; scoped to the package, they read 2,857 / 1,617 / 57.
DOC_METRICS = frozenset(k for k, _t, _d, g in METRICS if g == 'doc')

#: Layer-scoped: unscoped this wrongly flags the parser and the classifier.
TEXT_REPAIR_SCOPE = frozenset({'converter.py', 'emit/expr.py', 'emit/stmt.py',
                      'emit/commands.py'})
TEXT_REPAIR_PARAMS = frozenset({'lines', 'line', 'text', 'emitted', 'psc'})
#: (file, function) exempt for a legitimate reason.
TEXT_REPAIR_ALLOW = frozenset({
    ('converter.py', 'emit_string'),   # a TES4 string LITERAL, not our output
    ('emit/expr.py', '_number'),       # a numeric literal's source spelling
})

#: The round trip: a node flattened to TES4 text and RE-PARSED.
ROUND_TRIP = re.compile(
    r'_convert_expression\(|_tree_expression|USE_TREE_EXPRESSIONS'
    r'|emit_source\([^)]*\)\s*(?:,\s*extends)?\s*\)\s*$'
    r'|parse\(\s*emit_source|tokenize\(\s*emit_source')

#: A feature flag scanned from raw source after the tree exists.
RAW_SCAN = re.compile(r'in source_low'
                      r'|re\.(?:search|match)\([^\n]*source_low'
                      r'|source\.lower\(\)')

#: Reading back generated .psc -- a symbol table by grep.
PSC_READ = re.compile(r"\.psc['\"]|\*\.psc")

#: The whole string-era footprint in one number.
REGEX_OP = re.compile(r're\.(?:compile|search|match|sub|findall|fullmatch)\(')

#: getattr on an AST node: the node contract worked around, not used.
DUCK_GETATTR = re.compile(r'getattr\(\s*(?:st|node|n|stmt|expr)\b[^)]*\)')

BRANCH_NODES = (ast.If, ast.For, ast.While, ast.And, ast.Or,
                ast.ExceptHandler, ast.Assert, ast.comprehension)

#: A one- or two-line body gets one line of docstring, nothing more.
TINY_BODY_LINES = 2
TINY_DOC_CHARS = 80

#: Chars per code line above that: the measured 90th percentile (median 35).
DOC_CHARS_PER_LINE = 200

#: A `#:` doc is ONE line: it labels a declaration, it does not argue.
MAX_ATTR_DOC_CHARS = 120

#: A section heading is a `# ----` or `# ====` rule above and below a title.
BANNER_RE = re.compile(r'^#\s*(?:-{4,}|={4,})\s*$')

#: Exempt from F24; both are capped by F28/F31 so neither can launder prose.
COMMENT_EXEMPT_PREFIX = ('#:', '# ---', '# ===')
NEST_NODES = (ast.If, ast.For, ast.While, ast.With, ast.Try)

#: A set built by this call is DERIVED from the rows, not authored beside them.
FLAGGED = '_flagged'

#: The command universe, not per-command flags.
SET_UNIVERSE = frozenset({'KNOWN_COMMANDS', 'HANDLED_COMMANDS',
                          '_PAPYRUS_RESERVED'})
CONST_NAME = re.compile(r'^_?[A-Z][A-Z0-9_]*$')


#: FROZEN: a drifting fixture measures nothing (9 lines = 0.282ms, 14 = 0.459).
FIXTURE = '\n'.join([
    'scn T', 'short doonce', 'ref target', 'float timer',
    'begin GameMode',
    ' if doonce == 0',
    '  set doonce to 1',
    '  player.additem Gold001 100',
    '  set target to getself',
    '  if target.getdistance player < 500',
    '   target.setalert 1',
    '  endif',
    ' endif',
    'end',
])
FIXTURE_RUNS, FIXTURE_ITERS = 5, 40
#: Measured run-to-run spread is ~1.10x, so this sits well outside noise.
SPEED_TOLERANCE = 1.5


def code_lines(text: str, tree=None) -> int:
    """Non-blank, not-comment-only, NOT-docstring lines (one definition).

    Docstrings are documentation, not code, and counting them made F4 punish
    the very thing this refactor wants: a `Cmd` row carrying a 3-line
    rationale scored WORSE than the 12-line branch it replaced.  They are
    still measured -- by F26, F29 and F30 -- so what F4 reports is the size of
    the IMPLEMENTATION.
    """
    doc_lines = set()
    for node in ast.walk(tree) if tree is not None else ():
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, 'body', None)
        first = body[0] if body else None
        if (isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            doc_lines.update(range(first.lineno, first.end_lineno + 1))
    return sum(1 for n, line in enumerate(text.split('\n'), 1)
               if line.strip() and not line.strip().startswith('#')
               and n not in doc_lines)


def _complexity(node) -> int:
    """Cyclomatic complexity: 1 plus every branching node."""
    return 1 + sum(1 for x in ast.walk(node) if isinstance(x, BRANCH_NODES))


def _depth(node, level: int = 0) -> int:
    """Deepest nesting of if/for/while/with/try inside this node."""
    deepest = level
    for child in ast.iter_child_nodes(node):
        step = level + 1 if isinstance(child, NEST_NODES) else level
        deepest = max(deepest, _depth(child, step))
    return deepest


def _satellite_sets(trees, pkg: Path) -> list:
    """`(path, line, detail)` per name set authored apart from its row.

    Read from SOURCE, not runtime: a derived frozenset is indistinguishable
    from a typed-out one.  Skips a set built by `_flagged`, and one whose names
    are mostly not in `COMMAND_ROWS`.
    """
    tree = trees.get(pkg / 'constants.py')
    if tree is None:
        return []
    try:
        from script_convert import constants as C
        commands = set(C.COMMAND_ROWS)
    except Exception:
        return []
    hits = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        name = getattr(node.targets[0], 'id', '')
        if not CONST_NAME.match(name) or name in SET_UNIVERSE:
            continue
        if any(isinstance(x, ast.Call) and getattr(x.func, 'id', '') == FLAGGED
               for x in ast.walk(node.value)):
            continue
        literal = node.value
        if (isinstance(literal, ast.Call)
                and getattr(literal.func, 'id', '') == 'frozenset'
                and literal.args):
            literal = literal.args[0]
        if not isinstance(literal, ast.Set):
            continue
        if not 3 < len(literal.elts) < 300:
            continue
        members = [e.value.lower() for e in literal.elts
                   if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        named = sum(1 for m in members if m in commands)
        if named * 2 <= len(members):
            continue
        hits.append((pkg / 'constants.py', node.lineno,
                     '%s: %d of %d names have a row'
                     % (name, named, len(members))))
    return hits


def _constants_logic(trees, pkg: Path) -> int:
    """Functions taking a graph, plus import-time file reads, in constants.py.

    Both are the same defect: `constants.py` is meant to be DATA, so a function
    taking an `xref` is conversion logic that has drifted into it, and a read
    at module scope makes importing the data table do 309 KB of disk I/O.
    Only reads at MODULE scope count -- one inside a lazily-called function is
    the fix, not the defect.
    """
    tree = trees.get(pkg / 'constants.py')
    if tree is None:
        return 0
    logic = sum(1 for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and {a.arg for a in n.args.args} & {'xref', 'conv', 'ctx'})
    fn_spans = [(n.lineno, n.end_lineno) for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    io = sum(1 for n in ast.walk(tree) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == 'open'
             and not any(a <= n.lineno <= b for a, b in fn_spans))
    return logic + io


def _duplicate_literals(files, trees) -> list:
    """Redundant COPIES of the same string-literal collection.

    `('self','myself','getself')` appears five times and `('ACHR','ACRE',
    'REFR')` four; each extra copy is one more place to forget when the set
    changes.  Counts copies rather than groups, so the number is the work left.
    """
    seen = {}
    for path in files:
        for node in ast.walk(trees[path]):
            if not isinstance(node, (ast.Tuple, ast.Set, ast.List)):
                continue
            if len(node.elts) < 3:
                continue
            if all(isinstance(e, ast.Constant) and isinstance(e.value, str)
                   for e in node.elts):
                key = tuple(sorted(e.value for e in node.elts))
                seen.setdefault(key, []).append((path, node.lineno))
    hits = []
    for key, spots in seen.items():
        for path, line in spots[1:]:
            hits.append((path, line, '%d copies of %s'
                         % (len(spots), str(key)[:52])))
    return hits


def _module_blocks(files, texts, trees) -> list:
    """Module-level comment blocks running past MAX_DOC_CHARS.

    Function bodies are F24's, `#:` is F31's and banners are F28's, so what is
    left here is the free-floating module-level block.
    """
    hits = []
    for path in files:
        lines = texts[path].split(chr(10))
        spans = [(n.lineno, n.end_lineno) for n in ast.walk(trees[path])
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        run = start = 0
        for i, raw in enumerate(lines, 1):
            stripped = raw.strip()
            counts = (stripped.startswith('#')
                      and not stripped.startswith('#:')
                      and not BANNER_RE.match(stripped)
                      and not any(a <= i <= b for a, b in spans))
            if counts:
                if not run:
                    start = i
                run += len(stripped.lstrip('#').strip())
            else:
                if run > MAX_DOC_CHARS:
                    hits.append((path, start, '%d chars' % run))
                run = 0
        if run > MAX_DOC_CHARS:
            hits.append((path, start, '%d chars' % run))
    return hits


def _narration(files, texts, trees) -> list:
    """Comments inside a function body.  There is no legal reason for one."""
    hits = []
    for path in files:
        lines = texts[path].split(chr(10))
        spans = [(n.lineno, n.end_lineno)
                 for n in ast.walk(trees[path])
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        if not spans:
            continue
        for i, raw in enumerate(lines, 1):
            stripped = raw.strip()
            if not stripped.startswith('#'):
                continue
            if stripped.startswith(COMMENT_EXEMPT_PREFIX):
                continue
            if any(a <= i <= b for a, b in spans):
                hits.append((path, i, stripped[:60]))
    return hits


#: Metric key -> [(path, line, detail)] from the last measure(); `--why` reads it.
SITES = {}

_FN_CACHE = {}


def _functions(trees) -> list:
    """(path, FunctionDef) for every function, walked ONCE and memoised.

    Six metrics each ran their own `ast.walk` over the repo, 3.5s of a 5.7s
    total; a fitness function that slow stops being run.  Keyed on the tree
    dict's identity, so the package slice and the repo set stay distinct.
    """
    key = id(trees)
    if key not in _FN_CACHE:
        _FN_CACHE[key] = [
            (path, node) for path, tree in trees.items()
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    return _FN_CACHE[key]


def _fat_docstrings(trees) -> list:
    """Functions whose docstring runs past MAX_DOC_CHARS."""
    return [(p, node.lineno, '%s: %d chars' % (node.name, len(doc)))
            for p, node in _functions(trees)
            for doc in [ast.get_docstring(node) or '']
            if len(doc) > MAX_DOC_CHARS]


def _bloated_docstrings(files, texts, trees) -> list:
    """Docstrings out of proportion to the code they document.

    F26's flat cap is the same limit at one line or eighty.  A one- or two-line
    body gets ONE LINE; above that the limit is per code line.  See
    TINY_DOC_CHARS and DOC_CHARS_PER_LINE for the measured thresholds.
    """
    hits = []
    for path, node in _functions(trees):
        lines = texts[path].split(chr(10))
        doc = ast.get_docstring(node)
        if not doc:
            continue
        body = [l for l in lines[node.lineno - 1:node.end_lineno]
                if l.strip() and not l.strip().startswith('#')]
        code = max(len(body) - len(doc.split(chr(10))) - 2, 1)
        limit = (TINY_DOC_CHARS if code <= TINY_BODY_LINES
                 else DOC_CHARS_PER_LINE * code)
        if len(doc) > limit:
            hits.append((path, node.lineno,
                         '%s: %d chars of doc on %d code lines (limit %d)'
                         % (node.name, len(doc), code, limit)))
    return hits


def _missing_docstrings(trees) -> list:
    """Functions carrying no docstring; no exemption for closures."""
    return [(p, node.lineno, node.name)
            for p, node in _functions(trees)
            if ast.get_docstring(node) is None]


def _unsectioned(files, texts, trees) -> list:
    """Top-level defs sitting ABOVE the first heading in a sectioned file.

    A file with sections promises where things live, and the cheapest way to
    break it is to drop a new def outside them.  Only files that ALREADY use
    headings are scored, so this enforces a structure the file chose.
    """
    hits = []
    for path in files:
        lines = texts[path].split(chr(10))
        heads = [i + 2 for i, line in enumerate(lines)
                 if BANNER_RE.match(line.strip())
                 and i + 2 < len(lines)
                 and BANNER_RE.match(lines[i + 2].strip())]
        if not heads:
            continue
        first = min(heads)
        hits += [(path, n.lineno, '%s above the first heading (line %d)'
                  % (n.name, first))
                 for n in trees[path].body
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef))
                 and n.lineno < first]
    return hits


def _fat_attr_docs(files, texts) -> list:
    """`#:` docs running past ONE line of MAX_ATTR_DOC_CHARS characters.

    `#:` was the one uncapped location, on the theory that a doc bound to a
    single declaration cannot sprawl.  It can: this file carried an 11-line and
    a 15-line block on one-line constants.  A tag is not a licence.
    """
    hits = []
    for path in files:
        run = length = start = 0
        for i, line in enumerate(texts[path].split(chr(10))):
            stripped = line.strip()
            if stripped.startswith('#:'):
                if not run:
                    start = i + 1
                run += 1
                length += len(stripped.lstrip('#:').strip())
                continue
            if run > 1 or length > MAX_ATTR_DOC_CHARS:
                hits.append((path, start,
                             '%d lines, %d chars' % (run, length)))
            run = length = 0
        if run > 1 or length > MAX_ATTR_DOC_CHARS:
            hits.append((path, start, '%d lines, %d chars' % (run, length)))
    return hits


def _plain_comments(files, texts) -> list:
    """Plain `#` comments that are not a section heading.

    The only legal prose is a docstring, a one-line `#:` attribute doc, and a
    section heading.  A bare `#` anywhere else -- module-level narrative most
    of all -- has no home in the rules, and relabelling one `#:` to dodge F32
    is caught by F31 instead.
    """
    hits = []
    for path in files:
        lines = texts[path].split(chr(10))
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith('#') or stripped.startswith('#!'):
                continue
            if stripped.startswith('#:') or BANNER_RE.match(stripped):
                continue
            above = lines[i - 1].strip() if i else ''
            below = lines[i + 1].strip() if i + 1 < len(lines) else ''
            if BANNER_RE.match(above) and BANNER_RE.match(below):
                continue
            hits.append((path, i + 1, stripped[:60]))
    return hits


def _fat_sections(files, texts) -> list:
    """Section headings whose prose exceeds the docstring limit.

    A section tag is a LABEL: `nodes.py` reads `Expressions`, `Statements`,
    `Traversal`.  Held to the same MAX_DOC_CHARS as a docstring, because the
    banner is F24-exempt and an uncapped exemption is a laundering route --
    an essay wrapped in dashes would otherwise score nothing at all.
    """
    hits = []
    for path in files:
        lines = texts[path].split(chr(10))
        for i, line in enumerate(lines):
            if not BANNER_RE.match(line.strip()):
                continue
            prose = []
            for nxt in lines[i + 1:]:
                stripped = nxt.strip()
                if not stripped.startswith('#') or BANNER_RE.match(stripped):
                    break
                prose.append(stripped.lstrip('#').strip())
            if sum(len(x) for x in prose) > MAX_DOC_CHARS:
                hits.append((path, i + 1, '%d chars of prose'
                             % sum(len(x) for x in prose)))
    return hits


def _speed() -> float:
    """Median ms to convert the frozen fixture.

    Deliberately NOT a corpus run: converting anything real takes seconds and
    the suite would stop being run on every edit.  One script, no plugin, no
    export index, no I/O.
    """
    try:
        from script_convert.converter import ScriptConverter
        from script_convert.cross_ref import CrossRefGraph
    except Exception:
        return 0.0
    conv = ScriptConverter(CrossRefGraph())
    runs = []
    for _ in range(FIXTURE_RUNS):
        start = time.perf_counter()
        for i in range(FIXTURE_ITERS):
            conv.convert_standalone('T%d' % i, FIXTURE, 'ObjectReference',
                                    'T%d' % i)
        runs.append((time.perf_counter() - start) / FIXTURE_ITERS * 1000)
    return round(statistics.median(runs), 3)


def _package_files(pkg: Path) -> list:
    """Every .py file in the package under measurement."""
    return sorted(f for f in pkg.rglob('*.py')
                  if not any(part in REPO_SKIP for part in f.parts))


def _changed_files(pkg: Path) -> list:
    """Package files this working tree has touched vs the MERGE BASE.

    A rule broken in a file nobody edited is inherited debt, and scoring it
    hides whether THIS change helped.  Merge base, not HEAD, so a file
    rewritten earlier on the branch still counts as this rewrite's work.
    """
    base = subprocess.run(['git', 'merge-base', 'HEAD', 'master'],
                          cwd=ROOT, capture_output=True, text=True)
    ref = base.stdout.strip() or 'HEAD'
    out = subprocess.run(['git', 'diff', '--name-only', ref, '--', str(pkg)],
                         cwd=ROOT, capture_output=True, text=True)
    names = [n for n in out.stdout.split(chr(10)) if n.endswith('.py')]
    return sorted({ROOT / n for n in names if (ROOT / n).is_file()})


def _parse_all(paths) -> tuple:
    """(texts, trees) for a list of files, parsed once each.

    Warnings are suppressed rather than printed: several first-party files
    carry invalid escape sequences, and `ast.parse` reports each one on stdout,
    which corrupts `--json` for any caller trying to read the numbers.
    """
    texts = {p: p.read_text(encoding='utf-8', errors='replace') for p in paths}
    trees = {}
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        for path, source in texts.items():
            try:
                trees[path] = ast.parse(source)
            except SyntaxError:
                print('  !! %s does not parse' % path, file=sys.stderr)
                trees[path] = ast.Module(body=[], type_ignores=[])
    return texts, trees


def _pattern_counts(files, texts) -> dict:
    """The regex-based metrics: one findall per pattern per file."""
    return {key: sum(len(rx.findall(texts[p])) for p in files)
            for key, rx in (('reparse-round-trip', ROUND_TRIP),
                            ('source-rescans', RAW_SCAN),
                            ('psc-readback', PSC_READ),
                            ('regex-ops', REGEX_OP),
                            ('duck-typed-nodes', DUCK_GETATTR))}


def _size_counts(files, texts, trees, fns) -> dict:
    """The size and shape metrics, over the shared function walk."""
    return {
        'branch-points': sum(_complexity(fn) - 1 for _, fn in fns),
        'code-lines': sum(code_lines(texts[p], trees[p]) for p in files),
        'local-imports': sum(
            1 for p in files for n in ast.walk(trees[p])
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            for x in ast.walk(n)
            if isinstance(x, (ast.Import, ast.ImportFrom))),
    }


def _package_only(files, texts, trees, rel, fns, pkg) -> dict:
    """Metrics that mean something only inside `script_convert/`."""
    inner = ('emit/', 'commands/', 'assemble/')
    return {
        'text-repair-fns': sum(
            1 for path, fn in fns
            if rel[path] in TEXT_REPAIR_SCOPE
            and ({a.arg for a in fn.args.args}
                 | {a.arg for a in fn.args.kwonlyargs}) & TEXT_REPAIR_PARAMS
            and (rel[path], fn.name) not in TEXT_REPAIR_ALLOW),
        'private-reach-ins': sum(
            texts[p].count('conv._') + texts[p].count('ctx._')
            for p in files if rel[p].startswith(inner)),
        'stray-constants': sum(
            1 for p in files if rel[p] != 'constants.py'
            for n in ast.walk(trees[p]) if isinstance(n, ast.Assign)
            for t in n.targets
            if isinstance(t, ast.Name) and t.id.lstrip('_').isupper()
            and isinstance(n.value, (ast.Dict, ast.Set, ast.List, ast.Tuple))),
        'logic-in-constants': _constants_logic(trees, pkg),
    }


def _all_sites(files, texts, trees, pkg) -> dict:
    """Every metric that can name its own `(path, line, detail)`.

    The gate and the table read the SAME functions, so a rule cannot be
    enforced on one file under a definition the score does not share.
    """
    sites = dict(_structural_sites(files, texts, trees))
    sites.update({
        'satellite-cmd-sets': _satellite_sets(trees, pkg),
        'duplicate-literals': _duplicate_literals(files, trees),
        'comment-blocks': _module_blocks(files, texts, trees),
        'inline-comments': _narration(files, texts, trees),
        'fat-docstrings': _fat_docstrings(trees),
        'unsectioned-defs': _unsectioned(files, texts, trees),
        'fat-sections': _fat_sections(files, texts),
        'missing-docstrings': _missing_docstrings(trees),
        'bloated-docstrings': _bloated_docstrings(files, texts, trees),
        'fat-attr-docs': _fat_attr_docs(files, texts),
        'stray-comments': _plain_comments(files, texts),
    })
    return sites


def measure(pkg: Path = PKG, with_speed: bool = True,
            scope: list = None) -> dict:
    """Every metric for one package, from ONE parse per file."""
    files = scope if scope is not None else _package_files(pkg)
    texts, trees = _parse_all(files)
    rel = {p: p.relative_to(pkg).as_posix() for p in files}
    fns = [(p, n) for p in files for n in ast.walk(trees[p])
           if isinstance(n, ast.FunctionDef)]

    out = {}
    out.update(_pattern_counts(files, texts))
    out.update(_size_counts(files, texts, trees, fns))
    out.update(_package_only(files, texts, trees, rel, fns, pkg))

    sites = _all_sites(files, texts, trees, pkg)
    SITES.clear()
    SITES.update(sites)
    out.update({key: len(found) for key, found in sites.items()})

    public = [(p, fn) for p, fn in fns if not fn.name.startswith('_')]
    out['return-annotations'] = round(
        100 * sum(1 for _, fn in public if fn.returns is not None)
        / max(len(public), 1))
    out['ms-per-script'] = _speed() if with_speed else 0.0
    return out


#: How to FIX each gated rule; the locator says where, this says what to do.
REMEDY = {
    'stray-comments':
        'move the prose into the nearest docstring, or delete it',
    'inline-comments':
        'lift it into the function docstring, or name a helper after it',
    'comment-blocks':
        'move it into the module docstring',
    'fat-docstrings':
        'compress: keep every count, name and mechanism, cut the narration',
    'bloated-docstrings':
        'the body is short -- one line of docstring is enough',
    'missing-docstrings':
        'add a one-line docstring saying what it returns',
    'fat-attr-docs':
        'a `#:` doc is ONE line of 120 chars; longer goes in a docstring',
    'fat-sections':
        'a section heading is a LABEL; move the prose to a docstring',
    'unsectioned-defs':
        'this file uses `# ----` sections; move the def under one',
    'god-functions':
        'split it: extract the branch arms, or drive them from a table',
    'long-functions':
        'split it into named phases',
    'deep-nesting':
        'invert the guards and return early, or extract the inner block',
    'multi-return-fns':
        'a dispatch chain -- make it a table lookup',
    'mutable-class-state':
        'a class-level dict/list/set is a global; make it an instance field',
    'oversized-files':
        'split the file by responsibility (CLAUDE.md: keep files under ~1000)',
}


def _structural_sites(files, texts, trees) -> dict:
    """Locators for the structural rules, which `measure` only counts.

    A count tells the agent a rule broke; `file:line: name` tells it what to
    open.  Without the second the only way to find a violation is to
    reimplement the metric in a probe, which is how a one-unit regression once
    survived a dozen tool calls unlocated.
    """
    fns = [(p, n) for p in files for n in ast.walk(trees[p])
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    out = {
        'god-functions': [(p, f.lineno, '%s: complexity %d' % (f.name, c))
                          for p, f in fns
                          for c in [_complexity(f)] if c > 25],
        'long-functions': [(p, f.lineno, '%s: %d lines'
                            % (f.name, f.end_lineno - f.lineno + 1))
                           for p, f in fns
                           if f.end_lineno - f.lineno + 1 > MAX_FUNCTION_LINES],
        'deep-nesting': [(p, f.lineno, '%s: nested %d deep' % (f.name, d))
                         for p, f in fns
                         for d in [_depth(f)] if d > 4],
        'multi-return-fns': [(p, f.lineno, '%s: %d returns' % (f.name, r))
                             for p, f in fns
                             for r in [sum(1 for x in ast.walk(f)
                                           if isinstance(x, ast.Return))]
                             if r > 10],
        'mutable-class-state': [
            (p, st.lineno, '%s.%s is a class-level %s'
             % (cls.name, getattr(st.targets[0], 'id', '?'),
                type(st.value).__name__.lower()))
            for p in files for cls in ast.walk(trees[p])
            if isinstance(cls, ast.ClassDef)
            for st in cls.body
            if isinstance(st, ast.Assign)
            and isinstance(st.value, (ast.Dict, ast.List, ast.Set))],
        'oversized-files': [(p, 1, '%d code lines' % n) for p in files
                            for n in [code_lines(texts[p], trees[p])]
                            if n > 1000],
    }
    return out


def rule_sites(path: Path, text: str = None) -> dict:
    """`{rule: [(path, line, detail), ...]}` for one file's repo-wide rules.

    `text` scores that source instead of the file on disk, which is how the
    HEAD side of `--gate-diff` is measured without a checkout.
    """
    files = [path]
    if text is None:
        texts, trees = _parse_all(files)
    else:
        texts, trees = {path: text}, {path: ast.parse(text)}
    checks = {
        'comment-blocks': _module_blocks(files, texts, trees),
        'inline-comments': _narration(files, texts, trees),
        'fat-docstrings': _fat_docstrings(trees),
        'unsectioned-defs': _unsectioned(files, texts, trees),
        'fat-sections': _fat_sections(files, texts),
        'missing-docstrings': _missing_docstrings(trees),
        'bloated-docstrings': _bloated_docstrings(files, texts, trees),
        'fat-attr-docs': _fat_attr_docs(files, texts),
        'stray-comments': _plain_comments(files, texts),
    }
    checks.update(_structural_sites(files, texts, trees))
    return {k: v for k, v in checks.items() if v}


def _report(path: Path, broken: dict, limit: int, headline: str) -> int:
    """Print each violation with its fix; 1 when there was any, else 0."""
    if not broken:
        return 0
    try:
        shown = path.relative_to(ROOT)
    except ValueError:
        shown = path
    total = sum(len(v) for v in broken.values())
    print('  %s in %s -- %d violation%s'
          % (headline, shown, total, '' if total == 1 else 's'),
          file=sys.stderr)
    for key, found in sorted(broken.items()):
        print('', file=sys.stderr)
        print('    %s (%d) -- %s' % (key, len(found), EXPLAIN.get(key, '')),
              file=sys.stderr)
        print('    FIX: %s' % REMEDY.get(key, ''), file=sys.stderr)
        for _p, line, detail in sorted(found, key=lambda x: x[1])[:limit]:
            print('      %s:%d: %s' % (shown, line, detail), file=sys.stderr)
        if len(found) > limit:
            print('      ... %d more (same fix)' % (len(found) - limit),
                  file=sys.stderr)
    return 1


def gate_file(path: Path, limit: int = 25) -> int:
    """1 if `path` breaks any repo-wide rule, printing each site and its fix."""
    if not path.exists() or path.suffix != '.py':
        return 0
    return _report(path, rule_sites(path), limit, 'RULES BROKEN')


def _git(path: Path, *args) -> tuple:
    """(ok, stdout) for a git command about `path`, plus its repo-relative name.

    Returns `(False, '')` when the file is outside the repo or git refuses.
    """
    try:
        rel = path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return False, ''
    got = subprocess.run(['git'] + [a.replace('{}', rel) for a in args],
                         cwd=ROOT, capture_output=True, text=True, timeout=30)
    return got.returncode == 0, got.stdout


def _baseline_source(path: Path) -> str:
    """`path` before this branch's edits: HEAD's copy, else the INDEX's.

    A file `git add`ed this branch has no HEAD copy, but its staged blob is
    still a real prior version -- reading only HEAD made every such file score
    as brand new and inherit its whole comment history as "your edit".
    """
    for spec in ('HEAD:{}', ':{}'):
        ok, text = _git(path, 'show', spec)
        if ok:
            return text
    return ''


def _touched_lines(path: Path) -> set:
    """Working-tree line numbers this branch ADDED or CHANGED in `path`.

    `None` when git knows nothing of the file at all -- a wholly new file,
    every line of which is the edit's own.
    """
    ok, out = _git(path, 'diff', '-U0', 'HEAD', '--', '{}')
    if not ok:
        ok, out = _git(path, 'diff', '-U0', '--', '{}')
    if not ok:
        return None
    touched = set()
    for hunk in re.finditer(r'^@@ -\S+ \+(\d+)(?:,(\d+))? @@', out,
                            re.MULTILINE):
        start = int(hunk.group(1))
        touched.update(range(start, start + int(hunk.group(2) or 1)))
    return touched


def gate_diff(path: Path, limit: int = 25) -> int:
    """1 when the agent's OWN edit broke a rule; inherited debt is ignored.

    The whole-file gate made one line in a 3,900-line legacy file inherit all
    1,010 of its violations -- unpayable, so it gets switched off.  An edit is
    answerable for its own lines: a violation counts only when it sits on a
    line this branch added or changed AND its rule got no better file-wide.
    Improving a rule, or leaving it alone, passes.
    """
    if not path.exists() or path.suffix != '.py':
        return 0
    now = rule_sites(path)
    touched = _touched_lines(path)
    if touched == set():
        return 0
    before = _baseline_source(path)
    was = rule_sites(path, before) if before else {}
    blame = {}
    for rule, found in now.items():
        if len(found) <= len(was.get(rule, ())):
            continue
        mine = [s for s in found if touched is None or s[1] in touched]
        if mine:
            blame[rule] = mine
    return _report(path, blame, limit, 'YOUR EDIT BROKE RULES')


def _print_sites(keys: str, limit: int) -> int:
    """Print `file:line: detail` for each requested metric."""
    wanted = [k.strip().lower() for k in keys.split(',') if k.strip()]
    missing = [k for k in wanted if k not in SITES]
    for key in wanted:
        found = SITES.get(key)
        if found is None:
            print('  %s: no per-site locator (counts only)' % key,
                  file=sys.stderr)
            continue
        print()
        print('  %s -- %d site%s'
              % (key, len(found), '' if len(found) == 1 else 's'))
        for path, line, detail in found[:limit]:
            try:
                shown = Path(path).relative_to(ROOT)
            except ValueError:
                shown = path
            print('    %s:%s: %s' % (shown, line, detail))
        if len(found) > limit:
            print('    ... %d more (raise --limit)' % (len(found) - limit))
    print()
    return 1 if missing else 0


def regressions(now: dict, base: dict) -> list:
    """Metrics that moved the WRONG way.

    Movement TOWARD a target is never reported: a fitness function that blocks
    partial progress gets switched off halfway through a stage.  Speed is judged
    relative to the baseline, never absolutely, so a slower MACHINE cannot fail
    the gate.
    """
    out = []
    for key, _target, direction, _group in METRICS:
        if key not in base or key not in now:
            continue
        was, is_ = base[key], now[key]
        if key == 'ms-per-script':
            if was and is_ > was * SPEED_TOLERANCE:
                out.append('%s: %s -> %s ms (>%sx slower)'
                           % (key, was, is_, SPEED_TOLERANCE))
        elif direction == LOWER and is_ > was:
            out.append('%s: %s -> %s (worse)' % (key, was, is_))
        elif direction == HIGHER and is_ < was:
            out.append('%s: %s -> %s (worse)' % (key, was, is_))
    return out


def _print_table(now: dict, base: dict, elapsed: float) -> None:
    """Render the metric table, marking each against its target."""
    print('\n  script_convert/ fitness      (%.2fs)\n' % elapsed)
    for key, target, direction, group in METRICS:
        value = now[key]
        if key == 'ms-per-script':
            limit = base.get(key, 0) * SPEED_TOLERANCE
            mark = '   ' if not base.get(key) else (
                ' ok' if value <= limit else ' !!')
            goal = '<=%.3f' % limit if base.get(key) else 'baseline'
        else:
            ok = value <= target if direction == LOWER else value >= target
            mark = ' ok' if ok else ' !!'
            goal = '%s%s' % ('<=' if direction == LOWER else '>=', target)
        delta = ''
        if key in base and base[key] != value:
            delta = '  (%g -> %g)' % (base[key], value)
        print('  %s %-20s %8s  %-10s %-4s%s'
              % (mark, key, value, goal, group, delta))
    print()


def _print_legend() -> int:
    """One line per metric: what the name means and why it is scored."""
    print()
    for key, target, direction, group in METRICS:
        goal = '%s%s' % ('<=' if direction == LOWER else '>=', target)
        print('  %-20s %-8s %-4s %s'
              % (key, goal, group, EXPLAIN.get(key, '')))
    print()
    return 0


def _parser() -> argparse.ArgumentParser:
    """The command line."""
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--json', action='store_true', help='machine-readable')
    parser.add_argument('--fail-on-regression', action='store_true',
                        help='exit 1 when a metric moved away from its target')
    parser.add_argument('--update-baseline', action='store_true',
                        help='write current values to the baseline file')
    parser.add_argument('--accept-regression', action='store_true',
                        help='allow --update-baseline to record a '
                             'metric that moved AWAY from its target')
    parser.add_argument('--baseline', default=str(BASELINE))
    parser.add_argument('--no-speed', action='store_true',
                        help='skip ms-per-script (avoids importing the pkg)')
    parser.add_argument('--why', metavar='METRIC',
                        help='list the offending file:line for one metric '
                             '(e.g. --why stray-comments); comma-separated')
    parser.add_argument('--legend', action='store_true',
                        help='explain every metric name, one line each')
    parser.add_argument('--gate-file', metavar='PATH', action='append',
                        help='score ONE file against the repo-wide rules and '
                             'exit non-zero if any is broken; repeatable. The '
                             'enforcement half -- absolute, not relative to a '
                             'baseline, so it cannot be satisfied by an '
                             'unrelated improvement elsewhere')
    parser.add_argument('--gate-diff', metavar='PATH', action='append',
                        help='score only the LINES this branch changed in one '
                             'file, against the same file at HEAD; repeatable. '
                             'What the post-edit hook runs -- inherited debt in '
                             'a legacy file is not the edit\'s to pay')
    parser.add_argument('--changed', action='store_true',
                        help='score only the package files this branch has '
                             'touched, so inherited debt is not counted')
    parser.add_argument('--limit', type=int, default=40,
                        help='max sites to print per --why metric')
    return parser


def _write_baseline(path: Path, now: dict, base: dict, accept: bool) -> int:
    """Record `now` as the baseline, refusing to bake in a regression."""
    bad = regressions(now, base) if base else []
    if bad and not accept:
        print('  REFUSING to bake in a regression:', file=sys.stderr)
        for line in bad:
            print('    %s' % line, file=sys.stderr)
        print('  fix it, or re-run with --accept-regression.', file=sys.stderr)
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'metrics': now}, indent=2, sort_keys=True)
                    + chr(10), encoding='utf-8')
    print('  baseline written: %s' % path)
    return 0


def _check_regressions(now: dict, base: dict) -> int:
    """1 if any metric moved away from its target since the baseline."""
    if not base:
        print('  no baseline; run --update-baseline first', file=sys.stderr)
        return 1
    bad = regressions(now, base)
    if not bad:
        print('  no regressions')
        return 0
    print('  REGRESSED:', file=sys.stderr)
    for line in bad:
        print('    %s' % line, file=sys.stderr)
    return 1


def main(argv=None) -> int:
    """Parse arguments, measure, and report or gate."""
    args = _parser().parse_args(argv)
    if args.legend:
        return _print_legend()
    if args.gate_file:
        return max(gate_file(Path(n).resolve(), args.limit)
                   for n in args.gate_file)
    if args.gate_diff:
        return max(gate_diff(Path(n).resolve(), args.limit)
                   for n in args.gate_diff)

    start = time.perf_counter()
    scope = _changed_files(PKG) if args.changed else None
    if args.changed and not scope:
        print('  no changed package files')
        return 0
    now = measure(with_speed=not args.no_speed, scope=scope)
    elapsed = time.perf_counter() - start

    baseline_path = Path(args.baseline)
    base = (json.loads(baseline_path.read_text(encoding='utf-8'))
            .get('metrics', {})) if baseline_path.exists() else {}

    if args.why:
        return _print_sites(args.why, args.limit)
    if args.json:
        print(json.dumps({'metrics': now, 'elapsed': round(elapsed, 3)},
                         indent=2, sort_keys=True))
    else:
        _print_table(now, base, elapsed)

    if args.update_baseline:
        return _write_baseline(baseline_path, now, base,
                               args.accept_regression)
    if args.fail_on_regression:
        return _check_regressions(now, base)
    return 0


if __name__ == '__main__':
    sys.exit(main())
