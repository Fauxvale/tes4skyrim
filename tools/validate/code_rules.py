#!/usr/bin/env python3
"""The repo-wide code rules, enforced per file.

    python tools/validate/code_rules.py --gate-file  PATH   # whole file
    python tools/validate/code_rules.py --gate-diff  PATH   # what YOUR edit owns
    python tools/validate/code_rules.py --dead-code         # repo sweep
    python tools/validate/code_rules.py --legend            # what each rule means

These rules are true of any Python, so they apply everywhere.  The rules that
only mean something inside `script_convert/` live in
`tools/script/arch_fitness.py`, which imports the detectors from here so a rule
can never be enforced under a definition the score does not share.

Per-file rules are an ABSOLUTE gate with no baseline: a single file either has
the property or it does not.  Only package AGGREGATES get a baseline, because
only a trend is meaningful for those.

See: docs/reference/script_convert_architecture.md
"""

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import warnings
from pathlib import Path

from tools.validate import code_rules_ast as D

ROOT = Path(__file__).resolve().parent.parent.parent

#: Scratch, vendored and generated trees are not judged.
SKIP_PARTS = ('temp', 'references', 'external', 'output', 'export', 'build',
              '.git', 'node_modules', '__pycache__', '.venv')

#: Dataflow facts, not heuristics: ruff reports these with no false positives.
RUFF_CODES = 'F401,F811,F841,F821'

#: Only 100% confidence; at 60 the `@command` registry alone cries wolf.
VULTURE_CONFIDENCE = '100'
VULTURE_IGNORE_DECORATORS = '@command,@command.*'

#: A citation to a doc, the route prose takes out of the code.
CITATION = re.compile(r'docs/[A-Za-z0-9_./-]+\.md(?:#[A-Za-z0-9-]+)?')

#: A line that CITES a doc, not one naming a path as data or a CLI argument.
REFERENCE = re.compile(r'(?i)\b(?:see|per|details?|rationale|documented)\b'
                       r'|\]\(')

#: An explicit `<a id="...">` target, used by docs for mid-section anchors.
EXPLICIT_ANCHOR = re.compile(r'<a\s+id=["\']([^"\']+)["\']')

#: What each rule MEANS; with REMEDY it is the rule's whole definition.
EXPLAIN = {
    'inline-comments': 'a prose comment inside a function body',
    'stray-comments': 'a prose comment outside every function',
    'comment-blocks': 'comment block over %d chars' % D.MAX_DOC_CHARS,
    'bloated-docstrings': 'docstring over %d chars (%d on a 1-2 line body)'
                          % (D.MAX_DOC_CHARS, D.TINY_DOC_CHARS),
    'missing-docstrings': 'function with no docstring at all',
    'fat-attr-docs': 'a `#:` doc running past one %d-char line'
                     % D.MAX_ATTR_DOC_CHARS,
    'fat-sections': 'section heading over %d chars of prose' % D.MAX_DOC_CHARS,
    'unsectioned-defs': 'a def above the first heading in a sectioned file',
    'god-functions': 'cyclomatic complexity over %d' % D.MAX_COMPLEXITY,
    'long-functions': 'over %d physical lines' % D.MAX_FUNCTION_LINES,
    'deep-nesting': 'if/for/while/with/try nested over %d deep' % D.MAX_NESTING,
    'multi-return-fns': 'over %d return points -- a dispatch chain'
                        % D.MAX_RETURNS,
    'mutable-class-state': 'class-level dict/list/set as a global channel',
    'oversized-files': 'files over %d code lines' % D.MAX_FILE_LINES,
    'dead-imports': 'an unused import, variable, or undefined name',
    'dead-code': 'a symbol or branch nothing in the repo can reach',
    'dead-citations': 'a `docs/` path or anchor that does not exist',
}

