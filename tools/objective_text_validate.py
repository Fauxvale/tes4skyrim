#!/usr/bin/env python
"""Validate a generated objective-line table against the engine's constraints.

The Skyrim objective HUD does NOT wrap: characters past the field's limit are
simply not rendered, in every language. The hard cap is 71 characters -- the
longest token-free objective vanilla Skyrim ships (DA03, "Give the Rueful Axe
to Clavicus Vile OR kill Barbas with the Rueful Axe"). Vanilla strings longer
than that are only long because they carry unexpanded <Alias=>/<Global=>
tokens the engine substitutes at runtime; our converted text has no tokens, so
stored length IS rendered length and 71 is a hard ceiling, not a style target.

Checks, in severity order:
  toolong    -- over HARD_MAX; the tail would be invisible in game   (FATAL)
  empty      -- no objective text at all                             (FATAL)
  missing    -- a source string with no entry                        (FATAL)
  unknown    -- an entry whose key is not a real source string       (FATAL)
  firstperson-- "I"/"my"/"me" survived from the source prose         (warn)
  period     -- trailing '.', which vanilla objectives never carry   (warn)
  quoted     -- wrapped in stray quotes                              (warn)

Exit code is non-zero when any FATAL check fails, so this doubles as a
regression test over the shipped table.

Usage:
    python tools/objective_text_validate.py --table temp/objective_short.json
    python tools/objective_text_validate.py --table ... --slots temp/objective_slots.json
    python tools/objective_text_validate.py --table ... --list toolong
    python tools/objective_text_validate.py --table ... --ids-out temp/redo.json
"""
import argparse
import json
import os
import re
import statistics
import sys

# The objective field renders at most this many characters; the rest is
# dropped, not wrapped. Same in every language.
HARD_MAX = 71

# Vanilla style target (median 26-27 rendered) -- informational only.
STYLE_TARGET = 48

# "mine" is excluded deliberately: it is overwhelmingly the noun (a mine shaft)
# in this corpus, not the possessive, and matching it produced 21 false
# positives against correct lines like "Leave the mine". Quoted book titles can
# legitimately carry "my" ("Read \"For my Gods and Emperor\""), so a match
# inside quotes is not flagged either.
_FIRST_PERSON = re.compile(r"\b(?:I|I'(?:ve|ll|m|d)|my|me|myself)\b")
_QUOTED_SPAN = re.compile(r'"[^"]*"|“[^”]*”|\'[^\']*\'')


def _has_first_person(text):
    """True when first-person wording survived OUTSIDE any quoted title."""
    return bool(_FIRST_PERSON.search(_QUOTED_SPAN.sub("", text)))


def _needs_rewrite(src):
    """True when a source string would read badly as an objective on its own.

    Used only to decide whether a MISSING table entry is a defect. A source
    that is already a terse statement ("Aesliip is dead.") reads fine as the
    fallback; one that is long or written in the first person does not.
    """
    return len(src) > HARD_MAX or _has_first_person(src)


def load_table(path):
    """Accept either {'entries': {...}} or a bare {source: short} mapping."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and "entries" in data:
        data = data["entries"]
    if isinstance(data, list):
        data = {e["long"]: e["short"] for e in data}
    return data


def validate(table, sources=None, hard_max=HARD_MAX):
    """Return {check: [(key, short), ...]} for every failing entry."""
    bad = {k: [] for k in
           ("toolong", "empty", "missing", "unknown",
            "firstperson", "period", "quoted")}

    for src, short in table.items():
        s = (short or "").strip()
        if not s:
            bad["empty"].append((src, short))
            continue
        if len(s) > hard_max:
            bad["toolong"].append((src, s))
        if _has_first_person(s):
            bad["firstperson"].append((src, s))
        if s.endswith("."):
            bad["period"].append((src, s))
        # Only a line WHOLLY wrapped in quotes is a defect. An embedded quoted
        # title is correct and vanilla does it ("Read \"Modern Heretics\"").
        if len(s) > 1 and s[0] in '"“' and s[-1] in '"”' and \
                not _QUOTED_SPAN.sub("", s).strip():
            bad["quoted"].append((src, s))

    if sources is not None:
        for src in sources:
            if src not in table:
                # A source with no entry falls back to its own text, which is
                # correct whenever the source ALREADY reads as an objective
                # ("Aesliip is dead."). Those entries are deliberately omitted
                # -- an entry earns its place only by changing the line -- so
                # only a source that still needs rewriting counts as missing.
                if _needs_rewrite(src):
                    bad["missing"].append((src, None))
        for src in table:
            if src not in sources:
                bad["unknown"].append((src, table[src]))

    return bad


FATAL = ("toolong", "empty", "missing", "unknown")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--table", required=True,
                    help="objective table JSON (source text -> short line)")
    ap.add_argument("--slots",
                    help="tools/objective_text_extract.py output, to check "
                         "coverage (missing/unknown)")
    ap.add_argument("--list", metavar="CHECK",
                    help="print every failing entry for one check")
    ap.add_argument("--ids-out", metavar="PATH",
                    help="write the source strings that need re-authoring "
                         "(all FATAL checks except 'missing') as JSON")
    ap.add_argument("--max", type=int, default=HARD_MAX,
                    help=f"override the hard cap (default {HARD_MAX})")
    args = ap.parse_args()

    hard_max = args.max

    table = load_table(args.table)
    sources = None
    if args.slots:
        with open(args.slots, encoding="utf-8") as fh:
            sources = {" ".join(r["long"].split()) for r in json.load(fh)}

    bad = validate(table, sources, hard_max)

    lens = [len(v.strip()) for v in table.values() if v and v.strip()]
    print(f"entries: {len(table)}")
    if lens:
        print(f"length : mean={statistics.mean(lens):.1f} "
              f"median={statistics.median(lens)} max={max(lens)}  "
              f"(cap {hard_max})")
        over = sum(1 for x in lens if x > STYLE_TARGET)
        print(f"         over style target {STYLE_TARGET}: {over} "
              f"({100 * over / len(lens):.0f}%)")
    print()
    ok = True
    for check in ("toolong", "empty", "missing", "unknown",
                  "firstperson", "period", "quoted"):
        n = len(bad[check])
        tag = "FATAL" if check in FATAL else "warn "
        flag = "OK " if n == 0 else "FAIL" if check in FATAL else "note"
        print(f"  [{flag}] {tag} {check:12s} {n}")
        if n and check in FATAL:
            ok = False

    if args.list:
        print(f"\n--- {args.list} ---")
        for src, short in bad.get(args.list, []):
            print(f"{len(short or ''):3d}  {short}")
            print(f"     src: {src[:120]}")

    if args.ids_out:
        redo = []
        for check in ("toolong", "empty", "unknown"):
            for src, _ in bad[check]:
                redo.append(src)
        redo = sorted(set(redo))
        os.makedirs(os.path.dirname(args.ids_out) or ".", exist_ok=True)
        with open(args.ids_out, "w", encoding="utf-8") as fh:
            json.dump(redo, fh, ensure_ascii=False, indent=1)
        print(f"\nwrote {args.ids_out} ({len(redo)} strings need re-authoring)")

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
