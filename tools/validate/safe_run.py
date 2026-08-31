#!/usr/bin/env python3
"""Run a shell command, then gate every first-party `.py` it wrote.

    python tools/validate/safe_run.py <command...>

Bash is denied at the permission layer because a command's effect cannot be
predicted before it runs, which is how a `python - <<'PY'` heredoc wrote files
the PreToolUse gate never saw.  This is the one allowed passthrough: it hashes
the tracked `.py` files, runs the command with the streams INHERITED so output
still arrives live, re-hashes, and gates whatever changed.

Exit code is the child's, unless a write left a violation -- then it is 2, so a
rule-breaking write can never be reported as success.

See: docs/reference/script_convert_architecture.md#what-the-gate-must-see
"""

import hashlib
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from tools.validate import code_rules as CR

#: Exit code the harness reads as "blocked"; the child's own code otherwise.
BLOCKED = 2

#: The caller's own shell, so the command runs in the dialect it was written in.
SHELL = os.environ.get('SHELL') or os.environ.get('COMSPEC') or ''


def shell_argv(command: str) -> list:
    """`[shell, -c, command]`, or None to take the platform default shell.

    See: docs/reference/script_convert_architecture.md#what-the-gate-must-see
    """
    if not SHELL or SHELL.lower().endswith('cmd.exe'):
        return None
    return [SHELL, '-c', command]


def raw_tail() -> str:
    """The command line after this script's name, with quoting intact.

    `sys.argv` has already had one layer of quoting removed, so rejoining it
    is lossy in both directions: a plain join drops the quotes around
    `-k "a or b"`, and `list2cmdline` adds a second layer that the inner shell
    then takes literally.  The untouched line is read from the OS instead.
    See: docs/reference/script_convert_architecture.md#what-the-gate-must-see
    """
    if os.name != 'nt':
        return ' '.join(sys.argv[1:])
    import ctypes
    get = ctypes.windll.kernel32.GetCommandLineW
    get.restype = ctypes.c_wchar_p
    get.argtypes = []
    line = get() or ''
    stem = os.path.basename(__file__)
    cut = line.find(stem)
    tail = line[cut + len(stem):].strip() if cut >= 0 else ''
    if len(tail) > 1 and tail[0] == tail[-1] and tail[0] in '"\'':
        return tail[1:-1]
    return tail


def digests() -> dict:
    """`{path: sha256}` for every first-party `.py` file in the repo."""
    out = {}
    for path in CR.repo_files():
        try:
            out[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return out


def written(before: dict, after: dict) -> list:
    """Files whose contents changed, plus any the command created."""
    return sorted(p for p in after if before.get(p) != after[p])


def gate_paths(paths: list) -> int:
    """Print the gate report for each path; 1 when any of them broke a rule."""
    worst = 0
    for path in paths:
        worst = max(worst, CR.gate_diff(path))
    return worst


def main(argv: list) -> int:
    """Run the command in `argv` and gate what it wrote.

    The command is taken VERBATIM from the raw command line, never re-joined
    from `sys.argv`: the shell has already removed one layer of quoting, so
    `-k "a or b"` re-joins into three bare words and a heredoc loses its
    newlines.  Everything after the script name is passed through untouched.
    """
    if not argv:
        print(__doc__, file=sys.stderr)
        return BLOCKED
    before = digests()
    spawn = shell_argv(argv)
    done = (subprocess.run(spawn, cwd=CR.ROOT) if spawn
            else subprocess.run(argv, shell=True, cwd=CR.ROOT))
    changed = written(before, digests())
    if not changed:
        return done.returncode
    print('\n  safe_run: %d file(s) written -- gating them'
          % len(changed), file=sys.stderr)
    if not gate_paths(changed):
        return done.returncode
    print('\nTHE COMMAND WROTE CODE THAT BREAKS THE RULES. Fix the violations '
          'above; the write has already landed, so the file is dirty until '
          'you do.', file=sys.stderr)
    return BLOCKED


if __name__ == '__main__':
    sys.exit(main(raw_tail()))
