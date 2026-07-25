"""Statement interpreter for the converted Papyrus conversation loops.

Split out of `papyrus_emulator` so the engine model and the language subset
stay separately testable.

Models only what these state machines use: If/ElseIf/Else/EndIf, numeric
assignment, cross-instance property reads/writes, GetStage/SetStage/
GetStageDone/IsRunning, Say(), RegisterForSingleUpdate/UnregisterForUpdate.
Anything else evaluates to a neutral 0 rather than aborting, so an unrelated
line in a large converted script cannot mask the logic under test.
"""
import re

_NUM = r'-?\d+(?:\.\d+)?'


def _fmt(v):
    return ('%g' % v) if isinstance(v, float) else str(v)


def _num(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    return float(v) if isinstance(v, (int, float)) else 0.0


def truth(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    return v is not None


def _compare(a, op, b):
    a, b = _num(a), _num(b)
    return {'==': a == b, '!=': a != b, '>=': a >= b,
            '<=': a <= b, '>': a > b, '<': a < b}[op]


def _balanced(s):
    d = 0
    for ch in s:
        if ch == '(':
            d += 1
        elif ch == ')':
            d -= 1
            if d < 0:
                return False
    return d == 0


_SPLIT_CACHE = {}


def split_top(expr, op):
    """Split `expr` on `op` at parenthesis depth 0.

    Memoised: the interpreter re-evaluates the same handful of condition
    strings on every tick and tries ten operators against each, so an
    unmemoised scan dominated the profile (432k calls, half the runtime).
    Expressions are immutable source text, so the cache is exact.

    The depth check must happen BEFORE `(` increments it and AFTER `)`
    decrements it, or an expression that merely starts with a parenthesis
    ("(a) && (b)") is treated as entirely nested and never splits — which sent
    eval_expr back in with an identical string and recursed until the stack
    blew.
    """
    key = (expr, op)
    hit = _SPLIT_CACHE.get(key)
    if hit is not None:
        return hit
    out, depth, buf, i = [], 0, [], 0
    while i < len(expr):
        ch = expr[i]
        if ch == ')':
            depth -= 1
        at_top = depth == 0
        if ch == '(':
            depth += 1
        if at_top and expr.startswith(op, i):
            # '>' must not split '>='
            if op in ('>', '<') and i + 1 < len(expr) and expr[i + 1] == '=':
                buf.append(ch)
                i += 1
                continue
            # unary minus is not a binary operator
            if op == '-':
                prev = ''.join(buf).strip()
                if not prev or prev[-1] in '(=*/+-<>!':
                    buf.append(ch)
                    i += 1
                    continue
            out.append(''.join(buf))
            buf = []
            i += len(op)
            continue
        buf.append(ch)
        i += 1
    out.append(''.join(buf))
    result = out if len(out) > 1 else [expr]
    _SPLIT_CACHE[key] = result
    return result


class Interp:
    """Executes converted Papyrus statements against an Engine."""

    def __init__(self, engine):
        self.e = engine

    # -- entry -----------------------------------------------------------
    def run_event(self, inst, event, extra=None):
        stmts = inst.events.get(event.lower())
        if not stmts:
            return
        self.exec_block(stmts, inst, extra or {})

    def exec_block(self, stmts, inst, ctx):
        i = 0
        while i < len(stmts):
            i = self.exec_stmt(stmts, i, inst, ctx)

    # -- statements ------------------------------------------------------
    def exec_stmt(self, stmts, i, inst, ctx):
        line = stmts[i].strip()
        low = line.lower()

        if low.startswith('if '):
            j, branches = self._collect_if(stmts, i)
            for cond, body in branches:
                if cond is None or truth(self.eval_expr(cond, inst, ctx)):
                    self.exec_block(body, inst, ctx)
                    break
            return j

        if low.startswith(('elseif ', 'else', 'endif')):
            return i + 1

        m = re.match(r'^([\w.()\s]+?)\s*=\s*(.+)$', line)
        if m and not any(o in line for o in ('==', '!=', '>=', '<=')):
            self.assign(m.group(1).strip(),
                        self.eval_expr(m.group(2).strip(), inst, ctx),
                        inst, ctx)
            return i + 1

        self.eval_expr(line, inst, ctx)
        return i + 1

    def _collect_if(self, stmts, i):
        """Return (index past EndIf, [(cond_or_None, body), ...])."""
        branches = []
        cond = stmts[i].strip()[3:]
        body, depth = [], 0
        j = i + 1
        while j < len(stmts):
            s = stmts[j].strip()
            sl = s.lower()
            if sl.startswith('if '):
                depth += 1
            elif sl.startswith('endif'):
                if depth == 0:
                    branches.append((cond, body))
                    return j + 1, branches
                depth -= 1
            elif depth == 0 and sl.startswith('elseif '):
                branches.append((cond, body))
                cond, body = s[7:], []
                j += 1
                continue
            elif depth == 0 and sl == 'else':
                branches.append((cond, body))
                cond, body = None, []
                j += 1
                continue
            body.append(stmts[j])
            j += 1
        branches.append((cond, body))
        return j, branches

    # -- assignment ------------------------------------------------------
    def assign(self, target, val, inst, ctx):
        owner, field = self.resolve_target(target, inst, ctx)
        if owner is None:
            return
        owner.props[field.lower()] = val
        self.e.trace_line('    %s.%s = %s' % (owner.name, field, _fmt(val)))

    def resolve_target(self, target, inst, ctx):
        target = target.strip()
        m = re.match(r'^\((\w+)\s+as\s+\w+\)\.(\w+)$', target, re.I)
        if m:
            base = ctx.get(m.group(1).lower())
            return (base, m.group(2)) if base else (None, None)
        if '.' in target:
            head, field = target.rsplit('.', 1)
            base = self.resolve_object(head, inst, ctx)
            return (base, field) if base else (None, None)
        return inst, target

    def resolve_object(self, name, inst, ctx):
        n = name.strip().lower()
        if n in ('self', ''):
            return inst
        if n in ctx:
            return ctx[n]
        handle = inst.props.get(n)
        if handle is not None and hasattr(handle, 'props'):
            return handle
        return self.e.scripts.get(n)

    # -- expressions -----------------------------------------------------
    def eval_expr(self, expr, inst, ctx, _depth=0):
        expr = expr.strip()
        if not expr:
            return 0.0
        # Guard: a malformed split that returns the input unchanged would
        # otherwise recurse forever. Bail out neutrally instead of hanging —
        # a modelling gap must never cost the caller a timeout.
        if _depth > 64:
            self.e.trace_line('    [expr too deep] %s' % expr[:60])
            return 0.0

        for op, fn in (('&&', lambda a, b: a and b),
                       ('||', lambda a, b: a or b)):
            parts = split_top(expr, op)
            if len(parts) > 1:
                acc = truth(self.eval_expr(parts[0], inst, ctx, _depth + 1))
                for p in parts[1:]:
                    acc = fn(acc, truth(self.eval_expr(p, inst, ctx, _depth + 1)))
                return acc

        if expr.startswith('(') and expr.endswith(')') and _balanced(expr[1:-1]):
            return self.eval_expr(expr[1:-1], inst, ctx, _depth + 1)

        if expr.startswith('!'):
            return not truth(self.eval_expr(expr[1:], inst, ctx, _depth + 1))

        for op in ('==', '!=', '>=', '<=', '>', '<'):
            parts = split_top(expr, op)
            if len(parts) == 2:
                return _compare(self.eval_expr(parts[0], inst, ctx, _depth + 1), op,
                                self.eval_expr(parts[1], inst, ctx, _depth + 1))

        for op in ('+', '-'):
            parts = split_top(expr, op)
            if len(parts) == 2 and parts[0].strip():
                a = _num(self.eval_expr(parts[0], inst, ctx, _depth + 1))
                b = _num(self.eval_expr(parts[1], inst, ctx, _depth + 1))
                return a + b if op == '+' else a - b

        if re.fullmatch(_NUM, expr):
            return float(expr)

        return self.eval_call(expr, inst, ctx)

    def eval_call(self, expr, inst, ctx):
        e = self.e

        m = re.match(r'^(.*?)\.?SetStage\s*\(\s*(\d+)\s*\)$', expr, re.I)
        if m:
            self.set_stage(self.quest_name(m.group(1), inst, ctx),
                           int(m.group(2)))
            return 0.0

        m = re.match(r'^(.*?)\.?GetStage\s*\(\s*\)$', expr, re.I)
        if m:
            return float(e.quests.get(self.quest_name(m.group(1), inst, ctx), 0))

        m = re.match(r'^(.*?)\.?GetStageDone\s*\(\s*(\d+)\s*\)$', expr, re.I)
        if m:
            q = self.quest_name(m.group(1), inst, ctx)
            return 1.0 if e.quests.get(q, 0) >= int(m.group(2)) else 0.0

        m = re.match(r'^(.*?)\.?IsRunning\s*\(\s*\)$', expr, re.I)
        if m:
            return 1.0 if self.quest_name(m.group(1), inst, ctx) in e.quest_running else 0.0

        m = re.match(r'^(.*?)\.?Say\s*\(\s*(\w+)', expr, re.I)
        if m:
            speaker = self.resolve_object(m.group(1), inst, ctx) or inst
            prop = m.group(2).lower()
            handle = inst.props.get(prop)
            topic = handle if isinstance(handle, str) else prop
            e.say(speaker, topic)
            return 0.0

        m = re.match(r'^RegisterForSingleUpdate\s*\(\s*([\d.]+)\s*\)$',
                     expr, re.I)
        if m:
            e.register_update(inst, float(m.group(1)))
            return 0.0

        if re.match(r'^UnregisterForUpdate', expr, re.I):
            e.cancel_updates(inst)
            return 0.0

        m = re.match(r'^\((\w+)\s+as\s+\w+\)\.(\w+)$', expr, re.I)
        if m:
            base = ctx.get(m.group(1).lower())
            return base.props.get(m.group(2).lower(), 0.0) if base else 0.0

        if '.' in expr and '(' not in expr:
            head, field = expr.rsplit('.', 1)
            base = self.resolve_object(head, inst, ctx)
            return base.props.get(field.lower(), 0.0) if base else 0.0

        if re.fullmatch(r'\w+', expr):
            return inst.props.get(expr.lower(), 0.0)

        return 0.0          # unmodelled: neutral

    def quest_name(self, ref, inst, ctx):
        ref = (ref or '').strip().rstrip('.')
        if not ref:
            return inst.owner or inst.name.lower()
        obj = self.resolve_object(ref, inst, ctx)
        if obj is not None and getattr(obj, 'owner', None):
            return obj.owner
        return ref.lower()

    def set_stage(self, quest, stage):
        e = self.e
        if stage <= e.quests.get(quest, 0):
            return
        e.quests[quest] = stage
        e.quest_running.add(quest)
        e.trace_line('  SetStage(%s, %d)' % (quest, stage))
        for fn in list(e.stage_fragments.get((quest, stage), ())):
            fn()