#: How to FIX each rule; the locator says where, this says what to do.
REMEDY = {
    'inline-comments':
        'lift it into the function docstring, or name a helper after it',
    'stray-comments':
        'move it into the nearest docstring; a measurement or date goes to a '
        '`docs/` file cited by `See:`',
    'comment-blocks':
        'move it into the module docstring, or a `docs/` file cited by `See:`',
    'bloated-docstrings':
        'keep the contract; evidence goes to a `docs/` file cited by `See:`',
    'missing-docstrings':
        'add a one-line docstring saying what it returns',
    'fat-attr-docs':
        'a `#:` doc is ONE line of %d chars; longer goes in a docstring, or a '
        '`docs/` file cited by `See:`' % D.MAX_ATTR_DOC_CHARS,
    'fat-sections':
        'a section heading is a LABEL; move the prose to a docstring, or a '
        '`docs/` file cited by `See:`',
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
    'dead-imports':
        'delete it; an unused name is indistinguishable from a mistake',
    'dead-code':
        'delete it; unreachable code is a bug that cannot announce itself',
    'dead-citations':
        'fix the path or the anchor -- a citation that lies loses the knowledge',
}

#: What to run when a checker is missing, named so a new machine can recover.
INSTALL = 'python -m pip install -e ".[dev]"   (installs ruff, vulture, pytest)'

#: `(rule, substring) -> the fix for THAT cause`; see `_specific_hints`.
SPECIFIC = {
    ('stray-comments', 'noqa: E402'):
        'the import cannot sit at the top because of a `sys.path` insert -- '
        'delete both: `pythonpath` in pyproject.toml (tests) or a package '
        'import (tools) makes the path hack unnecessary',
    ('stray-comments', 'noqa'):
        'a suppression is not prose: fix what it silences, then delete it',
    ('dead-imports', 'F821'):
        'an undefined name is a CRASH waiting on that branch -- add the '
        'import or delete the code; never leave it inside a bare `except`',
    ('dead-imports', 'F841'):
        'the value is computed and thrown away: delete it, or use it',
}

#: Pulls the measured magnitude out of a site detail ("f: 73 lines" -> 73).
MAGNITUDE = re.compile(r'(\d+)')

#: Rules sited at the `def` line, so the body blames through the span.
NODE_SPAN_RULES = frozenset({
    'god-functions', 'long-functions', 'deep-nesting', 'multi-return-fns',
    'bloated-docstrings', 'missing-docstrings', 'unsectioned-defs'})

#: Rules reported at line 1 or file-wide: blamed only on crossing a threshold.
FILE_RULES = frozenset({'oversized-files'})


# ---------------------------------------------------------------------------
# Scoring one file
# ---------------------------------------------------------------------------


def _parse(path: Path, text: str = None):
    """`(text, tree)` for one file; an unparsable file scores nothing."""
    if text is None:
        text = path.read_text(encoding='utf-8', errors='replace')
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        try:
            return text, ast.parse(text)
        except SyntaxError:
            return text, ast.Module(body=[], type_ignores=[])


def _missing(tool: str) -> bool:
    """True when `tool` is not importable, after saying how to install it."""
    try:
        subprocess.run([sys.executable, '-m', tool, '--version'],
                       capture_output=True, timeout=30, check=True)
        return False
    except Exception:
        print('  %s is NOT INSTALLED, so its rules did not run.' % tool,
              file=sys.stderr)
        print('  INSTALL: %s' % INSTALL, file=sys.stderr)
        return True


