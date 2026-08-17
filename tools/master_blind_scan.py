#!/usr/bin/env python3
"""Find master-blind functions in the import pipeline.

The recurring defect (CLAUDE.md "master-export blindness"): a phase indexes only
`by_type` -- the CURRENT plugin's export -- and never consults
`ctx.master_export`, so an actor's master-owned packages, items, scripts or refs
resolve to nothing and the feature silently dies.

Auditing this PER FILE does not work: a module can be partly master-aware and
still contain a blind function. `pack_aliases.py` mentions `master_export` four
times, but all four are in `build_script_var_map` -- `PackagePlan.build()` in the
same file is blind, and a per-file scan scores the module "already fixed" and
skips it. This scans per FUNCTION instead.

A hit is a CANDIDATE, not a defect. Many functions are legitimately
plugin-scoped (a master's own records are converted in the master's own run).
Always measure the candidate against real export data before calling it a bug --
`--measure` does that for the base-record cases.

Usage:
    python tools/master_blind_scan.py                       # scan tes5_import/
    python tools/master_blind_scan.py --path script_convert
    python tools/master_blind_scan.py --measure ElsweyrAnequina.esp
"""
import argparse
import ast
import os
import sys

DEFAULT_ROOTS = ['tes5_import']
# A body mentioning any of these is treated as master-aware.
AWARE_TOKENS = ('master_export', 'ctx.', 'master_record', 'master_manifest')


def scan_file(path):
    """[(lineno, qualname, by_type_count)] for blind functions in one file."""
    try:
        src = open(path, encoding='utf-8', errors='replace').read()
    except OSError:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    lines = src.splitlines()
    hits = []

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def visit_ClassDef(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def _fn(self, node):
            body = '\n'.join(lines[node.lineno - 1:node.end_lineno])
            if 'by_type' in body and not any(t in body for t in AWARE_TOKENS):
                qual = '.'.join(self.stack + [node.name])
                hits.append((node.lineno, qual, body.count('by_type')))
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = _fn
        visit_AsyncFunctionDef = _fn

    V().visit(tree)
    return hits


def scan(roots):
    rows = []
    for root in roots:
        if os.path.isfile(root):
            files = [root]
        else:
            files = [os.path.join(dp, fn)
                     for dp, _, fns in os.walk(root)
                     if '__pycache__' not in dp
                     for fn in fns if fn.endswith('.py')]
        for p in sorted(files):
            for lineno, qual, n in scan_file(p):
                rows.append((n, p, lineno, qual))
    rows.sort(key=lambda r: (-r[0], r[1], r[2]))
    return rows


def measure(plugin):
    """Count this plugin's REFRs whose BASE record lives only in a master.

    Covers the base-record family of the defect (navmesh carving, door
    handling, package targets). Other families (SCPT, VTYP, PACK) need their
    own probes -- see docs/override_conversion.md.
    """
    sys.path.insert(0, os.getcwd())
    from tes5_import.text_reader import (parse_export_directory,
                                         group_records_by_type)
    from tes5_import.overrides import load_master_export

    export_dir = os.path.join('export', plugin)
    if not os.path.isdir(export_dir):
        print(f'no such export dir: {export_dir}')
        return 1
    by_type = group_records_by_type(parse_export_directory(export_dir))
    master = load_master_export(export_dir)
    if not master:
        print(f'{plugin}: no masters -- master-blindness cannot apply')
        return 0

    families = {
        'blocking base (navmesh carve)':
            ('STAT', 'ACTI', 'CONT', 'FURN', 'DOOR', 'MISC', 'TREE', 'FLOR'),
        'DOOR base': ('DOOR',),
        'actor base': ('NPC_', 'CREA'),
    }
    print(f'{plugin}: master_export={len(master)} records')
    for label, sigs in families.items():
        own = {r.get('FormID', '').upper()
               for s in sigs for r in by_type.get(s, [])}
        mst = {k.upper() for k, r in master.items()
               if r.get('Signature') in sigs}
        tot = miss = 0
        for sig in ('REFR', 'ACHR', 'ACRE'):
            for r in by_type.get(sig, []):
                b = (r.get('NAME') or '').upper()
                if not b:
                    continue
                tot += 1
                if b not in own and b in mst:
                    miss += 1
        pct = (100.0 * miss / tot) if tot else 0.0
        print(f'  {label:32} {miss:>7} / {tot:<7} placements ({pct:.1f}%) '
              f'resolve ONLY in a master')
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--path', action='append', dest='paths',
                    help='file or directory to scan (repeatable; '
                         'default: tes5_import)')
    ap.add_argument('--measure', metavar='PLUGIN',
                    help='measure base-record exposure for an exported plugin')
    args = ap.parse_args()

    if args.measure:
        return measure(args.measure)

    rows = scan(args.paths or DEFAULT_ROOTS)
    if not rows:
        print('no master-blind functions found')
        return 0
    print(f'{"by_type":>7}  location')
    for n, path, lineno, qual in rows:
        print(f'{n:>7}  {path}:{lineno} {qual}()')
    print(f'\n{len(rows)} candidate(s). A hit is NOT a defect -- many functions '
          f'are legitimately\nplugin-scoped. Measure before fixing '
          f'(--measure <plugin>).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
