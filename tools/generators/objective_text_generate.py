#!/usr/bin/env python
"""Derive short objective lines from TES4 journal text.

Skyrim renders two quest strings: the long retrospective log entry (CNAM) in
the journal, and a short imperative line (NNAM) on the objective HUD.  Oblivion
authored only one string per stage, so NNAM currently shows the full paragraph
-- vanilla NNAM averages 31 chars, our converted objectives average 158.

TES4 carries no second string, so the short form is supplied by a curated table
keyed on the SOURCE TEXT itself.  Keying on the string (rather than
plugin+EditorID+stage) means identical journal text anywhere resolves to one
entry, and the table needs no per-plugin bookkeeping.

This tool proposes the short line for each distinct source string.  Two tiers:

  cue    -- the source contains an explicit directive clause ("I should speak
            to X", "Ich sollte X aufsuchen").  The clause is lifted and
            rewritten to a second-person imperative.  ~26% of strings.
  trim   -- no directive; fall back to the leading clause, condensed.

Both tiers are PROPOSALS.  Rewriting first-person retrospective prose into a
second-person imperative is not a mechanical transform, so output is meant to
be reviewed and hand-corrected, then frozen into the shipped table.

Usage:
    python tools/generators/objective_text_generate.py --slots temp/objective_slots.json \
        --out temp/objective_short.json
    python tools/generators/objective_text_generate.py --slots ... --stats
    python tools/generators/objective_text_generate.py --slots ... --review 40
"""
import argparse
import json
import os
import re
import statistics
import sys

# Vanilla Skyrim NNAM: mean 30.6, median 27, p90 48, max 115 (measured over
# 1441 objectives in Skyrim.esm with strings resolved from the Interface BSA).
TARGET_MAX = 48
HARD_MAX = 115

# --- English directive clauses -------------------------------------------
# "I should speak to Ongar" / "I need to find the amulet" / "I must return".
_EN_CUE = re.compile(
    r"\bI\s+(?:should|must|need\s+to|have\s+to|ought\s+to|will\s+have\s+to|"
    r"am\s+to|can\s+now|may\s+now|'ll\s+need\s+to|will\s+need\s+to)\s+"
    r"(?P<verb>.+?)(?=[.!?;]|$)", re.IGNORECASE | re.DOTALL)

# "X has asked me to do Y" / "X wants me to do Y" / "X told me to do Y".
_EN_ASKED = re.compile(
    r"\b(?:asked|wants|told|instructed|requested)\s+me\s+to\s+"
    r"(?P<verb>.+?)(?=[.!?;]|$)", re.IGNORECASE | re.DOTALL)

# --- German directive clauses (Nehrim ships German; Translation.esp is EN) --
_DE_CUE = re.compile(
    r"\bIch\s+(?:sollte|muss|müsste|sollte\s+nun|kann\s+nun|werde)\s+"
    r"(?P<verb>.+?)(?=[.!?;]|$)", re.IGNORECASE | re.DOTALL)

# Completion markers: the stage records something already done, so there is no
# outstanding action.  Vanilla still shows an objective for these (it is the
# line that gets struck through), so they become a terse past statement.
_EN_DONE = re.compile(
    r"^\s*I(?:'ve|\s+have)\s+(?P<verb>.+?)(?=[.!?]|$)", re.IGNORECASE)

# Leading filler that adds nothing to a short line.
_LEAD_FILLER = re.compile(
    r"^(?:now|then|next|afterwards?|finally|first|also|however|but|and|so|"
    r"unfortunately|luckily|thankfully|apparently|it\s+seems\s+that|"
    r"it\s+appears\s+that|in\s+order\s+to)[,\s]+", re.IGNORECASE)

# Trailing rationale that a short line drops ("... so that I can ...").
_TAIL_FILLER = re.compile(
    r"\s*(?:,?\s*(?:so\s+that|so\s+I|because|since|although|though|while|"
    r"if\s+I|when\s+I|before\s+I|after\s+I|in\s+order\s+to|which|that\s+way)"
    r"\b.*)$", re.IGNORECASE | re.DOTALL)