def _ruff_sites(path: Path) -> list:
    """Unused imports/variables and undefined names, from ruff."""
    try:
        got = subprocess.run(
            [sys.executable, '-m', 'ruff', 'check', '--select', RUFF_CODES,
             '--output-format', 'json', '--force-exclude', str(path)],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        rows = json.loads(got.stdout or '[]')
    except Exception:
        _missing('ruff')
        return []
    return [(path, r.get('location', {}).get('row', 1),
             '%s %s' % (r.get('code', '?'), r.get('message', '')))
            for r in rows]


def _headings(doc: Path) -> set:
    """Anchor targets in a markdown file: heading slugs and `<a id>` tags."""
    text = doc.read_text(encoding='utf-8', errors='replace')
    out = set(EXPLICIT_ANCHOR.findall(text))
    for line in text.split('\n'):
        if not line.startswith('#'):
            continue
        title = EXPLICIT_ANCHOR.sub('', line).lstrip('#').strip().lower()
        out.add(re.sub(r'[^a-z0-9\s-]', '', title).replace(' ', '-'))
    return out


def _citation_sites(path: Path, text: str) -> list:
    """`docs/` citations whose file or anchor does not exist.

    Resolved case-sensitively: on Windows `Path.exists()` ignores case, which
    is how a citation to a file that is really `Script_Conversion_Plan.md`
    survived unnoticed.
    """
    hits = []
    for num, line in enumerate(text.split('\n'), 1):
        if not REFERENCE.search(line):
            continue
        for ref in CITATION.findall(line):
            rel, _, anchor = ref.partition('#')
            target = ROOT / rel
            real = target.parent.exists() and target.name in os.listdir(
                target.parent) if target.parent.exists() else False
            if not real:
                hits.append((path, num, 'no such file: %s' % rel))
            elif anchor and anchor not in _headings(target):
                hits.append((path, num, 'no anchor #%s in %s' % (anchor, rel)))
    return hits


def rule_sites(path: Path, text: str = None, with_tools: bool = True,
               tree=None) -> dict:
    """`{rule: [(path, line, detail), ...]}` for one file's repo-wide rules."""
    if tree is None:
        text, tree = _parse(path, text)
    checks = {
        'inline-comments': D.inline_comments(path, text, tree),
        'stray-comments': D.stray_comments(path, text, tree),
        'comment-blocks': D.comment_blocks(path, text, tree),
        'bloated-docstrings': D.bloated_docstrings(path, text, tree),
        'missing-docstrings': D.missing_docstrings(path, tree),
        'fat-attr-docs': D.fat_attr_docs(path, text),
        'fat-sections': D.fat_sections(path, text),
        'unsectioned-defs': D.unsectioned_defs(path, text, tree),
        'dead-citations': _citation_sites(path, text),
    }
    checks.update(D.structural_sites(path, text, tree))
    if with_tools:
        checks['dead-imports'] = _ruff_sites(path)
    return {k: v for k, v in checks.items() if v}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _specific_hints(key: str, found: list) -> list:
    """Fixes for the CAUSES actually present, printed under the general remedy.

    One rule fires for causes with different cures -- a `# noqa: E402` and a
    paragraph of narration are both `stray-comments`, and "move it into a
    docstring" is wrong for the first.  Matching on the site detail lets the
    report name the real fix instead of the commonest one.
    """
    out = []
    for (rule, needle), hint in SPECIFIC.items():
        if rule != key or hint in out:
            continue
        if any(needle in str(site[2]) for site in found):
            out.append(hint)
    return out


def _report(path: Path, broken: dict, limit: int, headline: str) -> int:
    """Print each violation with what it means and its fix; 1 if any."""
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
        for hint in _specific_hints(key, found):
            print('    ALSO: %s' % hint, file=sys.stderr)
        for _p, line, detail in sorted(found, key=lambda x: x[1])[:limit]:
            print('      %s:%d: %s' % (shown, line, detail), file=sys.stderr)
        if len(found) > limit:
            print('      ... %d more (same fix)' % (len(found) - limit),
                  file=sys.stderr)
    return 1


def gate_file(path: Path, limit: int = 10) -> int:
    """1 if `path` breaks any repo-wide rule, printing each site and its fix."""
    if not path.exists() or path.suffix != '.py':
        return 0
    return _report(path, rule_sites(path), limit, 'RULES BROKEN')


# ---------------------------------------------------------------------------
# Blaming an edit
# ---------------------------------------------------------------------------


def _git(path: Path, *args) -> tuple:
    """`(ok, stdout)` for a git command about `path`."""
    try:
        rel = path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return False, ''
    try:
        got = subprocess.run(['git'] + [a.replace('{}', rel) for a in args],
                             cwd=ROOT, capture_output=True, text=True,
                             timeout=30)
    except Exception:
        return False, ''
    return got.returncode == 0, got.stdout


def _baseline_source(path: Path) -> str:
    """`path` as the INDEX holds it, else HEAD's copy.

    The index comes first so that work someone else STAGED is baseline, not
    the edit's own: git bills a staged change to whoever asks next.
    """
    for spec in (':{}', 'HEAD:{}'):
        ok, text = _git(path, 'show', spec)
        if ok:
            return text
    return ''


def _touched_lines(path: Path):
    """Line numbers this edit ADDED or CHANGED; None if git knows nothing.

    Diffed against the INDEX, never `HEAD`: `git diff HEAD` folds in staged
    changes, which billed one edit for every violation in 10 files it had
    never opened.
    """
    tracked, _ = _git(path, 'ls-files', '--error-unmatch', '{}')
    if not tracked:
        return None
    ok, out = _git(path, 'diff', '-U0', '--', '{}')
    if not ok:
        return None
    touched = set()
    for hunk in re.finditer(r'^@@ -\S+ \+(\d+)(?:,(\d+))? @@', out,
                            re.MULTILINE):
        start = int(hunk.group(1))
        touched.update(range(start, start + int(hunk.group(2) or 1)))
    return touched


def edit_region(touched: set, text: str) -> set:
    """Changed code lines PLUS the comment run directly above each.

    A comment above a line you edit is yours: blaming only the exact changed
    line absolved every comment sitting above it, which is most of them.
    """
    lines = text.split('\n')
    region = set(touched)
    for line in touched:
        i = line - 1
        while i >= 1 and i <= len(lines):
            raw = lines[i - 1].strip()
            if not raw or raw.startswith('#'):
                region.add(i)
                i -= 1
                continue
            break
    return region


def _worsened(found: list, before: list) -> list:
    """Sites whose MEASURE rose, not merely whose count rose.

    Keyed on the detail before its colon, compared on the first number after.
    Every detail MUST name its site there: a detail that is only a number keys
    on the number, so shrinking mints a new key and scores as worsening.
    """
    old = {}
    for site in before:
        name = str(site[2]).split(':')[0]
        dug = MAGNITUDE.search(str(site[2]))
        old[name] = int(dug.group(1)) if dug else 0
    worse = []
    for site in found:
        name = str(site[2]).split(':')[0]
        dug = MAGNITUDE.search(str(site[2]))
        now_val = int(dug.group(1)) if dug else 0
        if name not in old or now_val > old[name]:
            worse.append(site)
    return worse


def _blame(now: dict, was: dict, touched, text: str, tree) -> dict:
    """Violations this edit owns, by rule family.

    Prose rules blame through `edit_region`, with no file-wide guard: a legacy
    comment above an edited line does not raise the file's count, so a guard
    would absolve exactly the case this exists to catch.  Shape rules keep the
    guard, so a one-line edit never inherits a long function's debt.
    """
    if touched is None:
        return now
    region = edit_region(touched, text)
    spans = D.function_spans(tree)
    owned = {}
    for rule, found in now.items():
        before = was.get(rule, ())
        if rule in FILE_RULES:
            mine = _worsened(found, before)
        elif rule in NODE_SPAN_RULES:
            mine = [s for s in _worsened(found, before)
                    if any(a <= s[1] <= b and touched & set(range(a, b + 1))
                           for a, b in spans)]
        else:
            mine = [s for s in found if s[1] in region]
        if mine:
            owned[rule] = mine
    return owned


def gate_diff(path: Path, limit: int = 10) -> int:
    """1 when the agent's own edit owns a violation; else 0."""
    if not path.exists() or path.suffix != '.py':
        return 0
    touched = _touched_lines(path)
    if touched == set():
        return 0
    text, tree = _parse(path)
    now = rule_sites(path)
    before = _baseline_source(path)
    was = rule_sites(path, before, with_tools=False) if before else {}
    headline = ('NEW FILE -- all of it is yours' if touched is None
                else 'YOUR EDIT BROKE RULES')
    return _report(path, _blame(now, was, touched, text, tree), limit,
                   headline)


# ---------------------------------------------------------------------------
# Repo sweeps
# ---------------------------------------------------------------------------


def repo_files() -> list:
    """Every first-party `.py` file the rules judge.

    Prunes as it walks: `rglob` descends into `output/` and `references/`
    to find 2,752 files and then discards 83% of them, which cost 5.9s of
    every run.  See: docs/commentary/performance.md
    """
    found = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_PARTS]
        found += [Path(base) / n for n in names if n.endswith('.py')]
    return sorted(found)


