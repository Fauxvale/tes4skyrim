#!/usr/bin/env python3
"""Audit the ambient (unprompted) dialogue channel in a converted TES5 file.

The question this answers: how much dialogue can an NPC speak at the player
*without being spoken to*, and how does that compare with vanilla Skyrim?

Skyrim fires exactly two channels unprompted:

  * HELO (Miscellaneous/Hello) -- the engine's ambient greeting. Any actor
    within fAIMinGreetingDistance whose HELO conditions pass says a line, then
    re-arms after fAIGreetingTimer (5.0s in Skyrim.esm). This is the channel
    the player hears as "NPCs quipping at me every few seconds".
  * IDLE (Miscellaneous/Idle) -- idle chatter, gated by
    fIdleChatterCommentTimer(Max) and the social GMSTs.

Oblivion instead splits the same content across TWO engine channels
(oblivion_engine_tables.json types 0 and 6):

  * GREETING (0) -- plays only when the player ACTIVATES the NPC and the
    dialogue menu opens. Never unprompted.
  * HELLO (6)    -- the ambient on-approach line.

So a converted GREETING INFO that lands on Skyrim's HELO subtype has been moved
from an on-activate channel to an ambient timer-driven one. This tool measures
that: it reports, per subtype, how many INFOs are reachable ambiently, and (with
--by-source) how many of them came from each Oblivion channel.

Usage:
    python tools/ambient_bark_audit.py output/Oblivion.esm/Oblivion.esm
    python tools/ambient_bark_audit.py output/Oblivion.esm/Oblivion.esm \
        --compare "C:/.../Skyrim.esm"
    python tools/ambient_bark_audit.py output/Oblivion.esm/Oblivion.esm \
        --by-source export/Oblivion.esm
    python tools/ambient_bark_audit.py output/Oblivion.esm/Oblivion.esm --topics HELO
"""
import argparse
import collections
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tools.tes5_esm_reader import read_tes5_file, _get

# Subtypes the engine can fire with no player action at all.
AMBIENT_SUBTYPES = ('HELO', 'IDLE')


def _zs(b):
    return b.split(b'\0')[0].decode('latin1', errors='replace')


def _edid(rec):
    e = _get(rec, 'EDID')
    return _zs(e.data) if e else ''


def _ncond(rec):
    return sum(1 for s in rec.subrecords if s.type == 'CTDA')


def load_dialogue(path):
    """-> (dials, infos, snam_by_dial, infos_by_dial)."""
    _hdr, recs, _loc = read_tes5_file(path, parse_types={'DIAL', 'INFO'})
    dials = [r for r in recs if r.type == 'DIAL']
    infos = [r for r in recs if r.type == 'INFO']
    snam = {}
    for d in dials:
        s = _get(d, 'SNAM')
        snam[d.form_id] = s.data[:4].decode('latin1') if s else '?'
    by_dial = collections.defaultdict(list)
    for i in infos:
        by_dial[i.parent_dial].append(i)
    return dials, infos, snam, by_dial


def summarize(path, label):
    dials, infos, snam, by_dial = load_dialogue(path)
    print(f"\n=== {label} ===")
    print(f"  DIAL={len(dials)}  INFO={len(infos)}")
    for sub in AMBIENT_SUBTYPES:
        topics = [d for d in dials if snam.get(d.form_id) == sub]
        lines = [i for d in topics for i in by_dial[d.form_id]]
        nocond = sum(1 for i in lines if _ncond(i) == 0)
        print(f"  {sub}: topics={len(topics):4d}  INFOs={len(lines):5d}  "
              f"conditionless={nocond}")
    return dials, infos, snam, by_dial


def show_topics(path, sub, limit):
    dials, _infos, snam, by_dial = load_dialogue(path)
    topics = [d for d in dials if snam.get(d.form_id) == sub]
    topics.sort(key=lambda d: -len(by_dial[d.form_id]))
    print(f"\n=== {sub} topics (largest {limit}) ===")
    for d in topics[:limit]:
        lines = by_dial[d.form_id]
        nc = sum(1 for i in lines if _ncond(i) == 0)
        print(f"  {_edid(d) or hex(d.form_id):50s} INFOs={len(lines):5d} "
              f"conditionless={nc}")


def _load_export(path):
    recs = []
    cur = {}
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip('\n')
            if line == '---RECORD_BEGIN---':
                cur = {}
            elif line == '---RECORD_END---':
                recs.append(cur)
            elif '=' in line:
                k, v = line.split('=', 1)
                cur[k] = v
    return recs


def by_source(esm_path, export_dir):
    """Attribute each ambient INFO back to the Oblivion channel it came from.

    Matching is by INFO FormID low-24 bits, which the converter preserves for
    records it does not clone.
    """
    src_dials = _load_export(os.path.join(export_dir, 'DIAL.txt'))
    src_infos = _load_export(os.path.join(export_dir, 'INFO.txt'))
    dial_edid = {d.get('FormID'): d.get('EditorID', '') for d in src_dials}
    channel_of_info = {}
    for i in src_infos:
        fid = i.get('FormID')
        if fid:
            channel_of_info[int(fid, 16) & 0xFFFFFF] = \
                dial_edid.get(i.get('ParentDIAL'), '?')

    dials, _infos, snam, by_dial = load_dialogue(esm_path)
    for sub in AMBIENT_SUBTYPES:
        topics = [d for d in dials if snam.get(d.form_id) == sub]
        counts = collections.Counter()
        for d in topics:
            for i in by_dial[d.form_id]:
                counts[channel_of_info.get(i.form_id & 0xFFFFFF,
                                           '(new/unmatched)')] += 1
        total = sum(counts.values())
        print(f"\n=== {sub}: {total} INFOs by ORIGINATING Oblivion topic ===")
        for name, n in counts.most_common(15):
            print(f"  {name:40s} {n:6d}   {100.0 * n / max(total, 1):5.1f}%")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('esm')
    ap.add_argument('--compare', help='second ESM (e.g. vanilla Skyrim.esm)')
    ap.add_argument('--by-source', metavar='EXPORT_DIR',
                    help='attribute ambient INFOs to their Oblivion channel')
    ap.add_argument('--topics', metavar='SUBTYPE',
                    help='list the largest topics of this subtype')
    ap.add_argument('--limit', type=int, default=15)
    args = ap.parse_args()

    summarize(args.esm, os.path.basename(args.esm))
    if args.compare:
        summarize(args.compare, os.path.basename(args.compare))
    if args.topics:
        show_topics(args.esm, args.topics.upper(), args.limit)
    if args.by_source:
        by_source(args.esm, args.by_source)


if __name__ == '__main__':
    main()
