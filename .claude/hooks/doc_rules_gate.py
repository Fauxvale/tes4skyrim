#!/usr/bin/env python3
"""PostToolUse gate: an edit must not make its own file's rules worse.

🛑 THIS EXISTS BECAUSE REPORTING IS NOT ENFORCEMENT.

`arch_fitness.py` printed `inline-comments 1617 !!` on every run for weeks and
the number never moved, because nothing made it move.  `--fail-on-regression`
only blocks movement AWAY from a target, so a metric parked at 2,704 passes
forever; an agent could always leave a file's comments exactly as they were and
stay green.  Being told the rule and choosing to skip it is the failure mode
this file removes: the check runs whether or not the agent decides to run it,
on the file it just wrote, immediately after writing it.

🛑 THE UNIT IS THE LINES THE EDIT CHANGED, NOT THE WHOLE FILE.  A whole-file
gate made a two-line fix in `converter.py` inherit all 1,010 of that file's
pre-existing violations -- a bill the edit did not run up and cannot pay, so
the gate would simply be switched off.  `--gate-diff` blames a violation only
when it sits on a line this branch changed AND its rule got no better
file-wide, so IMPROVING or HOLDING a rule passes and only worsening fails.
A file git does not track is wholly the agent's own work, so all of it counts.

Exit 2 feeds stderr back to the agent as a blocking error it must address.
Exit 0 is silent.
"""

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FITNESS = os.path.join(ROOT, 'tools', 'script', 'arch_fitness.py')

#: Trees the rules do not judge: scratch, vendored code, and generated output.
SKIP_PARTS = ('temp', 'references', 'external', 'output', 'export', 'build',
              '.git', 'node_modules', '__pycache__')


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
    result = subprocess.run(
        [sys.executable, FITNESS, '--gate-diff', path],
        cwd=ROOT, capture_output=True, text=True, timeout=120)
    if result.returncode == 0:
        return 0
    sys.stderr.write(result.stderr or result.stdout)
    sys.stderr.write(
        '\nThese sit on lines YOU changed. The fitness functions are a REQUIREMENT, not a guideline. Fix them\n')
    return 2


if __name__ == '__main__':
    sys.exit(main())
