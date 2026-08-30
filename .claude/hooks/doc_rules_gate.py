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
LINKS = os.path.join(ROOT, 'tools', 'validate', 'doc_links.py')
DOCS = os.path.join(os.path.realpath(ROOT), 'docs')

#: A fresh clone has no checkers and no import path until this is run once.
SETUP = ('  SET UP THIS REPO FIRST:  python -m pip install -e ".[dev]"\n'
         '  It puts the repo on sys.path and installs ruff, vulture, pytest.\n')

#: Trees the rules do not judge: scratch, vendored code, and generated output.
SKIP_PARTS = ('temp', 'references', 'external', 'output', 'export', 'build',
              '.git', 'node_modules', '__pycache__', '.venv')


def judged(path):
    """Is this a Python file INSIDE the repo that the rules judge?

    Containment, not a component scan: a scratchpad under `AppData\\Local\\
    Temp` shares no component with SKIP_PARTS, and its `Temp` never matched
    the lowercase `temp` either, so the gate used to score throwaway probes.
    """
    if not path or not path.endswith('.py') or not os.path.isfile(path):
        return False
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(ROOT))
    except ValueError:
        return False
    if rel.startswith(os.pardir):
        return False
    return not any(p.lower() in SKIP_PARTS for p in rel.split(os.sep))


def dirty_python():
    """Every tracked `.py` differing from HEAD, as absolute paths.

    A Bash tool call names no file, so the only honest answer to "what did that
    write?" is to ask git.  Without this the gate is trivially bypassed by
    doing the edit through `python - <<'PY'` instead of Edit/Write -- which is
    exactly how nine rule-breaking edits reached `script_convert/` unchecked.
    """
    try:
        got = subprocess.run(['git', 'diff', '--name-only'], cwd=ROOT,
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


def touched_docs(payload):
    """True when this call may have written a file under `docs/`."""
    tool = payload.get('tool_name')
    if tool in ('Edit', 'Write', 'NotebookEdit'):
        path = (payload.get('tool_input') or {}).get('file_path') or ''
        return path.endswith('.md') and DOCS in os.path.realpath(path)
    if tool not in ('Bash', 'PowerShell'):
        return False
    try:
        got = subprocess.run(['git', 'diff', '--name-only'], cwd=ROOT,
                             capture_output=True, text=True, timeout=30)
    except Exception:
        return False
    return any(n.startswith('docs/') for n in got.stdout.split('\n'))


def gate_docs():
    """(exit code, report) for the doc tree: links, anchors, index, bindings."""
    try:
        got = subprocess.run(
            [sys.executable, LINKS, '--index'], cwd=ROOT,
            capture_output=True, text=True, timeout=60)
    except Exception:
        return 0, ''
    return got.returncode, got.stderr


def pending_text(payload):
    """(path, post-edit text) this call WOULD write, or (None, None).

    Only Edit/Write can be predicted: their arguments fully determine the
    result.  A `Bash` heredoc's effect is unknowable without running it, so
    Bash stays on the PostToolUse sweep.
    """
    tool = payload.get('tool_name')
    args = payload.get('tool_input') or {}
    path = args.get('file_path')
    if not path or not path.endswith('.py'):
        return None, None
    if tool == 'Write':
        return path, args.get('content') or ''
    if tool != 'Edit':
        return None, None
    try:
        with open(path, encoding='utf-8') as fh:
            text = fh.read()
    except OSError:
        return None, None
    old, new = args.get('old_string') or '', args.get('new_string') or ''
    if old not in text:
        return None, None
    count = -1 if args.get('replace_all') else 1
    return path, text.replace(old, new, count)


def gate_pending(path, text):
    """(exit code, report) for the text an edit WOULD leave on disk.

    The gate reads the file from disk and diffs it against git, so the
    candidate is written in place, scored, and the original restored.  A
    crash between the two would leave the candidate behind, so the restore
    runs in `finally`.
    """
    try:
        with open(path, 'rb') as fh:
            original = fh.read()
    except OSError:
        return 0, ''
    try:
        with open(path, 'w', encoding='utf-8', newline='') as fh:
            fh.write(text)
        return gate(path)
    except OSError:
        return 0, ''
    finally:
        try:
            with open(path, 'wb') as fh:
                fh.write(original)
        except OSError:
            pass


def gate_pre(payload):
    """Deny an Edit/Write that would leave a violation on code it owns."""
    path, text = pending_text(payload)
    if not path or not judged(path):
        return 0
    code, report = gate_pending(path, text)
    if not code or not report:
        return 0
    sys.stderr.write(report)
    sys.stderr.write(
        '\nTHIS EDIT WAS NOT APPLIED. Revise it to clear the violations '
        'above -- they are on the lines this edit touches -- then retry. Do '
        'not route around this with Bash.\n')
    return 2


def main():
    """Read the hook payload, gate what was written, block on a violation."""
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    event = payload.get('hook_event_name')
    if not os.path.isfile(GATE):
        return 0
    if event == 'PreToolUse':
        return gate_pre(payload)
    if touched_docs(payload) and os.path.isfile(LINKS):
        code, report = gate_docs()
        if code:
            sys.stderr.write(report)
            sys.stderr.write('\nA broken link or an unindexed doc loses the '
                             'knowledge it points at. Fix it now.\n')
            return 2
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
