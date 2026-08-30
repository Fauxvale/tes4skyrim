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
    python tools/script/arch_fitness.py --json                 # machine readable
    python tools/script/arch_fitness.py --fail-on-regression   # the edit loop
    python tools/script/arch_fitness.py --update-baseline      # after a stage

`--fail-on-regression` compares against `docs/script_convert_fitness.json` and
exits non-zero ONLY when a metric moved the wrong way.  It never blocks
progress toward a target, which is what keeps it from being switched off.

Two families:

  F1-F12   one per anti-pattern recorded in the architecture doc.  These score
           the ABSENCE of a known disease.
  F13-F23  simple / organised / deduplicated / maintainable / fast.  These
           score the GOAL, so the package cannot rot in a way the original
           audit failed to anticipate.

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
import sys
import subprocess
import time
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

PKG = ROOT / 'script_convert'
BASELINE = ROOT / 'docs' / 'script_convert_fitness.json'

#: Repo-wide scope for the comment rules; structural rules stay package-only.
REPO_DIRS = ('script_convert', 'tes4_export', 'tes5_import', 'asset_convert',
             'tools', 'tests')

#: Scratch and vendored trees, excluded from every rule.
REPO_SKIP = ('temp', 'references', 'external', 'output', 'export', 'build')

#: F2 counts fat functions; F2T counts BRANCHES, which splitting cannot move.
#: 'le': lower is better.  'ge': higher is better.
LOWER, HIGHER = 'le', 'ge'

#: key, target, direction, label -- in printed order.
METRICS = [
    ('F1', 0, LOWER, 'AP2  functions consuming emitted Papyrus'),
    ('F2', 0, LOWER, '     functions with complexity >25'),
    ('F2T', 1500, LOWER, '     TOTAL branch points (split-proof)'),
    ('F3', 0, LOWER, 'AP4  private reach-ins from emit/commands/assemble'),
    ('F4', 6500, LOWER, '     package CODE lines'),
    ('F5', 0, LOWER, '     files over 1000 code lines'),
    ('F6', 0, LOWER, 'AP1  AST->text->AST round-trip references'),
    ('F7', 0, LOWER, 'AP3  raw-source scans after parse'),
    ('F8', 0, LOWER, 'AP4  mutable class-level dict/list/set'),
    ('F9', 10, LOWER, 'AP5  data constants outside constants.py'),
    ('F10', 0, LOWER, 'AP6  satellite per-command sets'),
    ('F11', 0, LOWER, 'AP7  logic + import-time I/O in constants.py'),
    ('F12', 0, LOWER, 'AP8  emitted-.psc read sites'),
    ('F13', 0, LOWER, '     duplicated string-literal collections'),
    ('F14', 0, LOWER, '     functions over 80 physical lines'),
    ('F15', 0, LOWER, '     functions with >10 return points'),
    ('F16', 0, LOWER, '     functions nested >4 deep'),
    ('F17', 10, LOWER, '     function-local imports'),
    ('F18', 25, LOWER, '     regex operations'),
    ('F19', 0, LOWER, '     module comment blocks over 400 chars'),
    ('F21', 0, LOWER, '     ms/script on the frozen fixture'),
    ('F22', 0, LOWER, '     duck-typed getattr on AST nodes'),
    ('F23', 95, HIGHER, '     pct public fns with a return annotation'),
    ('F24', 0, LOWER, '     inline comments in a function body'),
    ('F26', 0, LOWER, '     docstrings over 400 chars'),
    ('F27', 0, LOWER, '     defs outside a section in a SECTIONED file'),
    ('F28', 0, LOWER, '     section headings over 400 chars'),
    ('F29', 0, LOWER, '     functions with NO docstring'),
    ('F30', 0, LOWER, '     docstrings out of proportion to the body'),
    ('F31', 0, LOWER, '     `#:` docs over one 80-char line'),
    ('F32', 0, LOWER, '     plain `#` comments outside a section heading'),
]

#: F1 is layer-scoped: unscoped it wrongly flags the parser and classifier.
F1_SCOPE = frozenset({'converter.py', 'emit/expr.py', 'emit/stmt.py',
                      'emit/commands.py'})
