"""Statement BODIES: a tree walk that emits Papyrus lines with their nesting.

`convert_standalone` used to convert a block by handing `_convert_line` one
SOURCE LINE at a time.  That is why the string layer exists: a line on its own
has no structure, so every structural question -- is this `if` closed? how deep
am I? does this `else` belong to that `if`? -- had to be re-derived by counting
keywords across the emitted text afterwards (`_balance_if_endif`,
`_remove_dead_code_after_return`, the block-depth counter in `_convert_line`).

The parser already owns all of it: an `If` node holds its own body, its elseif
chain and its else, so walking the tree emits closed, correctly-indented blocks
by construction and there is nothing left to repair.

`emit/stmt.py` converts one statement; this module walks the bodies and owns
LAYOUT -- indentation, the `Else`/`EndIf`/`EndWhile` closers, and re-attaching
each statement's trailing source comment.
"""

from __future__ import annotations

from script_convert.emit import stmt as S
from script_convert.tes4 import nodes as N

#: Papyrus indents with two spaces per level, matching the emitted events.
INDENT = '  '


def emit_body(conv, body, extends: str, depth: int = 0) -> list[str]:
    """Papyrus lines for a list of statement nodes, indented from `depth`.

    Recurses through `If`/`While` rather than tracking a running depth counter,
    so a body cannot come out unbalanced and a `Return` cannot strand the lines
    that follow it inside the wrong block.
    """
    out = []
    open_walk = conv._refwalk_var
    conv._refwalk_var = ''
    for st in body:
        lines = emit_stmt(conv, st, extends, depth)
        # An OBSE `forEach <it> <- <container> ... loop` body is INERT: Papyrus
        # has no equivalent of OBSE's dynamic containers, the iterator carries
        # no value, and the body reads it element-by-element.  The opener
        # converts to a `;TODO:` and everything up to the `loop` follows it
        # into a comment rather than running against an unassigned iterator.
        if conv._in_foreach:
            lines = [_comment(l) for l in lines]
        if _opens_foreach(st):
            conv._in_foreach += 1
        elif conv._in_foreach and _closes_foreach(st):
            conv._in_foreach -= 1
        out += lines
    # An OBSE ref-walk's `While` is opened by a `Label` mid-body and its `Goto`
    # cannot close it in place (the Goto sits inside the loop's own `if` nest,
    # and `EndWhile` there would cross those blocks).  The walk therefore ends
    # where the body containing its Label ends, which only the walker knows.
    if conv._refwalk_var and conv._refwalk_labels:
        out.append(INDENT * depth + 'EndWhile')
        conv._refwalk_labels = set()
    conv._refwalk_var = open_walk
    return out


def emit_stmt(conv, st: N.Stmt, extends: str, depth: int) -> list[str]:
    """Papyrus lines for ONE statement, including any body it owns."""
    pad = INDENT * depth
    if isinstance(st, N.If):
        return _if(conv, st, extends, depth)
    if isinstance(st, N.While):
        return ([pad + _text(conv, st, extends)]
                + emit_body(conv, st.body, extends, depth + 1)
                + [pad + 'EndWhile'])
    text = _text(conv, st, extends)
    if not text:
        # A blank source line stays blank; a declaration emits nothing at all
        # (it was hoisted to a property), so it must not leave an empty line.
        return [''] if isinstance(st, N.Blank) else []
    return [pad + text]


def _if(conv, st: N.If, extends: str, depth: int) -> list[str]:
    """`If` with its elseif chain and else, each body owning its own nesting."""
    pad = INDENT * depth
    out = [pad + _text(conv, st, extends)]
    out += emit_body(conv, st.body, extends, depth + 1)
    for cond, body, _line in st.elifs:
        out.append(pad + 'ElseIf ' + S.emit_condition(conv, cond, extends))
        out += emit_body(conv, body, extends, depth + 1)
    if st.orelse:
        out.append(pad + 'Else')
        out += emit_body(conv, st.orelse, extends, depth + 1)
    out.append(pad + 'EndIf')
    return out


def _text(conv, st: N.Stmt, extends: str) -> str:
    """One statement's converted text, with its trailing comment re-attached.

    The comment rides on the NODE, so it can never be emitted in the middle of
    the expression it followed -- the failure `_repair_commented_condition`
    existed to undo.
    """
    conv._line_comments.clear()
    text = conv._guard_stage_timer(S.emit(conv, st, extends))
    notes = '  '.join(conv._line_comments)
    conv._line_comments.clear()
    if notes:
        # A command that converts to nothing but notes IS the comment; one
        # that produced a value keeps the notes beside it.
        text = notes if text.strip() in ('', '0') else f'{text}  {notes}'
    if st.comment and not text.lstrip().startswith(';'):
        text = f'{text}  {st.comment}' if text else st.comment
    return text


def _comment(line: str) -> str:
    """The line, commented out in place, keeping its indentation."""
    stripped = line.lstrip()
    if not stripped or stripped.startswith(';'):
        return line
    return line[:len(line) - len(stripped)] + ';' + stripped


def _opens_foreach(st) -> bool:
    """Does this statement open an OBSE `forEach` block?"""
    return isinstance(st, N.ExprStmt) and st.expr.called == 'foreach'


def _closes_foreach(st) -> bool:
    """Does this statement close one with `loop`?"""
    return isinstance(st, N.ExprStmt) and st.expr.called == 'loop'
