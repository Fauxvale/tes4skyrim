#!/usr/bin/env python3
"""Give a `docs/commentary/` file its title, `Code:` binding and Contents.

    python tools/validate/doc_header.py --spec headers.json --apply
    python tools/validate/doc_header.py --check

`--spec` takes `{"docs/commentary/x.md": {"title": ..., "code": [...],
"history": [...]}}` and rewrites the head of each file: an `#` title, a
`**Code:**` line naming the modules the file explains, an optional
`**History:**` line linking the audits and plans it grew out of, and a
`## Contents` list linking every `## ` heading by its explicit anchor.

Anchors are `<a id="...">` tags inserted under each heading, never heading
slugs: 55 of 95 headings in the old `nif_conversion.md` carried a date, so a
slug changed whenever a date was corrected.

`--check` reports commentary files missing the `**Code:**` binding.

See: docs/README.md
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
DOCS = ROOT / 'docs' / 'commentary'

#: Words dropped when a heading becomes an anchor slug.
NOISE = re.compile(r'\b(?:the|a|an|of|to|in|is|and|for|on|at|by|it)\b')

#: A date, a parenthetical or a status word: never part of a stable anchor.
TRIM = re.compile(r'\(.*?\)|[-—,;:]?\s*\b(?:solved|fixed|rewritten|'
                  r'verified|found|added|learned|implemented)\b.*'
                  r'|20\d\d[-/]?\d*.*|[^a-z0-9 -]')


def slug(title: str) -> str:
    """A short, date-free anchor for `title`."""
    bare = TRIM.sub('', title.lower())
    bare = NOISE.sub(' ', bare)
    words = [w for w in re.split(r'[^a-z0-9]+', bare) if w][:5]
    return '-'.join(words) or 'section'


def _headings(lines: list) -> list:
    """`(index, title)` for every `## ` heading."""
    return [(i, ln[3:].strip()) for i, ln in enumerate(lines)
            if ln.startswith('## ')]


def retitle(path: Path, spec: dict) -> str:
    """The rewritten text of `path`: header, anchors, then the body."""
    lines = path.read_text(encoding='utf-8', errors='replace').split('\n')
    body = [ln for ln in lines if not ln.startswith('# ')]
    while body and not body[0].strip():
        body.pop(0)
    used, contents = set(), []
    for idx, title in _headings(body):
        anchor = slug(title)
        while anchor in used:
            anchor += '-2'
        used.add(anchor)
        body[idx] = '%s\n<a id="%s"></a>' % (body[idx], anchor)
        contents.append('- [%s](#%s)' % (title.replace('`', ''), anchor))
    head = ['# %s' % spec['title'], '',
            '**Code:** %s' % ', '.join('`%s`' % c for c in spec['code'])]
    if spec.get('history'):
        head.append('**History:** %s' % ', '.join(spec['history']))
    head += ['', '## Contents', ''] + contents + ['']
    return '\n'.join(head + body).rstrip('\n') + '\n'


def check() -> int:
    """Report commentary files with no `**Code:**` binding."""
    bad = [d for d in sorted(DOCS.glob('*.md'))
           if '**Code:**' not in d.read_text(encoding='utf-8', errors='replace')]
    for doc in bad:
        print('  %s has no **Code:** line' % doc.relative_to(ROOT),
              file=sys.stderr)
    print('  %d commentary file(s) unbound' % len(bad))
    return 1 if bad else 0


def main(argv=None) -> int:
    """Apply a header spec, or check that every file carries one."""
    parser = argparse.ArgumentParser(
        description='Add title, Code binding and Contents to commentary docs.')
    parser.add_argument('--spec', metavar='JSON')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if args.check:
        return check()
    spec = json.loads(Path(args.spec).read_text(encoding='utf-8'))
    for name, meta in spec.items():
        path = ROOT / name
        text = retitle(path, meta)
        if args.apply:
            path.write_text(text, encoding='utf-8', newline='')
    print('headed %d file(s)%s' % (len(spec), '' if args.apply else ' (dry)'))
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(errors='replace')
    sys.exit(main())
