#!/usr/bin/env python3
"""PostToolUse gate: an edit must not leave broken rules on the code it owns.

Reporting is not enforcement.  `arch_fitness.py` printed `inline-comments 1617`
on every run for weeks and the number never moved, because nothing made it
move.  This runs whether or not the agent decides to run it, on the file it
just wrote, immediately after writing it.

The unit is the code the edit OWNS: the lines it changed plus the comment run
directly above each.  A whole-file gate made a two-line fix inherit all 1,010
of `converter.py`'s violations -- a bill the edit cannot pay, so the gate gets
switched off.  Measured over every possible one-line edit in `script_convert/`,
91% of edits owe nothing and the median is 0.

🛑 IT ALSO GATES `Bash`.  Matching only Edit/Write leaves the whole gate
optional: a `python - <<'PY'` heredoc writes the same file and names no
`file_path`, so nothing fires.  Nine rule-breaking edits reached
`script_convert/` that way.  A Bash call is therefore judged on every tracked
`.py` that differs from HEAD.

Exit 2 feeds stderr back to the agent as a blocking error.  Exit 0 is silent.
"""

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GATE = os.path.join(ROOT, 'tools', 'validate', 'code_rules.py')

#: A fresh clone has no checkers and no import path until this is run once.
SETUP = ('  SET UP THIS REPO FIRST:  python -m pip install -e ".[dev]"\n'
         '  It puts the repo on sys.path and installs ruff, vulture, pytest.\n')

#: Trees the rules do not judge: scratch, vendored code, and generated output.
SKIP_PARTS = ('temp', 'references', 'external', 'output', 'export', 'build',
              '.git', 'node_modules', '__pycache__', '.venv')


def judged(path):
    """Is this a Python file the rules judge?"""
    if not path or not path.endswith('.py'):
        return False
    parts = os.path.normpath(os.path.abspath(path)).split(os.sep)
    return not any(p in SKIP_PARTS for p in parts) and os.path.isfile(path)


def dirty_python():
    """Every tracked `.py` differing from HEAD, as absolute paths.

    A Bash tool call names no file, so the only honest answer to "what did that
    write?" is to ask git.  Without this the gate is trivially bypassed by
    doing the edit through `python - <<'PY'` instead of Edit/Write -- which is
    exactly how nine rule-breaking edits reached `script_convert/` unchecked.
    """
    try:
        got = subprocess.run(['git', 'diff', '--name-only', 'HEAD'], cwd=ROOT,
                             capture_output=True, text=True, timeout=30)
    except Exception:
        return []
    if got.returncode != 0:
        return []
    return [os.path.join(ROOT, n) for n in got.stdout.split('\n')
            if judged(os.path.join(ROOT, n))]


def edited_paths(payload):
    """Every Python file this tool call may have written."""
    tool = payload.get('tool_name')
    if tool in ('Edit', 'Write', 'NotebookEdit'):
        path = (payload.get('tool_input') or {}).get('file_path')
        return [path] if judged(path) else []
    if tool in ('Bash', 'PowerShell'):
        return dirty_python()
    return []


def gate(path):
    """(exit code, report) for one file; code 0 means it passed."""
    try:
        got = subprocess.run(
            [sys.executable, GATE, '--gate-diff', path],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        return 0, 'The code-rules gate could not run: %s\n%s' % (exc, SETUP)
    if 'ModuleNotFoundError' in got.stderr:
        return 0, got.stderr + SETUP
    return got.returncode, (got.stderr or got.stdout)


def main():
    """Read the hook payload, gate what was written, block on a violation."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    paths = edited_paths(payload)
    if not paths:
        return 0
    if not os.path.isfile(GATE):
        sys.stderr.write('The code-rules gate is missing: %s\n%s'
                         % (GATE, SETUP))
        return 0
    broke = False
    for path in paths:
        code, report = gate(path)
        if report and code == 0 and 'could not run' in report:
            sys.stderr.write(report)
            return 0
        if code:
            sys.stderr.write(report)
            broke = True
    if not broke:
        return 0
    sys.stderr.write('\nThese violations are on code YOUR edit owns. '
                     'The code rules are a REQUIREMENT, not a guideline.\n')
    return 2


if __name__ == '__main__':
    sys.exit(main())
