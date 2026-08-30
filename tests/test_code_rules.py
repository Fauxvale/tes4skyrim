"""The repo-wide code rules: the escape hatches that used to let edits pass.

Each test names a bypass that was measured before the rules moved into
`tools/validate/code_rules.py`.  They are pure in-memory calls -- no git, no
subprocess -- so the whole file runs in well under a second.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

from tools.validate import code_rules as CR
from tools.validate import code_rules_ast as D

ROOT = Path(__file__).resolve().parent.parent
FAKE = Path('t.py')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sites(source, rule):
    """Lines flagged for `rule` in `source`."""
    found = CR.rule_sites(FAKE, source, with_tools=False)
    return sorted(s[1] for s in found.get(rule, ()))


def blame(before, after, touched, rule):
    """Lines an edit owns for `rule`, given the lines it changed."""
    text, tree = CR._parse(FAKE, after)
    now = CR.rule_sites(FAKE, after, with_tools=False)
    was = CR.rule_sites(FAKE, before, with_tools=False)
    owned = CR._blame(now, was, set(touched), text, tree)
    return sorted(s[1] for s in owned.get(rule, ()))


# ---------------------------------------------------------------------------
# Comments: the scanner must tokenize, not read the first character
# ---------------------------------------------------------------------------


OWN_LINE = '''"""M."""


def f(x) -> int:
    """D."""
    # a measured 2026 census of 3,740 records
    return x
'''

TRAILING = '''"""M."""


def f(x) -> int:
    """D."""
    return x  # a measured 2026 census of 3,740 records
'''


def test_trailing_comment_is_not_an_escape_hatch():
    """The same prose must be flagged wherever on the line it sits."""
    assert sites(OWN_LINE, 'inline-comments') == [6]
    assert sites(TRAILING, 'inline-comments') == [6]


def test_no_comment_is_exempt_by_its_prefix():
    """A directive is still a comment: nothing may be waved past by prefix.

    An exemption keyed on `# noqa` would let any sentence through by prepending
    one, and the repo's own `# noqa: plugin-path` (14 uses, no reader) shows
    the costume is already worn.
    """
    for comment in ('# noqa: F401', '# noqa', '# noqa: plugin-path (.psc)',
                    '# pragma: no cover', '# type: ignore'):
        src = '"""M."""\nVALUE = 1  %s\n' % comment
        assert sites(src, 'stray-comments') == [2], comment


def test_banner_prefix_cannot_launder_prose():
    """`# --- text ---` is not a heading; a heading is a bare rule line."""
    src = '''"""M."""


def f(x) -> int:
    """D."""
    # --- anything I want to say at length ---
    return x
'''
    assert sites(src, 'inline-comments') == [6]


# ---------------------------------------------------------------------------
# Docstrings: a flat cap, never a multiple of the body
# ---------------------------------------------------------------------------


def _fn(doc_chars, body_lines):
    """A function with a docstring of `doc_chars` over `body_lines` lines."""
    body = '\n'.join('    a%d = %d' % (i, i) for i in range(body_lines))
    return '"""M."""\n\n\ndef f():\n    """%s"""\n%s\n    return 0\n' % (
        'x' * doc_chars, body)


def test_long_body_does_not_buy_prose_budget():
    """A 600-char docstring is over the cap however long the body is."""
    assert sites(_fn(600, 10), 'bloated-docstrings') == [4]
    assert sites(_fn(600, 40), 'bloated-docstrings') == [4]


def test_short_body_keeps_the_tight_limit():
    """A one- or two-line body gets one line of docstring."""
    assert sites(_fn(200, 1), 'bloated-docstrings') == [4]
    assert sites(_fn(60, 1), 'bloated-docstrings') == []


def test_citation_line_is_not_counted_as_prose():
    """The `See:` route must not be punished by the rule that asks for it."""
    doc = 'x' * 70 + '\n\n    See: docs/reference/pipeline.md'
    src = '"""M."""\n\n\ndef f():\n    """%s"""\n    return 0\n' % doc
    assert sites(src, 'bloated-docstrings') == []


# ---------------------------------------------------------------------------
# Blame: what an edit owns
# ---------------------------------------------------------------------------


LEGACY = '''"""M."""


def big(x) -> int:
    """D."""
    # legacy one
    a = x + 1
    # legacy two
    b = a + 1
    return b
'''


def test_legacy_comment_above_an_edited_line_is_owned():
    """Editing a line adopts the comment sitting above it."""
    after = LEGACY.replace('b = a + 1', 'b = a + 9')
    assert blame(LEGACY, after, [9], 'inline-comments') == [8]


def test_edit_with_no_comment_above_owes_nothing():
    """The common case: 91% of one-line edits owe zero."""
    after = LEGACY.replace('    """D."""', '    """D2."""')
    assert blame(LEGACY, after, [5], 'inline-comments') == []


def test_new_comment_is_owned():
    """A comment the edit wrote is always its own."""
    after = LEGACY.replace('    return b', '    # mine\n    return b')
    assert 10 in blame(LEGACY, after, [10, 11], 'inline-comments')


def test_shape_debt_is_not_inherited_by_a_small_edit():
    """A one-line edit never adopts a long function's pre-existing shape."""
    body = '\n'.join('    a%d = %d' % (i, i) for i in range(80))
    src = '"""M."""\n\n\ndef f():\n    """D."""\n%s\n    return 0\n' % body
    assert sites(src, 'long-functions') == [4]
    assert blame(src, src.replace('a5 = 5', 'a5 = 6'), [10],
                 'long-functions') == []


