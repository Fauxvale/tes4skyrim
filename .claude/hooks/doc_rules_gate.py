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


def edited_path(payload):
    """The file this tool call wrote, or None if it wrote no Python."""
    if payload.get('tool_name') not in ('Edit', 'Write', 'NotebookEdit'):
        return None
    path = (payload.get('tool_input') or {}).get('file_path')
    if not path or not path.endswith('.py'):
        return None
    if any(part in SKIP_PARTS for part in os.path.normpath(path).split(os.sep)):
        return None
    return path if os.path.isfile(path) else None


def main():
    """Read the hook payload, gate the edited file, block on a violation."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    path = edited_path(payload)
    if path is None:
        return 0
    if not os.path.isfile(GATE):
        sys.stderr.write('The code-rules gate is missing: %s\n' % GATE)
        sys.stderr.write(SETUP)
        return 0
    try:
        result = subprocess.run(
            [sys.executable, GATE, '--gate-diff', path],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
    except Exception as exc:
        sys.stderr.write('The code-rules gate could not run: %s\n' % exc)
        sys.stderr.write(SETUP)
        return 0
    if 'ModuleNotFoundError' in result.stderr:
        sys.stderr.write(result.stderr)
        sys.stderr.write(SETUP)
        return 0
    if result.returncode == 0:
        return 0
    sys.stderr.write(result.stderr or result.stdout)
    sys.stderr.write('\nThese violations are on code YOUR edit owns. '
                     'The code rules are a REQUIREMENT, not a guideline.\n')
    return 2


if __name__ == '__main__':
    sys.exit(main())