F1_PARAMS = frozenset({'lines', 'line', 'text', 'emitted', 'psc'})
#: (file, function) exempt for a legitimate reason.
F1_ALLOW = frozenset({
    ('converter.py', 'emit_string'),   # a TES4 string LITERAL, not our output
    ('emit/expr.py', '_number'),       # a numeric literal's source spelling
})

#: Names proving the string layer is still alive.
#: The round trip is a node flattened to TES4 text and RE-PARSED.  `emit_source`
#: alone is node->text for a `;NE:` marker, which never re-enters the parser.
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

#: Unused: F24 now bans an inline comment at any complexity.
F24_SIMPLE = 10

#: Roughly six lines of prose -- a contract, not a narrative.
F26_MAX_DOC = 400

#: A one- or two-line body gets one line of docstring, nothing more.
F30_TINY_BODY = 2
F30_TINY_DOC = 80

#: Chars per code line above that: the measured 90th percentile (median 35).
F30_MAX_RATIO = 200

#: A `#:` doc is ONE line: it labels a declaration, it does not argue.
F31_MAX_ATTR = 80

#: A section heading is a `# ----` or `# ====` rule above and below a title.
BANNER_RE = re.compile(r'^#\s*(?:-{4,}|={4,})\s*$')

#: Exempt from F24; both are capped by F28/F31 so neither can launder prose.
F24_EXEMPT_PREFIX = ('#:', '# ---', '# ===')
NEST_NODES = (ast.If, ast.For, ast.While, ast.With, ast.Try)

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


def code_lines(text: str) -> int:
    """Non-blank, not-comment-only lines; docstrings COUNT (one definition)."""
    return sum(1 for line in text.split('\n')
               if line.strip() and not line.strip().startswith('#'))


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