def dead_code(limit: int = 40) -> int:
    """Report whole-program dead code; 1 if any.  Needs the WHOLE repo."""
    try:
        got = subprocess.run(
            [sys.executable, '-m', 'vulture', '.',
             '--min-confidence', VULTURE_CONFIDENCE,
             '--ignore-decorators', VULTURE_IGNORE_DECORATORS,
             '--exclude', ','.join(SKIP_PARTS)],
            cwd=ROOT, capture_output=True, text=True, timeout=300)
    except Exception as exc:
        if not _missing('vulture'):
            print('  vulture did not run: %s' % exc, file=sys.stderr)
        return 0
    found = [l for l in got.stdout.split('\n') if l.strip()]
    if not found:
        print('  no dead code at %s%% confidence' % VULTURE_CONFIDENCE)
        return 0
    print('  DEAD CODE (%d) -- %s' % (len(found), EXPLAIN['dead-code']),
          file=sys.stderr)
    print('  FIX: %s' % REMEDY['dead-code'], file=sys.stderr)
    for line in found[:limit]:
        print('    %s' % line, file=sys.stderr)
    if len(found) > limit:
        print('    ... %d more' % (len(found) - limit), file=sys.stderr)
    return 1


def sweep(limit: int) -> int:
    """Gate every first-party file; 1 if any breaks a rule."""
    worst = 0
    for path in repo_files():
        worst = max(worst, gate_file(path, limit))
    return worst


