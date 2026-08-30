"""Structural view of emitted Papyrus lines.

Five passes each re-derived block structure from text with their own keyword
spellings, and disagreed: one matched `if(` and one did not, one knew a typed
`Int Function` header and one did not, one flattened embedded newlines and one
did not.  Each disagreement was a latent bug (docs/commentary/script_convert.md
§5), so the classification lives here ONCE and the passes above it express
intent -- balance, delete, reorder -- against `Line` records instead.

Not a Papyrus parser: the emitter controls the shapes, so `scan` need only
recognise them.  A real TES5 AST belongs here when the emitter is switched
over to build one (stage 7).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Kind(Enum):
    """What an emitted Papyrus line is, structurally."""

    OTHER = 'other'          # a plain statement, a blank, or a comment
    HEADER = 'header'        # Event / Function / typed Function
    END_HEADER = 'endheader'  # EndEvent / EndFunction
    IF = 'if'
    ELSEIF = 'elseif'
    ELSE = 'else'
    ENDIF = 'endif'
    WHILE = 'while'
    ENDWHILE = 'endwhile'
    RETURN = 'return'

    @property
    def opens(self) -> bool:
        return self in (Kind.IF, Kind.WHILE)

    @property
    def closes(self) -> bool:
        return self in (Kind.ENDIF, Kind.ENDWHILE)


OPENER_OF = {Kind.ENDIF: Kind.IF, Kind.ENDWHILE: Kind.WHILE}

# A TYPED function header reads `Int Function TES4Call(...)`, so matching only
# a leading `function ` missed every OBSE user function that returns a value --
# nothing inside them was balanced and an unclosed block ran on into
# EndFunction.  `State` blocks are headers too: they cannot nest inside an
# event, so treating one as a header resets the stack exactly as intended.
_HEADER_RE = re.compile(r'^(?:\w+\s+)?(?:function|event)\s', re.IGNORECASE)
_STATE_RE = re.compile(r'^(?:auto\s+)?state\s', re.IGNORECASE)

_SIMPLE = {
    'endevent': Kind.END_HEADER,
    'endfunction': Kind.END_HEADER,
    'endstate': Kind.END_HEADER,
    'else': Kind.ELSE,
    'endif': Kind.ENDIF,
    'endwhile': Kind.ENDWHILE,
    'return': Kind.RETURN,
    'if': Kind.IF,
}

# `If(x)` is as legal as `If x`, and the old balancer matched both while the
# dead-code pass matched only `if `, so an `If(` opener was invisible to it and
# a `Return` inside that block read as top-level.
_PREFIX = (
    ('if ', Kind.IF), ('if(', Kind.IF),
    ('elseif ', Kind.ELSEIF), ('elseif(', Kind.ELSEIF),
    ('while ', Kind.WHILE), ('while(', Kind.WHILE),
    ('return ', Kind.RETURN),
)


def classify(text: str) -> Kind:
    """Structural kind of one physical line of emitted Papyrus."""
    stripped = text.strip()
    # An inline comment cannot change what the statement IS, but a line that is
    # ONLY a comment must never read as one -- `; EndIf` is prose.
    if stripped.startswith(';'):
        return Kind.OTHER
    code = stripped.split(';')[0].strip()
    low = code.lower()
    if not low:
        return Kind.OTHER
    if low in _SIMPLE:
        return _SIMPLE[low]
    for prefix, kind in _PREFIX:
        if low.startswith(prefix):
            return kind
    if _HEADER_RE.match(low) or _STATE_RE.match(low):
        return Kind.HEADER
    return Kind.OTHER


@dataclass(frozen=True)
class Line:
    """An emitted line with the structure around it resolved.

    `stack` is the open block kinds innermost-last, counted BEFORE this line
    is applied, so a closer sees the body it closes and `not stack` means
    "top level of its header" -- the only depth question any caller asks.
    """

    text: str
    kind: Kind
    in_header: bool
    stack: tuple


def physical(lines) -> list:
    """Flatten emitted entries to physical lines.

    A converted statement can be a MULTI-LINE string (the chargen menu unit,
    the runtime setfactionreaction branch).  Read as one line it shows only its
    FIRST token, so a blob starting with `If` counted a phantom open block and
    the balancer appended a stray EndIf before EndEvent -- a 15,961-script
    compile failure, 2026-08-14.
    """
    out = []
    for entry in lines:
        out.extend(entry.split('\n'))
    return out


def scan(lines):
    """Yield a `Line` per physical line, with the block stack resolved.

    TOLERANT by design, because it runs on output that may not yet be
    balanced -- that is precisely what its callers are there to fix.  A closer
    with no matching opener pops nothing; a header resets the stack, since
    Papyrus forbids a block spanning one.
    """
    stack: list = []
    in_header = False
    for text in physical(lines):
        kind = classify(text)
        # Reported BEFORE the line is applied, so a closer sees the body it
        # closes and an opener sees the depth it sits at.
        yield Line(text, kind, in_header, tuple(stack))
        if kind is Kind.HEADER:
            in_header, stack = True, []
        elif kind is Kind.END_HEADER:
            in_header, stack = False, []
        elif not in_header:
            continue
        elif kind.opens:
            stack.append(kind)
        elif kind.closes and OPENER_OF[kind] in stack:
            while stack.pop() is not OPENER_OF[kind]:
                pass


# See: docs/commentary/script_convert.md#startquest-postpass-fails-open
_QUEST_START_RE = re.compile(r'^(\s*)(\w+)\.Start\(\)\s*(;.*)?$')
_QUEST_PROP_WRITE_RE = re.compile(r'^(\s*)(\w+)\.(\w+)\s*=(?!=)')


def hoist_quest_start_above_writes(lines: list) -> list:
    """Move `Q.Start()` ABOVE property writes to Q that precede it.

    **A TES4->TES5 semantic difference, not a style fix.**  A TES4 quest
    variable persists whether or not the quest runs, so "seed the variables,
    then start the quest" is a safe and common authored idiom.  Skyrim's
    `Quest.Start()` on a STOPPED quest re-initialises its scripts, resetting
    every `Auto` property -- so converted literally, the `Start()` wipes every
    value seeded above it.

    That softlocked the Imperial City Arena: the match INFO sets
    `Arena.ReadyMatch = 1` then calls `Arena.Start()` four lines later, so the
    announcer's `ReadyMatch == 1` gate never opened and neither did the gates.
    Measured 2026-08-18: 131 clobbered writes across 65 scripts.
    """
    out = list(lines)
    for idx, line in enumerate(out):
        m = _QUEST_START_RE.match(line)
        if not m:
            continue
        first = _first_clobbered_write(out, idx, m.group(2))
        if first is not None:
            # Hoist the Start() to just above the earliest clobbered write, so
            # the seeded values are written into the freshly started quest.
            out.insert(first, out.pop(idx))
    return out


def _first_clobbered_write(lines: list, start: int, quest: str):
    """Index of the earliest write to `quest` in the straight-line run above.

    `None` if there is none, or if a branch, loop or return separates them --
    the two may then not both execute, so the order is left as authored.  The
    barrier is `Kind`, not a private keyword list: the list this replaced knew
    neither `If(` nor a typed `Int Function` header.
    """
    first = None
    for j in range(start - 1, -1, -1):
        if classify(lines[j]) is not Kind.OTHER:
            break
        w = _QUEST_PROP_WRITE_RE.match(lines[j])
        if w and w.group(2) == quest:
            first = j     # a write to a DIFFERENT quest is no barrier
    return first
