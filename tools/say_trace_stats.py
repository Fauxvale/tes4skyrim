#!/usr/bin/env python3
"""Summarise the `TES4Say ...` traces TES4Polyfill writes for every spoken line.

TES4Polyfill.SayLine / LineBegan / LineEnded each Debug.Trace a stamped line:

    TES4Say request   actor 168456752 topic [Topic <...>] t=<real>
    TES4Say began     actor ... len=<measured> startLatency=<Say->Begin> waited=<pre-wait> t=<real>
    TES4Say dropped   actor ... waited=<pre-wait> t=<real>
    TES4Say LineBegan actor ... len=<measured> inDialogue=<bool> t=<real>
    TES4Say LineEnded actor ... measured=<len> actual=<Begin->End real seconds> t=<real>

Those are the numbers the Say-timer contract depends on and that no static
analysis can supply: how long after Say() the engine BEGINS a line, and how far
past the measured audio length the End fragment lands (fragment dispatch +
the engine's trailing hold + inter-response gaps).  SAY_TAIL in
TES4Polyfill.psc must cover the End overhead; this prints its distribution so
it can be set from measurement instead of guessed.

    python tools/say_trace_stats.py                      # Papyrus.0.log
    python tools/say_trace_stats.py --log path/to/log    # any file with the traces
    python tools/say_trace_stats.py --since 12345        # byte offset (papyrus_tail mark)
    python tools/say_trace_stats.py --actor 168456752    # one speaker
    python tools/say_trace_stats.py --lines              # every line, chronologically

Also prints the GAP between one line's End and the next SayLine's Begin on any
speaker (the dead air the player hears) and any End overhead that exceeds
SAY_TAIL, i.e. the cases where a poll's `T <= 0` guard reopened before the End
result ran.
"""
import argparse
import os
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from papyrus_tail import log_dir, DEFAULT_LOG  # noqa: E402

_LINE = re.compile(
    r'TES4Say (?P<kind>request|began|dropped|LineBegan|LineEnded) actor '
    r'(?P<actor>-?\d+)(?P<rest>.*?)t=(?P<t>-?[\d.]+)')
_KV = re.compile(r'(\w+)=(-?[\d.]+|True|False)')


def parse(text: str):
    out = []
    for m in _LINE.finditer(text):
        kv = {k: v for k, v in _KV.findall(m.group('rest'))}
        tm = re.search(r'topic \[Topic <(?P<topic>[^>]*)>', m.group('rest'))
        out.append({'kind': m.group('kind'), 'actor': m.group('actor'),
                    't': float(m.group('t')), 'topic': tm.group('topic') if tm else '',
                    **{k: (float(v) if v not in ('True', 'False') else v == 'True')
                       for k, v in kv.items()}})
    out.sort(key=lambda e: e['t'])
    return out


def _stats(vals):
    if not vals:
        return 'n=0'
    vals = sorted(vals)
    q = lambda p: vals[min(len(vals) - 1, int(p * len(vals)))]
    return (f'n={len(vals)} min={vals[0]:.2f} med={statistics.median(vals):.2f} '
            f'p90={q(0.9):.2f} max={vals[-1]:.2f}')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--log', default=None, help='log file (default Papyrus.0.log)')
    ap.add_argument('--since', type=int, default=0, help='byte offset to start from')
    ap.add_argument('--actor', default='', help='only this speaker id')
    ap.add_argument('--tail', type=float, default=1.0,
                    help='SAY_TAIL to judge End overhead against (default 1.0)')
    ap.add_argument('--lines', action='store_true', help='print every event')
    args = ap.parse_args(argv)

    path = Path(args.log) if args.log else log_dir() / DEFAULT_LOG
    if not path.exists():
        print(f'no such log: {path}')
        return 1
    with open(path, 'rb') as f:
        f.seek(args.since)
        text = f.read().decode('utf-8', errors='replace')
    ev = parse(text)
    if args.actor:
        ev = [e for e in ev if e['actor'] == args.actor]
    if not ev:
        print('no TES4Say traces found (is Papyrus logging on, and is this build deployed?)')
        return 1

    if args.lines:
        t0 = ev[0]['t']
        for e in ev:
            extra = ' '.join(f'{k}={v}' for k, v in e.items()
                             if k not in ('kind', 'actor', 't', 'topic'))
            print(f"{e['t'] - t0:9.2f}  {e['kind']:9s} {e['actor']:>11s} {e['topic'][:28]:28s} {extra}")
        print()

    start = [e['startLatency'] for e in ev if e['kind'] == 'began' and 'startLatency' in e]
    over = [e['actual'] - e['measured'] for e in ev
            if e['kind'] == 'LineEnded' and e.get('measured', 0) > 0.02 and 'actual' in e]
    waited = [e['waited'] for e in ev if e['kind'] == 'began' and e.get('waited', 0) > 0]
    dropped = [e for e in ev if e['kind'] == 'dropped']
    print(f'lines began       : {sum(1 for e in ev if e["kind"] == "began")}')
    print(f'lines dropped     : {len(dropped)}')
    print(f'Say -> Begin (s)  : {_stats(start)}')
    print(f'End overhead (s)  : {_stats(over)}   (actual Begin->End minus measured length)')
    print(f'pre-waits (s)     : {_stats(waited)}   (busy speaker / player in dialogue)')
    late = [o for o in over if o > args.tail]
    print(f'End overhead > SAY_TAIL({args.tail}): {len(late)} of {len(over)}')

    # dead air: End of a line -> Begin of the next SayLine-driven line
    gaps = []
    last_end = None
    for e in ev:
        if e['kind'] == 'LineEnded':
            last_end = e['t']
        elif e['kind'] == 'began' and last_end is not None:
            gap = e['t'] - e.get('startLatency', 0.0) - last_end
            if 0 <= gap < 30:
                gaps.append(gap)
            last_end = None
    print(f'gap End -> next Say issued (s): {_stats(gaps)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