def _legend() -> int:
    """Print every rule, what it means, and how to fix it."""
    for key in sorted(EXPLAIN):
        print('  %-20s %s' % (key, EXPLAIN[key]))
        print('  %-20s FIX: %s' % ('', REMEDY.get(key, '')))
    return 0


def _parser() -> argparse.ArgumentParser:
    """The command line."""
    parser = argparse.ArgumentParser(
        description='Enforce the repo-wide code rules on one file or the tree.')
    parser.add_argument('--gate-file', metavar='PATH', action='append',
                        help='score ONE file absolutely; exit 1 on any break')
    parser.add_argument('--gate-diff', metavar='PATH', action='append',
                        help='score only what THIS edit owns (the hook path)')
    parser.add_argument('--sweep', action='store_true',
                        help='gate every first-party file')
    parser.add_argument('--dead-code', action='store_true',
                        help='whole-program dead code (vulture, 100%%)')
    parser.add_argument('--legend', action='store_true',
                        help='every rule, what it means, and its fix')
    parser.add_argument('--limit', type=int, default=10,
                        help='max sites printed per rule')
    return parser


def main(argv=None) -> int:
    """Parse arguments and run the requested gate."""
    try:
        sys.stdout.reconfigure(errors='replace')
        sys.stderr.reconfigure(errors='replace')
    except Exception:
        pass
    args = _parser().parse_args(argv)
    if args.legend:
        return _legend()
    if args.dead_code:
        return dead_code(args.limit)
    if args.sweep:
        return sweep(args.limit)
    if args.gate_file:
        return max(gate_file(Path(n).resolve(), args.limit)
                   for n in args.gate_file)
    if args.gate_diff:
        return max(gate_diff(Path(n).resolve(), args.limit)
                   for n in args.gate_diff)
    _parser().print_help()
    return 0


if __name__ == '__main__':
    sys.exit(main())