def _sized(body_lines):
    """A function with a one-line docstring over `body_lines` of body."""
    return _fn(4, body_lines)


def _branchy(branches):
    """A function whose cyclomatic complexity is `branches` plus one."""
    body = '\n'.join('    if x == %d:\n        pass' % i
                     for i in range(branches))
    return '"""M."""\n\n\ndef f(x):\n    """D."""\n%s\n    return 0\n' % body


def test_growing_an_already_long_function_is_blamed():
    """A shape metric may never get worse: 100 lines -> 130 is blamed."""
    assert blame(_sized(100), _sized(130), range(106, 136),
                 'long-functions') == [4]


def test_shrinking_a_long_function_passes_even_while_over():
    """Improving is always allowed: the bill is what you added, not the debt."""
    assert blame(_sized(130), _sized(100), range(56, 106),
                 'long-functions') == []


def test_growing_complexity_of_a_god_function_is_blamed():
    """Same ratchet for complexity, which is also one site per function."""
    assert blame(_branchy(29), _branchy(59), range(60, 120),
                 'god-functions') == [4]


def test_nesting_added_below_the_def_is_owned():
    """The site is the `def`, but the edit that deepened it still owns it."""
    before = '''"""M."""


def f(x) -> int:
    """D."""
    if x:
        return 1
    return 0
'''
    after = '''"""M."""


def f(x) -> int:
    """D."""
    if x:
        for i in range(3):
            while i:
                with open('a') as h:
                    if h:
                        return 1
    return 0
'''
    assert blame(before, after, list(range(7, 12)), 'deep-nesting') == [4]


# ---------------------------------------------------------------------------
# The tool itself
# ---------------------------------------------------------------------------


def test_help_does_not_crash():
    """`--help` must work on a cp1252 console: no emoji in the description."""
    got = subprocess.run(
        [sys.executable, str(ROOT / 'tools' / 'validate' / 'code_rules.py'),
         '--help'], capture_output=True, text=True, timeout=60)
    assert got.returncode == 0
    assert got.stderr == ''


def test_a_situational_fix_beats_the_generic_one():
    """A rule with several causes must name the cure for the one it found."""
    sites_found = [(FAKE, 8, 'noqa: E402')]
    hints = CR._specific_hints('stray-comments', sites_found)
    assert any('pythonpath' in h for h in hints)
    assert CR._specific_hints('stray-comments', [(FAKE, 3, 'a plain note')]) == []


def test_every_situational_hint_names_a_real_rule():
    """A hint keyed on a rule that does not exist would never print."""
    assert {rule for rule, _ in CR.SPECIFIC} <= set(CR.EXPLAIN)


def test_every_gated_rule_defines_itself():
    """EXPLAIN and REMEDY are the rule's whole definition to the agent."""
    assert set(CR.EXPLAIN) == set(CR.REMEDY)


def test_multiplier_is_gone():
    """A prose budget must never be a function of the code around it."""
    source = (ROOT / 'tools' / 'validate' / 'code_rules_ast.py').read_text(
        encoding='utf-8')
    assert 'DOC_CHARS_PER_LINE' not in source


# ---------------------------------------------------------------------------
# What the gate must NOT bill: staged work, and files outside the repo
# ---------------------------------------------------------------------------


def _run_gate(*args):
    """`--gate-diff` on `args`, returning the finished process."""
    return subprocess.run(
        [sys.executable, str(ROOT / 'tools' / 'validate' / 'code_rules.py'),
         '--gate-diff'] + list(args), cwd=ROOT, capture_output=True, text=True)