def _satellite_sets() -> int:
    """Per-command flag sets that should be fields on a COMMAND_ROWS row."""
    try:
        from script_convert import constants as C
    except Exception:
        return -1
    return sum(1 for name, value in vars(C).items()
               if isinstance(value, (set, frozenset))
               and CONST_NAME.match(name)
               and 3 < len(value) < 300
               and name not in SET_UNIVERSE)


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
    """Module-level comment blocks running past F26_MAX_DOC.

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
                if run > F26_MAX_DOC:
                    hits.append((path, start, '%d chars' % run))
                run = 0
        if run > F26_MAX_DOC:
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
            if stripped.startswith(F24_EXEMPT_PREFIX):
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
    """Functions whose docstring runs past F26_MAX_DOC."""
    return [(p, node.lineno, '%s: %d chars' % (node.name, len(doc)))
            for p, node in _functions(trees)
            for doc in [ast.get_docstring(node) or '']
            if len(doc) > F26_MAX_DOC]


def _bloated_docstrings(files, texts, trees) -> list:
    """Docstrings out of proportion to the code they document.

    F26's flat cap is the same limit at one line or eighty.  A one- or two-line
    body gets ONE LINE; above that the limit is per code line.  See
    F30_TINY_DOC and F30_MAX_RATIO for the measured thresholds.
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
        limit = (F30_TINY_DOC if code <= F30_TINY_BODY
                 else F30_MAX_RATIO * code)
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
    """`#:` docs running past ONE line of F31_MAX_ATTR characters.

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
            if run > 1 or length > F31_MAX_ATTR:
                hits.append((path, start,
                             '%d lines, %d chars' % (run, length)))
            run = length = 0
        if run > 1 or length > F31_MAX_ATTR:
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
    `Traversal`.  Held to the same F26_MAX_DOC as a docstring, because the
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
            if sum(len(x) for x in prose) > F26_MAX_DOC:
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


def _changed_files(root: Path) -> list:
    """First-party .py files touched vs HEAD, for the fast edit-loop check.

    The full repo scan is 5.1s across 439 files, which is too slow to run on
    every edit.  Almost always only a handful changed, and a rule broken in a
    file nobody touched is pre-existing debt rather than something this edit
    introduced.
    """
    try:
        out = subprocess.run(['git', 'diff', '--name-only', 'HEAD'],
                             cwd=root, capture_output=True, text=True,
                             timeout=30)
        names = out.stdout.split(chr(10))
    except Exception:
        return []
    keep = []
    for name in names:
        if not name.endswith('.py'):
            continue
        path = root / name
        if path.is_file() and any(part in REPO_DIRS for part in path.parts)                 and not any(part in REPO_SKIP for part in path.parts):
            keep.append(path)
    return sorted(keep)


def _repo_files(root: Path) -> list:
    """Every first-party .py file, for the repo-wide comment rules."""
    out = []
    for name in REPO_DIRS:
        base = root / name
        if base.is_dir():
            out += [f for f in base.rglob('*.py')
                    if not any(part in REPO_SKIP for part in f.parts)]
    return sorted(out)


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


def measure(pkg: Path = PKG, with_speed: bool = True,
            scope: list = None) -> dict:
    """Every metric, from ONE parse per file.

    The comment rules judge the whole repo; the architecture rules judge only
    this package.  The package is a subset, so everything is parsed once and
    the package metrics read a slice of it.  Parsing twice cost 5.7s
    against 1.6s, and a slow fitness function stops being run.
    """
    rfiles = scope if scope is not None else _repo_files(pkg.parent)
    rtexts, rtrees = _parse_all(rfiles)
    files = [f for f in rfiles if pkg in f.parents]
    texts = {f: rtexts[f] for f in files}
    trees = {f: rtrees[f] for f in files}

    rel = {p: p.relative_to(pkg).as_posix() for p in files}
    fns = [(p, n) for p in files for n in ast.walk(trees[p])
           if isinstance(n, ast.FunctionDef)]
    out = {}

    out['F1'] = sum(
        1 for path, fn in fns
        if rel[path] in F1_SCOPE
        and ({a.arg for a in fn.args.args}
             | {a.arg for a in fn.args.kwonlyargs}) & F1_PARAMS
        and (rel[path], fn.name) not in F1_ALLOW)

    out['F2'] = sum(1 for _, fn in fns if _complexity(fn) > 25)
    out['F2T'] = sum(_complexity(fn) - 1 for _, fn in fns)
    out['F4'] = sum(code_lines(texts[p]) for p in files)
    out['F5'] = sum(1 for p in files if code_lines(texts[p]) > 1000)

    inner = ('emit/', 'commands/', 'assemble/')
    out['F3'] = sum(texts[p].count('conv._') + texts[p].count('ctx._')
                    for p in files if rel[p].startswith(inner))

    out['F6'] = sum(len(ROUND_TRIP.findall(texts[p])) for p in files)
    out['F7'] = sum(len(RAW_SCAN.findall(texts[p])) for p in files)
    out['F12'] = sum(len(PSC_READ.findall(texts[p])) for p in files)
    out['F18'] = sum(len(REGEX_OP.findall(texts[p])) for p in files)
    out['F22'] = sum(len(DUCK_GETATTR.findall(texts[p])) for p in files)

    out['F8'] = sum(1 for p in files for n in ast.walk(trees[p])
                    if isinstance(n, ast.ClassDef)
                    for st in n.body
                    if isinstance(st, ast.Assign)
                    and isinstance(st.value, (ast.Dict, ast.List, ast.Set)))

    out['F9'] = sum(1 for p in files if rel[p] != 'constants.py'
                    for n in ast.walk(trees[p]) if isinstance(n, ast.Assign)
                    for t in n.targets
                    if isinstance(t, ast.Name) and t.id.lstrip('_').isupper()
                    and isinstance(n.value, (ast.Dict, ast.Set, ast.List,
                                             ast.Tuple)))

    out['F10'] = _satellite_sets()
    out['F11'] = _constants_logic(trees, pkg)
    out['F14'] = sum(1 for _, fn in fns
                     if fn.end_lineno - fn.lineno + 1 > 80)
    out['F15'] = sum(1 for _, fn in fns
                     if sum(1 for x in ast.walk(fn)
                            if isinstance(x, ast.Return)) > 10)
    out['F16'] = sum(1 for _, fn in fns if _depth(fn) > 4)
    out['F17'] = sum(1 for p in files for n in ast.walk(trees[p])
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                     for x in ast.walk(n)
                     if isinstance(x, (ast.Import, ast.ImportFrom)))

    sites = {
        'F13': _duplicate_literals(files, trees),
        'F19': _module_blocks(rfiles, rtexts, rtrees),
        'F24': _narration(rfiles, rtexts, rtrees),
        'F26': _fat_docstrings(rtrees),
        'F27': _unsectioned(rfiles, rtexts, rtrees),
        'F28': _fat_sections(rfiles, rtexts),
        'F29': _missing_docstrings(rtrees),
        'F30': _bloated_docstrings(rfiles, rtexts, rtrees),
        'F31': _fat_attr_docs(rfiles, rtexts),
        'F32': _plain_comments(rfiles, rtexts),
    }
    SITES.clear()
    SITES.update(sites)
    for key, found in sites.items():
        out[key] = len(found)

    public = [(p, fn) for p, fn in fns if not fn.name.startswith('_')]
    out['F23'] = round(100 * sum(1 for _, fn in public
                                 if fn.returns is not None)
                       / max(len(public), 1))

    out['F21'] = _speed() if with_speed else 0.0
    return out


def _print_sites(keys: str, limit: int) -> int:
    """Print `file:line: detail` for each requested metric."""
    wanted = [k.strip().upper() for k in keys.split(',') if k.strip()]
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
    partial progress gets switched off halfway through a stage.  F21 is judged
    relative to the baseline, never absolutely, so a slower MACHINE cannot fail
    the gate.
    """
    out = []
    for key, _target, direction, _label in METRICS:
        if key not in base or key not in now:
            continue
        was, is_ = base[key], now[key]
        if key == 'F21':
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
    for key, target, direction, label in METRICS:
        value = now[key]
        if key == 'F21':
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
        print('  %s %-4s %9s  target %-10s %s%s'
              % (mark, key, value, goal, label, delta))
    print()


