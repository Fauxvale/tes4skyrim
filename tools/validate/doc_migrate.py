#!/usr/bin/env python3
"""Move and split `docs/` files, rewriting every citation that points at them.

    python tools/validate/doc_migrate.py --map moves.json --apply
    python tools/validate/doc_migrate.py --sections docs/commentary/asset_convert_nif.md

`--sections` prints each `## ` heading with its line span and the module it
names most often, which is what decides a split without reading the file.
`--map` takes `{"old/path.md": "new/path.md"}` and rewrites every markdown
link, bare `docs/...` citation and relative `../kind/name.md` across `docs/`,
`CLAUDE.md`, `README.md`, `TODO.txt` and every first-party `.py`.

Splitting carves line ranges with `--carve`, which takes
`{"new/path.md": ["src.md", [start, end], [start, end]]}` and keeps the
source's first heading block as the new file's title.

See: docs/README.md
"""

import argparse
import collections
import json
import re
import sys
from pathlib import Path

from tools.validate import code_rules as CR

ROOT = Path(__file__).resolve().parent.parent.parent
DOCS = ROOT / 'docs'

#: `asset_convert/foo.py` and bare `foo.py`, to guess a section's owner.
MODULE = re.compile(r'\b(?:[a-z0-9_]+/)*([a-z0-9_]+)\.py\b')

#: Files outside `docs/` that cite it and must be rewritten on a move.
EXTRA = ('CLAUDE.md', 'README.md', 'TODO.txt')


def sections(doc: Path) -> list:
    """`(line, span, module, title)` for every `## ` heading in `doc`."""
    lines = doc.read_text(encoding='utf-8', errors='replace').split('\n')
    heads = [(i + 1, ln) for i, ln in enumerate(lines) if ln.startswith('## ')]
    heads.append((len(lines) + 1, ''))
    out = []
    for (start, title), (nxt, _) in zip(heads, heads[1:]):
        body = '\n'.join(lines[start - 1:nxt - 1])
        mods = collections.Counter(MODULE.findall(body))
        owner = mods.most_common(1)[0][0] if mods else '-'
        out.append((start, nxt - start, owner, title[3:]))
    return out


def _sources() -> list:
    """Every file that may cite a doc path."""
    found = sorted(DOCS.rglob('*.md'))
    found += [ROOT / name for name in EXTRA if (ROOT / name).exists()]
    return found + CR.repo_files()


def _swaps(moves: dict) -> list:
    """`(pattern, replacement)` pairs covering every way a doc is cited."""
    out = []
    for old, new in moves.items():
        out.append((re.escape(old), new))
        out.append((re.escape(old.replace('docs/', '', 1)),
                    new.replace('docs/', '', 1)))
        out.append((re.escape('../' + old.replace('docs/', '', 1)),
                    '../' + new.replace('docs/', '', 1)))
    return sorted(out, key=lambda p: -len(p[0]))


def rewrite(moves: dict, apply: bool) -> int:
    """Repoint every citation; returns the number of files changed."""
    swaps = _swaps(moves)
    changed = 0
    for source in _sources():
        try:
            text = source.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError):
            continue
        fixed = text
        for pattern, into in swaps:
            fixed = re.sub(pattern, into.replace('\\', '/'), fixed)
        if fixed != text:
            changed += 1
            if apply:
                source.write_text(fixed, encoding='utf-8', newline='')
    return changed


def carve(spec: dict, apply: bool) -> int:
    """Write each new doc from the line ranges of its source."""
    made = 0
    for target, parts in spec.items():
        src = ROOT / parts[0]
        lines = src.read_text(encoding='utf-8', errors='replace').split('\n')
        body = []
        for start, end in parts[1:]:
            body.extend(lines[start - 1:end])
        out = ROOT / target
        made += 1
        if apply:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text('\n'.join(body).rstrip('\n') + '\n',
                           encoding='utf-8', newline='')
    return made


def main(argv=None) -> int:
    """Print a section map, or apply a move/carve spec."""
    parser = argparse.ArgumentParser(
        description='Move, split and re-cite files under docs/.')
    parser.add_argument('--sections', metavar='DOC')
    parser.add_argument('--map', metavar='JSON')
    parser.add_argument('--carve', metavar='JSON')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if args.sections:
        for start, span, owner, title in sections(Path(args.sections)):
            print('%5d %5d  %-20s %s' % (start, span, owner, title[:64]))
        return 0
    if args.carve:
        spec = json.loads(Path(args.carve).read_text(encoding='utf-8'))
        print('carved %d file(s)%s' % (carve(spec, args.apply),
                                       '' if args.apply else ' (dry run)'))
    if args.map:
        moves = json.loads(Path(args.map).read_text(encoding='utf-8'))
        print('rewrote %d file(s)%s' % (rewrite(moves, args.apply),
                                        '' if args.apply else ' (dry run)'))
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(errors='replace')
    sys.exit(main())
