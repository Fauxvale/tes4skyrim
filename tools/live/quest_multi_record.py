#!/usr/bin/env python3
"""Record several quests' Papyrus state from ONE bridge connection.

The game bridge pipe accepts a single client at a time, so running
`quest_debug.py record` once per quest fails with E_NO_PIPE for every
recorder after the first.  This polls all the named quests over one
connection and writes a single merged timeline, which is also what you want
when the bug spans several quests (the Arena announcer chain is Arena +
ArenaAnnouncer + ArenaDialogue).

    python tools/live/quest_multi_record.py --quests Arena ArenaAnnouncer \
        --seconds 1800 --out temp/run.txt

Only CHANGES are written, so a long idle stretch costs a line, not a file.
Prints as it goes so a truncated run is still readable.
"""
import argparse
import re
import sys
import time

sys.path.insert(0, __file__.rsplit('tools', 1)[0])
from tools.live.game_bridge import Bridge


_VAR_RE = re.compile(r'^\s*(::\w+|TES4_\w+)\s*=\s*(.+?)\s*$')


def snapshot(bridge, quest: str) -> dict:
    """Papyrus variables + engine state for one quest, as a flat dict."""
    try:
        r = bridge.console(f'sqv {quest}')
    except Exception as exc:                      # bridge hiccup, keep going
        return {'_error': str(exc)}
    text = r if isinstance(r, str) else (r.get('output') or r.get('text') or '')
    out = {}
    for line in text.splitlines():
        m = _VAR_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
        elif 'Current stage:' in line:
            out['_stage'] = line.split(':', 1)[1].strip()
        elif line.strip().startswith('State:'):
            out['_state'] = line.split(':', 1)[1].strip()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description='Record several quests from one bridge connection.')
    ap.add_argument('--quests', nargs='+', required=True)
    ap.add_argument('--seconds', type=float, default=1800)
    ap.add_argument('--interval', type=float, default=1.0)
    ap.add_argument('--out', required=True)
    args = ap.parse_args()

    bridge = Bridge()
    prev = {q: {} for q in args.quests}
    start = time.time()
    fh = open(args.out, 'w', encoding='utf-8')

    def emit(msg: str) -> None:
        stamp = f'[{time.time() - start:7.1f}s] {msg}'
        print(stamp, flush=True)
        fh.write(stamp + '\n')
        fh.flush()

    emit(f'recording {", ".join(args.quests)} '
         f'for {args.seconds:.0f}s @ {args.interval}s')
    for q in args.quests:
        cur = snapshot(bridge, q)
        prev[q] = cur
        emit(f'{q} BASELINE stage={cur.get("_stage")} '
             f'state={cur.get("_state")} vars={len(cur)}')

    while time.time() - start < args.seconds:
        time.sleep(args.interval)
        for q in args.quests:
            cur = snapshot(bridge, q)
            if not cur:
                continue
            old = prev[q]
            for k in sorted(set(cur) | set(old)):
                a, b = old.get(k), cur.get(k)
                if a != b:
                    emit(f'{q}.{k}: {a} -> {b}')
            prev[q] = cur
    emit('done')
    fh.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