# First person -> second person, applied to a lifted directive clause.
_PRONOUN = [
    (re.compile(r"\bmyself\b", re.I), "yourself"),
    (re.compile(r"\bmy\b", re.I), "your"),
    (re.compile(r"\bmine\b", re.I), "yours"),
    (re.compile(r"\bme\b", re.I), "you"),
    (re.compile(r"\bI\b"), "you"),
]


def _collapse(text):
    """Normalise whitespace and strip wrapper quotes."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    return text.strip('"“”')


def _to_imperative(clause):
    """Turn a lifted first-person clause into a second-person imperative."""
    clause = _collapse(clause)
    clause = _LEAD_FILLER.sub("", clause)
    clause = _TAIL_FILLER.sub("", clause)
    for pat, repl in _PRONOUN:
        clause = pat.sub(repl, clause)
    clause = _collapse(clause)
    if clause:
        clause = clause[0].upper() + clause[1:]
    return clause.rstrip(" .,;:")


def _first_clause(text):
    """The leading sentence, trimmed of filler -- the trim-tier fallback."""
    sentences = re.split(r"(?<=[.!?])\s+", _collapse(text))
    lead = sentences[0] if sentences else ""
    lead = _LEAD_FILLER.sub("", lead)
    lead = _TAIL_FILLER.sub("", lead)
    return _collapse(lead).rstrip(" .,;:")


def propose(text):
    """(short_line, tier) for one source string."""
    src = _collapse(text)
    if not src:
        return "", "empty"

    # Already short enough to serve as its own objective.
    if len(src) <= TARGET_MAX and src.count(".") <= 1:
        return src.rstrip(" ."), "asis"

    sentences = re.split(r"(?<=[.!?])\s+", src)

    # A directive clause anywhere wins; the LAST one is the live instruction
    # (measured: when a cue exists it is in the final sentence 86% of the time).
    for pat in (_EN_CUE, _DE_CUE, _EN_ASKED):
        hits = list(pat.finditer(src))
        if hits:
            out = _to_imperative(hits[-1].group("verb"))
            if 0 < len(out) <= HARD_MAX:
                return out, "cue"

    # No outstanding action -- the stage reports something completed.
    done = _EN_DONE.match(sentences[0])
    if done and len(sentences) == 1:
        out = _to_imperative(done.group("verb"))
        if 0 < len(out) <= HARD_MAX:
            return out, "done"

    out = _first_clause(src)
    if len(out) > HARD_MAX:
        out = out[:HARD_MAX].rsplit(" ", 1)[0].rstrip(" .,;:")
    return out, "trim"


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slots", default="temp/objective_slots.json",
                    help="output of tools/generators/objective_text_extract.py")
    ap.add_argument("--out", help="write the proposal table here (JSON)")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--review", type=int, metavar="N",
                    help="print N proposals side by side for eyeballing")
    ap.add_argument("--tier", help="restrict --review to one tier")
    args = ap.parse_args()

    with open(args.slots, encoding="utf-8") as fh:
        rows = json.load(fh)

    table = {}
    tiers = {}
    for r in rows:
        src = _collapse(r["long"])
        if not src or src in table:
            continue
        short, tier = propose(src)
        table[src] = short
        tiers[src] = tier

    if args.stats:
        counts = {}
        for t in tiers.values():
            counts[t] = counts.get(t, 0) + 1
        lens = [len(v) for v in table.values() if v]
        print(f"distinct source strings : {len(table)}")
        for t, c in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {t:6s} {c:5d}  ({100 * c / len(table):.0f}%)")
        if lens:
            print(f"proposed length: mean={statistics.mean(lens):.1f} "
                  f"median={statistics.median(lens)} max={max(lens)}")
            over = sum(1 for x in lens if x > TARGET_MAX)
            print(f"  over {TARGET_MAX} chars: {over} "
                  f"({100 * over / len(lens):.0f}%)   [vanilla p90 = {TARGET_MAX}]")
        return

    if args.review:
        shown = 0
        for src, short in table.items():
            if args.tier and tiers[src] != args.tier:
                continue
            print(f"[{tiers[src]}] SRC: {src[:150]}")
            print(f"        -> {short}")
            print()
            shown += 1
            if shown >= args.review:
                break
        return

    payload = {"__note__": "source journal text -> short objective line",
               "entries": table}
    text = json.dumps(payload, indent=1, ensure_ascii=False)
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {args.out} ({len(table)} entries)", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
