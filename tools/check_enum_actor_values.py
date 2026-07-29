"""Verify no SetActorValue writes an out-of-range value to an enum actor value.

Usage: python temp/check_enum_av.py <scripts/source dir> [...]
"""
import glob
import os
import re
import sys

# Inclusive maxima from xEdit wbDefinitionsCommon.pas.
MAX = {'aggression': 3, 'confidence': 4, 'assistance': 2,
       'mood': 8, 'morality': 3}
CALL = re.compile(r'(Set|Force)ActorValue\("(\w+)",\s*(-?[\d.]+)\)')


def main() -> int:
    dirs = sys.argv[1:] or ['output/Nehrim.esm/scripts/source']
    bad = 0
    total = 0
    for d in dirs:
        for path in glob.glob(os.path.join(d, '*.psc')):
            with open(path, encoding='utf-8', errors='replace') as fh:
                text = fh.read()
            for m in CALL.finditer(text):
                av = m.group(2).lower()
                if av not in MAX:
                    continue
                total += 1
                if not 0 <= float(m.group(3)) <= MAX[av]:
                    print(f'  ILLEGAL {os.path.basename(path)}: {m.group(0)}')
                    bad += 1
    print(f'enum-AV writes checked: {total}, illegal: {bad}')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
