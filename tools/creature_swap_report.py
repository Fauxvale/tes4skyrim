"""Report which CREA records the --vanilla-creatures option would swap.

Shows, per plugin, how many creatures would use a vanilla Skyrim race instead
of a generated one, at both match qualities, and which folders are left
generated because Skyrim has no equivalent.

Usage:
    python tools/creature_swap_report.py                    # all exports
    python tools/creature_swap_report.py -f Oblivion.esm
    python tools/creature_swap_report.py -f Nehrim.esm --near
    python tools/creature_swap_report.py -f Oblivion.esm --list
"""
import argparse
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tes5_import import vanilla_creature_swap as V

BS = chr(92)


def read_crea(path):
    """Yield (editorid, folder) for every CREA in an export CREA.txt."""
    edid = ''
    for line in open(path, encoding='utf-8', errors='replace'):
        line = line.rstrip('\n')
        if line.startswith('EditorID='):
            edid = line.split('=', 1)[1].strip()
        elif line.startswith('Model.MODL='):
            v = line.split('=', 1)[1].strip().replace('/', BS).lower()
            parts = [p for p in v.split(BS) if p]
            yield edid, (parts[-2] if len(parts) >= 2 else '')


def report(plugin, export_root, allow_near, show_list):
    path = os.path.join(export_root, plugin, 'CREA.txt')
    if not os.path.exists(path):
        return False
    swapped = collections.Counter()
    generated = collections.Counter()
    rows = []
    total = 0
    for edid, folder in read_crea(path):
        total += 1
        swap = V.resolve(edid, folder, allow_near=allow_near)
        if swap:
            swapped[(folder, swap.skyrim_name, swap.quality)] += 1
            rows.append((edid, folder, swap.skyrim_name, swap.quality))
        else:
            generated[folder] += 1
            rows.append((edid, folder, '', ''))

    n_swap = sum(swapped.values())
    print('=== %s ===' % plugin)
    print('  %d CREA records: %d swapped to vanilla (%.0f%%), %d generated'
          % (total, n_swap, 100.0 * n_swap / total if total else 0,
             sum(generated.values())))
    if swapped:
        print('  -- swapped --')
        for (folder, name, q), n in sorted(swapped.items(),
                                           key=lambda kv: -kv[1]):
            print('     %-18s -> %-22s %-5s %4d' % (folder, name, q, n))
    if generated:
        print('  -- generated (no vanilla equivalent) --')
        for folder, n in sorted(generated.items(), key=lambda kv: -kv[1]):
            print('     %-18s %4d' % (folder or '(no folder)', n))
    if show_list:
        print('  -- per record --')
        for edid, folder, name, q in sorted(rows):
            print('     %-40s %-16s %s' % (edid, folder,
                                           ('-> %s (%s)' % (name, q))
                                           if name else '(generated)'))
    print()
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('-f', '--file', help='plugin name, e.g. Oblivion.esm')
    ap.add_argument('--near', action='store_true',
                    help='also swap "near" matches (different species)')
    ap.add_argument('--list', action='store_true',
                    help='list every CREA record and its decision')
    ap.add_argument('--export-root', default='export')
    args = ap.parse_args()

    print('Swap table: %s\n' % V.stats(args.near))
    if args.file:
        if not report(args.file, args.export_root, args.near, args.list):
            print('No CREA.txt for %s under %s' % (args.file,
                                                   args.export_root))
            return 1
        return 0
    found = False
    for plugin in sorted(os.listdir(args.export_root)):
        if report(plugin, args.export_root, args.near, args.list):
            found = True
    if not found:
        print('No exports with CREA.txt found under %s' % args.export_root)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