def test_a_staged_file_with_a_clean_worktree_owes_nothing():
    """`git diff HEAD` folds in the INDEX, billing an edit for staged work.

    Reproduced with 10 files staged by a rename: every later tool call was
    blamed for `oversized-files` in two files it had never opened.
    """
    staged = subprocess.run(['git', 'diff', '--cached', '--name-only'],
                            cwd=ROOT, capture_output=True, text=True).stdout
    dirty = subprocess.run(['git', 'diff', '--name-only'], cwd=ROOT,
                           capture_output=True, text=True).stdout
    for name in staged.split():
        if not name.endswith('.py') or name in dirty:
            continue
        got = _run_gate(name)
        assert got.returncode == 0, '%s: %s' % (name, got.stderr)


def test_an_untracked_file_is_still_scored_whole():
    """The index fix must not blind the gate to a brand-new file."""
    probe = ROOT / 'tools' / 'validate' / '_gate_probe_tmp.py'
    probe.write_text(OWN_LINE, encoding='utf-8')
    try:
        got = _run_gate(str(probe))
        assert got.returncode == 1
        assert 'inline-comments' in got.stderr
    finally:
        probe.unlink()


def test_a_file_outside_the_repo_is_not_judged():
    """A scratchpad shares no component with SKIP_PARTS, and Temp != temp."""
    sys.path.insert(0, str(ROOT / '.claude' / 'hooks'))
    try:
        import doc_rules_gate as gate
    finally:
        sys.path.pop(0)
    outside = Path(tempfile.gettempdir()) / 'claude_gate_probe.py'
    outside.write_text(OWN_LINE, encoding='utf-8')
    try:
        assert not gate.judged(str(outside))
        assert gate.judged(str(ROOT / 'tools' / 'validate' / 'code_rules.py'))
    finally:
        outside.unlink()


# ---------------------------------------------------------------------------
# The `See:` exemption: a bare citation only, never a carrier for prose
# ---------------------------------------------------------------------------


def _module_comment(comment):
    """A module-level constant carrying `comment` above it."""
    return '"""M."""\n\n%s\nVALUE = 1\n' % comment


def test_a_bare_see_line_is_legal():
    """The outflow route needs one legal line to point at what it moved."""
    for good in ('# See: docs/commentary/performance.md',
                 '# See: docs/commentary/performance.md#parallelism-rules'):
        assert sites(_module_comment(good), 'stray-comments') == [], good


def test_a_see_line_may_not_carry_prose():
    """`See:` must not become the prefix that launders a sentence."""
    for bad in ('# See: docs/commentary/performance.md, but only when the '
                'writer is single-process',
                '# a measured census. See: docs/commentary/performance.md',
                '# See: docs/commentary/performance.md and the notes above'):
        assert sites(_module_comment(bad), 'stray-comments') == [3], bad


def test_a_see_line_is_capped_at_eighty_characters():
    """Past the cap the line is prose wearing a citation."""
    fits = '# See: docs/%s.md' % ('a' * 60)
    over = '# See: docs/%s.md' % ('a' * 70)
    assert len(fits) <= D.MAX_SEE_CHARS < len(over)
    assert sites(_module_comment(fits), 'stray-comments') == []
    assert sites(_module_comment(over), 'stray-comments') == [3]


def test_the_exemption_does_not_reach_inside_a_function():
    """A citation is module-level bookkeeping, not an in-body comment."""
    src = ('"""M."""\n\n\ndef f(x) -> int:\n    """D."""\n'
           '    # See: docs/commentary/performance.md\n    return x\n')
    assert sites(src, 'inline-comments') == [6]


def _size_site(n):
    """The `oversized-files` site a file of `n` code lines reports."""
    return [(ROOT / 'x.py', 1, 'file: %d code lines' % n)]


def test_shrinking_an_oversized_file_is_absolved():
    """A same-size or smaller file owes nothing, however far over it sits."""
    assert CR._worsened(_size_site(1190), _size_site(1200)) == []
    assert CR._worsened(_size_site(1200), _size_site(1200)) == []


def test_growing_or_crossing_an_oversized_file_is_charged():
    """Climbing while over, and crossing from under, are both owed."""
    assert CR._worsened(_size_site(1210), _size_site(1200))
    assert CR._worsened(_size_site(1010), [])


def test_a_test_file_is_not_judged_on_length():
    """Length is a COUNT of cases there, not a responsibility to split."""
    got = CR.rule_sites(Path(__file__).resolve(), with_tools=False)
    assert 'oversized-files' not in got


def test_the_length_exemption_reaches_no_other_rule():
    """Only `oversized-files` is lifted; a test file owes every other rule."""
    src = '"""M."""\n\n\ndef f(x) -> int:\n    """D."""\n    x += 1  # bump\n    return x\n'
    probe = ROOT / 'tests' / '_gate_probe_tmp.py'
    probe.write_text(src, encoding='utf-8')
    try:
        assert 'inline-comments' in CR.rule_sites(probe, with_tools=False)
    finally:
        probe.unlink()
