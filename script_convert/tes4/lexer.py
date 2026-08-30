"""Tokenise Oblivion (TES4/OBSE) script source.

The converter has always worked on raw text with regexes, which means every
question that needs structure -- is this expression balanced, does this `if`
have its `endif`, is this identifier declared -- gets re-answered by
pattern-matching the OUTPUT.  That is where the ~1,000-line repair layer comes
from.  This is the first half of the fix: turn source text into tokens once,
so the parser can build a tree the rest of the pipeline queries instead of
re-deriving.

Scope comes from a census of the real corpus (Oblivion.esm 96,050 source
lines, Nehrim.esm 91,928):

  * NO line continuations anywhere -- a statement is exactly one line, which
    is why NEWLINE is a token rather than insignificant whitespace.
  * `;` starts a comment that runs to end of line (2,234 trailing comments in
    Oblivion alone), and it can appear mid-expression.
  * Strings are double-quoted only, and Oblivion has no escape syntax inside
    them -- a `"` always ends the literal.
  * OBSE adds `let`, `[` `]` indexing, `while`/`loop` and `setfunctionvalue`,
    all rare (156/38/2/6 lines in Nehrim) but real.
  * No hex literals, no `->`, no `$`-sigils in code (the 49/127 `$`/`%` hits
    are inside string literals).

Tokens carry their source line and column so the parser can attach a comment
to the statement it followed, and so an emitted node can be traced back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto


class T(Enum):
    IDENT = auto()
    NUMBER = auto()
    STRING = auto()
    OP = auto()
    COMMENT = auto()
    NEWLINE = auto()
    EOF = auto()


@dataclass(frozen=True)
class Token:
    kind: T
    text: str
    line: int
    col: int

    def is_op(self, *ops: str) -> bool:
        return self.kind is T.OP and self.text in ops

    def is_ident(self, *names: str) -> bool:
        return self.kind is T.IDENT and self.text.lower() in names

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f'{self.kind.name}({self.text!r})@{self.line}:{self.col}'


# Longest-first: `==` must beat `=`, `<=` must beat `<`.  `:=` is not TES4 but
# costs nothing to recognise and keeps a mis-typed script from lexing as two
# tokens that silently reparse.
_OPERATORS = (
    # `<>` is TES4's inequality; it MUST be matched before `<` or it lexes as
    # two comparisons and `x <> 5` comes out `x < >` -- not an expression.
    '==', '!=', '<>', '<=', '>=', '&&', '||', ':=',
    '+=', '-=', '*=', '/=',
    '<', '>', '=', '+', '-', '*', '/', '%', '$',
    '(', ')', '[', ']', ',', '.', ':',
)

#: Binary precedence, loosest tier first; parser binds and emitter parens by it.
PRECEDENCE = (
    ('||',),
    ('&&',),
    ('==', '!=', '<>'),
    ('<', '>', '<=', '>='),
    ('+', '-'),
    ('*', '/', '%'),
)

#: op -> tier index; absent ops (unary, calls, literals) bind tightest of all.
RANK = {op: i for i, tier in enumerate(PRECEDENCE) for op in tier}
RANK_ATOM = len(PRECEDENCE)

#: Operators yielding a Bool -- the logical and comparison tiers of PRECEDENCE.
BOOL_OPS = frozenset(op for tier in PRECEDENCE[:4] for op in tier)

#: Same, longest first, for a left-to-right scan over text.
BOOL_OPS_LONGEST = tuple(sorted(BOOL_OPS, key=len, reverse=True))


# Identifiers accept NON-ASCII letters: Nehrim is German and ships EditorIDs
# like `MQ32Spiegelschuessel01SCN` spelled with an umlaut.  An ASCII-only class
# split those into three tokens and tore the name apart.
_IDENT_RE = re.compile(r'[^\W\d][\w]*', re.UNICODE)
# A number may start with a digit or a bare `.` (`.5` appears in the corpus).
# The exponent form never does, so it is deliberately not accepted -- a stray
# `e` stays an identifier and the parser reports it rather than silently
# swallowing the next token.
_NUMBER_RE = re.compile(r'(?:\d+\.\d*|\.\d+|\d+)')


def tokenize(source: str) -> list[Token]:
    """Tokenise one script body into a flat token list ending in EOF.

    Newlines are significant (a statement is a line), and comments are KEPT --
    they are round-tripped into the output, and dropping them here would lose
    the authored intent the converted Papyrus preserves.
    """
    out: list[Token] = []
    # `\r\n` and a lone `\r` both appear; normalise so column maths is honest.
    text = source.replace('\r\n', '\n').replace('\r', '\n')
    line = 1
    line_start = 0
    i = 0
    n = len(text)

    while i < n:
        ch = text[i]

        if ch == '\n':
            out.append(Token(T.NEWLINE, '\n', line, i - line_start + 1))
            i += 1
            line += 1
            line_start = i
            continue

        if ch in ' \t':
            i += 1
            continue

        col = i - line_start + 1

        if ch == ';':
            j = text.find('\n', i)
            if j < 0:
                j = n
            out.append(Token(T.COMMENT, text[i:j], line, col))
            i = j
            continue

        if ch == '"':
            j = text.find('"', i + 1)
            if j < 0:
                # Unterminated string.  Oblivion's own compiler accepted this
                # (it ran to end of line), so match that rather than refusing
                # to parse a script the original game shipped.
                j = text.find('\n', i)
                if j < 0:
                    j = n
                out.append(Token(T.STRING, text[i:j], line, col))
                i = j
                continue
            out.append(Token(T.STRING, text[i:j + 1], line, col))
            i = j + 1
            continue

        m = _IDENT_RE.match(text, i)
        if m:
            out.append(Token(T.IDENT, m.group(), line, col))
            i = m.end()
            continue

        # A `.` is only the start of a number when a digit follows; otherwise
        # it is the member operator (`BaurusRef.getdisposition`), which the
        # corpus uses 8,843 times in Nehrim alone.
        if ch.isdigit() or (ch == '.' and i + 1 < n and text[i + 1].isdigit()):
            m = _NUMBER_RE.match(text, i)
            # An EditorID MAY START WITH A DIGIT -- `01FlayerBladeScript`,
            # `001EffectCreatureGraywar`, `1TrapFireMineWorldRef`.  A digit run
            # that continues into letters or `_` is one identifier, not a
            # number followed by a name: splitting it turned a single argument
            # into two on 709 argument tails in Nehrim alone.
            j = m.end()
            if j < n and (text[j].isalpha() or text[j] == '_'):
                word = _IDENT_RE.match(text, j)
                out.append(Token(T.IDENT, text[i:word.end()], line, col))
                i = word.end()
                continue
            out.append(Token(T.NUMBER, m.group(), line, col))
            i = j
            continue

        for op in _OPERATORS:
            if text.startswith(op, i):
                out.append(Token(T.OP, op, line, col))
                i += len(op)
                break
        else:
            # Oblivion's compiler tolerated stray punctuation and so must we:
            # MG09Script ships a bare '`' after an `endif` (line 132) that the
            # original game compiled fine.  Refusing it here would fail a
            # script the source plugin actually uses, so keep the character as
            # its own token and let the parser decide it is noise.  Failing
            # loudly on Bethesda's own typo is the wrong tradeoff.
            out.append(Token(T.OP, ch, line, col))
            i += 1

    out.append(Token(T.EOF, '', line, i - line_start + 1))
    return out

def unwrap_parens(text: str) -> str:
    """Strip one pair of parentheses that encloses the WHOLE expression."""
    text = text.strip()
    while text.startswith('(') and text.endswith(')'):
        depth, in_str = 0, False
        for i, ch in enumerate(text):
            if in_str:
                in_str = ch != '"'
            elif ch == '"':
                in_str = True
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0 and i != len(text) - 1:
                    return text
        text = text[1:-1].strip()
    return text