def main(argv=None) -> int:
    """Parse arguments, measure, and report or gate."""
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
                        help='skip F21 (avoids importing the package)')
    parser.add_argument('--why', metavar='METRIC',
                        help='list the offending file:line for one metric '
                             '(e.g. --why F30); repeatable as F30,F31')
    parser.add_argument('--limit', type=int, default=40,
                        help='max sites to print per --why metric')
    parser.add_argument('--changed', action='store_true',
                        help='score the comment rules on files changed vs '
                             'HEAD only (fast; the full scan is 5s)')
    args = parser.parse_args(argv)

    start = time.perf_counter()
    scope = _changed_files(ROOT) if args.changed else None
    if args.changed and not scope:
        print('  no changed first-party .py files')
        return 0
    now = measure(with_speed=not args.no_speed, scope=scope)
    elapsed = time.perf_counter() - start

    base = {}
    baseline_path = Path(args.baseline)
    if baseline_path.exists():
        base = json.loads(
            baseline_path.read_text(encoding='utf-8')).get('metrics', {})

    if args.why:
        return _print_sites(args.why, args.limit)

    if args.json:
        print(json.dumps({'metrics': now, 'elapsed': round(elapsed, 3)},
                         indent=2, sort_keys=True))
    else:
        _print_table(now, base, elapsed)

    if args.update_baseline:
        bad = regressions(now, base) if base else []
        if bad and not args.accept_regression:
            print('  REFUSING to bake in a regression:', file=sys.stderr)
            for line in bad:
                print('    %s' % line, file=sys.stderr)
            print('  fix it, or re-run with --accept-regression.',
                  file=sys.stderr)
            return 1
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        baseline_path.write_text(
            json.dumps({'metrics': now}, indent=2, sort_keys=True) + '\n',
            encoding='utf-8')
        print('  baseline written: %s' % baseline_path)
        return 0

    if args.fail_on_regression:
        if not base:
            print('  no baseline; run --update-baseline first', file=sys.stderr)
            return 1
        bad = regressions(now, base)
        if bad:
            print('  REGRESSED:', file=sys.stderr)
            for line in bad:
                print('    %s' % line, file=sys.stderr)
            return 1
        print('  no regressions')
    return 0


if __name__ == '__main__':
    sys.exit(main())
