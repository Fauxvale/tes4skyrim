"""ScriptConverter class — core TES4→Papyrus line-by-line conversion."""

import re
from typing import Optional

from script_convert.constants import (
    BLOCK_MAP, BLOCK_FILTER_PARAM, TYPE_MAP, ACTOR_VALUE_MAP, KNOWN_GLOBALS,
    TES4_ATTRIBUTES, ATTRIBUTE_STUB_VALUE,
    _ACTOR_VALUE_FUNCTIONS, _ACTOR_VALUE_READ_FUNCTIONS,
    _PAPYRUS_RESERVED, FUNCTION_MAP, _BARE_BOOL_FUNCTIONS,
    _BARE_NO_EQUIV_COMMANDS, _OBSE_NO_EQUIV_COMMANDS,
    _ACTOR_ONLY_FUNCTIONS, _OBJREF_SHARED_FUNCTIONS, _ACTORBASE_ARG_FUNCTIONS,
    _ACTOR_ARG_FUNCTIONS,
    _OBJREF_IMPLICIT_SELF_FUNCTIONS, _ZERO_ARG_REF_FUNCTIONS,
    _FORM_TYPE_TESTS,
    _safe_property_name, _canonical_global, _record_type_to_papyrus,
    _record_type_to_base_papyrus, papyrus_script_name,
    script_type_may_override, _BASE_OBJECT_PAPYRUS,
    resolve_property_formid, _digit_stripped_formid,
    TES4_MURDER_BOUNTY, TES4_ASSAULT_BOUNTY, TES4_STEAL_BOUNTY,
    PLAYER_ALIAS_EXTENDS,
)
from script_convert.cross_ref import CrossRefGraph


_COND_LINE_RE = re.compile(r'^(\s*(?:If|ElseIf)\s+)(.*)$', re.IGNORECASE)

# A whole name wrapped in Oblivion's optional quotes: `"MQ01Tate"`.  Anchored,
# so it only ever strips a quoted IDENTIFIER handed to _convert_ref — never a
# string literal, which contains spaces/punctuation and reaches other handlers.
_QUOTED_NAME_RE = re.compile(r'^"([A-Za-z_]\w*)"$')
# TES4 reads of "is the player sleeping right now" (the MenuMode sleep idiom)
_SLEEP_READ_RE = re.compile(r'\b(?:ispcsleeping|isplayersleeping|getpcissleeping)\b',
                            re.IGNORECASE)
# Fallback line length (seconds) charged to a converted Say() timer at the CALL
# SITE when the topic has no measured audio.
#
# What the timer is actually for. Papyrus `Say()` is FIRE-AND-FORGET: it does not
# block, does not queue, and silently does nothing when no INFO under the topic
# qualifies (CK wiki, Say - ObjectReference). The owning script polls on its own
# update (0.1-0.5s) and re-issues `Say()` while its guard reads `timer <= 0`, but
# the INFO's End FRAGMENT — the thing that advances the conversation — only runs
# when the line FINISHES. So the timer has exactly one job: cover the window
# between issuing the line and its fragment firing. Charge zero and the poller
# re-Says the same line every tick, restarting it forever so the fragment never
# runs (Valen Dreth's CharacterGen taunts repeat line 1 endlessly).
#
# Why the LINE'S OWN LENGTH is the right value, and not more:
#   * It is the smallest value that reliably outlasts the window.
#   * The End fragment clears it the instant the line really ends, so a line that
#     played costs NO extra silence — the wait is not additive in practice.
#   * It self-clears via the owning script's countdown, so a line that never
#     played (Say dropped) costs one line's pacing and the scene continues.
#
# Do NOT "park" a large sentinel here that only the End fragment can clear. That
# inverts the failure mode: no line means no fragment, so the value strands and
# the conversation HALTS (CharacterGen's prison-cell scene sat ~20s between lines
# and stalled outright where its convCount chain runs through gaps with no INFO).
SAY_LINE_SECONDS = 3.0
# Papyrus method name for an OBSE user-defined function (`begin Function{...}`).
# One fixed name per script: OBSE allowed exactly one Function block per script
# and `Call <ScriptName> args` names the SCRIPT, never the function.
_UDF_NAME = 'TES4Call'

def _split_udf_params(block_filter: str) -> list[str]:
    """Parameter names from an OBSE `begin Function{...}` header.

    Both separators occur in the wild — `{ a, b, c }` and `{ refRuneSpell
    levelRequired}` — so split on commas AND whitespace.
    """
    inner = block_filter.strip()
    if inner.startswith('{'):
        inner = inner[1:]
    if inner.endswith('}'):
        inner = inner[:-1]
    return [p for p in re.split(r'[,\s]+', inner.strip()) if p]


def _split_obse_args(rest: str) -> list[str]:
    """Split the argument tail of an OBSE `Call <Script> ...` invocation.

    Mirrors _split_udf_params on the CALL side: OBSE accepts commas, whitespace,
    or a mix (`Call Foo 10, 1, -1` / `Call JDLevitate 1 0`).  Splitting is
    suppressed inside parentheses, brackets and string literals so a compound
    argument stays one argument — `Call Foo (a + b) 2` is two args, not four,
    and `Call Foo "x, y" 1` keeps the comma inside the string.

    Whitespace only separates when it is NOT joining an arithmetic expression.
    Nehrim writes `Call GlobalScriptExpGained 30 * ( x - y ), 1, 1, -1`, where
    `30 * (...)` is ONE argument spelled with spaces around the operator; naive
    whitespace splitting emitted `TES4Call(30, *, (...), 1, 1, -1)` and a bare
    `*` is not an expression.  A comma is always a real separator, so the mixed
    form still parses correctly.
    """
    # Operators that bind their two operands into a single argument.  A `-` is
    # deliberately NOT here: it is far more often a unary sign on a separate
    # argument (`Call Foo 1 -1`) than a spaced-out subtraction, and the comma
    # form covers the subtraction case unambiguously.
    _BINARY_OPS = {'*', '/', '+', '%', '&&', '||', '==', '!=', '<', '>',
                   '<=', '>=', '&', '|'}

    raw: list[str] = []
    buf: list[str] = []
    depth = 0
    in_str = False
    for ch in rest.strip():
        if in_str:
            buf.append(ch)
            if ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            buf.append(ch)
        elif ch in '([':
            depth += 1
            buf.append(ch)
        elif ch in ')]':
            depth -= 1
            buf.append(ch)
        elif depth == 0 and ch == ',':
            raw.append(''.join(buf))
            buf = []
            raw.append(',')          # keep hard separators visible below
        elif depth == 0 and ch.isspace():
            if buf:
                raw.append(''.join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        raw.append(''.join(buf))

    # Re-join whitespace-separated tokens across binary operators.
    tokens = [t for t in raw if t.strip() or t == ',']
    args: list[str] = []
    cur: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == ',':
            if cur:
                args.append(' '.join(cur))
                cur = []
            i += 1
            continue
        if tok in _BINARY_OPS and cur and i + 1 < len(tokens) and tokens[i + 1] != ',':
            # `a * b` — operator plus its right operand belong to the current arg
            cur.append(tok)
            cur.append(tokens[i + 1])
            i += 2
            continue
        if cur:
            args.append(' '.join(cur))
        cur = [tok]
        i += 1
    if cur:
        args.append(' '.join(cur))
    return [a for a in (x.strip() for x in args) if a]


# Placeholder for a bare TES4 `GetContainer` that is not inside an equip event.
# Papyrus cannot walk from an item to its container, so the expression has no
# standalone translation — but the comparison it sits in usually does, and
# _resolve_getcontainer rewrites the whole comparison once it is visible.
_GETCONTAINER_MARKER = '__TES4_GETCONTAINER__'

# Search radius standing in for OBSE's GetFirstRef/GetNextRef walk over the
# loaded cells.  Oblivion scans the loaded-cell grid, whose interior span is one
# 4096-unit cell out from the player in each direction, so 4096 covers the same
# ground the authored loop did without reaching into cells the engine has not
# loaded.
_REFWALK_RADIUS = 4096.0

# Papyrus calls whose return type is narrower (or simply other) than
# ObjectReference, keyed to the type a TES4 `ref` variable must be declared as
# once it is assigned from one.  TES4 spelled every handle `ref`; Papyrus does
# not convert between these implicitly, so the declaration follows the
# assignment (see the retype pass in convert_standalone).
_REF_ASSIGN_SOURCE_TYPES = {
    'getbaseobject': 'Form',
    'getequippedweapon': 'Weapon',
    'getequippedshield': 'Armor',
    'getworncoveringitem': 'Armor',
    'getactorowner': 'ActorBase',
    'getfactionowner': 'Faction',
}

# Papyrus natives whose FIRST argument is typed narrower than `Form`, which is
# the permissive type an OBSE user-function parameter falls back to when the
# TES4 `ref` declaration carries no usage evidence.  Papyrus refuses the
# implicit downcast, so a call site passing such a parameter needs an explicit
# `as <type>` or the whole script fails to compile — and a script that fails to
# compile takes every script declaring a property of its type down with it.
# HasSpell is deliberately absent: it really does take a Form.
_UDF_ARG_DOWNCASTS = {
    'addspell': 'Spell',
    'removespell': 'Spell',
    'isinfaction': 'Faction',
    'addtofaction': 'Faction',
    'removefromfaction': 'Faction',
    'getfactionrank': 'Faction',
    'setfactionrank': 'Faction',
    'modfactionrank': 'Faction',
    # Polyfill helpers that take a live reference.  A TES4 user function
    # declares its actor parameter as `ref`, which falls back to Form.
    'tes4polyfill.update3d': 'ObjectReference',
}

# The narrow-typed call sites above, capturing the function and its first
# argument.  A trailing `, ...` is allowed so the two-argument faction setters
# match as well as the one-argument tests.
# Longest name first so a dotted spelling (TES4Polyfill.Update3D) wins over any
# bare prefix of it.  The lookbehind rejects only a longer IDENTIFIER running
# into the name (`MyIsInFaction(`), NOT a receiver dot — these are method calls,
# so `NextActor.IsInFaction(x)` is the normal shape and must still match.
_UDF_DOWNCAST_RE = re.compile(
    r'(?<!\w)(' + '|'.join(
        re.escape(k) for k in sorted(_UDF_ARG_DOWNCASTS, key=len, reverse=True))
    + r')\(\s*([A-Za-z_]\w*)\s*(?=[,)])',
    re.IGNORECASE)


def _resolve_getcontainer(line: str) -> str:
    """Rewrite a comparison against the GetContainer placeholder.

    `GetContainer == 0` asks "am I lying in the world rather than in someone's
    inventory", which TES4Polyfill.IsInContainer answers exactly.  Any other
    comparison (`GetContainer != SomeRef` — "is a *particular* actor holding
    me") has no Papyrus equivalent; it is neutralised to the value that does not
    fire the branch, and left as a TODO rather than compiled into a lie.
    """
    if _GETCONTAINER_MARKER not in line:
        return line
    m = re.match(
        rf'^(\s*)(.*?){re.escape(_GETCONTAINER_MARKER)}\s*(==|!=)\s*0\b(.*)$',
        line)
    if m:
        indent, pre, op, rest = m.groups()
        call = 'TES4Polyfill.IsInContainer(Self)'
        expr = f'!{call}' if op == '==' else call
        return f'{indent}{pre}{expr}{rest}'
    # Unsupported shape — do not let the placeholder reach the compiler.
    neutral = 'False' if re.search(r'!=\s*\w', line) else 'True'
    stripped = line.strip()
    indent = line[:len(line) - len(line.lstrip())]
    if _COND_LINE_RE.match(line):
        kw = stripped.split()[0]
        return (f'{indent}{kw} {neutral}  '
                f';TODO: GetContainer has no Papyrus equivalent ({stripped})')
    return f'{indent};TODO: GetContainer has no Papyrus equivalent: {stripped}'


def _split_trailing_comment(expr: str) -> tuple[str, str]:
    """Split an expression at its first `;` outside a string literal."""
    in_str = False
    for i, ch in enumerate(expr):
        if ch == '"':
            in_str = not in_str
        elif ch == ';' and not in_str:
            return expr[:i].rstrip(), expr[i:]
    return expr.rstrip(), ''


def _repair_commented_condition(line: str) -> str:
    """Neutralise an If/ElseIf whose condition was EATEN by an emitted comment.

    Some conversions append an explanatory `;NE: …` comment mid-expression, which
    in Papyrus comments out the rest of the line and leaves a truncated condition
    like `If (False  ;NE: GetIsCurrentPackage == 0)`.  That will not compile, so
    the line is replaced with `If True` and the original preserved as a comment.

    The condition is only broken if what survives in front of the `;` is not a
    self-contained expression: unbalanced parentheses, or a dangling trailing
    operator.  A well-formed condition followed by an ordinary trailing comment
    is left ALONE — blanket-rewriting those to `True` silently deleted real
    guards (an item's OnEquipped body, a quest's GetItemCount gate) and made the
    guarded code run unconditionally.
    """
    m = _COND_LINE_RE.match(line)
    if not m:
        return line
    cond, comment = _split_trailing_comment(m.group(2))
    if not comment or not cond:
        return line
    balanced = cond.count('(') == cond.count(')')
    dangling = re.search(r'(==|!=|>=|<=|>|<|&&|\|\||\+|-|\*|/|\band\b|\bor\b|\bnot\b)$',
                         cond, re.IGNORECASE) is not None
    if balanced and not dangling:
        return line          # ordinary trailing comment — the condition is fine
    full = (cond + ' ' + comment).strip()
    return f'{m.group(1)}True  ;{full}'


class ScriptConverter:
    """Converts Oblivion script source to Papyrus .psc source."""

    # topic (lowercase) -> real spoken duration in seconds, measured from the
    # exported Oblivion voice files. Populated once per run by the pipeline;
    # empty falls back to SAY_LINE_SECONDS.
    say_durations: dict = {}

    # owner EditorID (lower) -> {timer field names needing a pending-beat
    # companion}. Built once per run by the pipeline from a whole-export scan
    # and shipped to every worker, so a `SomeQuest.convTimer` beat staged by
    # one script is declared on SomeQuest's script regardless of conversion
    # order or which process handles either script.
    beat_fields_by_owner: dict = {}
    # topic (lower) -> (kind, spec, beat), from pipeline.build_say_timer_owners.
    # `beat` is the deliberate pause the owning script stacks AFTER this topic's
    # line; the End fragment applies it once the line has actually finished.
    say_timer_owners: dict = {}

    # DIAL EditorID (lower) -> `TES4Unlock_<topic>` GlobalVariable name, from
    # tes5_import.dialog_unlocks.build_unlock_plan. Populated once per run by
    # the pipeline. `AddTopic X` on a GATED topic opens that topic's gate, the
    # same SetValue(1) the INFO/QUST fragments emit — see _NO_OP_FUNCS for why
    # an ungated topic stays an inert comment.
    topic_unlock_globals: dict = {}

    # script EditorID (lower) -> [(mesg_edid, text, buttons)], from
    # script_convert.message_menus.build_message_plan. Populated once per run
    # by the pipeline AND the importer from the same analysis, so the Message
    # properties the .psc declares are exactly the MESG records the ESM ships.
    message_menus: dict = {}

    def __init__(self, xref: CrossRefGraph):
        self.xref = xref
        self._property_refs: dict[str, str] = {}
        # GetInCell prefix families needing a generated helper: name -> cells.
        self._cell_families: dict[str, list] = {}
        self._has_gamemode = False
        self._has_menumode = False
        self._has_scripteffectupdate = False
        self._uses_getsecondspassed = False
        self._uses_hour_window = False
        self._uses_timer = False
        self._local_vars = set()
        self._var_renames: dict[str, str] = {}  # orig_lower -> safe_name
        self._var_types: dict[str, str] = {}  # lower_name -> papyrus_type
        self._current_event: str = ''  # Current event header for context-aware conversion
        self._line_comments: list[str] = []  # Comments accumulated during expression conversion
        # True once this script emitted TES4Polyfill.SuppressFallDamage() (the
        # ResetFallDamageTimer conversion).  It raises a GLOBAL game setting,
        # so the effect-finish path must put it back or fall damage stays off
        # for the rest of the save — the paired on/off trap in
        # docs/papyrus_conversion_notes.md.
        self._suppressed_fall_damage = False
        # Button-MessageBox state (see message_menus.py).
        self._msgbox_used = set()
        self._uses_msg_buttons = False
        # Nesting depth inside an OBSE `forEach … loop` block (body is inert).
        self._in_foreach = 0
        # OBSE ref-walk loop state (`GetFirstRef`/`GetNextRef` + `Label`/`Goto`).
        # Holds the ref variable the walk assigns, so the `Label` that opens the
        # loop knows what to iterate and `Goto` knows which loop it closes.
        self._refwalk_var = ''
        self._refwalk_labels: set = set()
        # Source-side if/while nesting depth of the line being converted.
        self._block_depth = 0
        self._udf_returns = False      # OBSE Function block uses SetFunctionValue
        self._udf_return_value = ''    # value staged by SetFunctionValue
        # Timers a Say() parked (see _say_seconds). They hold a large sentinel
        # until the topic's End fragment clears them, so any expression that
        # READS one back is no longer reading a line length and must not be
        # translated literally.
        self._parked_timers: set = set()
        # Timer expressions that need a pending-beat companion property
        # declared on THIS script (only those whose owner is this script; a
        # `Quest.field` beat is declared by the quest's own converted script).
        self._say_beat_targets: set = set()
        # EditorID of the SCPT being converted.  Needed to resolve a BARE
        # `GetCurrentAIPackage` back to the actor that attaches this script.
        self._current_script_edid: str = ''

    _SAY_TOPIC_RE = re.compile(r'\.?Say\(\s*([A-Za-z_]\w*)')

    def _say_seconds(self, say_expr: str) -> float:
        """Seconds to charge a converted Say() timer at the CALL SITE.

        This topic's longest MEASURED line (see SAY_LINE_SECONDS for why that is
        the right value): enough to stop the owning script's poller re-issuing
        `Say()` before the INFO's End fragment can run, and no more. The fragment
        clears it as soon as the line actually ends, so a line that played adds
        no silence; a line that was dropped drains through the owning script's
        own countdown instead of stranding the scene.
        """
        tm = self._SAY_TOPIC_RE.search(say_expr or '')
        topic = tm.group(1).lower() if tm else ''
        return float((self.say_durations or {}).get(topic)
                     or SAY_LINE_SECONDS)

    @staticmethod
    def beat_property(timer_expr: str) -> str:
        """Name of the pending-beat companion for a Say timer.

        `CharacterGen.convTimer` -> `CharacterGen.convTimerPendingBeat`
        `timer`                  -> `timerPendingBeat`

        Static so the INFO-fragment emitter (pipeline) derives the identical
        name from the same timer expression.
        """
        if '.' in timer_expr:
            owner, field = timer_expr.rsplit('.', 1)
            return f'{owner}.{field}PendingBeat'
        return f'{timer_expr}PendingBeat'

    def _beat_property(self, target: str) -> str:
        self._say_beat_targets.add(target)
        return self.beat_property(target)

    # `set <timer> to <ref.>Say[To] [target] <topic>` — the park sites.  Only
    # the ASSIGNMENT target matters here (which timer gets parked), so the
    # topic is not captured; `[^\n]*` keeps the match on ONE line.
    _PARK_SITE_RE = re.compile(
        r'set\s+([\w.]+)\s+to\s+[^\n]*?\bsay(?:to)?\b', re.IGNORECASE)
    # `set <timer> to <timer> - <getSecondsPassed|literal>` — the countdown.
    _COUNTDOWN_RE = re.compile(
        r'^\s*set\s+([\w.]+)\s+to\s+([\w.]+)\s*-\s*(\S+)\s*$', re.IGNORECASE)

    def _prescan_parked_timers(self, source: str) -> None:
        """Record every timer a Say() in this source will park.

        Populated up front because the countdown that decrements a parked timer
        is written ABOVE the park site in the source, and the countdown has to
        be emitted park-safe (see _parked_decrement).
        """
        for m in self._PARK_SITE_RE.finditer(source):
            self._parked_timers.add(m.group(1).lower())

    def _parked_decrement(self, target: str, value: str) -> Optional[str]:
        """Park-safe form of `timer = timer - dt` for a PARKED timer.

        The plain translation is a read-modify-write, and the timer is shared
        across processes that are not synchronised: the owning script counts it
        down on its own update while the speaking line's End fragment clears it
        to 0 from the engine's dialogue thread.  Papyrus gives no atomicity, so
        the fragment's release can land BETWEEN this statement's read and its
        write — the write then stores `park - dt`, RESURRECTING a park that was
        already released.  The next speaker's `timer <= 0` gate never opens, and
        because every INFO in these conversations is gated on an exact
        `convCount`, the chain does not resume late — it stops dead.  That is
        the intermittency: the line plays or it does not, depending purely on
        where the 0.1s tick fell relative to the line ending.

        The fix is to RE-READ the timer immediately before storing and drop the
        decrement if it no longer matches what was read.  A release that lands
        anywhere inside the statement changes the value, so the write is
        abandoned and the released 0 stands; only an uncontended tick stores.
        Losing one 0.1s decrement to a contended tick is invisible (the next
        tick takes it), whereas resurrecting the park is fatal.
        """
        if target.lower() not in self._parked_timers:
            return None
        # Match the snapshot's type to the timer's, and put the SUBTRACTION
        # through the same Float->Int coercion the plain assignment would have
        # used. TES4 let a `short` hold a Say duration
        # (SERelmynaVerenimScript's RelmynaSayToCorpse), and Papyrus rejects a
        # Float expression assigned into an Int without a cast.
        vtype = self._var_types.get(target.lower().split('.')[-1], '')
        if vtype not in ('Int', 'Float'):
            vtype = 'Float'
        local = '_tes4Tick' + re.sub(r'\W', '', target)
        expr = self._coerce_float_to_int(target, f'{local} - {value}')
        return (
            f'{vtype} {local} = {target}  ; the End fragment can clear this '
            f'mid-statement\n'
            f'  If {local} > 0 && {target} == {local}\n'
            f'    {target} = {expr}\n'
            f'  EndIf')


    def _resolve_parked_timer_expr(self, value: str) -> str:
        """Rewrite `<parked timer> +/- k` to just the adjustment.

        Substituting 0 for the parked read keeps the designer's deliberate
        beat (Oblivion's 13 such expressions are all "add a pause": +3 between
        assassin taunts, +2.5 after "My job right now...", +10 between taunt
        stages) while dropping the sentinel that would otherwise stall the
        conversation for a minute.
        """
        if not self._parked_timers or not value:
            return value
        for name in self._parked_timers:
            # match the bare variable (or Quest.field) followed by +/- number
            m = re.match(r'^\s*' + re.escape(name) + r'\s*([+-])\s*([\d.]+)\s*$',
                         value, re.IGNORECASE)
            if m:
                sign, num = m.group(1), m.group(2)
                if sign != '+':
                    # 0 - k would arm a negative timer; the guard is `<= 0`, so
                    # a subtraction simply means "no extra wait".
                    return '0'
                # An ADD is a deliberate beat Oblivion charged ON TOP of the
                # line's length — it starts when the line ENDS.  It cannot be
                # written into the timer here: the call site runs while the
                # line is still playing and the owning loop counts the timer
                # DOWN, so any value encoded in it is eroded by the line's
                # length before the End fragment can read it back.  Redirect it
                # to a dedicated pending-beat property the loop never touches;
                # the End fragment moves that into the timer once the line is
                # actually over.  Keeping the write HERE is what makes the beat
                # per-call-site: a topic Said by several scripts (CharGenMain
                # has five) gives each its own beat, and the branch guards that
                # wrap it (`If convCount == 5`) still apply.
                self._say_beat_targets.add(name)
                return f'__BEAT__{num}'
            if re.match(r'^\s*' + re.escape(name) + r'\s*$', value,
                        re.IGNORECASE):
                return '0'
        return value

    def _is_ref_typed_access(self, dotted_expr: str) -> bool:
        """Check if a dotted expression (e.g. 'SEHerdirRef.TargetRef') accesses a ref-typed variable.

        Checks both via EditorID resolution and via property type → script_all_vars.
        """
        if '.' not in dotted_expr:
            return False
        parts = dotted_expr.strip().split('.', 1)
        prop_low = parts[0].lower()
        var_low = parts[1].lower()
        # Method 1: resolve via EditorID
        if self.xref and self.xref.is_remote_ref_var(parts[0], parts[1]):
            return True
        # Method 2: resolve via property type declaration
        if self.xref:
            for pname, ptype in self._property_refs.items():
                if pname.lower() == prop_low and ptype.startswith('TES4_'):
                    script_name = ptype[5:].lower()
                    all_vars = self.xref.script_all_vars.get(script_name, {})
                    if all_vars.get(var_low) in ('ObjectReference', 'Actor'):
                        if (script_name, var_low) not in self.xref.ref_as_int:
                            return True
        return False

    def _ref_has_script_var(self, ref_name: str, var_name: str) -> bool:
        """Check if ref_name resolves to a script that declares var_name as a variable.

        Used to disambiguate Quest.variable vs Quest.function().
        """
        if not self.xref:
            return False
        var_low = var_name.lower()
        # Method 1: direct script name or via property type
        for pname, ptype in self._property_refs.items():
            if pname.lower() == ref_name.lower() and ptype.startswith('TES4_'):
                script_name = ptype[5:].lower()
                all_vars = self.xref.script_all_vars.get(script_name, {})
                if var_low in all_vars:
                    return True
        # Method 2: look up via EditorID → SCRI → script vars
        fid = self.xref.edid_to_formid.get(ref_name.lower(), '')
        if fid:
            scri_fid = self.xref.record_scri.get(fid, '')
            if scri_fid:
                se = self.xref.script_formid_to_edid.get(scri_fid, '')
                if se:
                    all_vars = self.xref.script_all_vars.get(se.lower(), {})
                    if var_low in all_vars:
                        return True
        return False

    def _is_ref_as_int_crossscript(self, dotted_expr: str) -> bool:
        """Check if a cross-script dotted var (e.g. 'MQConversations.OGDeadDaedra') was retyped to Int.

        Returns True if the variable exists in ref_as_int for the owning script.
        """
        if '.' not in dotted_expr or not self.xref:
            return False
        parts = dotted_expr.strip().split('.', 1)
        prop_low = parts[0].lower()
        var_low = parts[1].lower()
        # Resolve property type to script name
        for pname, ptype in self._property_refs.items():
            if pname.lower() == prop_low:
                if ptype.startswith('TES4_'):
                    script_name = ptype[5:].lower()
                else:
                    script_name = ptype.lower()
                if (script_name, var_low) in self.xref.ref_as_int:
                    return True
        # Also try EditorID resolution
        prop_upper = parts[0]
        edid = self.xref.edid_to_formid.get(prop_upper) or self.xref.edid_to_formid.get(prop_upper.lower())
        if edid:
            scri_fid = self.xref.record_scri.get(edid)
            if scri_fid:
                script_edid = self.xref.script_formid_to_edid.get(scri_fid, '').lower()
                if script_edid and (script_edid, var_low) in self.xref.ref_as_int:
                    return True
        return False

    @staticmethod
    def _infer_extends(source: str, extends: str) -> str:
        """Pre-scan source for bare Actor-only function calls; upgrade extends.

        `_ACTOR_ONLY_FUNCTIONS` is NOT sound for this question — 14 of its
        entries are declared on `ObjectReference` too (`GetDistance`, `AddItem`,
        `GetItemCount`, `Say`, `PlaceAtMe`, `SetScale`, ...), which is exactly
        why `_OBJREF_SHARED_FUNCTIONS` exists and why the call-site cast at
        `_emit_function` already subtracts it.  Here the set must be subtracted
        as well: an upgrade is not a cosmetic type widening but a hard runtime
        failure.  Papyrus binds a script to a form only when the declared base
        type matches, so an `extends Actor` script on a WEAP/ACTI/CONT/DOOR is
        rejected outright — *"Unable to bind script X because their base types
        do not match"* — and never runs at all.  A bare `GetDistance` upgraded
        88 non-actor scripts that way, 67 of which the last in-game run logged
        as unbindable (`GoblinHeadScript` on `GoblinShamanStaff`, every
        `Dark*DeadDropScript`, the Daedric statue scripts, the Publican inn
        triggers).  `get_extends_class` already answers this correctly from the
        attaching record's signature, so only a genuinely Actor-only call may
        override it.

        The scan must also see CODE ONLY.  Run over the raw source it matched
        prose — `MessageBox "…not kill them!"` (`DAMalacathStatueScript`),
        `; evp the post guards` (`ICUmbacanoExitDoorScript`),
        `;StartCombat to get the scene rolling` (`SE09AltarScript`) — and each
        of those upgraded a DOOR/ACTI script into an unbindable one.  Comments
        and string literals are therefore stripped per line first.

        Finally, a function whose TES4 form names its target as an ARGUMENT
        (`GetDeadCount JesanRilian`, `SetEssential SEMuurine 0`) says nothing
        about the calling script's own type — both are `ActorBase` methods in
        Skyrim, not `Actor` ones — so they are excluded from the scan.
        """
        code_lines = []
        locals_declared = set()
        in_actor_event = False
        for line in source.split('\n'):
            code, _ = _split_trailing_comment(line)
            code = re.sub(r'"[^"]*"', '""', code)
            begin = re.match(r'\s*begin\s+(\w+)', code, re.IGNORECASE)
            if begin:
                # A block whose Papyrus event HANDS US the actor
                # (`OnEquipped(Actor akActor)`) supplies the implicit subject
                # itself, so an actor-only call inside it says nothing about
                # the script's own type — the `MGBloodwormHelmScript*` helms
                # ride on ARMO records.  Their bodies are emitted against
                # `akActor` (see `_current_event_actor_param`).
                in_actor_event = (BLOCK_FILTER_PARAM.get(begin.group(1).lower(),
                                                         ('', ''))[1] == 'Actor')
            elif re.match(r'\s*end\s*$', code, re.IGNORECASE):
                in_actor_event = False
            decl = re.match(r'\s*(short|long|int|float|ref)\s+(\w+)\s*$', code,
                            re.IGNORECASE)
            if decl:
                # A TES4 local may be NAMED like an Actor function
                # (`MS05DreamworldAmuletScript`'s `short isEquipped`); reading
                # or assigning it is not a call and must not upgrade the type.
                locals_declared.add(decl.group(2).lower())
                continue
            if not in_actor_event:
                code_lines.append(code)
        code = '\n'.join(code_lines)
        for func in _ACTOR_ONLY_FUNCTIONS:
            if (func in _OBJREF_SHARED_FUNCTIONS
                    or func in _ACTORBASE_ARG_FUNCTIONS
                    or func in locals_declared):
                continue
            # Match bare calls (not preceded by '.') anywhere in source
            if re.search(r'(?<!\.)(?<!\w)' + re.escape(func) + r'(?:\s|$|\()',
                         code, re.IGNORECASE):
                return 'Actor'
        return extends

    def convert_standalone(self, name: str, source: str, extends: str = 'ObjectReference',
                           editor_id: str = '') -> str:
        """Convert a standalone SCPT record to a full .psc file."""
        saved_refs = dict(self._property_refs)
        self._reset()
        self._property_refs = saved_refs
        self._current_script_edid = editor_id or name

        # Pre-scan: if script uses Actor-only functions on self (no ref prefix),
        # upgrade extends to Actor
        if extends == 'ObjectReference':
            extends = self._infer_extends(source, extends)

        variables, blocks = self._parse_source(source)
        # Store locally declared variable names for expression disambiguation.
        # Register BOTH the original TES4 name and the Papyrus-safe name: the
        # body still spells the variable the TES4 way, and a variable whose name
        # collides with a TES4 command (DiveRockScript's `short message`) is only
        # recognised as a variable — instead of being compiled as that command —
        # if the ORIGINAL spelling is in this set.
        self._local_vars = set()
        self._in_foreach = 0
        self._refwalk_var = ''
        self._refwalk_labels = set()
        self._block_depth = 0
        for v in variables:
            self._local_vars.add(v[1].lower())
            self._local_vars.add(_safe_property_name(v[1]).lower())
        # Store variable types for type-aware assignment conversion
        _edid_low = editor_id.lower()
        for v in variables:
            vtype_low = v[0].lower()
            vname_safe = _safe_property_name(v[1])
            ptype = TYPE_MAP.get(vtype_low, 'Int')
            if ptype == 'ObjectReference' and _edid_low and \
               (_edid_low, vname_safe.lower()) in self.xref.ref_as_int:
                ptype = 'Int'
            self._var_types[vname_safe.lower()] = ptype
            self._var_types[v[1].lower()] = ptype
        # Build rename map: original_lower -> safe_name (only when they differ).
        # Compare CASE-SENSITIVELY: the `temp` -> `Temp` rename (which dodges the
        # compiler's ::temp* scratch-register namespace) differs only in case, and
        # a case-insensitive test skipped it — leaving the declaration renamed but
        # every reference still pointing at the old name.
        for _, vname in variables:
            safe = _safe_property_name(vname)
            if safe != vname:
                self._var_renames[vname.lower()] = safe

        source_low = source.lower()
        # Which of this script's own timers a Say() will PARK.  Must be known
        # BEFORE any line is converted: the countdown that decrements the timer
        # is written ABOVE the park site in every one of these scripts
        # (CharGenQuest counts convTimer down at the top of its GameMode block
        # and parks it 40 lines later), so discovering parks lazily as they are
        # converted leaves the decrement unguarded.  See _parked_decrement.
        self._prescan_parked_timers(source)
        self._uses_getsecondspassed = 'getsecondspassed' in source_low
        self._uses_timer = bool(re.search(r'\btimer\b', source_low))
        # A chime/bell script: a top-of-the-hour GameHour window latched by a
        # countdown.  Its negative sentinel is a real-seconds value tuned
        # against the author's TimeScale and has to be rescaled to ours, or the
        # latch expires while the window is still open and the sound repeats.
        # See _scaled_debounce_seconds.
        self._uses_hour_window = bool(self._GAMEHOUR_WINDOW_RE.search(source))
        # A bare `begin MenuMode` is merged into the GameMode poll (see the
        # block loop below), so it needs the OnUpdate loop emitted even when the
        # script has no GameMode block of its own.
        self._has_gamemode = any(
            b[0] == 'gamemode'
            or (b[0] == 'menumode' and not str(b[1] or '').strip()
                and not any(_SLEEP_READ_RE.search(l) for l in b[2]))
            for b in blocks)
        self._has_menumode = any(b[0] == 'menumode' for b in blocks)
        self._has_scripteffectupdate = any(b[0] == 'scripteffectupdate' for b in blocks)

        # Value-typed TES4 script variables must be readable by the engine's
        # condition system: GetVMScriptVariable/GetVMQuestVariable(629/630) look
        # up the mangled `::<name>_var` backing variable, which only exists in
        # the .pex when BOTH the script and the auto-property carry the
        # Conditional flag. Without it every converted GetScriptVariable/
        # GetQuestVariable condition silently fails (CK: "Unable to find
        # variable ::X_var on any VM scripts").
        has_value_vars = any(
            TYPE_MAP.get(vtype.lower(), 'Int') in ('Int', 'Float', 'Bool')
            or (_edid_low and (_edid_low, _safe_property_name(vname).lower())
                in self.xref.ref_as_int)
            for vtype, vname in variables)
        cond_flag = ' Conditional' if has_value_vars else ''

        out = []
        out.append(f'ScriptName {papyrus_script_name(name)} extends {extends}'
                   f'{cond_flag}')
        out.append(f'{{Converted from TES4: {editor_id or name}}}')
        out.append('')

        # Variable declarations as properties (type may be upgraded after conversion)
        # An OBSE `begin Function{a, b}` declares its parameters as ordinary
        # script variables.  They become the Papyrus Function's parameters, so
        # they must NOT also be emitted as auto-properties: the parameter would
        # shadow the property inside the body while callers write neither,
        # leaving the body reading a permanent 0.
        _udf_param_names = set()
        for _bt, _bf, _bl in blocks:
            if _bt == 'function':
                _udf_param_names = {p.lower() for p in _split_udf_params(_bf)}

        _var_info = []
        _seen_vars = set()
        for vtype, vname in variables:
            ptype = TYPE_MAP.get(vtype, 'Int')
            safe_vname = _safe_property_name(vname)
            if safe_vname.lower() in _seen_vars:
                continue  # skip duplicate declarations
            if vname.lower() in _udf_param_names:
                continue  # becomes a Function parameter instead
            _seen_vars.add(safe_vname.lower())
            # Override ref vars that are only used with integers (cross-script analysis)
            if ptype == 'ObjectReference' and _edid_low and \
               (_edid_low, safe_vname.lower()) in self.xref.ref_as_int:
                ptype = 'Int'
            # A ref var that some OTHER script assigns a base record into must
            # be Form -- the local retype pass below only sees this script's
            # own assignments, so a cross-script write is invisible to it.
            elif ptype == 'ObjectReference' and _edid_low and \
                    (_edid_low, safe_vname.lower()) in \
                    self.xref.ref_as_base_form:
                ptype = 'Form'
            _var_info.append((safe_vname, ptype))
        var_start_idx = len(out)
        for safe_vname, ptype in _var_info:
            # Conditional so the ::<name>_var backing variable is visible to
            # CTDA GetVMScriptVariable/GetVMQuestVariable lookups (value types
            # only — object properties cannot be conditional).
            cond = ' Conditional' if ptype in ('Int', 'Float', 'Bool') else ''
            if ptype == 'Float':
                out.append(f'{ptype} Property {safe_vname} = 0.0 Auto{cond}')
            else:
                out.append(f'{ptype} Property {safe_vname} Auto{cond}')

        if variables:
            out.append('')

        # Convert blocks — merge duplicate event types
        needs_oninit_update = self._has_gamemode or self._has_scripteffectupdate
        gamemode_body = []
        menumode_blocks: list[tuple[str, list]] = []   # (menu id filter, source lines)
        sleep_menumode_blocks: list[list] = []         # bare MenuMode + isPCSleeping

        # Group blocks by event type to merge duplicates (Papyrus forbids
        # duplicate Event declarations).  Each source block keeps its own filter
        # guard, because blocks that merge into one event can carry different
        # filters (`begin OnAdd player` and `begin OnDrop player` both become
        # OnContainerChanged, but guard on different parameters).
        from collections import defaultdict
        merged_blocks: dict[str, list] = defaultdict(list)   # key -> [(guard, lines)]
        block_order: list[str] = []

        udf_params: list[str] = []       # OBSE user-function parameter names
        udf_body: list[str] = []         # its body, emitted as a global Function

        for block_type, block_filter, block_lines in blocks:
            if block_type in ('gamemode', 'scripteffectupdate'):
                gamemode_body.extend(block_lines)
                continue

            # OBSE user-defined function: `begin Function { a, b, c }`, invoked
            # elsewhere as `Call ThisScript a, b, c`.  This is a plain global
            # function, so it maps directly onto a Papyrus global Function whose
            # parameters take the declared script variables' types.  The params
            # must become real function arguments (not the auto-properties the
            # variable pass emitted) or the caller has no way to pass them.
            if block_type == 'function':
                udf_params = _split_udf_params(block_filter)
                udf_body = list(block_lines)
                continue

            # `begin MenuMode <id>` fires ONLY while that specific menu is open
            # (1014 = lockpicking, 1030 = class menu, 1002 = inventory, ...).
            # Skyrim has no per-menu equivalent — Utility.IsInMenuMode() is only
            # "some menu is open" — so there is nothing to convert the trigger to.
            # These bodies used to be merged into the GameMode OnUpdate loop with
            # NO guard at all, which meant they ran on the very first tick as if
            # every menu were open simultaneously.  MQ01Script is the worst case:
            # its MenuMode 1014 and 1030 blocks do `setstage MQ01 70` / `84`
            # unconditionally, so the tutorial quest blew through its whole stage
            # machine the moment a new game started and hit stage 100's
            # `stopquest MQ01` — the "MQ01 starts then immediately fails" bug.
            # Commenting the body out is the honest conversion: the trigger cannot
            # be reproduced, so it must not fire, and the source stays visible for
            # anyone hand-porting it to a Papyrus menu hook.
            # EXCEPTION — the sleep-detection idiom: a BARE `begin MenuMode`
            # whose body reads isPCSleeping.  In Oblivion the only frames where
            # isPCSleeping==1 are sleep-menu frames, so these bodies are
            # self-gated and exist purely to observe the player sleeping
            # (Rufio's murder, vampirism onset, MG04's inn ambush, bed
            # disease...).  Skyrim's native equivalent is RegisterForSleep():
            # the body runs once in OnSleepStart and once in OnSleepStop (the
            # two observable "frames" of a Skyrim sleep) with a script-managed
            # TES4_PCSleeping flag standing in for isPCSleeping.  Menu-id
            # blocks and non-sleep bare blocks stay commented (below).
            # SECOND EXCEPTION — a bare `begin MenuMode` that does NOT read
            # isPCSleeping.  The blowout above was caused entirely by the
            # menu-ID form: all five MQ01 blocks carry an id (1, 1002, 1014,
            # 1023, 1030), and censused over the corpus not one bare block is
            # a menu-specific trigger.  What the 20 bare bodies actually are is
            # time-and-inventory bookkeeping that Oblivion runs on the frames
            # where GameMode does NOT run — the wait/sleep and inventory
            # frames.  Several say so in their own comments (ErthorScript:
            # "contingency if player is waiting/resting"; SE02OrcCaptainScript
            # guards on `isTimePassing`), and the innkeeper rent timers
            # (7 near-identical Publican* scripts) can only ever advance while
            # a menu is open.  Dropping them silently deletes that logic:
            # MelisandeScript's body holds the ONLY `set MS40.cureready to 1`
            # in the whole plugin, so MS40's vampirism cure could never be
            # handed over, and Dark09RetirementScript's holds the only
            # `set GotFinger to 1`.
            #
            # Merging them into the GameMode poll is the faithful conversion:
            # in Oblivion the pair (GameMode + bare MenuMode) together covered
            # every frame, so a single always-running pass reproduces the union
            # rather than half of it.  Running one of these bodies on a
            # non-menu frame is harmless — they are all idempotent state
            # machines guarded by their own doonce/stage variables.
            if block_type == 'menumode':
                is_bare = not str(block_filter or '').strip()
                reads_sleep = any(_SLEEP_READ_RE.search(l) for l in block_lines)
                if is_bare and reads_sleep:
                    sleep_menumode_blocks.append(block_lines)
                elif is_bare:
                    gamemode_body.extend(block_lines)
                else:
                    menumode_blocks.append((block_filter, block_lines))
                continue

            # Merge blocks by their target Papyrus event name, not TES4 block type
            # This prevents duplicate events (e.g. onadd+ondrop→OnContainerChanged)
            mapping = BLOCK_MAP.get(block_type)
            merge_key = mapping[0] if mapping else block_type
            if merge_key not in merged_blocks:
                block_order.append(merge_key)
            guard = self._block_filter_guard(block_type, block_filter)
            # OnAlarm/OnStartCombat both land on OnCombatStateChanged, which
            # ALSO fires when combat ends (state 0).  Gate each body on the
            # state its TES4 block meant: alarm = combat or searching, start
            # combat = combat begins.  Without this every OnStartCombat body
            # re-ran when the fight ended.
            state_guard = {'onalarm': 'aeCombatState != 0',
                           'onstartcombat': 'aeCombatState == 1'}.get(block_type)
            if state_guard and guard is not None:
                guard = f'{state_guard} && {guard}' if guard else state_guard
            merged_blocks[merge_key].append((guard, block_lines))

        for merge_key in block_order:
            segments = merged_blocks[merge_key]
            # merge_key is already the event_begin string (or the block_type if unmapped)
            self._current_event = merge_key
            commented = not merge_key.startswith('Event ')
            if commented:
                out.append(merge_key if merge_key.startswith(';')
                           else f';TODO: Unknown event block: {merge_key}')
            else:
                out.append(merge_key)

            for guard, block_lines in segments:
                body = []
                for bline in block_lines:
                    body.append(self._convert_line(bline, extends))
                if commented:
                    # Unsupported event — comment out all code to avoid
                    # top-level errors.  The guard is meaningless here.
                    for converted in body:
                        out.append(f'  ;{converted}')
                    continue
                if guard is None:
                    # The TES4 filter exists but cannot be expressed; running
                    # the body for EVERY event would be wrong (see
                    # _block_filter_guard), so keep it visible-but-inert.
                    out.append('  ; TES4 block filter could not be converted; '
                               'body preserved but NOT executed:')
                    for converted in body:
                        out.append(f'  ;{converted}')
                elif guard:
                    out.append(f'  If {guard}')
                    for converted in body:
                        out.append(f'    {converted}')
                    out.append('  EndIf')
                else:
                    for converted in body:
                        out.append(f'  {converted}')

            if not commented:
                out.append('EndEvent')
            out.append('')

            # TES4 `begin OnTrigger` ALSO has to fire on the crossing frame.
            #
            # Skyrim's OnTrigger is the repeat event, so it stays the body's
            # home (Nehrim's Magieverbot counters need repeat semantics, and
            # remapping to OnTriggerEnter froze them -- see BLOCK_MAP).  But
            # the engine does NOT deliver OnTrigger for a fast crossing: every
            # vanilla trap trigger implements OnTriggerEnter instead, and the
            # census is unanimous -- Tripwire.pex, PressurePlate.pex,
            # TrapTriggerBase.pex and TrapTriggerHinge.pex ALL define
            # OnTriggerEnter, and vanilla's own Tripwire does not define
            # OnTrigger at all.  Walking over a converted tripwire or pressure
            # plate therefore never ran the body.
            #
            # Emitting BOTH keeps each event's meaning: OnTriggerEnter catches
            # the entry frame, OnTrigger keeps repeating while inside.  The
            # entry event just calls the repeat one, so the body exists once
            # and both paths stay in lockstep.
            # Skipped when the script authors its own OnTriggerEnter block:
            # Papyrus allows one definition per event, and the author's own
            # body is authoritative.  (No Oblivion script does both, but a
            # third-party plugin may.)
            if (merge_key == BLOCK_MAP['ontrigger'][0]
                    and BLOCK_MAP['ontriggerenter'][0] not in merged_blocks):
                out.append('Event OnTriggerEnter(ObjectReference akActionRef)')
                out.append('  ; Entry frame: Skyrim sends OnTriggerEnter, not '
                           'OnTrigger (vanilla Tripwire/PressurePlate do the '
                           'same).  Repeat ticks still arrive on OnTrigger.')
                out.append('  OnTrigger(akActionRef)')
                out.append('EndEvent')
                out.append('')

        # Emit the OBSE user-defined function (`begin Function{a,b}`, invoked as
        # `Call ThisScript a, b`) as a Papyrus Function.  NOT Global: these
        # bodies read the script's own object properties (GlobalScriptExpGained
        # updates the EP GlobalVariable, GlobalWaitMenu moves a stored ref), and
        # a Global function cannot touch instance state.  Callers therefore go
        # through a property typed as this script — which is what the caller-side
        # `Call` rewrite emits.
        #
        # The parameters shadow the same-named auto-properties the variable pass
        # emitted, so those declarations are dropped (below): keeping both makes
        # the body's reads resolve to the property, which no caller ever writes,
        # so every call would silently act on 0.
        if udf_params or udf_body:
            self._current_event = 'Function'
            # OBSE returned a value by assigning it with SetFunctionValue and
            # then falling out via a bare `return`.  Papyrus carries the value on
            # the Return itself, so a function that uses it needs a return type
            # and each `SetFunctionValue X` + `return` pair collapses to
            # `Return X` (done in _convert_line).
            self._udf_returns = any(
                re.match(r'^\s*setfunctionvalue\b', b, re.IGNORECASE)
                for b in udf_body)
            # Convert the body BEFORE writing the signature: a TES4 `ref` is an
            # untyped handle, and the declared type alone is too weak to pick a
            # Papyrus parameter type.  GlobalScriptAddSpellIfNotOwned takes a
            # `ref` that every caller fills with a Spell and the body feeds to
            # AddSpell/HasSpell — typing it ObjectReference (the literal
            # translation of `ref`) rejects all 170 call sites.  Converting first
            # lets the usage-driven type inference run, then read the result.
            self._block_depth = 0
            udf_lines = [self._convert_line(b, extends) for b in udf_body]

            def _param_type(p: str) -> str:
                safe = _safe_property_name(p)
                declared = self._var_types.get(p.lower(), 'Int')
                inferred = (self._property_refs.get(safe)
                            or self._property_refs.get(safe.lower(), ''))
                # Only a `ref` is ambiguous enough to override; Int/Float came
                # from an explicit TES4 type and mean what they say.
                if declared == 'ObjectReference' and inferred:
                    return inferred
                # A `ref` with no usage evidence still has to accept whatever
                # callers pass — Form is the permissive Papyrus handle.
                if declared == 'ObjectReference':
                    return 'Form'
                return declared

            _param_types = {p: _param_type(p) for p in udf_params}
            # The BODY refers to a parameter by its safe (renamed) spelling —
            # TES4's `faction` becomes `myFaction` because Faction is a Papyrus
            # type name — so the downcast lookup below must find it under that
            # name too, not only the raw one.
            for _p in list(_param_types):
                _safe_p = _safe_property_name(_p)
                _param_types.setdefault(_safe_p, _param_types[_p])
                _param_types.setdefault(_safe_p.lower(), _param_types[_p])
                _param_types.setdefault(_p.lower(), _param_types[_p])
            sig = ', '.join(f'{_param_types[p]} {_safe_property_name(p)}'
                            for p in udf_params)
            rtype = 'Int ' if self._udf_returns else ''
            out.append(f'{rtype}Function {_UDF_NAME}({sig})')
            # A parameter typed `Form` (the permissive fallback for a TES4 `ref`)
            # cannot be passed where Papyrus declares a narrower type: AddSpell
            # takes a Spell, IsInFaction takes a Faction, and Form→X is a
            # downcast the compiler refuses to make implicitly.  Insert the cast
            # at the call sites in the body.
            for _i, _conv in enumerate(udf_lines):
                def _cast_arg(m, _pt=_param_types):
                    arg = m.group(2)
                    if _pt.get(arg, _pt.get(arg.lower(), '')) in (
                            'Form', 'ObjectReference'):
                        want = _UDF_ARG_DOWNCASTS[m.group(1).lower()]
                        return f'{m.group(1)}({arg} as {want}'
                    return m.group(0)
                udf_lines[_i] = _UDF_DOWNCAST_RE.sub(_cast_arg, _conv)
            # Close anything the body left open (an OBSE ref-walk's `While`,
            # whose `Goto` cannot emit the closer in place) BEFORE the
            # synthesised Return, so the fall-through return is not stranded
            # outside the loop it belongs after.
            udf_lines = self._balance_if_endif(
                [f'{rtype}Function {_UDF_NAME}({sig})'] + udf_lines
                + ['EndFunction'])[1:-1]
            for converted in udf_lines:
                out.append(f'  {converted}')
            if self._udf_returns:
                # Papyrus requires every path out of a typed function to return
                # a value; the TES4 body could simply run off the end.
                out.append('  Return 0')
            out.append('EndFunction')
            out.append('')
            self._udf_returns = False
            self._udf_return_value = ''

        # In TES4 a `begin GameMode` block on a placed object/actor reference
        # only runs while that reference is LOADED (in/near an active cell); on
        # a quest script it runs globally once the quest is running.  Auto-
        # starting an OnUpdate poll from OnInit (fires once per instance the
        # moment the save loads, for EVERY reference in the game) turned every
        # scripted object into a permanent ticker — hundreds of scripts firing
        # SetStage / ForceWeather / quest completion at once on load, which
        # floods the engine and crashes.  So:
        #   * ObjectReference/Actor scripts gate the loop on load state
        #     (OnCellAttach start → OnCellDetach stop), matching "while loaded".
        #   * Quest scripts gate the BODY on IsRunning(): in TES4 a quest
        #     script's GameMode block only executes while the quest is running,
        #     so its body may (and routinely does) assume that.  Skyrim raises
        #     OnInit on the quest object whether or not the quest ever started,
        #     and SetStage on a stopped quest STARTS it — so an ungated body
        #     silently auto-starts the quest at load (MQDragonArmor's
        #     `if gamedayspassed >= armorFinishDay` is true at day 1 vs 0).
        #   * ActiveMagicEffect keeps the plain OnInit self-start (its lifecycle
        #     IS the effect).
        load_gated = extends in ('ObjectReference', 'Actor')
        quest_gated = extends == 'Quest'

        # Emit OnUpdate for GameMode/ScriptEffectUpdate
        if gamemode_body:
            interval = self._get_update_interval()
            self._current_event = 'Event OnUpdate()'
            out.append('Event OnUpdate()')
            if quest_gated:
                out.append('  If (!IsRunning())')
                # Not running: don't execute the body, but keep polling so the
                # loop resumes on its own once the quest is started elsewhere.
                out.append(f'    RegisterForSingleUpdate({interval})')
                out.append('    Return')
                out.append('  EndIf')
            body_start = len(out)
            for bline in gamemode_body:
                converted = self._convert_line(bline, extends)
                out.append(f'  {converted}')
            # An early `return` in the body must NOT kill the polling loop.
            # TES4 `return` ends only THIS FRAME's GameMode pass — the script
            # runs again next frame. Papyrus OnUpdate is one-shot and
            # self-rescheduling, so a Return that skips the trailing
            # RegisterForSingleUpdate stops the script for the rest of the game.
            # The `if GetStage X < 10 / return` early-out is a standard Oblivion
            # idiom, so this silently disabled quest drivers the moment they
            # took the guard once (MG01/MG02/MG05/MG06/MG08/MG12/MG17/MG18...).
            self._reregister_before_returns(out, body_start, interval,
                                            load_gated)
            if load_gated:
                # Only keep ticking while still loaded (OnCellDetach clears it).
                out.append(f'  If ({self._GAMEMODE_GATE})')
                out.append(f'    RegisterForSingleUpdate({interval})')
                out.append('  EndIf')
            else:
                out.append(f'  RegisterForSingleUpdate({interval})')
            out.append('EndEvent')
            out.append('')

        # Sleep-idiom MenuMode bodies become real Papyrus sleep listeners.
        # Oblivion ran the body every menu frame while the player slept; the
        # two Skyrim-observable moments of a sleep are its start and stop
        # events, so the body runs once in each (several bodies need two
        # passes: MG04 records GameHour on the first and arms its trigger on
        # the second).  isPCSleeping reads inside the body compile to the
        # TES4_PCSleeping flag, which is 1 for both passes — matching
        # Oblivion, where every frame that executed the body had
        # isPCSleeping==1.  Registration rides the same lifecycle as the
        # OnUpdate loop (OnCellAttach/OnInit below).
        if sleep_menumode_blocks:
            self._current_event = 'Function TES4_MenuModeSleepBody()'
            out.append('Int TES4_PCSleeping = 0')
            out.append('')
            out.append('Function TES4_MenuModeSleepBody()')
            if quest_gated:
                out.append('  If (!IsRunning())')
                out.append('    Return')
                out.append('  EndIf')
            self._in_sleep_menumode = True
            for block_lines in sleep_menumode_blocks:
                for bline in block_lines:
                    out.append(f'  {self._convert_line(bline, extends)}')
            self._in_sleep_menumode = False
            out.append('EndFunction')
            out.append('')
            out.append('Event OnSleepStart(float afSleepStartTime, float afDesiredSleepEndTime)')
            out.append('  TES4_PCSleeping = 1')
            out.append('  TES4_MenuModeSleepBody()')
            out.append('EndEvent')
            out.append('')
            out.append('Event OnSleepStop(bool abInterrupted)')
            out.append('  TES4_MenuModeSleepBody()')
            out.append('  TES4_PCSleeping = 0')
            out.append('EndEvent')
            out.append('')

        # MenuMode bodies, preserved as comments (see the block loop above for
        # why they must not execute).  Converted rather than dumped raw so a
        # hand-port only has to supply the menu hook, not redo the translation.
        for menu_id, block_lines in menumode_blocks:
            label = f'MenuMode {menu_id}'.strip()
            out.append(f'; --- TES4 `begin {label}` — no Skyrim equivalent; '
                       'body preserved but NOT executed ---')
            for bline in block_lines:
                converted = self._convert_line(bline, extends)
                if converted.strip():
                    out.append(f';  {converted}')
            out.append('')

        # Start/stop the update loop (and the sleep listener, which shares the
        # same lifecycle: TES4 MenuMode also only ran while the script's owner
        # was loaded / its quest instantiated).
        needs_sleep_reg = bool(sleep_menumode_blocks)
        if needs_oninit_update or needs_sleep_reg:
            interval = self._get_update_interval()
            if load_gated:
                # Object/actor: run only while loaded.  OnCellAttach fires each
                # time the reference streams into an active cell; OnCellDetach
                # when it streams out.  This confines the loop to when the
                # object is actually present, exactly like TES4 GameMode.
                out.append('Event OnCellAttach()')
                if needs_oninit_update:
                    out.append(f'  RegisterForSingleUpdate({interval})')
                if needs_sleep_reg:
                    out.append('  RegisterForSleep()')
                out.append('EndEvent')
                out.append('')
                out.append('Event OnCellDetach()')
                if needs_oninit_update:
                    out.append('  UnregisterForUpdate()')
                if needs_sleep_reg:
                    out.append('  UnregisterForSleep()')
                out.append('EndEvent')
                out.append('')
                # OnCellAttach only fires when a cell BECOMES attached.  A
                # persistent actor standing in an already-attached cell when the
                # script is first bound (new game, or the player is simply
                # already there) never gets that event, so the poll would never
                # start and a GameMode variable the rest of the quest depends on
                # stays 0 forever.  That is what kept Arielle (MG04Restore)
                # standing still: her package waits on `startconv == 1`, which
                # only her GameMode body ever sets.
                #
                # Gating on Is3DLoaded() keeps the anti-storm property that
                # motivated dropping OnInit here: it is true ONLY for references
                # that are actually loaded, so this cannot re-create the "every
                # scripted object in the game starts ticking at load" failure —
                # an unconditional OnInit register is what did that.
                #
                # KNOWN GAP (re-opened deliberately): an initially-disabled
                # reference has no 3D, so a 3D-gated poll never starts, and on
                # ~200 Nehrim refs the poll body is the only thing that ever
                # calls Enable() on that same reference.  The cell-attachment
                # gate that fixed this (TES4Polyfill.ShouldRunGameMode) broke
                # CharacterGen and was reverted; see _GAMEMODE_GATE.
                #
                # But OnInit ALONE is not enough once the script lives on the
                # placed reference (which reference events like OnPackageEnd
                # require).  On a reference OnInit runs at load BEFORE the 3D
                # exists, so Is3DLoaded() is false and the poll never starts —
                # that is what silenced Valen Dreth.  OnLoad is the event that
                # actually means "this object is completely loaded ... fired
                # every time this object is loaded" (vanilla ObjectReference.psc),
                # so it starts the loop for an actor already standing in the
                # player's current cell, which OnCellAttach cannot do.
                if not any(b[0] == 'onload' for b in blocks):
                    out.append('Event OnLoad()')
                    if needs_oninit_update:
                        out.append(f'  RegisterForSingleUpdate({interval})')
                    if needs_sleep_reg:
                        out.append('  RegisterForSleep()')
                    out.append('EndEvent')
                    out.append('')
                if not any(b[0] == 'oninit' for b in blocks):
                    out.append('Event OnInit()')
                    out.append(f'  If ({self._GAMEMODE_GATE})')
                    if needs_oninit_update:
                        out.append(f'    RegisterForSingleUpdate({interval})')
                    if needs_sleep_reg:
                        out.append('    RegisterForSleep()')
                    out.append('  EndIf')
                    out.append('EndEvent')
                    out.append('')
            else:
                has_oninit = any(b[0] == 'oninit' for b in blocks)
                if not has_oninit:
                    out.append('Event OnInit()')
                    if needs_oninit_update:
                        out.append(f'  RegisterForSingleUpdate({interval})')
                    if needs_sleep_reg:
                        out.append('  RegisterForSleep()')
                    out.append('EndEvent')
                    out.append('')

        # TES4Polyfill.SuppressFallDamage() (the ResetFallDamageTimer
        # conversion) applies a lasting actor value, so a script that called it
        # must undo it when the effect ends or the actor keeps the damage
        # resistance for the rest of the save — the paired on/off trap in
        # docs/papyrus_conversion_notes.md.
        #
        # Runs HERE, not next to the block loop: the synthesized OnInit/OnUpdate
        # events are appended after that loop, and the teardown event has to be
        # in `out` already for the restore to land inside it.
        if self._suppressed_fall_damage:
            out = self._append_fall_damage_restore(out, extends)

        # Balance If/EndIf within event blocks (some TES4 scripts have extra EndIf)
        out = self._balance_if_endif(out)

        # Remove dead code after Return statements within event/function blocks
        out = self._remove_dead_code_after_return(out)

        # Apply shared post-processing (TES4-only functions, type mismatches, etc.)
        out = self._postprocess_lines(out)

        # A PlayerAlias script's `Self` is a ReferenceAlias, so `Self as Actor`
        # is a cast the compiler rejects outright and a bare `Self` passed where
        # an ObjectReference is wanted is the wrong object.  Every emitter above
        # routes these correctly, but the paths are many and one missed site
        # fails the whole script to compile — so normalise here as a backstop.
        if extends == PLAYER_ALIAS_EXTENDS:
            out = [self._PLAYER_ALIAS_SELF_RE.sub('GetActorReference()', ln)
                   for ln in out]

        # Post-process: retype ObjectReference variables that are only used as integers
        # TES4 'ref' type was general-purpose; scripts often used ref vars as int flags
        if _var_info:
            _ref_typed_vars = {name.lower(): idx for idx, (name, ptype) in enumerate(_var_info)
                            if ptype == 'ObjectReference'}
            if _ref_typed_vars:
                _assign_re = re.compile(r'^\s*(\w+)\s*=\s*(.+)', re.IGNORECASE)
                for var_low, vi in list(_ref_typed_vars.items()):
                    has_int_assign = False
                    has_ref_assign = False
                    has_ref_usage = False
                    for line in out[var_start_idx + len(_var_info) + 1:]:
                        # Check if variable is used as a reference (method calls, comparisons with refs)
                        if re.search(r'\b' + re.escape(var_low) + r'\.\w+\s*\(', line, re.IGNORECASE):
                            has_ref_usage = True
                            break
                        # Check for comparisons with None or ref variables
                        if re.search(r'\b' + re.escape(var_low) + r'\s*[!=]=\s*None\b', line, re.IGNORECASE):
                            has_ref_usage = True
                            break
                        # Check if variable is used as a function argument (not on LHS of =)
                        stripped = line.lstrip()
                        if not stripped.startswith(var_low) and re.search(
                                r'\(\s*' + re.escape(var_low) + r'\b', line, re.IGNORECASE):
                            has_ref_usage = True
                            break
                        am = _assign_re.match(line)
                        if not am:
                            continue
                        if am.group(1).lower() != var_low:
                            continue
                        val = am.group(2).split(';')[0].strip()
                        # Integer literal assignments (0, 1, 2, etc.)
                        if re.match(r'^-?\d+$', val):
                            has_int_assign = True
                        # None assignment (already converted from ref = 0)
                        elif val == 'None':
                            has_ref_assign = True
                        # Math expressions producing int
                        elif re.match(r'^[\w.]+ [+\-*/] \d+$', val):
                            has_int_assign = True
                        else:
                            has_ref_assign = True
                    if has_int_assign and not has_ref_assign and not has_ref_usage:
                        # Retype: ObjectReference → Int (now a value type, so it
                        # also becomes visible to the condition system)
                        decl_idx = var_start_idx + vi
                        if decl_idx < len(out):
                            out[decl_idx] = out[decl_idx].replace(
                                'ObjectReference Property', 'Int Property', 1)
                            if not out[decl_idx].rstrip().endswith('Conditional'):
                                out[decl_idx] = out[decl_idx].rstrip() + ' Conditional'
                            if not out[0].rstrip().endswith('Conditional'):
                                out[0] = out[0].rstrip() + ' Conditional'
                            real_name = _var_info[vi][0]
                            self._var_types[real_name.lower()] = 'Int'
                            # Also replace = None back to = 0 in body
                            none_re = re.compile(
                                r'^(\s*' + re.escape(real_name) + r'\s*=\s*)None\b',
                                re.IGNORECASE)
                            for bidx in range(var_start_idx + len(_var_info) + 1, len(out)):
                                if none_re.match(out[bidx]):
                                    out[bidx] = none_re.sub(r'\g<1>0', out[bidx])

                # TES4's `ref` was one type for references AND base forms, so a
                # `ref` variable routinely holds something Papyrus types
                # narrower — GetBaseObject() is a Form, GetEquippedWeapon() is a
                # Weapon.  Papyrus refuses the implicit conversion in BOTH
                # directions, so the declaration has to follow what the script
                # actually assigns.  Retype from the first such assignment.
                _typed_assign_re = re.compile(
                    r'^\s*(\w+)\s*=\s*\(?\s*.*?\b(' +
                    '|'.join(_REF_ASSIGN_SOURCE_TYPES) + r')\s*\(',
                    re.IGNORECASE)
                # Assignment from a BARE property, whose type is whatever the
                # record it names is: `let rCrosshairsLast := jailshoes` stores
                # an ARMO base record in a TES4 `ref`.  Papyrus will not put an
                # Armor in an ObjectReference, and no cast is right either
                # (a base form is not a reference) — so the VARIABLE widens to
                # Form, which legally holds both that and the real references
                # the same variable is assigned elsewhere.
                _bare_assign_re = re.compile(
                    r'^\s*(\w+)\s*=\s*([A-Za-z_]\w*)\s*(?:;.*)?$')
                # Types come from _property_refs, NOT from the emitted lines:
                # the "External references" block is appended AFTER this pass,
                # so scanning `out` for declarations sees none of them and the
                # widening silently never fires.
                _known_decls = {k.lower(): v
                                for k, v in self._property_refs.items()}

                for line in out[var_start_idx + len(_var_info) + 1:]:
                    bm = _typed_assign_re.match(line)
                    want = ''
                    if bm:
                        want = _REF_ASSIGN_SOURCE_TYPES[bm.group(2).lower()]
                    else:
                        bm = _bare_assign_re.match(line)
                        if not bm:
                            continue
                        src_type = _known_decls.get(bm.group(2).lower(), '')
                        # Only a type Papyrus refuses to store in an
                        # ObjectReference forces the widening.
                        if (not src_type or src_type in (
                                'ObjectReference', 'Actor', 'Form')
                                or src_type.startswith('TES4_')):
                            continue
                        want = 'Form'
                    vi = _ref_typed_vars.get(bm.group(1).lower())
                    if vi is None:
                        continue
                    decl_idx = var_start_idx + vi
                    if decl_idx < len(out) and 'ObjectReference Property' in out[decl_idx]:
                        out[decl_idx] = out[decl_idx].replace(
                            'ObjectReference Property', f'{want} Property', 1)
                        self._var_types[_var_info[vi][0].lower()] = want

        # Pending-beat companions for Say timers this script owns.  Declared
        # after conversion because the need is discovered while converting the
        # body.  Only script-LOCAL timers get a declaration here; a
        # `SomeQuest.convTimer` beat lives on that quest's own script.
        # A beat written as `SomeQuest.convTimer` needs its companion declared
        # on SomeQuest's script, not here.  `beat_fields_by_owner` is built once
        # by the pipeline (a whole-export scan) and shipped to every worker, so
        # the declaration does not depend on which script converted first.
        #
        # Dedupe CASE-INSENSITIVELY: Papyrus identifiers are case-insensitive,
        # and the two sources disagree on casing (this script's own writes keep
        # the source's `sayLength`, the export scan lowercases), so a
        # case-sensitive set declared both and the compiler rejected the file
        # with "property with `saylengthPendingBeat` name already exists".
        beat_locals = []
        _beat_seen = set()
        for field in (sorted(t for t in self._say_beat_targets if '.' not in t)
                      + sorted(self.beat_fields_by_owner.get(_edid_low, ()))):
            if field.lower() not in _beat_seen:
                _beat_seen.add(field.lower())
                beat_locals.append(field)
        if beat_locals:
            decls = [f'Float Property {self.beat_property(t)} = 0.0 Auto'
                     '  ; pause to run after the current Say line ends'
                     for t in beat_locals]
            out[var_start_idx + len(_var_info):
                var_start_idx + len(_var_info)] = decls

        # Post-process: a TES4 `ref` variable that is only ever assigned BASE
        # FORMS of one kind is not an ObjectReference at all.  NQ15W02Turret01
        # declares `ref SelectedSpell` and assigns SPEL records to it, which
        # Papyrus rejects ("value with type Spell cannot be assigned to a
        # variable with type ObjectReference").  Retype the declaration to the
        # assigned form's own Papyrus type when every assignment agrees.
        if _var_info and self.xref:
            # Papyrus functions whose return type is not ObjectReference, so a
            # TES4 `ref` variable holding one needs that type instead.
            _RET_TYPES = {'getparentcell': 'Cell'}
            _assigned: dict[str, set] = {}
            for line in out:
                am = re.match(r'^\s*(\w+)\s*=\s*(.+?)\s*(?:;.*)?$', line)
                if not am:
                    continue
                vlow = am.group(1).lower()
                val = am.group(2).strip()
                # `x = None` clears the variable and is assignable to EVERY
                # object type, so it says nothing about what the variable holds.
                # Counting it as an unknown type made the set non-unanimous and
                # blocked the retype (soulGemRef ends with exactly this line).
                if val == 'None':
                    continue
                # A call: take the type from the LAST method in the chain.
                call_m = re.search(r'(\w+)\(\s*\)\s*$', val)
                if call_m:
                    ptype = _RET_TYPES.get(call_m.group(1).lower(), '')
                    _assigned.setdefault(vlow, set()).add(ptype)
                    continue
                if not re.match(r'^\w+$', val):
                    _assigned.setdefault(vlow, set()).add('')
                    continue
                # Resolve exactly as the property binder does. A bare
                # edid_to_formid lookup misses the sanitized spellings it
                # handles -- `0probeUbent` is emitted as the property
                # `probeUbent`, so the plain lookup found nothing, the assigned
                # type came back unknown, and the retype was blocked even
                # though the property itself had bound as MiscObject.
                fid = resolve_property_formid(self.xref, val)
                rtype = self.xref.record_type.get(fid, '') if fid else ''
                # Only BASE records retype; a placed ref really is a reference.
                ptype = (_record_type_to_base_papyrus(rtype)
                         if rtype and rtype not in ('ACHR', 'ACRE', 'REFR')
                         else '')
                _assigned.setdefault(vlow, set()).add(ptype)
            for idx in range(var_start_idx, var_start_idx + len(_var_info)):
                if idx >= len(out):
                    break
                line = out[idx]
                # `Form Property` is the cross-script ref-as-base-form
                # pre-declaration (see ref_as_base_form). It is the right
                # FALLBACK, but a UNANIMOUS specific base type still upgrades
                # it — stockFX only ever holds EFSH records, and as a bare
                # Form its `.Play(...)` no longer compiled.
                gen = next((g for g in ('ObjectReference Property ',
                                        'Form Property ') if g in line), None)
                if gen is None:
                    continue
                pname = line.split('Property ', 1)[1].split()[0]
                types = _assigned.get(pname.lower(), set())
                # Unanimous, non-empty, and not already ObjectReference/Form.
                if len(types) == 1:
                    only = next(iter(types))
                    if only and only not in ('ObjectReference', 'Form'):
                        out[idx] = line.replace(gen, f'{only} Property ', 1)
                        self._var_types[pname.lower()] = only
                        self._property_refs[pname] = only
                elif gen == 'ObjectReference Property ' and len(types) > 1 \
                        and all(t in _BASE_OBJECT_PAPYRUS for t in types):
                    # A TES4 `ref` that holds SEVERAL different base objects --
                    # mwShrineGhostgateScript's soulGemRef takes any of 12 soul
                    # gems, moXscrXtrapXwritedynamicdata's takes several MISCs.
                    # No single item class covers them, and ObjectReference is
                    # WRONG (these are base records, not placed refs), so the
                    # assignment fails the checker. Form is their common
                    # supertype and is what the item functions accept.
                    out[idx] = line.replace('ObjectReference Property ',
                                            'Form Property ', 1)
                    self._var_types[pname.lower()] = 'Form'
                    self._property_refs[pname] = 'Form'

        # Post-process: upgrade ObjectReference/Actor variables to more specific types
        # based on usage (Actor from actor-only functions, or script type from SCRO/xref)
        if _var_info and self._property_refs:
            # Build case-insensitive lookup for type upgrades
            _ci_refs = {k.lower(): v for k, v in self._property_refs.items()}
            for idx in range(var_start_idx, var_start_idx + len(_var_info)):
                if idx >= len(out):
                    break
                line = out[idx]
                # Upgrade ObjectReference → script type or Actor
                if 'ObjectReference Property ' in line:
                    parts = line.split('Property ', 1)
                    if len(parts) >= 2:
                        prop_name = parts[1].split()[0]
                        new_type = _ci_refs.get(prop_name.lower(), '')
                        if new_type and new_type != 'ObjectReference':
                            out[idx] = line.replace('ObjectReference Property ',
                                                    f'{new_type} Property ', 1)
                            self._var_types[prop_name.lower()] = new_type
                # Upgrade Actor → TES4_ script type when cross-script property access needed
                elif 'Actor Property ' in line:
                    parts = line.split('Property ', 1)
                    if len(parts) >= 2:
                        prop_name = parts[1].split()[0]
                        new_type = _ci_refs.get(prop_name.lower(), '')
                        if new_type and new_type.startswith('TES4_'):
                            out[idx] = line.replace('Actor Property ',
                                                    f'{new_type} Property ', 1)
                            self._var_types[prop_name.lower()] = new_type

        # Post-process: add 'as Actor' casts for ObjRef-returning assignments to Actor vars
        _actor_vars = {k.lower() for k, v in self._property_refs.items() if v == 'Actor'}
        _actor_vars |= {k for k, v in self._var_types.items() if v == 'Actor'}
        if _actor_vars:
            _objref_re = self._OBJREF_RETURNING
            _objref_params = self._OBJREF_PARAMS
            # Build set of variables known to be ObjectReference
            _objref_vars = {k for k, v in self._var_types.items() if v == 'ObjectReference'}
            _objref_vars |= {k.lower() for k, v in self._property_refs.items() if v == 'ObjectReference'}
            for idx in range(len(out)):
                line = out[idx]
                s = line.lstrip()
                # Match: VarName = expr  (not already cast)
                eq_m = re.match(r'^(\w+)\s*=\s*(.+)', s)
                if not eq_m:
                    continue
                var_name = eq_m.group(1)
                val = eq_m.group(2).rstrip()
                if var_name.lower() not in _actor_vars:
                    continue
                if 'as Actor' in val:
                    continue
                # Strip inline comments for checking
                val_check = val.split(';')[0].strip() if ';' in val else val
                needs_cast = False
                if _objref_re.search(val_check):
                    needs_cast = True
                elif val_check.lower().strip('() ') in _objref_params:
                    needs_cast = True
                elif val_check.lower() in _objref_vars:
                    needs_cast = True
                elif val_check == 'Self' and extends != 'Actor':
                    needs_cast = True
                elif '.' in val_check and self._is_ref_typed_access(val_check):
                    needs_cast = True
                if needs_cast:
                    indent = line[:len(line) - len(s)]
                    # Insert 'as Actor' before any inline comment
                    if ';' in val and not val.startswith(';'):
                        code_part = val.split(';')[0].rstrip()
                        comment_part = ';' + val.split(';', 1)[1]
                        out[idx] = f'{indent}{var_name} = {code_part} as Actor  {comment_part}'
                    else:
                        out[idx] = f'{indent}{var_name} = {val} as Actor'

        # Post-process: cast ObjRef-typed args to Actor in actor-parameter functions
        _actor_param_funcs = re.compile(
            r'\b(?:StartCombat|IsDetectedBy|PushActorAway|SendAssaultAlarm|GetRelationshipRank'
            r'|SetRelationshipRank|SetPlayerTeammate)\s*\(',
            re.IGNORECASE)
        _all_objref = _objref_vars if '_objref_vars' in dir() else set()
        _all_objref |= {k for k, v in self._var_types.items() if v == 'ObjectReference'}
        _all_objref |= {k.lower() for k, v in self._property_refs.items() if v == 'ObjectReference'}
        if _all_objref:
            for idx in range(len(out)):
                line = out[idx]
                if not _actor_param_funcs.search(line):
                    continue
                # Replace ObjRef variables with 'var as Actor' in function args
                for var in _all_objref:
                    # Match var as a whole word inside parentheses, not already cast
                    pattern = r'(\b' + re.escape(var) + r')(\b)(?!\s+as\s+Actor)'
                    if re.search(pattern, line, re.IGNORECASE):
                        line = re.sub(pattern, r'\1 as Actor\2', line, flags=re.IGNORECASE)
                out[idx] = line

        # Post-process: convert integer literal assignments to None for ref-typed variables
        # Handles both local (someActorVar = 0) and cross-script (Quest.Var = 1)
        if self._property_refs or self._var_types:
            _ref_types = ('ObjectReference', 'Actor', 'ActorBase')
            _assign_int_re = re.compile(r'^(\s*)([\w.]+)\s*=\s*(-?\d+)\s*(;.*)?$')
            for idx in range(len(out)):
                m = _assign_int_re.match(out[idx])
                if not m:
                    continue
                tgt = m.group(2)
                int_val = m.group(3)
                is_ref = False
                if '.' in tgt:
                    # Cross-script: check remote ref type via xref graph
                    parts = tgt.split('.', 1)
                    if self.xref and self.xref.is_remote_ref_var(parts[0], parts[1]):
                        is_ref = True
                else:
                    low = tgt.lower()
                    vtype = self._var_types.get(low, '')
                    if not vtype:
                        vtype = self._property_refs.get(tgt, self._property_refs.get(low, ''))
                    if vtype in _ref_types or vtype.startswith('TES4_'):
                        is_ref = True
                if is_ref:
                    cmt = m.group(4) or ''
                    out[idx] = f'{m.group(1)}{tgt} = None  {cmt}'.rstrip()

        # Post-process: add 'as Int' for cross-script Float args in item count functions
        # (RemoveItem/AddItem count param should be Int but cross-script may be Float)
        _item_count_re = re.compile(
            r'(\.(RemoveItem|AddItem)\s*\(\s*\w+\s*,\s*)(\w+\.\w+)(\s*\))',
            re.IGNORECASE)
        for idx in range(len(out)):
            m = _item_count_re.search(out[idx])
            if m and ' as Int' not in m.group(3):
                out[idx] = out[idx][:m.start(3)] + m.group(3) + ' as Int' + out[idx][m.end(3):]

        # Post-process: fix conditions containing embedded comments that break parsing
        # e.g. "If (False  ;comment == 0)" → the ; eats the ==0 part
        for idx in range(len(out)):
            out[idx] = _repair_commented_condition(out[idx])

        # Post-process: fix assignments where RHS contains embedded comment that eats operators
        # e.g. "temp = (False  ;comment == 0)" → just the comment
        for idx in range(len(out)):
            line = out[idx]
            assign_m = re.match(r'^(\s*)(\w[\w.]*)\s*=\s*(.*)$', line)
            if assign_m:
                rhs = assign_m.group(3)
                semi_pos = rhs.find(';')
                if semi_pos >= 0:
                    # Check if there's meaningful code after the comment that was eaten
                    after_semi = rhs[semi_pos+1:]
                    if re.search(r'==|!=|>=|<=|>|<|&&|\|\||\)', after_semi):
                        out[idx] = f'{assign_m.group(1)}{rhs[semi_pos:]}'

        # Post-process: remove spurious commas from conditions
        # e.g. "if((, expr, == , 1, ))" → "if(expr == 1)"
        for idx in range(len(out)):
            line = out[idx]
            if ',  ==' in line or ', ==' in line or '==,' in line or '== ,' in line:
                # Strip commas that are not inside string literals
                cleaned = re.sub(r',\s*', ' ', line)
                # Collapse multiple spaces
                cleaned = re.sub(r'  +', ' ', cleaned)
                # Restore indentation
                indent = len(line) - len(line.lstrip())
                cleaned = line[:indent] + cleaned.lstrip()
                out[idx] = cleaned

        # Post-process: fix "None as Int/Float" casts (can't cast None to Int/Float)
        # These arise when a TODO function returns None but variable is Int/Float
        for idx in range(len(out)):
            line = out[idx]
            if 'None as Int' in line:
                out[idx] = line.replace('None as Int', '0')
            elif 'None as Float' in line:
                out[idx] = line.replace('None as Float', '0.0')

        # Post-process: promote local variables used across events to properties
        # TES4 locals are script-scoped; Papyrus locals are event-scoped
        _event_re = re.compile(r'^\s*Event\s+(\w+)', re.IGNORECASE)
        _endevent_re = re.compile(r'^\s*EndEvent\b', re.IGNORECASE)
        _local_decl_re = re.compile(r'^(\s*)(Int|Float|Bool|String|ObjectReference|Actor)\s+(\w+)\s*=', re.IGNORECASE)
        _local_use_re = {}  # Populated per-variable below
        # Pass 1: find local declarations and their owning events
        event_locals = {}  # var_name_lower -> (event_name, decl_line_idx, type, indent)
        current_event = None
        for idx, line in enumerate(out):
            em = _event_re.match(line)
            if em:
                current_event = em.group(1)
                continue
            if _endevent_re.match(line):
                current_event = None
                continue
            if current_event:
                dm = _local_decl_re.match(line)
                if dm:
                    vname = dm.group(3)
                    vtype = dm.group(2)
                    event_locals[vname.lower()] = (current_event, idx, vtype, dm.group(1))
        # Pass 2: find variables used in events OTHER than where declared
        promote = {}  # var_name_lower -> (vtype, decl_idx)
        if event_locals:
            for var_low, (decl_event, decl_idx, vtype, indent) in event_locals.items():
                current_event = None
                for idx, line in enumerate(out):
                    em = _event_re.match(line)
                    if em:
                        current_event = em.group(1)
                        continue
                    if _endevent_re.match(line):
                        current_event = None
                        continue
                    if current_event and current_event != decl_event:
                        if re.search(r'\b' + re.escape(var_low) + r'\b', line, re.IGNORECASE):
                            promote[var_low] = (vtype, decl_idx)
                            break
        # Pass 2b: promote variables accessed from OTHER scripts (cross-script access)
        if event_locals and self.xref and _edid_low:
            cross_vars = self.xref.cross_script_vars.get(_edid_low, set())
            for var_low in cross_vars:
                if var_low in event_locals and var_low not in promote:
                    _, decl_idx, vtype, _ = event_locals[var_low]
                    promote[var_low] = (vtype, decl_idx)
        # Promote: remove local declaration, add as property at top
        _promoted_props = []
        for var_low, (vtype, decl_idx) in promote.items():
            # Comment out the local declaration
            out[decl_idx] = f';{out[decl_idx].lstrip()}  ;promoted to property'
            _promoted_props.append((_safe_property_name(var_low), vtype))

        # Insert property declarations for referenced FormIDs
        if self._property_refs or _promoted_props:
            # Collect declared variable names (case-insensitive) to avoid collisions
            declared = {v[0].lower() for v in _var_info}
            insert_idx = 3 + len(_var_info) + (1 if _var_info else 0)
            prop_lines = []
            # Insert promoted local→property declarations first
            for pname, ptype in _promoted_props:
                if pname.lower() not in declared:
                    default = ' = 0.0' if ptype == 'Float' else (' = None' if ptype in ('ObjectReference', 'Actor') else '')
                    prop_lines.append(f'{ptype} Property {pname} Auto')
                    declared.add(pname.lower())
            if self._property_refs:
                prop_lines.append('; --- External references (auto-linked via VMAD) ---')
                # Merge case-variant keys: prefer the most specific type (non-Quest wins)
                _merged: dict[str, tuple[str, str]] = {}
                for pname, ptype in sorted(self._property_refs.items()):
                    key = pname.lower()
                    if key in _merged:
                        _, ex_type = _merged[key]
                        if ex_type == 'Quest' and ptype != 'Quest':
                            _merged[key] = (pname, ptype)
                    else:
                        _merged[key] = (pname, ptype)
                for pname, ptype in sorted(_merged.values(), key=lambda x: x[0].lower()):
                    safe_name = _safe_property_name(pname)
                    if safe_name.lower() in declared:
                        continue  # skip if already declared as a variable
                    declared.add(safe_name.lower())
                    prop_lines.append(f'{ptype} Property {safe_name} Auto')
            prop_lines.append('')
            for i, pl in enumerate(prop_lines):
                out.insert(insert_idx + i, pl)

        out.extend(self._emit_cell_family_helpers())
        out.extend(self._emit_button_helpers())
        return '\n'.join(out)

    def _mesg_for_box(self, text, buttons) -> str:
        """The planned MESG EDID for a button-MessageBox call site, matched by
        content (blocks can convert out of source order — MenuMode merges into
        the GameMode poll — so positional matching would misnumber duplicate
        texts). Returns '' when this context has no plan (fragments) or the
        site is not in it."""
        plan = self.message_menus.get((self._current_script_edid or '').lower())
        if not plan:
            return ''
        for name, ptext, pbuttons in plan:
            if name in self._msgbox_used:
                continue
            if ptext == text and list(pbuttons) == list(buttons):
                self._msgbox_used.add(name)
                return name
        return ''

    def _emit_button_helpers(self) -> list:
        """The shared state behind the button-MessageBox conversion: Show()
        writes the clicked index here, and the converted GetButtonPressed
        reads it back through the consumer — once, then -1 again, which is
        TES4's own contract and what keeps every `if button == N` poll from
        re-firing forever on a stale index."""
        if not getattr(self, '_uses_msg_buttons', False):
            return []
        return [
            '',
            'Int TES4_MsgButton = -1',
            '',
            '; Displaying a box resets the pressed state (TES4: GetButtonPressed',
            '; reads -1 from display until the click), then Show() parks this',
            '; thread on the box and its return lands in TES4_MsgButton.',
            'Int Function TES4_ShowMsg(Message TES4_akMsg)',
            '  TES4_MsgButton = -1',
            '  Return TES4_akMsg.Show()',
            'EndFunction',
            '',
            'Int Function TES4_TakeMsgButton()',
            '  Int TES4_taken = TES4_MsgButton',
            '  TES4_MsgButton = -1',
            '  Return TES4_taken',
            'EndFunction',
        ]

    def _emit_cell_family_helpers(self) -> list:
        """Helper functions for the GetInCell prefix families used by a script.

        TES4 matches GetInCell on an EditorID prefix, so one call can mean "in
        any of these 86 cells" — see the GetInCell handler in _emit_function.
        """
        if not self._cell_families:
            return []
        lines = ['']
        for entry in sorted(self._cell_families.values(),
                            key=lambda kv: kv[0].lower()):
            key, cells = entry[0], entry[1]
            exterior = entry[2] if len(entry) > 2 else []
            lines.append(
                f'; TES4 `GetInCell {key}` matched {len(cells)} interior and '
                f'{len(exterior)} exterior cells by EditorID prefix.')
            lines.append(
                f'Bool Function TES4_IsIn{key}(ObjectReference akRef)')
            # `parent` is taken in this scope (the CK compiler rejects it with
            # "function variable parent already defined"), hence the prefix.
            lines.append('  Cell TES4_parentCell = akRef.GetParentCell()')
            # Papyrus has no line-continuation, and a several-hundred-term
            # expression on one line is unreadable, so test-and-return instead.
            for c in cells:
                lines.append(f'  If TES4_parentCell == {c}')
                lines.append('    Return true')
                lines.append('  EndIf')
            if exterior:
                # An exterior cell cannot be a bound Cell property, so match it
                # by the position that identifies it: same worldspace, same
                # 4096-unit grid square. GetPositionX/Y are world units;
                # floor-divide to the cell grid the same way the engine does.
                lines.append('  WorldSpace TES4_ws = akRef.GetWorldSpace()')
                lines.append('  Float TES4_fx = akRef.GetPositionX() / 4096.0')
                lines.append('  Float TES4_fy = akRef.GetPositionY() / 4096.0')
                lines.append('  Int TES4_gx = TES4_fx as Int')
                lines.append('  Int TES4_gy = TES4_fy as Int')
                # `as Int` truncates toward zero; the grid floors. Correct only
                # when truncation actually rounded UP, i.e. the value was
                # negative and not already exact (-4096.0 is cell -1, not -2).
                lines.append('  If TES4_fx < 0.0 && TES4_fx != (TES4_gx as Float)')
                lines.append('    TES4_gx = TES4_gx - 1')
                lines.append('  EndIf')
                lines.append('  If TES4_fy < 0.0 && TES4_fy != (TES4_gy as Float)')
                lines.append('    TES4_gy = TES4_gy - 1')
                lines.append('  EndIf')
                for wrld, x, y in exterior:
                    if not wrld:
                        continue
                    if x is None or y is None:
                        # Worldspace dummy cell: anywhere in the worldspace.
                        lines.append(f'  If TES4_ws == {wrld}')
                    else:
                        lines.append(
                            f'  If TES4_ws == {wrld} && TES4_gx == {x} '
                            f'&& TES4_gy == {y}')
                    lines.append('    Return true')
                    lines.append('  EndIf')
            lines.append('  Return false')
            lines.append('EndFunction')
            lines.append('')
        return lines

    def convert_fragment(self, source: str, extends: str = 'Quest') -> list[str]:
        """Convert a script fragment body (not a full script).

        Returns list of converted lines (indented for function body).
        Preserves _property_refs across calls (quest fragments share a converter).
        """
        # Reset conversion state but preserve accumulated property_refs and the
        # GetInCell families they go with — the caller emits both AFTER all
        # fragments are converted, so a reset here would drop the helper a
        # fragment body already called (undefined function TES4_IsIn...).
        saved_refs = dict(self._property_refs)
        saved_families = dict(self._cell_families)
        self._reset()
        self._cell_families = saved_families
        self._property_refs = saved_refs
        lines = source.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        result = []
        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped:
                result.append('')
                continue
            low = stripped.lower()
            if low.startswith('scriptname ') or low.startswith('scn '):
                continue
            if re.match(r'^(string_var|array_var|short|long|int|float|ref|reference)\s+\w+', stripped, re.IGNORECASE):
                m = re.match(r'^(string_var|array_var|short|long|int|float|ref|reference)\s+(\w+)', stripped, re.IGNORECASE)
                if m:
                    ptype = TYPE_MAP.get(m.group(1).lower(), 'Int')
                    vname = m.group(2)
                    if ptype == 'Float':
                        result.append(f'  {ptype} {vname} = 0.0')
                    else:
                        result.append(f'  {ptype} {vname} = 0')
                continue
            if low.startswith('begin ') or low == 'end':
                continue
            result.append(f'  {self._convert_line(raw_line, extends)}')
        # Apply shared post-processes to fragment lines
        result = self._postprocess_lines(result)
        return result

    def _postprocess_lines(self, lines: list[str]) -> list[str]:
        """Shared post-processing for both standalone and fragment scripts."""
        # Oblivion's measure-then-deliver idiom speaks a line ONCE:
        #     Set InfoLength to ArmandRef.Say TG01Armand1     ; returns duration
        #     ArmandRef.SayTo Player TG01Armand1              ; delivers to listener
        # Both TES4 functions speak, so the author's pair relies on the engine
        # collapsing them; converted literally it became two identical
        # `ref.Say(topic)` calls in a row and every such line played TWICE
        # (Armand's whole TG01 briefing, the SE07A Sheogorath/Thadon endgame,
        # SE03's chamber chatter — 92 pairs in all).  The SayTo carries the
        # listener, so it is the one to keep; drop the measuring Say, whose
        # only real output — the timer charge on the preceding line — stays.
        # The measuring half arrives as a timer-charge line with the Say glued on
        # after an embedded newline ("t = 9.25  ;line length\n  ref.Say(topic)"),
        # so flatten to real lines before pairing them up.
        _say_call_re = re.compile(r'^(\s*)(\S+\.Say\((?:[^()]*)\))\s*$')
        _flat = []
        for line in lines:
            _flat.extend(line.split('\n')) if '\n' in line else _flat.append(line)
        # The delivery is usually the next line, but the author may slip a
        # Look/SetLookAt between the two halves (SE07A's Sheogorath/Thadon
        # exchange), so scan a short window.  Stop at anything that changes
        # control flow or re-arms the timer, so two Says that genuinely belong
        # to different beats are never collapsed.
        _dedup_window = 3
        _stop_re = re.compile(
            r'^\s*(If|ElseIf|Else|EndIf|While|EndWhile|Return|'
            r'Event|EndEvent|Function|EndFunction)\b|;line length',
            re.IGNORECASE)
        _skip = set()
        for idx, line in enumerate(_flat):
            m = _say_call_re.match(line)
            if not m:
                continue
            for j in range(idx + 1, min(idx + 1 + _dedup_window, len(_flat))):
                if _stop_re.match(_flat[j]):
                    break
                nxt = _say_call_re.match(_flat[j])
                if nxt and nxt.group(2) == m.group(2):
                    # Same speaker+topic again: `idx` is the measuring half of
                    # the measure-then-deliver pair.  Drop it, keep `j`.
                    _skip.add(idx)
                    break
        lines = [l for i, l in enumerate(_flat) if i not in _skip]
        # SetDestroyed(1) must not CANCEL the clip that was started just above
        # it.  TES4 pairs the two constantly -- `playgroup forward 0` then
        # `setDestroyed 1` (CTrigTripwire01SCRIPT, CTrapLogs01SCRIPT,
        # CTrapCaveIn01SCRIPT, MPlanksBreakAway01Script) -- because in Oblivion
        # setDestroyed on a record with no destruction data only blocked
        # re-activation.  Oblivion ships ZERO DEST subrecords (censused across
        # the whole export: 0 in ACTI), so no converted record has a destroyed
        # state to switch TO -- but Skyrim's SetDestroyed still resets the
        # reference's 3D, which kills the NiControllerSequence that started one
        # line earlier.  That is why the tripwire never visibly snapped: the
        # break animation was dispatched and then immediately torn down.
        #
        # Defer the destroy past the clip instead of dropping it (it still has
        # to run -- it is what stops the trap re-triggering).  The clip length
        # is not knowable here, so hand it to the polyfill, which waits out the
        # animation on its own thread and then destroys.
        _playanim_re = re.compile(r'^(\s*)(.+?)\.PlayAnimation\("([^"]+)"\)\s*$')
        _setdestroyed_re = re.compile(
            r'^(\s*)(?:(.+?)\.)?SetDestroyed\(\s*(?:1|true)\s*\)\s*$',
            re.IGNORECASE)
        _anim_targets: dict[str, int] = {}
        for idx, line in enumerate(lines):
            pm = _playanim_re.match(line)
            if pm:
                _anim_targets[pm.group(2).strip()] = idx
                continue
            dm = _setdestroyed_re.match(line)
            if not dm:
                continue
            tgt = (dm.group(2) or 'Self').strip()
            # Only the object that was just animated is at risk; a destroy on
            # anything else is untouched.
            if tgt in _anim_targets or (tgt == 'Self' and 'Self' in _anim_targets):
                lines[idx] = (f'{dm.group(1)}TES4Polyfill.DestroyAfterAnimation('
                              f'{tgt})')
        # Consecutive PlayGroups on the SAME reference (Nehrim MQ23's loose
        # planks: Forward / Backward / Unequip in one frame) are left as plain
        # PlayAnimation calls: the last event wins and the object snaps to the
        # final clip.  That mirrors Oblivion's own queue-depth-1 PlayGroup
        # semantics (a newly queued group replaces the pending one), and it is
        # what Nehrim itself authors for the start-cell planks (Unequip only).
        #
        # Chaining them with PlayAnimationAndWait("<seq>", "end") was tried and
        # is WRONG: the wait event never fires for a BGSGamebryoSequenceGenerator
        # state.  Vanilla proof — every gamebryo-sequence object script
        # (norsarcophagustopanim01script, dunsolitudejailopencelldoor, the
        # Solitude jail wall scene) uses plain PlayAnimation with a state
        # debounce and NEVER PlayAnimationAndWait; the scripts that do wait
        # (sarcophagusskulllock01script "alldone", dunlabyanimateontrig "done")
        # drive NATIVE-hkx objects whose events are havok annotations, which a
        # gamebryo NIF sequence does not have.  The wait would block the
        # calling thread forever (OnTrigger on MQ23's planks: the second plank
        # set's PlayAnimation would never run).
        # Fix akActionRef used in events that don't define it
        # TES4 scripts could use GetActionRef across blocks; Papyrus scopes params to events
        _event_re2 = re.compile(r'^\s*Event\s+(\w+)', re.IGNORECASE)
        _endevent_re2 = re.compile(r'^\s*EndEvent\b', re.IGNORECASE)
        _EVENTS_WITH_ACTIONREF = {'ontriggerenter', 'ontrigger', 'onactivate'}
        current_event = None
        has_actionref = False
        for idx in range(len(lines)):
            em = _event_re2.match(lines[idx])
            if em:
                current_event = em.group(1).lower()
                has_actionref = current_event in _EVENTS_WITH_ACTIONREF
                continue
            if _endevent_re2.match(lines[idx]):
                current_event = None
                has_actionref = False
                continue
            if current_event and not has_actionref and 'akActionRef' in lines[idx]:
                # Replace undefined akActionRef with Self
                lines[idx] = lines[idx].replace('akActionRef', 'Self')
        # GetContainer() returns an ObjectReference.  When the variable it lands
        # in was upgraded to Actor (because the script later calls an actor-only
        # method on it, e.g. UnequipItem), Papyrus needs an explicit downcast.
        _getcontainer_assign_re = re.compile(
            r'^(\s*)(\w+)(\s*=\s*.*\.GetContainer\(\))\s*$', re.IGNORECASE)
        for idx in range(len(lines)):
            m = _getcontainer_assign_re.match(lines[idx])
            if not m:
                continue
            tgt = m.group(2)
            ptype = self._property_refs.get(
                tgt, self._property_refs.get(tgt.lower(), ''))
            if ptype == 'Actor' or self._var_types.get(tgt.lower()) == 'Actor':
                lines[idx] = f'{m.group(1)}{tgt}{m.group(3)} as Actor'
        # Fix cross-script Float args in item count functions
        _item_count_re = re.compile(
            r'(\.(RemoveItem|AddItem)\s*\(\s*\w+\s*,\s*)(\w+\.\w+)(\s*\))',
            re.IGNORECASE)
        for idx in range(len(lines)):
            m = _item_count_re.search(lines[idx])
            if m and ' as Int' not in m.group(3):
                lines[idx] = lines[idx][:m.start(3)] + m.group(3) + ' as Int' + lines[idx][m.end(3):]
        # Resolve GetContainer placeholders (needs the whole comparison in view)
        for idx in range(len(lines)):
            lines[idx] = _resolve_getcontainer(lines[idx])
        # Fix conditions containing embedded comments that break parsing
        for idx in range(len(lines)):
            lines[idx] = _repair_commented_condition(lines[idx])
        # Fix assignments where RHS contains embedded comment that eats operators
        for idx in range(len(lines)):
            line = lines[idx]
            assign_m = re.match(r'^(\s*)(\w[\w.]*)\s*=\s*(.*)$', line)
            if assign_m:
                rhs = assign_m.group(3)
                semi_pos = rhs.find(';')
                if semi_pos >= 0:
                    after_semi = rhs[semi_pos+1:]
                    if re.search(r'==|!=|>=|<=|>|<|&&|\|\||\)', after_semi):
                        lines[idx] = f'{assign_m.group(1)}{rhs[semi_pos:]}'
        # Fix standalone no-op results (bare "0  ;comment" statements)
        _standalone_noop_re = re.compile(r'^(\s*)0\s+(;.*)$')
        for idx in range(len(lines)):
            m = _standalone_noop_re.match(lines[idx])
            if m:
                lines[idx] = f'{m.group(1)}{m.group(2)}'
        # Fix None as Int/Float
        for idx in range(len(lines)):
            line = lines[idx]
            if 'None as Int' in line:
                lines[idx] = line.replace('None as Int', '0')
            elif 'None as Float' in line:
                lines[idx] = line.replace('None as Float', '0.0')
        # Backstop for TES4-only condition functions that slipped past the
        # dedicated _emit_function handlers (property-access shapes the
        # expression parser doesn't route).  The lookbehind spares the
        # handlers' own TES4Polyfill.GetIsCreature(...)/HasVampireFed() output.
        _tes4_only_props = re.compile(
            r'(?<!Polyfill\.)\b(HasVampireFed|GetIsCreature|getClothingValue)\b',
            re.IGNORECASE)
        for idx in range(len(lines)):
            # Only the code part counts — an ;NE:/;TODO comment naming the
            # function must not re-trigger the rewrite on an already-converted
            # line (e.g. getClothingValue's NE note).
            code_only = lines[idx].split(';', 1)[0]
            m = _tes4_only_props.search(code_only)
            if not m:
                continue
            func_name = m.group(1)
            line = lines[idx]
            indent = len(line) - len(line.lstrip())
            stripped = line.strip()
            # If it's an If/ElseIf condition, replace with True + TODO
            if re.match(r'(?:If|ElseIf)\b', stripped, re.IGNORECASE):
                kw = stripped.split()[0]
                lines[idx] = ' ' * indent + f'{kw} True  ;TODO: {func_name} - No Papyrus equivalent ({stripped})'
            else:
                # Assignment or other statement — comment it out
                lines[idx] = ' ' * indent + f';TODO: {func_name} - No Papyrus equivalent: {stripped}'
        # Fix spurious commas in conditions
        for idx in range(len(lines)):
            line = lines[idx]
            if ',  ==' in line or ', ==' in line or '==,' in line or '== ,' in line:
                cleaned = re.sub(r',\s*', ' ', line)
                cleaned = re.sub(r'  +', ' ', cleaned)
                indent = len(line) - len(line.lstrip())
                lines[idx] = line[:indent] + cleaned.lstrip()
        # Fix Int vs form-typed comparisons left behind by TES4 condition
        # functions with no Papyrus equivalent, e.g. GetCurrentAIPackage becomes
        # the literal 0 and leaves `If (0 == SE10GoldenSaintPray2x12)` — a
        # Package compared to an Int, which will not compile.
        #
        # Only a form-typed identifier compared DIRECTLY against a numeric
        # literal is a genuine mismatch.  A form used as a function argument
        # (`GetItemCount(MSShadowScaleHeart)`) or compared to another form
        # (`GetBaseObject() == SE08BarrierCrystal`) is valid Papyrus, and
        # rewriting those to True silently deletes the guard — which made every
        # such script run its body unconditionally on load (quests auto-started
        # because a SetStage behind a dead GetItemCount check always fired).
        #
        # Neutralise only the offending comparison, not the whole condition, so
        # the surviving terms still gate the body.
        _type_mismatch_types = {'Package', 'Topic', 'MiscObject', 'Quest'}

        def _is_form_typed(ident: str) -> bool:
            low = ident.lower()
            # A script's own variable shadows any same-named form: DABoethia
            # declares `short Salutation` while a Topic named Salutation also
            # exists, and `Salutation == 1` is an ordinary Int test on the
            # variable, not a form comparison.
            if self._var_types.get(low) or low in self._local_vars:
                return False
            ptype = self._property_refs.get(
                ident, self._property_refs.get(low, ''))
            # TES4_<Script>-typed quest properties numeric-compared bare
            # (`ms04 < 55`) are the same TES4 form-vs-number pun.
            return ptype in _type_mismatch_types or ptype.startswith('TES4_')

        # <form ident> <cmp> <number>   or   <number> <cmp> <form ident>
        # A leading '.' (obj.Method) or trailing '(' (a call) disqualifies it.
        _mismatch_cmp_re = re.compile(
            r'(?<![.\w])([a-zA-Z_]\w*)(?!\s*\()\s*(==|!=|>=|<=|>|<)\s*(-?\d+(?:\.\d+)?)'
            r'|(-?\d+(?:\.\d+)?)\s*(==|!=|>=|<=|>|<)\s*([a-zA-Z_]\w*)(?!\s*[.(])')
        _cond_re = re.compile(r'^\s*(?:If|ElseIf)\b', re.IGNORECASE)
        for idx in range(len(lines)):
            if not _cond_re.match(lines[idx]):
                continue
            original = lines[idx].strip()

            def _neutralise(m: 're.Match') -> str:
                # These all come from a TES4 condition function that returned
                # a form (GetIsCurrentPackage, GetCurrentAIPackage, …) and has
                # no Papyrus equivalent, so the truth of the test is unknowable.
                # Resolve to the value that does NOT fire the branch: an
                # equality test becomes False, an inequality becomes True.
                ident = m.group(1) if m.group(1) is not None else m.group(6)
                if not _is_form_typed(ident):
                    return m.group(0)
                op = m.group(2) if m.group(1) is not None else m.group(5)
                return 'True' if op == '!=' else 'False'

            fixed = _mismatch_cmp_re.sub(_neutralise, lines[idx])
            if fixed != lines[idx]:
                # ;NE, not ;TODO: the neutralised value reproduces what the
                # TES4 runtime did with the form-vs-number pun, so there is
                # nothing left to hand-port.
                lines[idx] = f'{fixed.rstrip()}  ;NE: Type mismatch fix ({original})'
        # Fix integer assignments to cross-script ref-typed variables
        if self.xref:
            _assign_int_re = re.compile(r'^(\s*)([\w.]+)\s*=\s*(-?\d+)\s*(;.*)?$')
            for idx in range(len(lines)):
                m = _assign_int_re.match(lines[idx])
                if not m:
                    continue
                tgt = m.group(2)
                # fQuestDelayTime → RegisterForSingleUpdate (TES4 built-in)
                if tgt.lower().endswith('.fquestdelaytime'):
                    val = m.group(3)
                    fval = float(val) if val != '0' else 0
                    indent = m.group(1)
                    if fval > 0:
                        lines[idx] = f'{indent}RegisterForSingleUpdate({val}.0)  ;fQuestDelayTime'
                    else:
                        lines[idx] = f'{indent}UnregisterForUpdate()  ;fQuestDelayTime = 0'
                    continue
                if '.' in tgt:
                    if self._is_ref_typed_access(tgt) and not self._is_ref_as_int_crossscript(tgt):
                        lines[idx] = f'{m.group(1)}{tgt} = None  {m.group(4) or ""}'.rstrip()
        # Fix TODO comments inside function call arguments
        # e.g. "RemoveItem(Gold001, ;TODO: ...)" -> "RemoveItem(Gold001)  ;TODO: ..."
        for idx in range(len(lines)):
            line = lines[idx]
            if ';TODO' not in line or '(' not in line:
                continue
            semi_idx = line.find(';TODO')
            if semi_idx < 0:
                continue
            code_before = line[:semi_idx]
            open_count = code_before.count('(') - code_before.count(')')
            if open_count > 0:
                # TODO is inside unclosed parens - close properly
                todo_text = line[semi_idx:].rstrip().rstrip(')')
                code = code_before.rstrip().rstrip(',').rstrip()
                code += ')' * open_count
                lines[idx] = f'{code}  {todo_text}'
        # Fix Say() with Int variable as Topic arg (TES4 used FormIDs interchangeably)
        for idx in range(len(lines)):
            say_m = re.search(r'\.Say\((\w+)\)', lines[idx])
            if say_m:
                arg = say_m.group(1)
                arg_type = self._var_types.get(arg.lower(), '') or self._property_refs.get(arg, self._property_refs.get(arg.lower(), ''))
                if arg_type == 'Int':
                    indent = len(lines[idx]) - len(lines[idx].lstrip())
                    lines[idx] = ' ' * indent + f';TODO: Say() needs Topic form, not Int ({lines[idx].strip()})'
        # Fix Self assigned to Actor property: Self → Self as Actor
        for idx in range(len(lines)):
            m = re.match(r'^(\s*)(\w+)\s*=\s*Self\s*(;.*)?$', lines[idx])
            if m:
                tgt_low = m.group(2).lower()
                ptype = self._var_types.get(tgt_low, '') or self._property_refs.get(m.group(2), self._property_refs.get(tgt_low, ''))
                if ptype == 'Actor':
                    comment = m.group(3) or ''
                    lines[idx] = f'{m.group(1)}{m.group(2)} = Self as Actor  {comment}'.rstrip()
        # Fix ActorBase vs Script type comparisons (e.g. GetActorBase() == ScriptProperty)
        # TES4 compared refs directly to base objects - in Papyrus, base form needs GetBaseObject()
        for idx in range(len(lines)):
            line = lines[idx]
            if '.GetActorBase()' not in line:
                continue
            comp_m = re.search(r'(GetActorBase\(\))\s*(==|!=)\s*(\w+)', line)
            if comp_m:
                rhs = comp_m.group(3)
                ptype = self._property_refs.get(rhs, self._property_refs.get(rhs.lower(), ''))
                if ptype and ptype.startswith('TES4_'):
                    # Replace GetActorBase() == ScriptProp with GetActorBase() == ScriptProp.GetActorBase()
                    indent = len(line) - len(line.lstrip())
                    kw = line.strip().split()[0]
                    lines[idx] = ' ' * indent + f'{kw} True  ;TODO: ActorBase vs Script type ({line.strip()})'
        # Fix ObjectReference vs MiscObject/Ingredient/etc comparisons
        # TES4: "akActionRef == SomeMiscObject" → Papyrus: "akActionRef.GetBaseObject() == SomeMiscObject"
        _base_form_types = {'MiscObject', 'Ingredient', 'Potion', 'Weapon', 'Armor', 'Book', 'Key'}
        for idx in range(len(lines)):
            line = lines[idx]
            comp_m = re.search(r'\b(akActionRef|\w+Ref)\s*(==|!=)\s*(\w+)', line)
            if comp_m:
                rhs = comp_m.group(3)
                ptype = self._property_refs.get(rhs, self._property_refs.get(rhs.lower(), ''))
                if ptype in _base_form_types:
                    old_expr = comp_m.group(0)
                    new_expr = f'{comp_m.group(1)}.GetBaseObject() {comp_m.group(2)} {rhs}'
                    lines[idx] = line.replace(old_expr, new_expr)

        # Bool-returning call ordered against a number: TES4's GetDetected/GetDead
        # return 0/1, so scripts write `getdetected X > 0`.  Papyrus refuses to
        # order a Bool, so cast the call.
        for idx in range(len(lines)):
            code, _, comment = lines[idx].partition(';')
            fixed = self._BOOL_CMP_RE.sub(r'(\1 as Int)\2', code)
            if fixed != code:
                lines[idx] = fixed + (';' + comment if comment else '')

        # `;/` opens a Papyrus BLOCK comment that runs until a matching `/;`.
        # Oblivion scripts use `;///////...` banner rules freely (Nehrim's do
        # constantly), and TES4 had no block-comment syntax — so every banner
        # silently swallowed the rest of the file, which the compiler only
        # reports as "unexpected end of file" at the last line.  A single
        # unterminated banner in a widely-extended base script cascaded into
        # ~300 downstream failures.  Break the digraph by padding a space after
        # the `;`; the comment text is preserved verbatim.
        for idx in range(len(lines)):
            code, sep, comment = lines[idx].partition(';')
            if sep and comment.startswith('/'):
                lines[idx] = f'{code}; {comment}'

        # `Self == <Actor-typed thing>` — inside a script that extends
        # ObjectReference, `Self` is that script's own type, and Papyrus refuses
        # to compare it with an Actor ("you can't compare type TES4_X with type
        # Actor").  TES4 had one reference type and compared them freely.  The
        # object behind Self really is the reference being tested, so cast it
        # rather than dropping the comparison, which would change which branch
        # runs.
        _self_cmp_re = re.compile(
            r'(?<![.\w])Self\s*(==|!=)\s*([A-Za-z_]\w*(?:\.\w+)*)',
            re.IGNORECASE)

        _self_decl_re = re.compile(r'^\s*(\w+)\s+Property\s+(\w+)\b',
                                   re.IGNORECASE)
        _self_decls = {}
        for line in lines:
            dm = _self_decl_re.match(line)
            if dm:
                _self_decls[dm.group(2).lower()] = dm.group(1)

        def _cast_self(m: 're.Match') -> str:
            other = m.group(2)
            base = other.split('.')[0]
            otype = (_self_decls.get(base.lower())
                     or self._property_refs.get(base, ''))
            if otype == 'Actor' or ('.' in other and otype.startswith('TES4_')):
                return f'(Self as Actor) {m.group(1)} {other}'
            return m.group(0)

        for idx in range(len(lines)):
            if not re.match(r'^\s*(?:If|ElseIf)\b', lines[idx], re.IGNORECASE):
                continue
            lines[idx] = _self_cmp_re.sub(_cast_self, lines[idx])

        # The same mismatch on the ASSIGNMENT side: `OtherScript.ActorProp =
        # Self`.  TES4 stored the calling reference into another script's `ref`
        # variable; if that variable inferred `Actor`, Papyrus rejects the raw
        # Self.  Cast for the same reason as the comparison above.
        _self_assign_re = re.compile(
            r'^(\s*)([A-Za-z_]\w*)\.(\w+)(\s*=\s*)Self\s*(;.*)?$',
            re.IGNORECASE)
        for idx in range(len(lines)):
            am = _self_assign_re.match(lines[idx])
            if not am:
                continue
            owner_type = (_self_decls.get(am.group(2).lower())
                          or self._property_refs.get(am.group(2), ''))
            if not owner_type.startswith('TES4_'):
                continue
            tail = am.group(5) or ''
            lines[idx] = (f'{am.group(1)}{am.group(2)}.{am.group(3)}'
                          f'{am.group(4)}(Self as Actor)  {tail}'.rstrip())

        # `<form>.Cast(...)` — Cast is declared on Spell.  A TES4 `ref` holding
        # a spell lands as Form (often read out of another script's variable
        # table, where nothing narrows it), and Papyrus will not call a Spell
        # method on a Form.  Cast at the call site: the object really is a spell,
        # the declaration just cannot say so.
        _cast_recv_re = re.compile(
            r'(?<![.\w])([A-Za-z_]\w*)\.Cast\(', re.IGNORECASE)

        # Read the declarations out of the emitted lines rather than
        # _property_refs: a `ref` retyped by a later pass (or declared from the
        # script's own variable table) is only correct in the text by now.
        _decl_types = {}
        _decl_re = re.compile(
            r'^\s*(\w+)\s+Property\s+(\w+)\b', re.IGNORECASE)
        for line in lines:
            dm = _decl_re.match(line)
            if dm:
                _decl_types[dm.group(2).lower()] = dm.group(1)

        def _cast_receiver(m: 're.Match') -> str:
            recv = m.group(1)
            rtype = (_decl_types.get(recv.lower())
                     or self._property_refs.get(recv, ''))
            if rtype in ('Form', 'ObjectReference'):
                return f'({recv} as Spell).Cast('
            return m.group(0)

        for idx in range(len(lines)):
            if '.Cast(' in lines[idx]:
                lines[idx] = _cast_recv_re.sub(_cast_receiver, lines[idx])

        lines = self._shadow_controls_writes(lines)
        return lines

    _CONTROLS_WRITE_RE = re.compile(
        r'^(\s*)Game\.(Disable|Enable)PlayerControls\(\)\s*(;.*)?$')

    def _shadow_controls_writes(self, lines: list) -> list:
        """Mirror every Game.{Disable,Enable}PlayerControls() into a global.

        Skyrim has both writers as natives but no getter, so TES4's
        GetPlayerControlsDisabled is read back from TES4ControlsDisabled (see
        _create_tes4_special_records).  The shadow write is spliced here rather
        than returned from the call handler because a trailing source comment
        is appended to whatever that handler returns, which would strand a
        second line behind it.

        EVERY writer is shadowed, not just those in a script that also reads:
        in MG18 — the only reader in the plugin — the writers live in two
        SEPARATE magic-effect scripts (MG18MannimarcoSpellScript1/2), so a
        same-script gate would shadow nothing at all.
        """
        out = []
        touched = False
        for line in lines:
            out.append(line)
            m = self._CONTROLS_WRITE_RE.match(line)
            if m:
                val = 1 if m.group(2) == 'Disable' else 0
                out.append(f'{m.group(1)}TES4ControlsDisabled.SetValue({val})')
                touched = True
        if touched:
            self._property_refs['TES4ControlsDisabled'] = 'GlobalVariable'
        return out

    def get_cell_family_helpers(self) -> list:
        """Helper functions for the GetInCell families used so far.

        Fragment callers (QUST stage / INFO scripts) assemble their own file, so
        they must append these once every fragment body has been converted —
        the bodies call them by name.
        """
        return self._emit_cell_family_helpers()

    def get_property_refs(self) -> dict[str, str]:
        """Get accumulated external property references.

        Property TYPES are decided by how the script body uses each ref (the
        per-function handlers promote to Actor/ObjectReference/base as needed).
        We deliberately do NOT blanket-coerce types here based on the bound
        record: a property the body uses as an Actor/ObjectReference must stay
        that type even if it happens to be bound to a base, because retyping it
        to ActorBase would break the body (`StartCombat`, MoveTo, ==Actor…).

        The one confirmed alias-break case — an NPC base used ONLY via
        `GetActorBase()` (SetEssential) but typed as an Actor-derived script —
        is fixed at the point of use (the SetEssential handler types it
        ActorBase), not here.
        """
        return dict(self._property_refs)

    def _mark_topic_property(self, name: str) -> None:
        """Type `name` as a Topic, but only if it really names a DIAL.

        Say/SayTo/StartConversation take a topic, and TES4 EditorIDs are not
        unique across record types: Morroblivion has CELLs named DagothSUr and
        KoalSCave with no DIAL of that name at all. Typing those `Topic`
        produced a property the VM refuses to bind ("is not the right type"),
        which reads None. Leave the name untyped instead -- the AddTopic unlock
        global is what actually drives the topic.
        """
        key = (name or '').strip()
        if not key:
            return
        if self.xref:
            fid = self.xref.edid_to_formid.get(key.lower(), '')
            rtype = self.xref.record_type.get(fid, '') if fid else ''
            if rtype and rtype != 'DIAL':
                return
        self._property_refs[key] = 'Topic'

    def _papyrus_type_for(self, fid: str, rtype: str) -> str:
        """Papyrus property type for a record, as the IMPORTER writes it.

        `_record_type_to_papyrus` maps the TES4 signature, which is right until
        the importer changes the signature on the way out. A BOOK carrying an
        ENAM becomes a SCRL (see project_enchanted_book_is_a_scroll), so a
        `Book` property naming one cannot bind and reads None in-game.
        """
        ptype = _record_type_to_papyrus(rtype)
        if (ptype == 'Book' and self.xref
                and fid in getattr(self.xref, 'enchanted_books', ())):
            return 'Scroll'
        return ptype

    def _script_type_binds(self, ptype: str, fid: str) -> bool:
        """Whether an attached script class may stand in for `ptype` HERE.

        Base-object types normally cannot (the VM refuses the base record —
        see script_type_may_override), but a scripted world object (ACTI/LIGH)
        with exactly ONE placed ref can: the property binder redirects the
        binding to that ref, which carries the script instance. Without this,
        the base gate stripped cross-script variables off unique activators —
        `SE01Metronome.weatherVAR` and the SE11 trigzone stopped compiling.
        Inventory item types (ARMO/WEAP/...) stay excluded even with a lone
        world placement, because their properties mean the BASE (AddItem /
        RemoveItem), never that placement.
        """
        if script_type_may_override(ptype):
            return True
        return (self.xref.record_type.get(fid, '') in ('ACTI', 'LIGH')
                and bool(self.xref.unique_placed_ref(fid)))

    def _register_cell_family(self, name: str, cells: list,
                              exterior: list = None) -> str:
        """Record a GetInCell prefix family and return its helper's name.

        See the GetInCell handler in _emit_function for why a family exists at
        all.  Helpers are keyed case-insensitively so `Chorrol` and `chorrol`
        (both appear in vanilla scripts) share one function.

        `cells` are INTERIOR EditorIDs (compared as Cell properties);
        `exterior` are (worldspace EditorID, x, y) grid keys.
        """
        key = _safe_property_name(name)
        existing = self._cell_families.get(key.lower())
        if existing is None:
            self._cell_families[key.lower()] = (key, list(cells),
                                                list(exterior or []))
            # Register the worldspace properties HERE, not when the helper body
            # is emitted: get_cell_family_helpers() runs after the property
            # declarations have already been written, so a ref added there
            # never gets declared and the helper cites an undefined identifier.
            for wrld, _x, _y in (exterior or []):
                if wrld:
                    self._property_refs[wrld] = 'WorldSpace'
        else:
            key = existing[0]
        return f'TES4_IsIn{key}'

    # -----------------------------------------------------------------------
    # Private
    # -----------------------------------------------------------------------

    def _reset(self):
        self._property_refs = {}
        self._cell_families = {}
        self._has_gamemode = False
        self._has_menumode = False
        self._has_scripteffectupdate = False
        self._uses_getsecondspassed = False
        self._uses_hour_window = False
        self._uses_timer = False
        self._local_vars = set()
        self._in_foreach = 0
        self._refwalk_var = ''
        self._refwalk_labels = set()
        self._block_depth = 0
        self._var_renames = {}
        self._var_types = {}
        self._udf_returns = False
        self._udf_return_value = ''
        self._current_script_edid = ''
        # Must clear per script: the converter instance is reused across every
        # SCPT in a job, and a leaked True would append a RestoreFallDamage to
        # an unrelated script's teardown event.
        self._suppressed_fall_damage = False
        # Button-MessageBox state (see message_menus.py): MESG names already
        # matched to a call site this script, and whether the helpers are due.
        self._msgbox_used = set()
        self._uses_msg_buttons = False

    @staticmethod
    def _balance_if_endif(lines: list[str]) -> list[str]:
        """Balance If/EndIf and While/EndWhile within event/function blocks.

        Remove extra EndIf/Else/ElseIf that don't have matching If.
        Insert missing EndIf/EndWhile before EndEvent/EndFunction.

        While is tracked on the SAME stack as If, because Papyrus requires the
        two to nest rather than interleave: an OBSE ref-walk opens its `While`
        outside the body's `if` nest but the authored `Goto` that ends it sits
        several levels deep inside it (see the Label/Goto conversion), so the
        closer has to be emitted out here, after those EndIfs, in stack order.
        """
        result = []
        # Stack of open block kinds, innermost last: 'if' or 'while'.
        stack: list = []
        in_event = False
        for line in lines:
            stripped = line.strip().lower()
            # Strip inline comments for keyword matching
            code_part = stripped.split(';')[0].strip() if ';' in stripped else stripped
            # A TYPED function header reads `Int Function TES4Call(...)`, so
            # matching only a leading `function ` missed every OBSE user
            # function that returns a value — nothing inside them was balanced
            # and an unclosed block ran on into EndFunction.
            if (code_part.startswith('event ')
                    or re.match(r'^(?:\w+\s+)?function\s', code_part)):
                in_event = True
                stack = []
            elif code_part in ('endevent', 'endfunction'):
                # Insert missing EndIf/EndWhile statements before closing
                while stack:
                    result.append('EndWhile' if stack.pop() == 'while'
                                  else 'EndIf')
                in_event = False
                stack = []
            elif in_event:
                if code_part.startswith('if ') or code_part.startswith('if(') or code_part == 'if':
                    stack.append('if')
                elif (code_part.startswith('while ')
                      or code_part.startswith('while(')):
                    stack.append('while')
                elif code_part.startswith('elseif '):
                    if 'if' not in stack:
                        continue  # orphaned ElseIf
                elif code_part == 'else':
                    if 'if' not in stack:
                        continue  # orphaned Else
                elif code_part == 'endif':
                    if 'if' not in stack:
                        # Extra EndIf — skip it
                        continue
                    # Close any While the body left open inside this If, so the
                    # EndIf lands on its own opener.
                    while stack and stack[-1] != 'if':
                        result.append('EndWhile')
                        stack.pop()
                    stack.pop()
                elif code_part == 'endwhile':
                    if 'while' not in stack:
                        continue  # orphaned EndWhile
                    while stack and stack[-1] != 'while':
                        result.append('EndIf')
                        stack.pop()
                    stack.pop()
            result.append(line)
        return result

    @staticmethod
    def _remove_dead_code_after_return(lines: list[str]) -> list[str]:
        """Comment out executable code after Return at event/function top-level."""
        result = []
        in_dead_zone = False
        depth = 0  # if/while nesting depth
        for line in lines:
            stripped = line.strip().lower()
            if stripped.startswith('event ') or stripped.startswith('function '):
                in_dead_zone = False
                depth = 0
            elif stripped in ('endevent', 'endfunction'):
                in_dead_zone = False
                depth = 0
            elif in_dead_zone:
                # Allow empty lines and comments through
                if stripped and not stripped.startswith(';'):
                    result.append(f';  {line.strip()}  ;dead code after Return')
                    continue
            else:
                # Track nesting
                if stripped.startswith('if ') or stripped == 'if':
                    depth += 1
                elif stripped == 'endif':
                    depth = max(0, depth - 1)
                elif stripped.startswith('while '):
                    depth += 1
                elif stripped == 'endwhile':
                    depth = max(0, depth - 1)
                elif stripped == 'return' and depth == 0:
                    in_dead_zone = True
            result.append(line)
        return result

    # A bare `Return` on its own line (optionally trailed by a comment). A
    # value-returning Return belongs to an OBSE user function, not a GameMode
    # early-out, and must be left alone.
    _BARE_RETURN_RE = re.compile(r'^(\s*)Return\s*(;.*)?$', re.IGNORECASE)

    # The condition under which a placed reference's TES4 `begin GameMode` body
    # would run, used at every site that arms the OnUpdate poll for an
    # object/actor script.
    #
    # REVERTED to Is3DLoaded() while a CharacterGen regression is bisected:
    # Valen Dreth stopped moving to his taunt marker and stopped delivering his
    # first lines after the cell-attachment gate landed.  The self-enable
    # deadlock the cell gate was introduced to fix (Nehrim's Celebro, ~200
    # disabled refs) is REAL and is re-opened by this revert — see
    # TES4Polyfill.ShouldRunGameMode(), which is still shipped and unused.
    # Do not re-apply the cell gate without re-testing CharacterGen in-game.
    _GAMEMODE_GATE = 'Is3DLoaded()'

    def _reregister_before_returns(self, out: list, start: int, interval: str,
                                   load_gated: bool) -> None:
        """Re-arm the OnUpdate poll before every early Return in `out[start:]`.

        Emits the SAME re-register the fall-through path uses, so a body that
        returns early keeps ticking exactly like one that runs to the end:
        `Is3DLoaded()`-gated for object/actor scripts (whose poll is meant to
        stop on unload), unconditional otherwise.
        """
        i = start
        while i < len(out):
            m = self._BARE_RETURN_RE.match(out[i])
            if m:
                indent = m.group(1)
                if load_gated:
                    out[i:i] = [f'{indent}If ({self._GAMEMODE_GATE})',
                                f'{indent}  RegisterForSingleUpdate({interval})',
                                f'{indent}EndIf']
                else:
                    out[i:i] = [f'{indent}RegisterForSingleUpdate({interval})']
                i += 3 if load_gated else 1
            i += 1

    def _get_update_interval(self) -> str:
        if self._uses_getsecondspassed:
            return '0.1'
        if self._uses_timer:
            return '0.25'
        return '0.5'

    def _parse_source(self, source: str):
        """Parse Oblivion source into (variables, blocks)."""
        lines = source.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        variables = []
        blocks = []
        current_block = None
        current_filter = ''
        current_lines = []
        _seen_vars = set()

        for raw_line in lines:
            stripped = raw_line.strip()
            if not stripped or stripped.startswith(';'):
                if current_block is not None:
                    current_lines.append(raw_line)
                continue

            low = stripped.lower()

            if low.startswith('scriptname ') or low.startswith('scn '):
                continue

            # Variable declarations — TES4 vars are ALWAYS script-global,
            # even if declared inside a begin/end block
            m = re.match(r'^(string_var|array_var|short|long|int|float|ref|reference)\s+(\w+)', stripped, re.IGNORECASE)
            if m:
                vname_low = m.group(2).lower()
                if vname_low not in _seen_vars:
                    variables.append((m.group(1).lower(), m.group(2)))
                    _seen_vars.add(vname_low)
                continue  # Don't add var decls to block lines

            begin_m = re.match(r'^begin\s+(\w+)(.*)', stripped, re.IGNORECASE)
            if begin_m:
                if current_block is not None:
                    # Previous block never saw its End (e.g. `End ;comment`
                    # before this fix) — close it rather than discard it.
                    blocks.append((current_block, current_filter, current_lines))
                current_block = begin_m.group(1).lower()
                # `begin OnEquip player` — the trailing argument is a FILTER that
                # restricts the block to that object.  Dropping it makes the
                # block fire for everyone (any actor equipping the item, any
                # actor tripping the trigger), so it must be carried through and
                # compiled into a guard on the Papyrus event parameter.
                current_filter = begin_m.group(2).split(';')[0].strip()
                current_lines = []
                continue

            # `End` optionally followed by a comment or block-name label
            # (`End ;OnActivate`, `End GameMode`) — the Shivering Isles scripts
            # use this style heavily; matching only a bare `end` silently
            # dropped whole event blocks (SE09AddItemsScript's OnActivate with
            # all its SetStage calls).
            if re.match(r'^end(?:\s|;|$)', low):
                if current_block is not None:
                    blocks.append((current_block, current_filter, current_lines))
                    current_block = None
                    current_filter = ''
                    current_lines = []
                continue

            if current_block is not None:
                current_lines.append(raw_line)

        if current_block is not None:
            # Unterminated final block (missing End) — keep what we have.
            blocks.append((current_block, current_filter, current_lines))

        return variables, blocks

    def _current_event_actor_param(self) -> str:
        """Name of the Actor parameter of the event being converted, if any.

        Used for TES4 calls whose implicit subject is "whoever this event is
        about" — e.g. bare GetContainer inside OnEquipped is the equipping
        actor, which is exactly akActor.
        """
        ev = self._current_event or ''
        m = re.search(r'\bActor\s+(ak\w+)', ev)
        return m.group(1) if m else ''

    # TES4 GMSTs a script writes at runtime → the Skyrim ACTOR VALUE that
    # produces the same observable change on the actor.  Skyrim has no vanilla
    # Papyrus GMST *writer* (only readers), so a global setting cannot be
    # changed without SKSE; every one of these settings does, however, have a
    # per-actor equivalent the engine already reads.
    #
    # Names verified against Skyrim.esm's AVIF records and the actor-value
    # table in SkyrimSE.exe.  Note fJumpHeightMax does NOT exist in Skyrim at
    # all (only fJumpHeightMin) — scripts that set both are writing one real
    # setting and one that Oblivion had and Skyrim dropped.
    _GMST_TO_ACTOR_VALUE = {
        'fjumpheightmin':      'JumpingBonus',
        'fjumpheightmax':      'JumpingBonus',
        'fmoverunmult':        'SpeedMult',
        'fmovecharwalkmin':    'SpeedMult',
        'fmovecharwalkmax':    'SpeedMult',
        'fmoverunathleticsmult': 'SpeedMult',
    }

    def _gamesetting_write(self, setting: str, value: str, extends: str) -> str:
        """A runtime GMST write, re-expressed as the actor value it changes."""
        av = self._GMST_TO_ACTOR_VALUE.get(setting.lower())
        if not av:
            return (f';TODO: SetNumericGameSetting {setting} {value}  '
                    f';no vanilla Papyrus GMST writer and no actor-value '
                    f'equivalent (SKSE Game.SetGameSetting* would be needed)')
        # ForceActorValue, not ModActorValue: the TES4 call SETS the value
        # outright, and a script that writes the same setting on every update
        # would otherwise stack the modifier without bound.
        target = self._actor_target_for_gamesetting(extends)
        return f'{target}.ForceActorValue("{av}", {value})'

    def _actor_target_for_gamesetting(self, extends: str) -> str:
        """The actor a runtime game-setting write should apply to.

        These settings were GLOBAL in Oblivion, so every script that writes one
        is changing the world for whoever is affected — in practice the player,
        which is who casts the scroll or wears the ring.  A magic-effect script
        has a real target parameter and uses it; anything else applies to the
        player, matching the global's practical scope.
        """
        if extends == 'ActiveMagicEffect':
            param = self._current_event_actor_param()
            if param:
                return param
        return 'Game.GetPlayer()'

    _FALL_RESTORE = 'TES4Polyfill.RestoreFallDamage()'

    def _append_fall_damage_restore(self, out: list, extends: str) -> list:
        """Pair every SuppressFallDamage() with a restore when the effect ends.

        `TES4Polyfill.SuppressFallDamage()` (the ResetFallDamageTimer
        conversion) writes fJumpFallHeightMin, a GLOBAL game setting.  Oblivion
        needed no teardown because ResetFallDamageTimer only cleared a
        per-actor accumulator; leaving the Skyrim equivalent set would disable
        fall damage permanently.

        The restore goes in whichever teardown event the script already has —
        OnEffectFinish for a magic-effect script, otherwise OnUpdate's exit —
        and a fresh OnEffectFinish is synthesized when the script has none.
        """
        idx = next((i for i, line in enumerate(out)
                    if line.startswith('Event OnEffectFinish(')), None)

        if idx is not None:
            # Restore the SAME actor the suppression applied to, which is the
            # teardown event's own target parameter.
            m = re.search(r'\bActor\s+(ak\w+)', out[idx])
            actor = m.group(1) if m else ''
            end = next((i for i in range(idx + 1, len(out))
                        if out[i] == 'EndEvent'), None)
            if end is not None:
                out.insert(end, f'  TES4Polyfill.RestoreFallDamage({actor})')
                return out

        # No teardown event at all: an ActiveMagicEffect always gets one, so
        # synthesize it rather than leaving the suppression permanent.
        if extends == 'ActiveMagicEffect':
            out.append('Event OnEffectFinish(Actor akTarget, Actor akCaster)')
            out.append('  TES4Polyfill.RestoreFallDamage(akTarget)')
            out.append('EndEvent')
            out.append('')
        return out

    def _block_filter_guard(self, block_type: str,
                            block_filter: str) -> 'str | None':
        """Compile a TES4 block filter into a Papyrus condition, or '' if none.
        Returns None when a real filter exists but CANNOT be expressed — the
        caller must then keep the body commented out rather than run it
        unconditionally for every event.

        `begin OnEquip player` fires the block ONLY when the player equips the
        item; `begin OnPackageDone SomePkg` only when that package ends.  Papyrus
        events carry no filter, so the restriction becomes an `If` around the
        body, testing the event parameter that holds the filtered object (see
        BLOCK_FILTER_PARAM).  Without this the block runs for every actor /
        container / package, which is how an item's "you can't equip this"
        message ended up firing for NPCs the moment they loaded in.
        """
        if not block_filter:
            return ''
        target = BLOCK_FILTER_PARAM.get(block_type)
        if not target:
            # MenuMode's argument is a menu ID and OnAlarm's is a crime type —
            # neither names an object, and neither block has a parameter to
            # filter on.  Nothing to guard.
            return ''
        param, param_type = target

        name = block_filter.strip()
        if name.lower() == 'player':
            return f'{param} == Game.GetPlayer()'

        # Anything else is a form EditorID. Bind it as a property and compare.
        if not re.match(r'^\w+$', name) or not self.xref:
            return ''
        fid = self.xref.edid_to_formid.get(name.lower(), '')
        if not fid:
            return ''
        rtype = _record_type_to_papyrus(self.xref.record_type.get(fid, ''))

        # The comparison has to typecheck against the event parameter.  On an
        # ACTOR script `begin OnEquip SomePotion` filters the ITEM equipped, not
        # the equipper — but Skyrim's OnEquipped only hands us the actor, so
        # there is nothing to test the item against.  Emitting the comparison
        # anyway gives `akActor == SomePotion`, which will not compile.
        param_is_actor = param_type == 'Actor'
        filter_is_actor = rtype in ('Actor', 'ObjectReference')
        if param_is_actor and not filter_is_actor:
            # (no Papyrus parameter carries the item; the filter is lost)
            return ''
        if param_type in ('ObjectReference', 'Actor', 'Form'):
            ptype = rtype if filter_is_actor else param_type
        else:
            ptype = param_type
        safe = _safe_property_name(name)
        existing = self._property_refs.get(safe)
        if existing and existing != ptype:
            # Already bound at a TES4_* script type: those extend Actor/
            # ObjectReference, so the comparison against the event parameter
            # still compiles — keep the existing binding and emit the guard.
            # (Dropping it here ran CGRenote's `begin onHit CGAssassin01Ref`
            # bodies on EVERY hit: any stray arrow killed her and jumped
            # CharacterGen's stages out of order.)
            if (existing.startswith('TES4_')
                    and ptype in ('Actor', 'ObjectReference', 'Form')):
                return f'{param} == {safe}'
            # Genuinely incomparable (e.g. bound as Faction/GlobalVariable).
            # An unguarded body is WRONG for every event the filter excluded —
            # signal the caller to keep the body but not execute it.
            return None
        self._property_refs[safe] = ptype
        return f'{param} == {safe}'

    def _convert_line(self, line: str, extends: str) -> str:
        """Convert a single Oblivion script line to Papyrus."""
        stripped = line.strip()
        if not stripped:
            return ''
        if stripped.startswith(';'):
            return stripped

        # Strip inline comments (;) that aren't inside string literals
        # TES4 uses ; for comments, these must not leak into Papyrus expressions
        inline_comment = ''
        in_str = False
        for ci, ch in enumerate(stripped):
            if ch == '"':
                in_str = not in_str
            elif ch == ';' and not in_str:
                inline_comment = '  ' + stripped[ci:]
                stripped = stripped[:ci].rstrip()
                break

        if not stripped:
            return inline_comment.strip() if inline_comment else ''

        # Clear accumulated expression-level comments before conversion
        self._line_comments.clear()

        # Track how deeply the SOURCE nests this line inside if/while blocks.
        # Read off the TES4 spelling before conversion, so it is independent of
        # what the emitter does with the line.  Used by SetFunctionValue to tell
        # a top-level stage (which a following Return will carry) from one
        # inside a branch (which must return where it stands).
        _depth_low = stripped.lower()
        if _depth_low in ('endif', 'endwhile', 'loop') or _depth_low == 'end':
            self._block_depth = max(0, self._block_depth - 1)

        stripped = self._unquote_identifiers(stripped)

        result = self._convert_line_inner(stripped, extends)

        # Opens a block AFTER the line itself was converted, so a
        # SetFunctionValue sitting on the `if` line is judged at its own depth.
        if (re.match(r'^(if|while)\b', _depth_low)
                and not _depth_low.startswith('endif')):
            self._block_depth += 1

        # Append any accumulated expression-level comments (from no-op functions)
        if self._line_comments:
            comments = '  '.join(self._line_comments)
            self._line_comments.clear()
            # If result is just '0' (standalone no-op), replace with comment
            if result.strip() == '0':
                result = comments
            elif not result.lstrip().startswith(';'):
                result = f'{result}  {comments}'

        if inline_comment and not result.lstrip().startswith(';'):
            return result + inline_comment
        return result

    # A quoted EditorID used as a REFERENCE, i.e. one side of a `.` member
    # access: `"NQ16"."NQ16CountBooksVar"`, `"NQ16".Var`, `Ref."Var"`.
    # Oblivion's parser accepts quotes around any EditorID, and Nehrim's authors
    # use them constantly.  Only the dotted form is unquoted here: a bare
    # `"text"` elsewhere on the line is a real string literal (a Message, a
    # PlaySound EditorID that the sound handlers already dequote themselves),
    # and stripping those would corrupt every message in the plugin.
    _QUOTED_MEMBER_RE = re.compile(r'"([A-Za-z_]\w*)"(?=\s*\.)|(?<=\.)\s*"([A-Za-z_]\w*)"')

    @classmethod
    def _unquote_identifiers(cls, line: str) -> str:
        """Strip Oblivion's optional quotes from a dotted EditorID reference.

        `Set "NQ16"."NQ16CountBooksVar" to "NQ16"."NQ16CountBooksVar" +1`
        (1AlmanachDerBeschwoerungSCN) reached the emitter with the quotes still
        on: the assignment TARGET went through _convert_ref, which mangled them
        into `_NQ16_._NQ16CountBooksVar_`, while the VALUE went through
        _convert_expression, which left them alone and emitted the un-parseable
        `NQ16."NQ16CountBooksVar" + 1`.  Normalising once, here, fixes both
        sides and every other path that reads the line.
        """
        return cls._QUOTED_MEMBER_RE.sub(
            lambda m: m.group(1) or m.group(2), line)

    def _convert_line_inner(self, stripped: str, extends: str) -> str:
        """Core line conversion logic (no inline-comment handling)."""
        low = stripped.lower()

        # --- OBSE ref-walk loop -------------------------------------------
        # Oblivion scans the loaded cells with
        #     set <ref> to GetFirstRef <type>
        #     Label <n>
        #       if ( <ref> ) … set <ref> to GetNextRef / Goto <n> … endif
        # There is no Papyrus iterator over loaded references, and `Label` /
        # `Goto` are not Papyrus keywords at all — emitted verbatim they are
        # undefined-function errors that fail the ENTIRE script, which is far
        # worse than it sounds: every other script declaring a property typed
        # as this one then fails to LINK, so a single unconvertible loop can
        # silently disable a whole quest line.
        #
        # Skyrim's own mechanism for "an actor near here" is
        # Game.FindRandomActorFromRef, so the walk becomes a bounded sampling
        # loop over that: each pass draws another nearby actor and runs the
        # authored body against it.  `Goto <label>` closes the loop it opened
        # (the authored `set <ref> to GetNextRef` is what advances it), and a
        # `Goto` naming an unopened label degrades to a comment rather than a
        # compile error.
        rw_m = re.match(r'^label\s+(\d+)\s*$', low)
        if rw_m and self._refwalk_var:
            self._refwalk_labels.add(rw_m.group(1))
            return (f'While ({self._refwalk_var} != None)'
                    f'  ;OBSE ref-walk (Label {rw_m.group(1)})')
        goto_m = re.match(r'^goto\s+(\d+)\s*$', low)
        if goto_m:
            # The authored `Goto <label>` sits DEEP inside the loop body's `if`
            # nest — Morroblivion's witness walk closes four `if`s after it — so
            # it cannot emit `EndWhile` here without crossing those blocks
            # ("EndWhile before EndIf").  The `While` header re-tests the ref
            # every pass anyway, and the `set <ref> to GetNextRef` immediately
            # above already advanced it, so the jump back is implicit: the Goto
            # itself is a no-op and `EndWhile` is emitted where the loop's
            # enclosing block actually ends (see _close_refwalk).
            if goto_m.group(1) in self._refwalk_labels:
                return f';{stripped}  ;OBSE ref-walk continues (loop re-tests)'
            return f';{stripped}  ;NE: OBSE Goto has no Papyrus equivalent'
        if rw_m:
            # A Label with no ref-walk in flight controls something this
            # converter cannot model; drop it rather than emit an undefined call.
            return f';{stripped}  ;NE: OBSE Label has no Papyrus equivalent'

        # An OBSE `forEach <it> <- <container> … loop` block.  The iterator has no
        # Papyrus equivalent, so the header no-ops — but the BODY reads that
        # iterator, and left live it referenced an identifier that was never
        # declared ("value with type String cannot be assigned to ...").  The loop
        # cannot run at all without its iterator, so comment the whole block out.
        if getattr(self, '_in_foreach', 0):
            if low == 'loop' or low.startswith('loop '):
                self._in_foreach -= 1
                return f';{stripped}  ;NE: end of OBSE forEach block'
            if low.startswith('foreach'):
                self._in_foreach += 1
            return f';{stripped}  ;NE: inside OBSE forEach block'
        if low.startswith('foreach'):
            self._in_foreach = getattr(self, '_in_foreach', 0) + 1
            return (f';{stripped}  ;NE: OBSE forEach — no Papyrus iterator '
                    f'equivalent')

        # Variable declarations inside blocks — already declared as Properties by _parse_source
        var_m = re.match(r'^(string_var|array_var|short|long|int|float|ref|reference)\s+(\w+)', stripped, re.IGNORECASE)
        if var_m:
            # Variable already declared as a Property; skip the inline declaration
            return ''

        # set X to Y
        # An OBSE ARRAY ELEMENT write (`let arDayNameEval[6] := ...`, and the
        # `set` spelling).  array_var has no Papyrus equivalent, so there is no
        # element to assign — _safe_property_name mangled the subscript into the
        # identifier `arDayNameEval_6_`, which was then undefined.  Comment the
        # line out and keep the rest of the script.
        _elem_m = re.match(r'^(?:set|let)\s+(\w+)\s*\[[^\]]*\]\s*(?::|[-+*/])?=',
                           stripped, re.IGNORECASE)
        if _elem_m:
            return (f';{stripped}  ;NE: OBSE array element write — array_var '
                    f'has no Papyrus equivalent')

        # ...and the READ side, `let item := arr[i]` / `set item to obj.arr[i]`.
        # Only the write was handled, so a read emitted the subscript verbatim
        # ("index expression is only available for arrays") and failed the whole
        # script.  There is no array to read, so the assignment is inert.
        _elem_read_m = re.match(
            r'^(?:set|let)\s+\S+\s+(?:to|:=)\s*[\w.]+\s*\[[^\]]*\]\s*$',
            stripped, re.IGNORECASE)
        if _elem_read_m:
            return (f';{stripped}  ;NE: OBSE array element read — array_var '
                    f'has no Papyrus equivalent')

        set_m = re.match(r'^set\s+(\S+)\s+to\s+(.*)', stripped, re.IGNORECASE)
        if set_m:
            target = self._convert_ref(set_m.group(1), extends)
            value = self._convert_expression(set_m.group(2), extends)
            # `set <ref> to GetFirstRef <type>` opens an OBSE ref-walk: remember
            # which variable it drives so the `Label` that follows can emit the
            # matching `While (<ref> != None)`.
            if re.match(r'^\s*getfirstref\b', set_m.group(2), re.IGNORECASE):
                self._refwalk_var = target
            # A PARKED timer's own countdown (`set t to t - getSecondsPassed`)
            # is a read-modify-write on a variable the dialogue thread clears
            # asynchronously — emit it park-safe rather than literally.  Tested
            # on the RAW source spelling: the target and the value must name the
            # SAME timer, which is only visible before `_convert_ref` renames
            # the target and `_resolve_parked_timer_expr` collapses the value.
            cd_m = self._COUNTDOWN_RE.match(stripped)
            if cd_m and cd_m.group(1).lower() == cd_m.group(2).lower():
                safe = self._parked_decrement(
                    target, self._convert_expression(cd_m.group(3), extends))
                if safe:
                    return safe
            # A timer PARKED by a Say (see _say_seconds) holds a sentinel, not
            # the line length TES4 put there, so reading it back and adjusting
            # ("convTimer = timer - .5" to cut a speaker off, "timer + 10" for
            # an inter-stage beat) would propagate the sentinel and stall the
            # conversation. The line is over when the End fragment clears the
            # timer to 0, so the faithful value of these expressions is the
            # ADJUSTMENT alone.
            value = self._resolve_parked_timer_expr(value)
            # A beat redirect: `set convTimer to convTimer + 2.5` becomes a
            # write to the pending-beat property instead of the timer, so the
            # loop's countdown cannot erode it before the line ends.
            if value.startswith('__BEAT__'):
                return (f'{self._beat_property(target)} = {value[8:]}'
                        "  ;pause after this line; applied when it ends")
            # Can't assign to Self/GetTargetActor()/akSpeakerRef in Papyrus
            if target in ('Self', 'GetTargetActor()', 'akSpeakerRef'):
                return f';{target} = {value}  ;cannot assign to Self in Papyrus'
            # A cross-script write whose variable the owner script never
            # declares is dangling in the ORIGINAL mod, not a conversion bug:
            # three Nehrim scripts write `AutoSaveQuest.ReadyForAutosave`, which
            # AutoSaveQuestScript does not define.  Oblivion silently ignored it;
            # Papyrus fails the whole file ("field or property not found"), so
            # comment it out and keep the rest of the script.
            _dangling = self._dangling_cross_script_target(set_m.group(1))
            if _dangling:
                return f';{target} = {value}  ;{_dangling}'
            # akSpeakerRef is ObjectReference; cast when assigned to Actor-typed fields
            if extends == 'TopicInfo' and value == 'akSpeakerRef':
                value = '(akSpeakerRef as Actor)'
            # In AME/TopicInfo scripts, Self refers to the target actor, not the script
            if value == 'Self':
                if extends == 'ActiveMagicEffect':
                    value = 'GetTargetActor()'
                elif extends == 'TopicInfo':
                    value = '(akSpeakerRef as Actor)'
            if value.lstrip().startswith(';TODO:'):
                # Use None for ref-typed targets, 0 for others
                tgt_low_todo = target.lower().split('.')[-1]
                tgt_type_todo = self._var_types.get(tgt_low_todo, '') or self._property_refs.get(target, self._property_refs.get(tgt_low_todo, ''))
                if tgt_type_todo == 'GlobalVariable':
                    return f'{target}.SetValue(0)  {value}'
                dflt = 'None' if tgt_type_todo in ('ObjectReference', 'Actor', 'ActorBase') or tgt_type_todo.startswith('TES4_') else '0'
                return f'{target} = {dflt}  {value}'
            value = self._fix_ref_zero(target, value)
            # Say() returns None in Papyrus but TES4 returned audio duration
            if '.Say(' in value or value.startswith('Say('):
                # Extract the Say() call using balanced-paren matching
                # TES4: "set timer to (ref.Say topic args) + delay" → "(ref.Say(topic)) + 0.2"
                say_idx = value.find('.Say(')
                if say_idx < 0:
                    say_idx = value.find('Say(')
                if say_idx >= 0:
                    # Find the closing paren of the Say call args
                    paren_start = value.index('(', say_idx)
                    depth = 0
                    paren_end = paren_start
                    for ci in range(paren_start, len(value)):
                        if value[ci] == '(':
                            depth += 1
                        elif value[ci] == ')':
                            depth -= 1
                            if depth == 0:
                                paren_end = ci
                                break
                    # Find the start of the Say expression: scan backward with paren depth
                    expr_start = 0
                    bk_depth = 0
                    for ci in range(say_idx - 1, -1, -1):
                        ch = value[ci]
                        if ch == ')':
                            bk_depth += 1
                        elif ch == '(':
                            if bk_depth > 0:
                                bk_depth -= 1
                            else:
                                expr_start = ci + 1
                                break
                        elif ch in ' \t' and bk_depth == 0:
                            expr_start = ci + 1
                            break
                    say_call = value[expr_start:paren_end + 1]
                    # Strip balanced outer wrapping parens: "(ref.Say(topic))" → "ref.Say(topic)"
                    if say_call.startswith('(') and say_call.endswith(')'):
                        inner = say_call[1:-1]
                        d = 0
                        balanced = True
                        for ch in inner:
                            if ch == '(':
                                d += 1
                            elif ch == ')':
                                d -= 1
                                if d < 0:
                                    balanced = False
                                    break
                        if balanced and d == 0:
                            say_call = inner
                    remainder = value[paren_end + 1:].strip()
                    # If remainder is just "+ number", extract the delay for the timer
                    delay_m = re.match(r'[+\-]\s*([\d.]+)', remainder) if remainder else None
                    # TES4 `set t to Say ...` charged the line's duration and the
                    # owning script counted it down before the next speaker went.
                    #
                    # ORDER MATTERS, and BOTH orders have failed in game:
                    #
                    #  * Charge AFTER the Say: a SHORT line's End fragment (which
                    #    zeroes the timer and advances `speaker`) runs on the
                    #    engine's dialogue thread BEFORE this statement lands, so
                    #    the charge RESURRECTS a timer that was already released.
                    #    The same speaker's guard reopens, it re-Says the same
                    #    line, and the counter never moves — NPCs repeating a line
                    #    "sometimes, not the same each run" (CharacterGen lines
                    #    12/13 re-fired 2x and 4x, ~3s apart).
                    #
                    #  * Charge BEFORE with nothing else: a DROPPED Say (no INFO
                    #    qualifies) leaves a stale charge nobody clears, holding
                    #    the next speaker's `timer <= 0` guard shut.
                    #
                    # Charge BEFORE, which shuts this speaker's guard immediately
                    # so a re-fire is impossible, and let the End fragment clear
                    # it. The stale-charge case is now harmless: the fragment's
                    # sequence gate (pipeline._sequence_gate) makes the counter
                    # authoritative, so a stranded timer only costs one line's
                    # pacing before it drains.
                    delay_f = (self._say_seconds(say_call) +
                               (float(delay_m.group(1)) if delay_m else 0.0))
                    delay_val = f'{delay_f:g}'
                    # An Int target (TES4 `short`) can't take a Float literal
                    if self._var_types.get(target.lower().split('.')[-1]) == 'Int':
                        delay_val = str(int(delay_f))
                    return (f'{target} = {delay_val}'
                            '  ;line length; the End fragment clears it early\n'
                            f'  {say_call}')
                secs = self._say_seconds(value)
                say_dflt = (str(int(round(secs))) if self._var_types.get(
                    target.lower().split('.')[-1]) == 'Int'
                    else f'{secs:g}')
                return (f'{target} = {say_dflt}'
                        '  ;line length; the End fragment clears it early\n'
                        f'  {value}')
            # GlobalVariable: use SetValue() instead of direct assignment
            if self._is_global_target(target):
                # Strip inline TODO comments from value to avoid broken parentheses
                val_clean = value.split(';TODO')[0].rstrip() if ';TODO' in value else value
                todo_part = '  ;TODO' + value.split(';TODO', 1)[1] if ';TODO' in value else ''
                return f'{target}.SetValue({val_clean}){todo_part}'
            # fQuestDelayTime cross-script access → RegisterForUpdate
            if target.endswith('.fQuestDelayTime'):
                quest_ref = target.rsplit('.', 1)[0]
                return f'{quest_ref}.RegisterForUpdate({value})'
            # Float→Int coercion: if target is Int and value is from a Float-returning function, cast
            value = self._coerce_float_to_int(target, value)
            # ObjectReference→Actor coercion: if target is Actor and value is ObjectReference param, cast
            value = self._coerce_ref_to_actor(target, value)
            # Cross-script ref→Int mismatch: TES4 allowed storing refs in short variables
            if '.' in target:
                parts = target.split('.', 1)
                owner_type = self._property_refs.get(parts[0], self._property_refs.get(parts[0].lower(), ''))
                if owner_type and owner_type.startswith('TES4_'):
                    remote_script = owner_type[5:].lower()
                    remote_vars = self.xref.script_all_vars.get(remote_script, {})
                    remote_type = remote_vars.get(parts[1].lower(), '')
                    val_low = value.strip().lower()
                    is_ref_value = (
                        'gettargetactor()' in val_low or
                        'getself' in val_low or
                        val_low == 'self' or
                        val_low == 'akspeakerref' or
                        self._OBJREF_RETURNING.search(value.strip()) is not None
                    )
                    if remote_type == 'Int' and is_ref_value:
                        return f';{target} = {value}  ;TES4 stored ref in short'
            return f'{target} = {value}'

        # let X := Y (OBSE), plus the compound forms `let X += Y` / -= *= /=.
        # Papyrus has no compound assignment, so they expand to `X = X op Y`;
        # without this they fell through to the function-call path and came out
        # as `Let(X, +=, Y)`, which does not compile.
        let_m = re.match(r'^let\s+(\S+)\s*(?::|([-+*/]))=\s*(.*)',
                         stripped, re.IGNORECASE)
        if let_m:
            target = self._convert_ref(let_m.group(1), extends)
            value = self._convert_expression(let_m.group(3), extends)
            op = let_m.group(2)
            if op and not value.lstrip().startswith(';TODO:'):
                if self._is_global_target(target):
                    # A compound write to a global reads through GetValue() and
                    # writes back through SetValue().
                    return (f'{target}.SetValue({target}.GetValue() '
                            f'{op} {value})')
                return (f'{target} = '
                        f'{self._coerce_float_to_int(target, f"{target} {op} {value}")}')
            if value.lstrip().startswith(';TODO:'):
                tgt_low_todo = target.lower().split('.')[-1]
                tgt_type_todo = self._var_types.get(tgt_low_todo, '') or self._property_refs.get(target, self._property_refs.get(tgt_low_todo, ''))
                dflt = 'None' if tgt_type_todo in ('ObjectReference', 'Actor', 'ActorBase') or tgt_type_todo.startswith('TES4_') else '0'
                return f'{target} = {dflt}  {value}'
            value = self._fix_ref_zero(target, value)
            # OBSE `let` assigns just like `set`, so it needs the SAME Float→Int
            # coercion.  Without it `let i := value / 24` (i short, value float)
            # emitted a bare Float assignment to an Int and the CK rejected it.
            value = self._coerce_float_to_int(target, value)
            # ...and the same GlobalVariable treatment: a global is an OBJECT in
            # Papyrus, so it is written through SetValue().  The `set` path did
            # this and `let` did not, so an OBSE-style global write emitted
            # `Global = <float>` and failed the whole script to compile.
            if self._is_global_target(target):
                return f'{target}.SetValue({value})'
            return f'{target} = {value}'

        # if / elseif — TES4 also writes `if((x))` with no space, which must not
        # be parsed as a call to a function named "if"
        if_m = re.match(r'^(if|elseif)(?:\s+|(?=\())\s*(.*)', stripped, re.IGNORECASE)
        if if_m:
            keyword = 'If' if if_m.group(1).lower() == 'if' else 'ElseIf'
            condition = self._convert_expression(if_m.group(2), extends)
            condition = self._rescale_hour_window_latch(condition)
            # If the condition converted entirely to a ;TODO comment, keep the
            # block structure valid by using True as placeholder
            if condition.lstrip().startswith(';TODO:'):
                return f'{keyword} True  {condition}'
            return f'{keyword} {condition}'

        # OBSE `while (cond) ... loop` — Papyrus spells the same construct
        # `While (cond) ... EndWhile`.  Without this the keyword fell through to
        # the generic call path and emitted `while((index,  < , numFollowers))`,
        # taking down the file and everything that imports it.  The `(?=\()`
        # branch matches the no-space form `while(x)` the same way `if` does.
        while_m = re.match(r'^while(?:\s+|(?=\())\s*(.*)', stripped, re.IGNORECASE)
        if while_m:
            condition = self._convert_expression(while_m.group(1), extends)
            if condition.lstrip().startswith(';TODO:'):
                return f'While True  {condition}'
            return f'While {condition}'
        if low == 'loop' or low == 'endwhile':
            return 'EndWhile'

        if low == 'else':
            return 'Else'
        # TES4 allows "else <condition>" as equivalent to "elseif <condition>"
        if low.startswith('else ') and not low.startswith('elseif'):
            rest = stripped[5:].strip()
            if rest:
                # TES4 also allows "else if <cond>" — strip leading 'if' keyword
                rest_low = rest.lower()
                if rest_low.startswith('if '):
                    rest = rest[3:].strip()
                condition = self._convert_expression(rest, extends)
                if condition.lstrip().startswith(';TODO:'):
                    return f'ElseIf True  {condition}'
                return f'ElseIf {condition}'
            return 'Else'
        if low == 'endif' or low.startswith('endif') and not low[5:6].isalpha():
            return 'EndIf'
        # OBSE `SetFunctionValue X` stages the return value; the `return` that
        # follows delivers it.  Papyrus has no staging step, so remember the
        # value and fold it into the next Return.
        sfv_m = re.match(r'^setfunctionvalue\b\s*(.*)$', stripped, re.IGNORECASE)
        if sfv_m:
            staged = (self._convert_expression(sfv_m.group(1).strip(), extends)
                      if sfv_m.group(1).strip() else '')
            self._udf_return_value = staged
            # OBSE does NOT require a `return` after SetFunctionValue: the body
            # may simply run off the end of the `begin Function` block, and the
            # last staged value is what the caller receives.
            # mwGetFactionWitnessesFunc does exactly that — it stages 0 up front,
            # stages 1 deep inside the witness search, and never returns — so
            # folding "into the following Return" dropped the 1 on the floor and
            # the function was a constant false.  That silently disabled guild
            # expulsion everywhere it is called.
            #
            # A staged value INSIDE a conditional branch has to be delivered
            # where it is staged, because control may never reach another
            # statement. At the body's top level there is still a following
            # Return (or the synthesised one) to carry it, and returning early
            # there would skip the rest of the body, so only nested stages are
            # promoted.
            if self._block_depth > 0:
                self._udf_return_value = ''
                return f'Return {staged or "0"}  ;OBSE SetFunctionValue'
            return f';SetFunctionValue folded into the following Return: {stripped}'
        if low == 'return':
            if self._udf_return_value:
                val = self._udf_return_value
                self._udf_return_value = ''
                return f'Return {val}'
            # Inside a value-returning function every path must return a value.
            return 'Return 0' if self._udf_returns else 'Return'
        # return followed by a comment or anything (TES4 return has no value)
        if low.startswith('return ') or low.startswith('return;'):
            rest = stripped[6:].strip()
            if rest.startswith(';'):
                return f'Return  {rest}'
            value = self._fix_ref_zero(target, value)
            return f'{target} = {value}'

        return self._convert_function_call(stripped, extends)

    def _fix_ref_zero(self, target: str, value: str) -> str:
        """If target is a ref-typed variable and value is an integer literal, return 'None'.

        TES4 scripts often use ref vars as boolean flags (set refVar to 0/1).
        In Papyrus, Actor/ObjectReference cannot hold integers, so convert to None.
        """
        val_stripped = value.strip()
        if not re.match(r'^-?\d+$', val_stripped):
            return value
        # Check local/declared var type first (takes priority)
        tgt_low = target.lower().split('.')[-1]  # handle quest.var as var
        vtype = self._var_types.get(tgt_low, '')
        if vtype in ('ObjectReference', 'Actor', 'ActorBase') or vtype.startswith('TES4_'):
            return 'None'
        if vtype:
            return value  # Known non-ref type, don't convert
        # Check property refs (cross-script variables) only if not a declared var
        ptype = self._property_refs.get(target, self._property_refs.get(tgt_low, ''))
        if ptype in ('ObjectReference', 'Actor', 'ActorBase') or ptype.startswith('TES4_'):
            # But if the cross-script var was retyped to Int via ref_as_int, keep integer
            if '.' in target and self.xref and self._is_ref_as_int_crossscript(target):
                return value  # retyped to Int, keep integer
            return 'None'
        # Check cross-script ref type via xref graph (e.g. MQ00.nearOblivionGate)
        if '.' in target:
            parts = target.split('.', 1)
            if self.xref and self.xref.is_remote_ref_var(parts[0], parts[1]):
                if self._is_ref_as_int_crossscript(target):
                    return value
                return 'None'
            # Also check via property type → script_all_vars (property name != EditorID)
            if self._is_ref_typed_access(target):
                if self._is_ref_as_int_crossscript(target):
                    return value
                return 'None'
        return value

    # Functions that return Float in Papyrus (need 'as Int' cast when assigned to Int vars)
    _FLOAT_RETURNING_FUNCS = re.compile(
        r'(?:GetBaseActorValue|GetActorValue|GetAV|GetSecondsPassed|GetDistance|'
        r'GetPosition[XYZ]|GetAngle[XYZ]|GetHeadingAngle|GetScale|GetLevel|'
        r'GetPos[XYZ]|GetWalkSpeed|GetCurrentTime|RandomFloat|Utility\.RandomFloat|'
        r'GetHeight|GetWidth|GetLength|GetValue)\s*\(', re.IGNORECASE)

    # Papyrus functions that return Bool where the TES4 original returned an
    # Int 0/1.  Oblivion scripts freely write `getdetected X > 0` / `getdead ==
    # 0`, but Papyrus refuses to order or add a Bool ("cannot relatively compare
    # variables of type bool", "cannot add a bool to a int"), so these need an
    # explicit `as Int` wherever they meet a number.
    # (name list defined below, shared with _BOOL_CMP_RE)

    # A Bool-returning call placed in a RELATIONAL comparison against a number.
    # `X.IsDead() > 0` must become `(X.IsDead() as Int) > 0`.  The argument list
    # may itself contain a call (`IsDetectedBy(Game.GetPlayer())`), so the arg
    # pattern allows one level of nested parentheses.
    _BOOL_FUNC_NAMES = (
        r'IsDetectedBy|HasLOS|CanSee|IsInDialogueWithPlayer|IsRidingMount|'
        r'IsInCombat|IsAnimPlaying|GetDetected|IsDead|IsRunning|IsLocked|'
        r'IsEnabled|IsHostileToActor|IsWeaponDrawn|IsSneaking|IsSwimming|'
        r'IsInInterior|IsChild|IsEssential|IsInFaction|IsGuard|IsPlayerTeammate|'
        r'IsAlarmed|IsAlerted|IsUnconscious|IsBleedingOut|IsTrespassing|'
        r'HasKeyword|HasSpell|HasPerk|HasMagicEffect|IsCompleted|IsObjectiveCompleted')
    _ARGS = r'(?:[^()]|\([^()]*\))*'      # args, allowing one nesting level
    _BOOL_CMP_RE = re.compile(
        r'((?:\w+(?:\(' + _ARGS + r'\))?\.)*'              # optional receiver chain
        r'(?:' + _BOOL_FUNC_NAMES + r')'
        r'\s*\(' + _ARGS + r'\))'                          # the call itself
        r'(\s*(?:>=|<=|>|<)\s*-?\d+(?:\.\d+)?)',           # relational op + number
        re.IGNORECASE)
    # Same functions, matched as a method call anywhere in an expression — used
    # to add `as Int` when one is ASSIGNED to a TES4 short/long variable.
    _BOOL_RETURNING_FUNCS = re.compile(
        r'\.(?:' + _BOOL_FUNC_NAMES + r')\s*\(', re.IGNORECASE)

    @staticmethod
    def _cast(expr: str, ptype: str) -> str:
        """Cast `expr` to `ptype`, unless it is already cast to it.

        Papyrus rejects a doubled cast (`X as Int as Int`) outright, and several
        handlers emit their own cast before the caller adds one.
        """
        if re.search(rf'\bas\s+{ptype}\s*$', expr, re.IGNORECASE):
            return expr
        return f'{expr} as {ptype}'

    # The hour-boundary guard these scripts use: `GameHour >= X.98` /
    # `GameHour <= X.02`, i.e. a window HALF_WINDOW_GAME_HOURS wide either
    # side of the top of the hour.
    _HOUR_WINDOW_GAME_HOURS = 0.04
    # Both games ship GLOB 0x3A TimeScale = 30 by default, and every vanilla
    # Oblivion chime script is tuned against that.
    _DEFAULT_TIMESCALE = 30.0
    _GAMEHOUR_WINDOW_RE = re.compile(
        r'\bGameHour\b\s*(?:>=|<=)\s*\d+\.\d+', re.IGNORECASE)

    def _timescale(self) -> float:
        """The plugin's own TimeScale (GLOB 0x3A), or the vanilla default."""
        if self.xref:
            val = self.xref.global_values.get('timescale')
            if val and val > 0:
                return float(val)
        return self._DEFAULT_TIMESCALE

    def _scaled_debounce_seconds(self, seconds: float) -> float:
        """Widen a chime script's real-seconds debounce to outlast its window.

        The bell/chime idiom is a one-shot latch: a `GameHour` window at the
        top of the hour sets `soundplaying = 1`, and a countdown holds the
        latch until it passes a negative sentinel.  The window is measured in
        GAME hours but the sentinel in REAL seconds, so the two only stay in
        step at the TimeScale the author used.

        At Oblivion's TimeScale 30 a 0.04-game-hour window is 4.8 real
        seconds, which the stock -5 sentinel just outlasts — the latch clears
        after the window has closed and the bell rings once.  Nehrim ships
        TimeScale 10, stretching the same window to 14.4 real seconds: the
        latch expires twice while GameHour is STILL inside it, and each
        expiry re-fires the sound.  That is the chapel bell ringing on
        repeat, and it is not a Papyrus artifact — Oblivion's own interpreter
        does the same thing at TimeScale 10.

        Scale the sentinel by TimeScale so the latch always outlasts its
        window, keeping the authored value whenever it is already long enough
        (so vanilla Oblivion output is unchanged).
        """
        needed = self._HOUR_WINDOW_GAME_HOURS * 3600.0 / self._timescale()
        if seconds > needed:
            # The authored sentinel already outlasts the window (this is the
            # vanilla-Oblivion case: 5s latch vs a 4.8s window).  Leave it
            # exactly as written so TimeScale-30 output is byte-identical.
            return seconds
        # Otherwise clear the window with a margin, so neither a coarse update
        # tick nor float slop can land the expiry back inside it.
        return needed * 1.25

    # `timer <= -5` — the chime latch's expiry test.  The sentinel is negative
    # because the countdown runs past zero; its magnitude is how many REAL
    # seconds the latch holds.
    _LATCH_EXPIRY_RE = re.compile(
        r'^(?P<head>.*?\b\w[\w.]*\s*<=\s*)-(?P<secs>\d+(?:\.\d+)?)(?P<tail>\s*\)?\s*)$')

    def _rescale_hour_window_latch(self, condition: str) -> str:
        """Rescale a chime latch's expiry sentinel for this plugin's TimeScale.

        Only touches scripts that actually use the top-of-the-hour GameHour
        window (`_uses_hour_window`), so ordinary timers keep their authored
        durations.  See _scaled_debounce_seconds for why this is needed.
        """
        if not self._uses_hour_window:
            return condition
        m = self._LATCH_EXPIRY_RE.match(condition)
        if not m:
            return condition
        authored = float(m.group('secs'))
        scaled = self._scaled_debounce_seconds(authored)
        if abs(scaled - authored) < 0.05:
            return condition
        self._line_comments.append(
            f';NE: chime latch widened {authored:g}s -> {scaled:.3g}s for '
            f'TimeScale {self._timescale():g} (window is '
            f'{self._HOUR_WINDOW_GAME_HOURS * 3600.0 / self._timescale():.3g}s '
            f'of real time)')
        return f"{m.group('head')}-{scaled:.3g}{m.group('tail')}"

    # Engine-owned globals that Oblivion declares `short` but that carry a
    # genuine fractional value at runtime (and that Skyrim declares float).
    # GameHour is 0x00000038 in both games; Oblivion's own bell scripts bracket
    # the top of the hour with `>= X.98 / <= X.02` windows, which only ever
    # match because the read is fractional.
    # Deliberately NOT here: TimeScale (Skyrim FNAM=115, genuinely short) and
    # GameDaysPassed.  Skyrim declares GameDaysPassed float (FNAM=102, Ord('f')
    # per xEdit's GLOB definition), but OBLIVION declares it Short
    # (GLOB 00000039, FNAM.Type=s), so the source scripts only ever saw whole
    # days and the `as Int` truncation is what REPRODUCES their behaviour.  That
    # matters beyond the day-of-week idiom: 72 lines across 28 scripts compare
    # it against script floats, several by exact equality
    # (MS39Script: `GameDaysPassed == (CurrentDay + 1)`), which only ever
    # matched in Oblivion because both sides were whole numbers.
    _FRACTIONAL_ENGINE_GLOBALS = frozenset(('gamehour',))

    def _global_read(self, safe: str) -> str:
        """Emit a GlobalVariable read, casting to Int only when that is lossless.

        A blanket `as Int` truncates float globals, which silently turns any
        fractional comparison into a whole-number one.  For GameHour that
        collapsed each `>= 23.98 || <= 0.02` hour-boundary window into an
        always-true test, so the guarded body ran every single frame — the
        Erodans-Kapelle chapel bell (and Oblivion's BellTowerScript) rang
        continuously instead of once on the hour.
        """
        low = safe.lower()
        gtype = ''
        if self.xref:
            gtype = self.xref.global_types.get(low, '')
        if low in self._FRACTIONAL_ENGINE_GLOBALS or gtype == 'f':
            return f'{safe}.GetValue()'
        return f'{safe}.GetValue() as Int'

    def _coerce_float_to_int(self, target: str, value: str) -> str:
        """Add 'as Int' cast when assigning Float-returning function to Int variable."""
        tgt_low = target.lower().split('.')[-1]
        vtype = self._var_types.get(tgt_low, '')
        if not vtype:
            vtype = self._property_refs.get(target, self._property_refs.get(tgt_low, ''))
        # Cross-script type resolution: Owner.Var → look up var type on remote script
        if not vtype and '.' in target and self.xref:
            parts = target.split('.', 1)
            owner_type = self._property_refs.get(parts[0], self._property_refs.get(parts[0].lower(), ''))
            if owner_type and owner_type.startswith('TES4_'):
                remote_script = owner_type[5:].lower()
                remote_vars = self.xref.script_all_vars.get(remote_script, {})
                vtype = remote_vars.get(parts[1].lower(), '')
        if vtype != 'Int':
            return value
        # Already an Int-typed expression.  Several handlers emit their own cast
        # (`gamedayspassed` -> `GameDaysPassed.GetValue() as Int`), and casting
        # that again produces `X as Int as Int`, which Papyrus cannot parse —
        # this was the single biggest CK compile error (1965 of them).
        #
        # But a trailing `as Int` only types the WHOLE expression when it is not
        # sitting inside arithmetic: `as` binds tighter than the operators, so
        # `GetBaseActorValue("Magicka") - X.GetValue() as Int` is
        # `Float - Int` — still Float, and rejected on assignment to an Int.
        # Only skip when the cast really does cover everything.
        _tail_cast = re.search(r'\bas\s+Int\s*$', value, re.IGNORECASE)
        if _tail_cast:
            head = value[:_tail_cast.start()]
            # Arithmetic outside any parenthesised group means the cast applies
            # to the last operand only.
            _depth = 0
            _bare_op = False
            for _ch in head:
                if _ch == '(':
                    _depth += 1
                elif _ch == ')':
                    _depth -= 1
                elif _depth == 0 and _ch in '+-*/%':
                    _bare_op = True
                    break
            if not _bare_op:
                return value
            # Drop the inner cast and wrap the whole expression instead, so the
            # arithmetic happens in Float and the RESULT becomes the Int.
            return f'({head.rstrip()}) as Int'
        if self._FLOAT_RETURNING_FUNCS.search(value):
            # Wrap in parens if expression contains arithmetic to prevent binding issues
            if re.search(r'[+\-*/]', value):
                return f'({value}) as Int'
            return f'{value} as Int'
        # Also detect float literals in arithmetic (e.g. X * 0.8, -50 * 0.5)
        if re.search(r'\d+\.\d+', value):
            return f'({value}) as Int'
        # Detect Float variables in the value — with OR without arithmetic.
        # A plain Float-to-Int copy needs the cast just as much as an
        # expression does: `ihour = vtime` (short = float) and
        # `PositionX = (NullpunktKoordinateX)` were both rejected outright.
        _float_ident = False
        for ident in re.findall(r'\b([a-zA-Z_]\w*)\b', value):
            id_type = self._var_types.get(ident.lower(), '')
            if not id_type:
                id_type = self._property_refs.get(
                    ident, self._property_refs.get(ident.lower(), ''))
            if id_type == 'Float':
                _float_ident = True
                break
        if _float_ident:
            # Parenthesise only when needed — `as` binds tighter than the
            # arithmetic operators, so a bare operand needs no extra parens.
            if re.search(r'[+\-*/]', value):
                return f'({value}) as Int'
            return f'{value} as Int'
        # Bool→Int coercion: functions like IsDetectedBy return Bool, TES4 assigns to Int
        if self._BOOL_RETURNING_FUNCS.search(value):
            return f'{value} as Int'
        return value

    # ObjectReference event parameter names that may need Actor cast
    _OBJREF_PARAMS = {'akactionref', 'aknewcontainer', 'akoldcontainer', 'akcastref',
                      'akactionref', 'akaggressor', 'akcaster'}

    # Functions that return ObjectReference in Papyrus
    _OBJREF_RETURNING = re.compile(
        r'(?:GetLinkedRef|PlaceAtMe|GetParentRef|PlaceActorAtMe|GetEditorLocation|'
        r'GetItemInSlot|GetCombatTarget)\s*\(', re.IGNORECASE)

    def _coerce_ref_to_actor(self, target: str, value: str) -> str:
        """Add 'as Actor' cast when assigning ObjectReference to Actor variable."""
        val_stripped = value.strip()
        val_low = val_stripped.lower()
        # Check if value is an ObjectReference event param, an ObjRef-returning function,
        # or the bare 'akActionRef' identifier
        is_objref_value = (
            val_low in self._OBJREF_PARAMS
            or self._OBJREF_RETURNING.search(val_stripped)
            or val_low == 'akactionref'
        )
        # Check if value is a known ObjectReference variable/property
        if not is_objref_value and '.' not in val_stripped:
            val_type = self._var_types.get(val_low, '')
            if not val_type:
                val_type = self._property_refs.get(val_stripped, self._property_refs.get(val_low, ''))
            if val_type == 'ObjectReference':
                is_objref_value = True
        # Also check cross-script property access returning ObjectReference
        if not is_objref_value and '.' in val_stripped:
            is_objref_value = self._is_ref_typed_access(val_stripped)
            # Even if _is_ref_typed_access returns False (e.g. ref_as_int),
            # cross-script dot access to a ref variable still resolves as ObjectReference
            if not is_objref_value:
                parts = val_stripped.split('.', 1)
                ref_part = parts[0].strip()
                if self.xref.is_quest_ref(ref_part) or ref_part in self._property_refs:
                    is_objref_value = True
        if not is_objref_value:
            return value
        tgt_low = target.lower().split('.')[-1]
        vtype = self._var_types.get(tgt_low, '')
        if vtype in ('Actor', 'ActorBase') or vtype.startswith('TES4_'):
            return f'{value} as Actor'
        # Check property refs too
        ptype = self._property_refs.get(target, self._property_refs.get(tgt_low, ''))
        if ptype in ('Actor', 'ActorBase') or (ptype and ptype.startswith('TES4_')):
            return f'{value} as Actor'
        # Cross-script target: resolve remote property type
        if '.' in target and self.xref:
            parts = target.split('.', 1)
            owner_type = self._property_refs.get(parts[0], self._property_refs.get(parts[0].lower(), ''))
            if owner_type and owner_type.startswith('TES4_'):
                remote_script = owner_type[5:].lower()
                remote_vars = self.xref.script_all_vars.get(remote_script, {})
                remote_type = remote_vars.get(parts[1].lower(), '')
                # A remote `ref` var is only declared Actor when the remote
                # script itself calls an Actor-only method on it.  Casting
                # unconditionally "for safety" was unsafe in the other
                # direction: MQ16 assigns two static markers into
                # MQ16OblivionGate1Script.mySpawnMarker, which that script only
                # ever calls PlaceAtMe on, so it stays ObjectReference — and
                # `marker as Actor` fails the downcast and stores None, leaving
                # both endgame Oblivion gates spawning nothing.
                if (remote_type == 'ObjectReference'
                        and parts[1].lower() in self.xref.script_actor_vars.get(
                            remote_script, ())):
                    return f'{value} as Actor'
        return value

        # Direct assignment: X.Y = Z or X = Z (OBSE-style, no 'set' prefix)
        assign_m = re.match(r'^(\S+)\s*=\s*(.*)', stripped)
        if assign_m:
            target = self._convert_ref(assign_m.group(1), extends)
            value = self._convert_expression(assign_m.group(2), extends)
            # akSpeakerRef is ObjectReference; cross-script fields expecting Actor need a cast
            if extends == 'TopicInfo' and value == 'akSpeakerRef':
                value = '(akSpeakerRef as Actor)'
            return f'{target} = {value}'

        return self._convert_function_call(stripped, extends)

    @staticmethod
    def _split_logical(expr: str, op: str) -> list[str] | None:
        """Split *expr* on a logical operator (``||`` or ``&&``) only at
        top-level — i.e. not inside parentheses.  Returns ``None`` if the
        operator does not appear at top level.
        """
        parts: list[str] = []
        depth = 0
        start = 0
        i = 0
        while i < len(expr):
            c = expr[i]
            if c == '(':
                depth += 1
            elif c == ')':
                depth -= 1
            elif depth == 0 and expr[i:i+len(op)] == op:
                parts.append(expr[start:i])
                start = i + len(op)
                i += len(op)
                continue
            i += 1
        if len(parts) == 0:
            return None
        parts.append(expr[start:])
        return parts

    def _convert_expression(self, expr: str, extends: str) -> str:
        """Convert an Oblivion expression to Papyrus."""
        expr = expr.strip()
        if not expr:
            return expr

        # OBSE `eval <expr>` just forces expression evaluation in a context that
        # would otherwise take a bare command — it contributes nothing to the
        # value.  Nehrim uses it only to wrap `Call` (`if eval (Call Foo x, 1)`),
        # and leaving the keyword in place stopped the inner Call from ever being
        # recognised.  Papyrus evaluates expressions natively, so drop it.
        # `eval` is ONLY the wrapper when a sub-expression FOLLOWS it — Oblivion
        # scripts legally use `Eval` as a variable name (`if Eval == 0`,
        # Dark17FollowingScript), and stripping it there emitted `If  == 0`,
        # which broke the whole Dark Brotherhood script family at compile.
        eval_m = re.match(r'^eval\s+(?![=<>!&|+\-*/%])(.+)$', expr,
                          re.IGNORECASE)
        if eval_m:
            return self._convert_expression(eval_m.group(1).strip(), extends)

        # Quoted EditorID → property ref (TES4 allows quoting form names)
        if len(expr) > 2 and expr[0] == '"' and expr[-1] == '"':
            inner_name = expr[1:-1]
            # A LOCAL VARIABLE may be quoted too: NQ15Turret01SCRIPT declares
            # `ref TowerTargetRef` and then writes `GetDistance
            # "TowerTargetRef"`.  There is no form by that name, so the quotes
            # survived and Papyrus got a String where an ObjectReference was
            # required.  Resolve the variable instead.
            _inner_low = inner_name.lower()
            if _inner_low in self._local_vars:
                return self._var_renames.get(_inner_low, inner_name)
            # `GetDistance "Player"` — the keyword quotes just as readily.
            if _inner_low in ('player', 'playerref'):
                return 'Game.GetPlayer()'
            fid = self.xref.edid_to_formid.get(_inner_low, '')
            if fid:
                rtype = self.xref.record_type.get(fid, '')
                ptype = self._papyrus_type_for(fid, rtype)
                script_type = self.xref.get_record_script_type(inner_name)
                if script_type and self._script_type_binds(ptype, fid):
                    ptype = script_type
                safe = _safe_property_name(inner_name)
                self._property_refs[safe] = ptype
                if ptype == 'GlobalVariable':
                    return self._global_read(safe)
                return safe

        # `"EditorID".Function ...` — TES4 let a quoted form name stand in for the
        # reference itself, and Nehrim uses the style in 143 scripts.  The quotes
        # stop every ref.Func path below from matching (they all anchor on an
        # identifier), so the call fell through and was emitted as a property
        # access on a string: `"1TrapFireMineWorldTrigZoneRef".GetDisabled == 1`.
        # Unquote and re-enter; the bare name resolves through the normal lookup.
        quoted_ref = re.match(r'^"([^"]+)"\s*\.\s*(.+)$', expr)
        if quoted_ref:
            return self._convert_expression(
                f'{quoted_ref.group(1)}.{quoted_ref.group(2)}', extends)

        # Strip balanced outer parens and recurse — TES4 conditions always
        # wrap in parens e.g. "( GetStage Quest >= 10 )" which blocks regex
        if expr.startswith('(') and expr.endswith(')'):
            depth = 0
            balanced = True
            for i, c in enumerate(expr):
                if c == '(': depth += 1
                elif c == ')': depth -= 1
                if depth == 0 and i < len(expr) - 1:
                    balanced = False
                    break
            if balanced:
                inner = self._convert_expression(expr[1:-1].strip(), extends)
                # If inner contains a ;TODO comment, close parens before the comment
                if ';TODO:' in inner or ';TODO ' in inner:
                    semi_idx = inner.index(';TODO')
                    code_part = inner[:semi_idx].rstrip()
                    comment_part = inner[semi_idx:]
                    if not code_part:
                        # Entirely TODO'd — pass through as bare TODO
                        return comment_part
                    return f'({code_part}){comment_part}'
                return f'({inner})'

        expr = expr.replace('<>', '!=')

        # An OBSE array element READ (`arMonthNameEval[GameMonth + 1]`).  There is
        # no Papyrus array behind it — the variable converted to a plain String —
        # so the subscript cannot be honoured.  Drop it and read the variable,
        # which at least keeps the expression well-typed instead of emitting the
        # mangled identifier `arMonthNameEval_GameMonth...`.
        _arr_read = re.match(r'^(\w+)\s*\[[^\]]*\]$', expr)
        if _arr_read and _arr_read.group(1).lower() in self._local_vars:
            return self._var_renames.get(_arr_read.group(1).lower(),
                                         _arr_read.group(1))

        # OBSE's `$expr` sigil casts a number to a string, used to build display
        # text (`let sTime := "0" + $ihour + ":"`).  Papyrus has no `$` — it is
        # not even a legal character, so the whole script died with "Scanner
        # error: invalid character `$`".  An explicit `as String` is the exact
        # equivalent and keeps the concatenation well-typed.
        expr = re.sub(r'\$([A-Za-z_]\w*)', r'(\1 as String)', expr)

        # Fix spaces around dots in method chains (e.g. "Player. GetItemCount" → "Player.GetItemCount")
        expr = re.sub(r'(\w)\.\s+(\w)', r'\1.\2', expr)

        # Split on logical operators first, convert each part independently
        # Handle || and && — paren-aware to avoid splitting inside subexprs
        or_parts = self._split_logical(expr, '||')
        if or_parts is not None:
            converted = [self._convert_expression(p.strip(), extends) for p in or_parts]
            if any(';TODO:' in c for c in converted):
                return f';TODO: {expr}  ;partially unconvertible'
            return ' || '.join(converted)
        and_parts = self._split_logical(expr, '&&')
        if and_parts is not None:
            converted = [self._convert_expression(p.strip(), extends) for p in and_parts]
            if any(';TODO:' in c for c in converted):
                return f';TODO: {expr}  ;partially unconvertible'
            return ' && '.join(converted)

        # TES4 boolean functions compared to 1/0 (e.g., "IsActionRef player == 1", "ref.GetIsRace Argonian == 1")
        _BOOL_FUNC_NAMES = (
            r'IsActionRef|GetDead|IsDead|IsInCombat|IsSneaking|IsWeaponOut|IsSwimming|'
            r'IsGhost|GetLocked|IsEnabled|HasSpell|GetInFaction|GetQuestRunning|GetStageDone|'
            r'GetDetected|IsActorDetected|GetIsID|GetIsRace|GetPCIsRace|GetIsRef|'
            r'GetPCIsClass|GetIsClass|'
            r'GetInCell|GetInSameCell|GetIsSex|IsInFaction|IsEssential|IsInInterior|'
            r'GetIsCurrentPackage|IsOwner|GetTalkedToPCParam|GetTalkedToPC|'
            r'IsActorUsingATorch|IsRidingHorse')
        # The function name must be followed by a WORD BOUNDARY and, when an
        # argument follows, by a real separator (whitespace or the comma form).
        # Making the separator optional let `GetDead` match the prefix of
        # `GetDeadCount X == 1` and split off `Count` as an argument, emitting
        # `IsDead(Count, X)` across 28 scripts.
        bool_comp_m = re.match(
            r'^(?:(\w+)\.)?' + r'(' + _BOOL_FUNC_NAMES
            + r')\b(?:(?:\s*,\s*|\s+)(.+?))?\s*==\s*([01])\s*$',
            expr, re.IGNORECASE)
        if bool_comp_m:
            ref_part = bool_comp_m.group(1)
            fname = bool_comp_m.group(2)
            args_part = (bool_comp_m.group(3) or '').strip()
            bool_val = bool_comp_m.group(4)
            # `IsInCombat, Player == 1` names the RECEIVER, not an argument — the
            # comma form of `Player.IsInCombat`.  These bool commands take no
            # parameters, so passing it through emitted `IsInCombat(Player)`
            # ("function takes 0 parameters not 1").
            if (not ref_part and args_part
                    and fname.lower() in _ZERO_ARG_REF_FUNCTIONS
                    and re.match(r'^[A-Za-z_]\w*$', args_part)):
                ref_part = args_part
                args_part = ''
            converted_call = self._emit_function(ref_part, fname, args_part, extends)
            # If function converted to TODO, propagate it
            if converted_call.lstrip().startswith(';TODO'):
                return converted_call
            if bool_val == '0':
                return f'!({converted_call})'
            return converted_call

        # Pre-split: strip trailing TES4 truth test "== 1" / "== 0"
        # TES4 comparisons return 0/1, so "a == b == 1" means "(a == b) is true"
        trail_m = re.match(r'^(.+\S)\s*==\s*([01])\s*$', expr)
        if trail_m and re.search(r'\s[=!<>]{1,2}\s', trail_m.group(1)):
            inner = trail_m.group(1)
            converted = self._convert_expression(inner, extends)
            if trail_m.group(2) == '1':
                return converted
            return f'!({converted})'

        # Handle comparison operators: split into LHS op RHS (depth-aware)
        # Scan left-to-right for comparison operators at paren depth 0
        comp_m = None
        _depth = 0
        for _ci in range(len(expr)):
            ch = expr[_ci]
            if ch == '(':
                _depth += 1
            elif ch == ')':
                _depth -= 1
            elif _depth == 0:
                # Check for 2-char operators first (==, !=, >=, <=)
                two = expr[_ci:_ci+2]
                if two in ('==', '!=', '>=', '<='):
                    comp_m = (expr[:_ci].strip(), two, expr[_ci+2:].strip())
                    break
                # Single-char: > or < (but not part of >= or <=)
                if ch in ('>', '<') and (_ci + 1 >= len(expr) or expr[_ci+1] != '='):
                    comp_m = (expr[:_ci].strip(), ch, expr[_ci+1:].strip())
                    break
        if comp_m:
            # GetCurrentAIPackage compared against a PACK EditorID: vanilla
            # Papyrus has Actor.GetCurrentPackage(), so this converts exactly.
            pkg_m = re.match(r'^(?:(\w+)\.)?(?:getcurrentaipackage|getcurrentpackage)$',
                             comp_m[0], re.IGNORECASE)
            if pkg_m and comp_m[1] in ('==', '!=') and self.xref:
                cand = comp_m[2].strip().strip('()').strip()
                fid = self.xref.edid_to_formid.get(cand.lower(), '')
                if fid and self.xref.record_type.get(fid, '') == 'PACK':
                    ref = self._resolve_self_ref(pkg_m.group(1), extends,
                                                 actor_func=True)
                    if ref == 'Self' and extends not in ('Actor',):
                        ref = '(Self as Actor)'
                    safe = _safe_property_name(cand)
                    self._property_refs[safe] = 'Package'
                    return f'{ref}.GetCurrentPackage() {comp_m[1]} {safe}'
                # A NUMERIC comparand is a TES4 package-TYPE code (5 Wander,
                # 6 Travel, ... — xEdit wbPackageTypeEnum).  Skyrim exposes no
                # way to read a Package's type from Papyrus, but the set of
                # packages an actor can be running is fixed at conversion time
                # by its own AIPackage list, so the type test is reconstructed
                # as an equality against each of that actor's packages of the
                # requested type.  Falls through to the `0` no-op when the
                # actor or its package list cannot be resolved, which is the
                # old behaviour.
                if re.fullmatch(r'\d+', cand):
                    packs = self._packages_of_type(pkg_m.group(1), int(cand))
                    if packs:
                        ref = self._resolve_self_ref(pkg_m.group(1), extends,
                                                     actor_func=True)
                        if ref == 'Self' and extends not in ('Actor',):
                            ref = '(Self as Actor)'
                        join = ' || ' if comp_m[1] == '==' else ' && '
                        terms = []
                        for p in packs:
                            safe = _safe_property_name(p)
                            self._property_refs[safe] = 'Package'
                            terms.append(f'{ref}.GetCurrentPackage() '
                                         f'{comp_m[1]} {safe}')
                        body = join.join(terms)
                        return body if len(terms) == 1 else f'({body})'
            lhs = self._convert_expression(comp_m[0], extends)
            op = comp_m[1]
            rhs_src = comp_m[2]
            # A comparison RHS of `<reference> <number>` is a TES4 idiom where
            # the reference token is redundant: Oblivion's parser reads the
            # comparand as the trailing number and ignores the leading name.
            # `GetDistance, Player <= Player 500` means `... <= 500`.  Keeping
            # the token emitted `<= Game.GetPlayer() 500`, which is not an
            # expression at all, so the whole script failed to compile.  Only a
            # bare identifier followed by a lone numeric literal is stripped —
            # anything with an operator between them is a real expression.
            stray_m = re.match(r'^([A-Za-z_]\w*)\s+(-?\d+(?:\.\d+)?)$',
                               rhs_src.strip())
            if stray_m:
                rhs_src = stray_m.group(2)
            rhs = self._convert_expression(rhs_src, extends)
            # If LHS is entirely a TODO comment, propagate it
            if lhs.lstrip().startswith(';TODO'):
                return lhs
            # If LHS contains an inline TODO comment, extract code part for comparison
            if ';TODO' in lhs:
                semi_idx = lhs.index(';TODO')
                code_part = lhs[:semi_idx].rstrip()
                comment_part = lhs[semi_idx:]
                return f'{code_part} {op} {rhs}  {comment_part}'
            # Fix ref == 0 / ref != 0 → ref == None / ref != None
            if rhs.strip() == '0' and op in ('==', '!='):
                lhs_var = lhs.strip().lower().split('.')[-1]
                lhs_raw = lhs.strip().split('.')[-1]
                lhs_type = self._var_types.get(lhs_var, '') or self._property_refs.get(lhs_raw, self._property_refs.get(lhs_var, ''))
                is_ref = lhs_type in ('ObjectReference', 'Actor', 'ActorBase') or lhs_type.startswith('TES4_')
                # Self is always a ref type
                if not is_ref and lhs.strip() == 'Self':
                    is_ref = True
                # Ref-returning function calls: GetContainer(), GetLinkedRef(), etc.
                if not is_ref and re.search(r'\.Get(?:Container|LinkedRef|ParentRef)\(\)', lhs.strip(), re.IGNORECASE):
                    is_ref = True
                # Also check cross-script ref type (e.g. MQ00.nearOblivionGate == 0)
                if not is_ref and '.' in lhs.strip():
                    is_ref = self._is_ref_typed_access(lhs.strip())
                if is_ref:
                    rhs = 'None'
            # Reversed: 0 == ref / 0 != ref → None == ref / None != ref
            if lhs.strip() == '0' and op in ('==', '!='):
                rhs_var = rhs.strip().lower().split('.')[-1]
                rhs_raw = rhs.strip().split('.')[-1]
                rhs_type = self._var_types.get(rhs_var, '') or self._property_refs.get(rhs_raw, self._property_refs.get(rhs_var, ''))
                is_ref = rhs_type in ('ObjectReference', 'Actor', 'ActorBase') or rhs_type.startswith('TES4_')
                if not is_ref and rhs.strip() == 'Self':
                    is_ref = True
                if not is_ref and '.' in rhs.strip():
                    is_ref = self._is_ref_typed_access(rhs.strip())
                if is_ref:
                    lhs = 'None'
            # ObjectReference variable in numeric comparison (<, <=, >, >=) with a
            # number: TES4 undeclared variables default to 0, so this is the
            # name-collision-with-a-form case.
            #
            # It must NOT swallow the null-check shapes.  `ref > 0` / `ref >= 1`
            # / `ref <= 0` is Oblivion's standard "is this ref set" idiom and is
            # handled by the block below (and by the `== 0` block above), but
            # this ran FIRST and flattened the whole left side to the literal 0
            # — which both killed the test and dropped the operator behind an
            # inline comment, emitting `If combattarget != Player && 0`.  All 8
            # occurrences in Oblivion.esm were null checks, none a collision:
            # MQ04's `elseif speaker > 0` is the entire Cloud Ruler conversation
            # driver (Martin/Jauffre/Cyrus never spoke a line), MQ09's
            # `elseif restrainedRef > 0` releases the first ghost blade, and
            # CGEmperorScript's `combattarget > 0` gates the Blades' call for
            # help when someone other than the player attacks the Emperor.
            _is_null_check_shape = (
                (op in ('>', '>=') and rhs.strip() in ('0', '1'))
                or (op == '<=' and rhs.strip() == '0'))
            if (op in ('<', '<=', '>', '>=')
                    and re.match(r'^-?\d+(\.\d+)?$', rhs.strip())
                    and not _is_null_check_shape):
                lhs_var = lhs.strip().lower().split('.')[-1]
                lhs_raw = lhs.strip().split('.')[-1]
                lhs_type = self._var_types.get(lhs_var, '') or self._property_refs.get(lhs_raw, self._property_refs.get(lhs_var, ''))
                if lhs_type == 'ObjectReference':
                    lhs = '0  ;undeclared TES4 var'
            # ref > 0 / ref >= 1 → ref != None, and ref <= 0 → ref == None
            # (null-check patterns for ref types).  TES4 refs coerce to 0 when
            # unset, so scripts test them with ordering operators.
            if (rhs.strip() in ('0', '1') and op in ('>', '>=')) or \
                    (rhs.strip() == '0' and op == '<='):
                lhs_var = lhs.strip().lower().split('.')[-1]
                lhs_raw = lhs.strip().split('.')[-1]
                lhs_type = self._var_types.get(lhs_var, '') or self._property_refs.get(lhs_raw, self._property_refs.get(lhs_var, ''))
                is_ref = lhs_type in ('ObjectReference', 'Actor', 'ActorBase') or lhs_type.startswith('TES4_')
                if not is_ref and '.' in lhs.strip():
                    is_ref = self._is_ref_typed_access(lhs.strip())
                if not is_ref and '.' in lhs.strip():
                    parts = lhs.strip().split('.', 1)
                    if self.xref and self.xref.is_remote_ref_var(parts[0], parts[1]):
                        is_ref = True
                if is_ref:
                    return f'{lhs} == None' if op == '<=' else f'{lhs} != None'
            # TES4 boolean comparison: (comparison_expr) == 1 → comparison_expr
            # In TES4, comparisons return 0/1 so "== 1" checks truth, "== 0" checks false
            if op in ('==', '!=') and rhs.strip() in ('0', '1'):
                # Check if LHS already contains a comparison (implying boolean result)  
                inner = lhs.strip()
                if inner.startswith('(') and inner.endswith(')'):
                    inner_content = inner[1:-1]
                    if re.search(r'\s(==|!=|>=|<=|>|<)\s', inner_content):
                        if (op == '==' and rhs.strip() == '1') or (op == '!=' and rhs.strip() == '0'):
                            return lhs  # Already a boolean, == 1 is redundant
                        if (op == '==' and rhs.strip() == '0') or (op == '!=' and rhs.strip() == '1'):
                            return f'!{lhs}'  # Negate
            # Bool function compared to 0/1: IsDisabled() == 0 → !IsDisabled()
            if op in ('==', '!=') and rhs.strip() in ('0', '1'):
                inner = lhs.strip()
                # Match ref.Func() or Func() patterns
                bool_call_m = re.match(
                    r'^(?:.*\.)?('
                    r'Is(?:Disabled|Enabled|Dead|InCombat|Sneaking|WeaponOut|Swimming|Ghost|'
                    r'InInterior|Essential|Guard|ActionRef|ChildOf|InDialogueWithPlayer|'
                    r'Running|InFaction|Arrested|BleedingOut|UnconscIous|Commanded|'
                    r'PlayerTeammate|Hostile|Sprinting|OnMount|Alerted|EquipPed|'
                    r'Mounted|Trespassing|AVRecoveryDisabled|FurnitureInUse|FlightBlocked)|'
                    r'Get(?:Dead|Disabled|Locked|Ghost|IsAlerted|InCombat|NoBleedoutRecovery|'
                    r'IsPlayableRace|CurrentWeatherPercent|IsCurrentPackage)|'
                    r'Has(?:Spell|MagicEffect|Perk|EffectKeyword|KeyWord|Node|LOSToRef|RefType)|'
                    r'(?:IsInterior|IsInInterior|WornHasKeyword|PathToReference)'
                    r')\s*\(', inner, re.IGNORECASE)
                if bool_call_m:
                    if (op == '==' and rhs.strip() == '1') or (op == '!=' and rhs.strip() == '0'):
                        return lhs  # Already bool, == 1 is redundant
                    if (op == '==' and rhs.strip() == '0') or (op == '!=' and rhs.strip() == '1'):
                        return f'!({lhs})'  # Negate
            return f'{lhs} {op} {rhs}'

        # Handle arithmetic operators at top level (+, -, *, /)
        # Scan right-to-left for + and - (lowest precedence), then * and /
        # This lets recursive conversion handle each operand properly
        # A STRING LITERAL is opaque to this scan.  Morroblivion's installation
        # check tests `FileExists "Data\Morrowind_ob - Meshes.bsa"`, and the
        # hyphen inside that path was read as subtraction: the literal was torn
        # in half and the fragments leaked out as code
        # (`If 0 - Meshes.bsa(") == 0`), a parser error that failed the script.
        # Mark every character inside quotes so operators there never split.
        _in_lit = [False] * len(expr)
        _lit = False
        for _i, _c in enumerate(expr):
            if _c == '"':
                _lit = not _lit
                _in_lit[_i] = True
            elif _lit:
                _in_lit[_i] = True

        for ops in (('+', '-'), ('*', '/', '%')):
            depth = 0
            best_i = -1
            for i in range(len(expr) - 1, 0, -1):  # right-to-left, skip pos 0 (unary)
                c = expr[i]
                if _in_lit[i]:
                    continue
                if c == ')': depth += 1
                elif c == '(': depth -= 1
                elif depth == 0 and c in ops:
                    # Don't split on +/- that are part of a number (.2, 1e+5)
                    # or are unary (preceded by operator or open paren)
                    # Look back past whitespace to find the significant prev char
                    sig_prev = ' '
                    for k in range(i - 1, -1, -1):
                        if expr[k] not in ' \t':
                            sig_prev = expr[k]
                            break
                    if sig_prev in '(,=<>!+-*/%':
                        continue
                    best_i = i
                    break
            if best_i > 0:
                lhs = self._convert_expression(expr[:best_i].strip(), extends)
                op = expr[best_i]
                rhs = self._convert_expression(expr[best_i+1:].strip(), extends)
                return f'{lhs} {op} {rhs}'

        # Handle function calls in expressions: "funcname arg1 arg2"
        # Route through _emit_function for special-case handling.
        # The separator may be a comma rather than whitespace — Oblivion accepted
        # `IsActionRef, Player` as readily as `IsActionRef Player`, and without
        # matching that form the call fell through unconverted and emitted the
        # comma into the Papyrus condition.  _emit_function strips it.
        func_in_expr = re.match(r'^(\w+)(?:\s*,\s*|\s+)(.+)$', expr)
        if func_in_expr:
            fname = func_in_expr.group(1).lower()
            if fname in FUNCTION_MAP or fname in ('getstage', 'getstagedone', 'setstage',
                    'startquest', 'stopquest', 'getquestrunning', 'getrandompercent',
                    'completequest', 'isquestcompleted',
                    'getpos', 'getangle', 'setpos', 'setangle', 'getstartingangle', 'getself',
                    'getactionref', 'isactionref', 'message', 'messagebox',
                    'getisid', 'getisrace', 'getpcisrace', 'getisref',
                    'getincell', 'getinsamecell', 'getissex', 'getcrimeknown',
                    'sme', 'pme', 'setdisplayname', 'placeatme',
                    'createfullactorcopy', 'wakeuppc', 'isexpelled',
                    'sayto', 'say', 'saycustom', 'getpcissex',
                    'getcontainer', 'getbookread', 'bookread', 'showclassmenu',
                    'showbirthsignmenu', 'showracemenu', 'setinchargen',
                    'setplayerinseworld', 'forcecloseobliviongate',
                    'closecurrentobliviongate', 'isinfaction', 'call'):
                return self._emit_function(None, func_in_expr.group(1),
                                           func_in_expr.group(2).strip(), extends)
            # Prefix-matched no-equivalent families, mirroring the bare-command
            # path below.  These have handlers in _emit_function but deliberately
            # no FUNCTION_MAP entry, so WITH arguments they matched nothing here
            # and survived verbatim into the output: `sv_compare
            # "Characters\_male\skeleton.nif" modelpath` reached the Papyrus
            # lexer, where the Windows path separators read as string escapes
            # ("mismatched character '\' expecting '\"'") and took down every
            # script that imported the file too.
            if (re.match(r'^(?:get|set)menu\w*$', fname)
                    or fname.startswith(('con_', 'ar_', 'sv_'))):
                return self._emit_function(None, func_in_expr.group(1),
                                           func_in_expr.group(2).strip(), extends)

        # Handle ref.Func in expressions (only if no parens yet — avoid re-matching)
        # Require ref to start with a letter (not digit) to avoid matching floats like 0.5
        # The ref may START WITH A DIGIT — Papyrus forbids it but TES4 EditorIDs
        # do not, and Nehrim names many refs `1TrapFireMineWorldRef`.  A pure
        # number before the dot is excluded so float literals (0.5) stay literals.
        ref_func = re.match(r'^(?!\d+\.)(\w+)\.(\w+)\s*((?:[^(].*)?)', expr)
        if ref_func and '(' not in ref_func.group(2):
            args_rest = ref_func.group(3).strip()
            ref_name = ref_func.group(1)
            prop_name = ref_func.group(2)
            prop_low = prop_name.lower()
            # Sanitize property name to avoid Papyrus reserved word collisions
            safe_prop = _safe_property_name(prop_name)
            # If "args" starts with arithmetic op, it's property access not function call
            # e.g. Quest.Var + 1 -> Quest.Var + 1, not Quest.Var(+, 1)
            if args_rest and args_rest[0] in '+-*/%':
                ref = self._convert_ref(ref_name, extends)
                rest = self._convert_expression(args_rest, extends)
                return f'{ref}.{safe_prop} {rest}'
            # If "args" starts with comparison op, it's also a property comparison
            # e.g. Quest.Var > 0, Quest.Var == 1
            # BUT: if prop is a known function, route through _emit_function instead
            if args_rest and re.match(r'^(==|!=|>=|<=|>|<)\s*', args_rest):
                if prop_low in FUNCTION_MAP or prop_low in _BARE_BOOL_FUNCTIONS or prop_low in (
                        'isininterior', 'isanimplaying', 'getparentcell',
                        'getdead', 'isdead', 'getdisabled', 'isdisabled',
                        'isinfaction', 'isessential', 'getisrace',
                        'getisid', 'getissex', 'getincell', 'getinsamecell',
                        'isactionref', 'getactionref', 'getisplayablerace',
                        'isactorusingatorch', 'getdetected', 'isdetectedby',
                        'getismurderer', 'isguard', 'getnosneakwaterpenalty',
                        'getstartingangle', 'getstartingpos'):
                    # It's a function call followed by comparison — split and handle
                    comp = re.match(r'^(==|!=|>=|<=|>|<)\s*(.*)', args_rest)
                    func_result = self._emit_function(ref_name, prop_name, '', extends)
                    if comp:
                        rhs = self._convert_expression(comp.group(2).strip(), extends)
                        return f'{func_result} {comp.group(1)} {rhs}'
                    return func_result
                ref = self._convert_ref(ref_name, extends)
                rest = self._convert_expression(args_rest, extends)
                return f'{ref}.{safe_prop} {rest}'
            # Cross-script variable access: if ref's script declares this variable, always property
            if self._ref_has_script_var(ref_name, prop_name):
                ref = self._convert_ref(ref_name, extends)
                if args_rest:
                    rest = self._convert_expression(args_rest, extends)
                    return f'{ref}.{safe_prop} {rest}'
                return f'{ref}.{safe_prop}'
            # If ref is a known quest and prop is NOT a known function, treat as property access
            if self.xref.is_quest_ref(ref_name) and prop_low not in FUNCTION_MAP and prop_low not in (
                    'getstage', 'setstage', 'getstagedone', 'start', 'stop', 'isrunning',
                    'iscompleted', 'completequest', 'setstage', 'getstage'):
                ref = self._convert_ref(ref_name, extends)
                if args_rest:
                    rest = self._convert_expression(args_rest, extends)
                    return f'{ref}.{safe_prop} {rest}'
                return f'{ref}.{safe_prop}'
            # No args and not a known function — treat as property access
            # e.g. NpcRef.someVar (cross-script variable)
            if not args_rest and prop_low not in FUNCTION_MAP and prop_low not in _BARE_BOOL_FUNCTIONS and prop_low not in (
                    'getstage', 'setstage', 'getstagedone', 'start', 'stop', 'isrunning',
                    'iscompleted', 'completequest', 'evaluatepackage', 'enable', 'disable',
                    'delete', 'activate', 'reset', 'kill', 'resurrect', 'moveto',
                    'getparentcell', 'getself', 'getactionref', 'getlinkedref',
                    'getparentref', 'getbaseobject', 'getactorbase',
                    'isactorusingatorch', 'isridinghorse', 'createfullactorcopy'):
                ref = self._convert_ref(ref_name, extends)
                return f'{ref}.{safe_prop}'
            return self._emit_function(ref_name, prop_name,
                                       args_rest, extends)

        # Bare function names used as values (no ref, no args)
        # e.g. "getParentRef" -> "GetLinkedRef()", "GetActionRef" -> "akActionRef"
        #
        # A LEADING DIGIT is allowed: Papyrus forbids it, but TES4 EditorIDs do
        # not, and Nehrim names hundreds of forms `1Feuerball`, `01SetBonus...`.
        # Those still have to reach the EditorID lookup below, which renames them
        # via _safe_property_name to match the emitted property declaration —
        # otherwise the call site keeps the raw name and nothing resolves.
        # Pure numbers are excluded so numeric literals fall through untouched.
        if re.match(r'^\w+$', expr) and not expr.isdigit():
            bare_low = expr.lower()
            # Local variables ALWAYS take priority over function name matching
            if bare_low in self._local_vars:
                safe = self._var_renames.get(bare_low, expr)
                return safe
            # Special bare identifiers
            if bare_low in ('getactionref', 'isactionref'):
                return self._get_action_ref_param()
            # getnextref takes no arguments, so it is always read BARE and
            # never reaches the argument-bearing path — without routing it
            # survived as the undefined identifier `GetNextRef`, leaving the
            # ref-walk's advance step doing nothing.
            # Every one of these is a zero-argument read, so it is ALWAYS
            # written bare and never reaches the argument-bearing path.  Without
            # routing, its special handler in _emit_function is unreachable dead
            # code and the name survives into the output as an undefined
            # identifier — a hard compile error that fails the whole script.
            if bare_low in ('isanimplaying', 'getiscreature', 'iscreature',
                            'hasvampirefed', 'isspelltarget', 'isguard',
                            'getnextref', 'isowner', 'getbaseobject',
                            'isonground', 'isthirdperson',
                            # Verified against the export corpus as spellings
                            # that really are read bare somewhere in a plugin.
                            'isplayerinjail', 'getpcinfamy', 'getrestrained',
                            'ispcamurderer', 'getcrimegold', 'getpcfame',
                            'gettalkedtopc', 'payfine',
                            ) or bare_low in _FORM_TYPE_TESTS:
                return self._emit_function(None, expr, '', extends)
            if bare_low == 'isxbox':
                return 'False'
            if bare_low in ('getdayofweek', 'getdayoftheweek'):
                self._property_refs['GameDaysPassed'] = 'GlobalVariable'
                return '(GameDaysPassed.GetValue() as Int) % 7'
            if bare_low in ('getrandompercent', 'getrandpercent'):
                return 'Utility.RandomInt(0, 99)'
            if bare_low in ('getcurrenttime', 'gamehour'):
                self._property_refs['GameHour'] = 'GlobalVariable'
                # NOT `as Int`.  GameHour (0x00000038) is the engine's own global
                # in both games and Skyrim declares it float (FNAM=102), so
                # GetValue() returns fractional hours — 23.9847, not 23.  The
                # bell/chime idiom brackets the top of each hour with a ±0.02
                # window (`GameHour >= 23.98 || GameHour <= 0.02`), and
                # truncating collapses every such window into an always-true
                # whole-hour test: `23 >= 23.98` is false but `0 <= 0.02` is
                # true for all of hour 0, so the guarded body ran every frame.
                # That made the Erodans-Kapelle bell (and Oblivion's
                # BellTowerScript) ring on a continuous loop.  Assignments into
                # Int variables still get their cast from _coerce_float_to_int,
                # which already lists GetValue in _FLOAT_RETURNING_FUNCS.
                return 'GameHour.GetValue()'
            if bare_low == 'getpcfame':
                self._property_refs['TES4Fame'] = 'GlobalVariable'
                return 'TES4Fame.GetValueInt()'
            if bare_low in ('getpcinfamy', 'getinfame'):
                self._property_refs['TES4Infamy'] = 'GlobalVariable'
                return 'TES4Infamy.GetValueInt()'
            if bare_low in ('isplayerinprison', 'getplayerinjail', 'isplayerinjail',
                            'senttojail'):
                return 'Game.GetPlayer().IsArrested()'
            if bare_low in ('getpcissleeping', 'ispcsleeping', 'isplayersleeping'):
                # Inside a sleep-idiom MenuMode body the read means "is this a
                # sleep frame" — that's the script-managed flag.  Elsewhere
                # (GameMode) Oblivion never ran while sleeping, so a raw
                # GetSleepState() read (0 when awake) keeps the same truth.
                if getattr(self, '_in_sleep_menumode', False):
                    return 'TES4_PCSleeping'
                return 'Game.GetPlayer().GetSleepState()'
            if bare_low == 'isininterior':
                if extends == 'ActiveMagicEffect':
                    return 'GetTargetActor().GetParentCell().IsInterior()'
                return 'Self.GetParentCell().IsInterior()'
            if bare_low == 'getdestroyed':
                return 'IsDisabled()'
            # Handle bare function references that need special handling
            if bare_low == 'getbuttonpressed':
                # A script that shows a button MessageBox of its own reads the
                # clicked index back through the consume-on-read helper (TES4
                # returns it once, then -1). A script that never shows one is
                # polling a box some OTHER script displayed — cross-script
                # GetButtonPressed was global in TES4 — and keeps the dead -1
                # rather than being silently miswired to its own (nonexistent)
                # state.
                if self.message_menus.get(
                        (self._current_script_edid or '').lower()):
                    self._uses_msg_buttons = True
                    return 'TES4_TakeMsgButton()'
                return '-1'
            if bare_low in ('getcrimegold',):
                self._property_refs['TES4CyrodiilCrimeFaction'] = 'Faction'
                return 'TES4CyrodiilCrimeFaction.GetCrimeGold()'
            if bare_low in ('getdisposition',):
                return '50'
            # Only the ARGUMENT-LESS spelling lands here, and with no target
            # named there is nothing to ask IsDetectedBy about (same reasoning
            # as IsActorDetected).  The real one-argument form — which is what
            # all 56 sites in the plugin use — is handled in _emit_function and
            # maps onto IsDetectedBy.
            if bare_low == 'getdetectionlevel':
                return '0'
            # Bare GetContainer means "the container I am in".  Skyrim has no
            # ObjectReference.GetContainer(), but the two things TES4 scripts
            # ask with it both convert:
            #   * inside an equip/unequip event the container IS the actor the
            #     event hands us, so `set tempRef to GetContainer` is akActor;
            #   * `GetContainer == 0` is "am I lying in the world", which is
            #     TES4Polyfill.IsInContainer (see there).
            # It must not silently become 0 — `set ref to GetContainer` would
            # yield a None ref and kill every call that follows it.
            if bare_low == 'getcontainer':
                actor_param = self._current_event_actor_param()
                if actor_param:
                    return actor_param
                return _GETCONTAINER_MARKER
            # "Is the player a murderer" takes NO arguments, so it is ALWAYS
            # read bare — which meant this fallback ran every time and the real
            # handler in _emit_function was unreachable dead code.  Both sites
            # became the literal `If 0 == 1`: DarkBrotherhoodScript's is the
            # ONLY trigger for the entire Dark Brotherhood questline (it starts
            # Dark01Knife and enables Lucien Lachance after the player's first
            # murder), so the questline could never begin.  Route it to the
            # same crime-gold reconstruction the handler uses — the R4-1 rule,
            # where a violent bounty at or above the vanilla murder price is
            # what distinguishes a killing from an assault.
            if bare_low in ('ispcamurderer', 'ispcanmurderer',
                            'getpcismurderer'):
                self._property_refs['TES4CyrodiilCrimeFaction'] = 'Faction'
                return (f'(TES4CyrodiilCrimeFaction.GetCrimeGoldViolent() '
                        f'>= {TES4_MURDER_BOUNTY})')
            if bare_low in ('getisalerted', 'israining', 'menumode',
                            'istimepassing', 'getplayerinseworld',
                            'getcurrentaiprocedure', 'getcurrentaipackage',
                            'getiscurrentpackage', 'isidleplaying',
                            'getbookread', 'gettalkedtopc',
                            'getcrimeknown', 'getstartingpos',
                            'getisplayerbirthsign',
                            'hasbeenpickedup', 'getweatherpercent',
                            'getgameloaded', 'hasvariable', 'getownership',
                            'isonguard', 'isindangerouswater',
                            'getarmorrating', 'isspelltarget', 'isswimming',
                            'isactor', 'getspellcount',
                            'getrestrained',
                            'getpcfactionattack', 'getpcfactionsteal',
                            'getpcfactionmurder'):
                return '0'
            if bare_low == 'reset':
                if extends == 'ActiveMagicEffect':
                    return 'GetTargetActor().Reset()'
                if extends == 'TopicInfo':
                    return 'akSpeakerRef.Reset()'
                return 'Self.Reset()'
            # Only check FUNCTION_MAP if NOT a declared local variable
            if bare_low not in self._local_vars:
                entry = FUNCTION_MAP.get(bare_low)
                # A None Papyrus name normally falls through on purpose: bare
                # reads like getSecondsPassed are rewritten by dedicated later
                # passes, and routing them here TODO's them mid-expression,
                # leaving `timer = timer - `.  The commands below have no such
                # pass and no same-named form, so they must be routed or they
                # survive into the output as undefined identifiers.
                if entry and (entry[0] is not None
                              or bare_low in _BARE_NO_EQUIV_COMMANDS):
                    return self._emit_function(None, expr, '', extends)
                # Prefix-matched no-equivalent families (OBSE menu/UI, console
                # commands, array/string helpers).  These have handlers in
                # _emit_function but deliberately no FUNCTION_MAP entry — one per
                # variant would have to be added by hand, and every one missed
                # becomes an undefined identifier at compile time.
                if (re.match(r'^(?:get|set)menu\w*$', bare_low)
                        or bare_low.startswith(('con_', 'ar_', 'sv_'))):
                    return self._emit_function(None, expr, '', extends)
            # A TES4 script may name a form by its RAW FORMID instead of an
            # EditorID — `additem 0000000f 500` is how Morrowind_ob's INFO
            # result scripts hand out gold.  Papyrus has no bare-hex literal, so
            # the token survived as an undefined identifier ("missing RPAREN at
            # 'f'").  Resolve it to the canonical EditorID and fall through to
            # the normal property path, which types it and declares it like any
            # other named form.
            # TES4 scripts write the ID with the leading zeroes of the load-order
            # byte trimmed as often as not (`additem 00000F` alongside `additem
            # 0000000F`), so accept 6-8 digits and zero-pad to the 8-char key the
            # export uses.  6 is the floor because that is a full 24-bit object
            # index; fewer digits would start matching ordinary numeric literals.
            # A pure-DECIMAL run (`100000`) is an ordinary numeric literal and
            # must not be reinterpreted as hex, so require at least one A-F
            # digit or a leading zero — both of which a decimal literal in these
            # scripts never has, and every real FormID here does.
            if (re.fullmatch(r'[0-9A-Fa-f]{6,8}', expr)
                    and (not expr.isdigit() or expr.startswith('0'))):
                edid = self.xref.formid_to_edid.get(expr.upper().zfill(8), '')
                if edid:
                    bare_low = edid.lower()
                    expr = edid
            # Check if it's a known EditorID -> property ref.  Falls back to the
            # digit-stripped spelling for the same reason _convert_ref does: a
            # Papyrus identifier cannot start with a digit, so a Morroblivion
            # record named `0<name>` arrives here already stripped and the
            # direct lookup misses.  Without this an operand like
            # `AddItem(dwemerUshieldUbattleUunique, 1)` declared no property and
            # the name reached the compiler undefined.
            fid = self.xref.edid_to_formid.get(bare_low, '')
            if not fid:
                fid = _digit_stripped_formid(self.xref, bare_low)
            if fid:
                rtype = self.xref.record_type.get(fid, '')
                ptype = self._papyrus_type_for(fid, rtype)
                # Prefer attached script type for cross-script property access
                # -- but never on a base-object type, where it cannot bind
                # (unless the binder can redirect to a unique placed ref).
                script_type = self.xref.get_record_script_type(expr)
                if script_type and self._script_type_binds(ptype, fid):
                    ptype = script_type
                # Key the property on the CANONICAL EditorID, not the spelling
                # this script happened to use.  TES4 name lookup is
                # case-insensitive, so `SetEssential Kornderbraumeister` refers
                # to `KornderBraumeister`; keying on the local spelling created a
                # SECOND _property_refs entry differing only in case, and since
                # Papyrus is also case-insensitive the two declarations
                # collided — the type set by the caller (ActorBase) lost to the
                # other entry, and the call became "undefined function".
                canon = self.xref.formid_to_edid.get(fid, expr)
                safe = _safe_property_name(canon)
                self._property_refs[safe] = ptype
                if ptype == 'GlobalVariable':
                    return self._global_read(safe)
                return safe

        # Terminal substitutions (applied last, after all function matching).
        # A LOCAL VARIABLE always wins over the `player` keyword — TES4 lets a
        # script declare `Short Player` (StartCelleAufzugTriggerZone01Script
        # does, as its own "has the player triggered me" flag), and rewriting
        # that to `Game.GetPlayer()` produced the assignment
        # `Game.GetPlayer() = 1` and the comparison
        # `Game.GetPlayer() == 0`, i.e. the flag silently became the player
        # actor.  Local variables take priority everywhere else in this
        # converter; honour that here too.
        # `PlayerRef` is the same keyword — TES4 scripts use both spellings
        # interchangeably (`StartCombat PlayerRef`), and matching only `player`
        # left it as an undefined identifier.
        for _kw in ('playerref', 'player'):
            if _kw in self._local_vars:
                continue
            expr = re.sub(rf'\b{_kw}\b', 'Game.GetPlayer()', expr,
                          flags=re.IGNORECASE)
        # In AME/TopicInfo scripts, Self/GetSelf refers to the target actor
        if extends == 'ActiveMagicEffect':
            expr = re.sub(r'\bgetSelf\b', 'GetTargetActor()', expr, flags=re.IGNORECASE)
            expr = re.sub(r'\bthis\b', 'GetTargetActor()', expr, flags=re.IGNORECASE)
            # Replace bare Self (not followed by '.') with GetTargetActor() for comparisons
            expr = re.sub(r'\bSelf\b(?!\.)', 'GetTargetActor()', expr)
        elif extends == 'TopicInfo':
            expr = re.sub(r'\bgetSelf\b', 'akSpeakerRef', expr, flags=re.IGNORECASE)
            expr = re.sub(r'\bthis\b', 'akSpeakerRef', expr, flags=re.IGNORECASE)
            # Replace bare Self (not followed by '.') with akSpeakerRef for comparisons
            expr = re.sub(r'\bSelf\b(?!\.)', 'akSpeakerRef', expr)
        else:
            expr = re.sub(r'\bgetSelf\b', 'Self', expr, flags=re.IGNORECASE)
            expr = re.sub(r'\bthis\b', 'Self', expr, flags=re.IGNORECASE)
        # GetSecondsPassed = seconds since the last tick.  The substituted
        # constant MUST equal the RegisterForSingleUpdate interval the script
        # actually runs at (_get_update_interval returns 0.1 for exactly these
        # scripts) — a 0.5 literal at a 0.1s tick made every converted timer
        # run 5x fast (Valen Dreth's 10s taunt pause became 2s).
        expr = re.sub(r'\bGetSecondsPassed\b', self._get_update_interval(),
                      expr, flags=re.IGNORECASE)
        expr = re.sub(r'\bScriptEffectElapsedSeconds\b',
                      self._get_update_interval(), expr, flags=re.IGNORECASE)

        # Fix bare decimals: .5 -> 0.5 (Papyrus requires leading zero)
        expr = re.sub(r'(?<![.\w])\.(\d)', r'0.\1', expr)

        # Known TES4 globals -> GlobalVariable.GetValue()
        if expr.lower() in KNOWN_GLOBALS:
            canonical = _canonical_global(expr)
            self._property_refs[canonical] = 'GlobalVariable'
            return f'{canonical}.GetValue()'

        # Actor value name substitution
        for ob_av, sk_av in ACTOR_VALUE_MAP.items():
            expr = re.sub(r'\b' + ob_av + r'\b', sk_av, expr, flags=re.IGNORECASE)

        # Rename reserved-word variables (e.g. next -> myNext)
        for orig_low, safe in self._var_renames.items():
            expr = re.sub(r'\b' + re.escape(orig_low) + r'\b', safe, expr, flags=re.IGNORECASE)

        return expr

    def _convert_ref(self, name: str, extends: str, as_receiver: bool = False) -> str:
        """Convert an Oblivion reference name to Papyrus.

        `as_receiver` marks the name as the target of a method call.  A local
        variable can shadow the `player` keyword in a VALUE position but never
        as a receiver — a `Short` has no methods — so the keyword wins there.
        """
        # Oblivion's parser accepts quotes around any EditorID, and Nehrim's
        # authors use them constantly (173 sites: `SetStage "MQ01Tate" 20`,
        # `GetStage "NQ00Karick"`, `StartQuest "NQ05"`,
        # `AddScriptPackage "..."`).  The quotes reached the property namer,
        # which turned each `"` into `_` — so `"MQ01Tate"` became the property
        # `_MQ01Tate_` while the same script's UNQUOTED `GetStage MQ01Tate`
        # became `MQ01Tate`.  Only the unquoted spelling matched an EditorID,
        # so only it was bound in the VMAD; `_MQ01Tate_` stayed None and every
        # `_MQ01Tate_.SetStage(...)` threw at runtime.  That stranded MQ01Tate
        # at stage 15 — it could never reach stage 40, which is the only thing
        # that starts MQ01, so MQ00 could never be completed either.
        name = _QUOTED_NAME_RE.sub(r'\1', name.strip())
        low = name.lower()
        # A declared local otherwise wins over the built-in keywords, including
        # `player`.  StartCelleAufzugTriggerZone01Script declares `Short Player`
        # as its own trigger flag; mapping that to Game.GetPlayer() turned
        # `Set Player to 1` into the un-assignable `Game.GetPlayer() = 1`.
        # (The same precedence is applied further down for EditorIDs.)
        _is_player_kw = low in ('player', 'playerref')
        if ((low in self._local_vars or low in self._var_types)
                and not (as_receiver and _is_player_kw)):
            return _safe_property_name(name)
        if _is_player_kw:
            return 'Game.GetPlayer()'
        if low in ('getself', 'myself', 'self'):
            if extends == 'ActiveMagicEffect':
                return 'GetTargetActor()'
            if extends == 'TopicInfo':
                return 'akSpeakerRef'
            if extends == PLAYER_ALIAS_EXTENDS:
                return 'GetReference()'
            return 'Self'

        # Known TES4 globals -> property
        if low in KNOWN_GLOBALS:
            canonical = _canonical_global(name)
            self._property_refs[canonical] = 'GlobalVariable'
            return canonical

        if '.' in name:
            parts = name.split('.', 1)
            ref_part = self._convert_ref(parts[0], extends)
            return f'{ref_part}.{_safe_property_name(parts[1])}'

        if self.xref.is_quest_ref(name):
            # Use the canonical EditorID (original case from export) as the key
            # so this matches what _add_scro_ref stores (both use formid_to_edid).
            canon_fid = self.xref.edid_to_formid.get(low, '')
            canon_edid = self.xref.formid_to_edid.get(canon_fid, name) if canon_fid else name
            # Through _safe_property_name like every other ref: an Oblivion quest
            # EditorID can collide with a Skyrim script name (MS14), and emitting
            # it raw here left the body calling `MS14.SetStage()` while the
            # declaration said `myMS14` — the CK then reads MS14 as the TYPE
            # ("cannot call the member function SetStage ... on a type").
            safe = _safe_property_name(canon_edid)
            self._property_refs[safe] = self.xref.get_quest_script_type(name)
            return safe

        # Local variables take precedence over game form EditorIDs (name collision)
        if low in self._local_vars or low in self._var_types:
            return _safe_property_name(name)

        # Check if this is any known EditorID from the export.
        #
        # A Papyrus identifier may not begin with a digit, so a TES4 script that
        # names a record with a leading digit — Morroblivion names almost
        # everything `0<name>` — reaches here already stripped
        # (`dwemerUshieldUbattleUunique` for `0dwemerUshieldUbattleUunique`).
        # The direct lookup misses, no property gets DECLARED, and the name then
        # survives into the body as an undefined identifier that fails the whole
        # script.  resolve_property_formid already reverses the strip when
        # BINDING the VMAD, so without the same reversal here the two disagreed:
        # the binder had a FormID for a property the script never declared.
        fid = self.xref.edid_to_formid.get(low, '')
        if not fid:
            fid = _digit_stripped_formid(self.xref, low)
        if fid:
            # Use canonical EditorID (original case) as key to match _add_scro_ref
            canon_edid = self.xref.formid_to_edid.get(fid, name)
            rtype = self.xref.record_type.get(fid, '')
            ptype = self._papyrus_type_for(fid, rtype)
            # Prefer attached script type over generic Actor/ObjectReference
            # so cross-script property access works (e.g., NPCRef.rent).
            # Base-object types (Armor/Weapon/Potion/...) are excluded: the VM
            # refuses to bind an ObjectReference-derived script class to a base
            # record, and the property then reads None. A unique-placed
            # ACTI/LIGH is the exception — the binder redirects to its ref.
            script_type = self.xref.get_record_script_type(name)
            if script_type and self._script_type_binds(ptype, fid):
                ptype = script_type
            safe = _safe_property_name(canon_edid)
            # Don't downgrade a more specific type (e.g., Actor from
            # _resolve_self_ref) back to a generic one (ObjectReference).
            cur = self._property_refs.get(safe, '')
            _generic = ('', 'ObjectReference')
            if not cur or ptype not in _generic or cur in _generic:
                self._property_refs[safe] = ptype
            return safe

        return _safe_property_name(name)

    def _convert_args(self, args_str: str, func_name: str, extends: str) -> str:
        """Convert Oblivion function arguments to Papyrus."""
        if not args_str:
            return ''

        # Actor value functions: first arg is AV name -> quoted string
        # The OBSE `...2` aliases take the same (AV name, value) arguments as the
        # vanilla commands they map onto, so they must quote the AV name here
        # too — without them `modAV2 Health 300` emitted an unquoted `Health`
        # ("undefined identifier `Health`").
        if func_name in _ACTOR_VALUE_FUNCTIONS:
            parts = args_str.split(None, 1)
            av_name = parts[0].rstrip(',').strip('"\'')
            sk_av = ACTOR_VALUE_MAP.get(av_name.lower(), av_name)
            # Oblivion's single Encumbrance AV is TWO AVs in Skyrim: the current
            # carried weight is InventoryWeight (index 31), the maximum is
            # CarryWeight (index 32).  TES4 splits them the modified-vs-base way
            # (`getav` = what you carry now, `getbaseav` = Strength x 5), so the
            # over-encumbered idiom is
            #     player.getav encumbrance > player.getbaseav encumbrance
            # — MQ01's stage 75/78 tutorial, whose own text reads "your CURRENT
            # encumbrance exceeds the MAXIMUM you can carry".  Mapping both
            # sides to CarryWeight compared the cap against itself, so the test
            # was never true and neither tutorial stage could fire.
            if av_name.lower() == 'encumbrance' and func_name in (
                    'getactorvalue', 'getav'):
                sk_av = 'InventoryWeight'
            rest = ''
            if len(parts) > 1:
                rest_str = parts[1].lstrip(', ')
                if rest_str:
                    is_set = func_name in ('setactorvalue', 'setav',
                                           'forceactorvalue', 'forceav')
                    scaled = self._scale_enum_av(sk_av, rest_str) if is_set else None
                    if scaled is not None:
                        rest = f', {scaled}'
                    else:
                        rest = f', {self._convert_expression(rest_str, extends)}'
            return f'"{sk_av}"{rest}'

        # Default: Oblivion scripts use both "func arg1 arg2" and
        # "func arg1, arg2".  Split QUOTE-AWARE — a plain comma/whitespace split
        # tore filenames apart: `IsModLoaded "Voice Overs V002.esp"` became
        # three arguments and emitted the nonsense
        # `IsModLoaded("Voice, Overs, V002.esp(")`, which then converted to a
        # bare `If True` and fired the mod's "deprecated plugin detected"
        # warning unconditionally.  _split_obse_args already suppresses
        # splitting inside string literals, parens and brackets.
        parts = _split_obse_args(args_str)
        converted = [self._convert_expression(p, extends) for p in parts]
        # A property typed as the SCRIPT attached to the record it names (see
        # _add_scro_ref) is not an Actor, so passing it where the Papyrus
        # signature wants one does not compile — `StartCombat(NQ05Soldat01nRef)`
        # with that property typed TES4_NQ05NOActivationScript.  The bound
        # object IS an actor, so cast at the call site rather than retyping the
        # property, which the cross-script variable reads still need.
        if func_name in _ACTOR_ARG_FUNCTIONS:
            converted = [
                f'({c} as Actor)'
                if self._property_refs.get(c, '').startswith('TES4_') else c
                for c in converted]
        # Note: the Form→Spell downcast that AddSpell/RemoveSpell need is applied
        # where the UDF signature is emitted, because the parameter's type is not
        # decided until after the body has been converted.
        return ', '.join(converted)

    # Actor values that TES4 stores on a 0-100 scale but TES5 defines as a small
    # ENUM (xEdit wbDefinitionsCommon.pas: wbAggressionEnum 0-3,
    # wbConfidenceEnum 0-4, wbAssistanceEnum 0-2, wbMoodEnum 0-8, and Morality
    # 0-3).  Writing the raw TES4 number is rejected outright by the engine —
    # `SetActorValue("Aggression", 100)` logs "attempt made to set illegal
    # value" and leaves the trait UNCHANGED, so every scripted "now turn
    # hostile" beat silently did nothing.
    # Value is the inclusive maximum for each trait.
    _ENUM_ACTOR_VALUES = {
        'aggression': 3, 'confidence': 4, 'assistance': 2,
        'mood': 8, 'morality': 3,
    }

    def _scale_enum_av(self, sk_av: str, value_src: str):
        """Map a TES4 0-100 trait value onto its TES5 enum tier.

        Returns None when this is not an enum-valued actor value, or when the
        operand is not a literal (a variable cannot be bucketed at conversion
        time), so the caller falls back to normal expression conversion.
        """
        max_tier = self._ENUM_ACTOR_VALUES.get(sk_av.lower())
        if max_tier is None:
            return None
        literal = value_src.strip().rstrip(',').strip()
        if not re.match(r'^-?\d+(?:\.\d+)?$', literal):
            return None
        raw = float(literal)
        # A value already inside the enum range is a deliberate Skyrim-style
        # tier (or the TES4 default 0) — pass it through untouched rather than
        # re-bucketing it and changing behaviour.
        if 0 <= raw <= max_tier:
            return str(int(raw))
        if raw < 0:
            return '0'
        # Mirror the record-side thresholds in tes5_import/record_types/
        # actors.py so a scripted change lands on the same tier the NPC's AIDT
        # was converted to: <=5 never initiates, >=106 attacks everyone.
        if sk_av.lower() == 'aggression':
            # TES4 aggression is only half of a PER-TARGET rule: an actor
            # attacks a target when disposition(actor->target) < aggression - 5
            # (UESP Oblivion:Aggression).  TES5 aggression is a GLOBAL tier
            # naming which reaction class it attacks, so the TES4 number cannot
            # be read on its own — the disposition it has to beat decides the
            # tier.
            #
            # Collapsing everything from 6..105 onto tier 2 was wrong because
            # tier 2 is "attacks enemies AND NEUTRALS on sight", and the player
            # is a Neutral to most factions.  CharacterGen stage 22 does
            # `GlenroyRef.setav aggression 10` purely so the Emperor's guards
            # will fight the assassins; 10 only beats a disposition below 5,
            # and the guards' disposition toward the player is ~47, so in
            # Oblivion they never turn on you.  Converted to tier 2 they
            # attacked the player on sight from stage 22 onward.  UESP names
            # this exact failure: "a guard would attack the whole town if their
            # aggression were sufficiently raised".
            #
            # _ONSIGHT_AGGRESSION is the aggression needed to beat an ordinary
            # NPC disposition and so genuinely mean "hostile to bystanders".
            # It matches the record path's margin test, which subtracts
            # disposition before it will grant tier 2: there, a default actor
            # (disposition ~= Personality 50) needs (aggr-5) - 50 >= 10, i.e.
            # aggression >= 65.  Values below that are Oblivion's "defend
            # yourself / join this specific fight" idiom and belong on tier 1,
            # which attacks declared Enemies only and leaves Neutrals alone —
            # the faction graph then picks the actual opponent, exactly as the
            # TES4 rule did.  Census of the 227 scripted calls in Oblivion.esm:
            # 38 land on 0, 76 on tier 1 (10/20/25/30/40/50), 113 on tier 2+
            # (90/100 = the real "now attack anyone" beats).
            _ONSIGHT_AGGRESSION = 65
            if raw <= 5:
                tier = 0
            elif raw >= 106:
                tier = 3
            elif raw >= _ONSIGHT_AGGRESSION:
                tier = 2
            else:
                tier = 1
        elif sk_av.lower() == 'confidence':
            # Mirror _convert_aidt in tes5_import/record_types/actors.py: only
            # tier 4 (Foolhardy) never flees, and Oblivion's 100 means fearless.
            if raw >= 100:
                tier = 4
            elif raw >= 70:
                tier = 3
            elif raw >= 40:
                tier = 2
            elif raw >= 15:
                tier = 1
            else:
                tier = 0
        else:
            # Generic 0-100 → 0..max_tier proportional bucket.
            tier = int(round((min(raw, 100.0) / 100.0) * max_tier))
        tier = max(0, min(max_tier, tier))
        return str(tier)

    def _faction_reaction_call(self, f1: str, f2: str, amount_src: str,
                               is_mod: bool, extends: str):
        """Map a TES4 faction disposition amount onto SetEnemy/SetAlly.

        TES4 dispositions run -100..+100.  Skyrim only stores a four-value
        Group Combat Reaction, so the amount is bucketed onto the tier that
        preserves the intent:

            <= -50   Enemy    (`setfactionreaction X Y -100` = "now hate them")
            <  0     Neutral  (a mild grudge is not open warfare)
            == 0     Neutral  (explicitly clearing a relation)
            >  0     Friend   (goodwill between two DIFFERENT factions)

        Positive amounts stop at Friend and never reach Ally.  A TES4
        disposition is a 0-100 scalar meaning "likes them more"; TES5's Ally is
        a hard contract that makes members ASSIST each other into combat (UESP
        Skyrim:Factions — reaction combines with aggression and assistance to
        decide who joins a fight).  Since `setfactionreaction` always names two
        DIFFERENT factions, promoting its positive amounts to Ally wired
        bystanders into other people's fights.  Ally is reserved for a
        faction's relation to itself, which only the FACT record path emits
        (see convert_FACT in tes5_import/record_types/actors.py).

        Returns None when the amount is not a literal, so the caller can emit a
        runtime branch instead.  ModFactionReaction shifts an existing value we
        cannot read at conversion time, so only its SIGN is honoured — that is
        the part vanilla scripts actually depend on.
        """
        literal = amount_src.strip().rstrip(',').strip()
        if not re.match(r'^-?\d+(?:\.\d+)?$', literal):
            return None
        amount = float(literal)
        if is_mod:
            # A relative nudge: treat any negative shift as souring the
            # relation and any positive one as improving it.
            if amount < 0:
                return f'{f1}.SetEnemy({f2}, false, false)'
            if amount > 0:
                return f'{f1}.SetAlly({f2}, true, true)'
            return f';{f1}.ModReaction({f2}, 0)  ;no-op'
        if amount <= -50:
            return f'{f1}.SetEnemy({f2}, false, false)'
        if amount <= 0:
            # Neutral: SetEnemy with the "self is neutral to other" bool set.
            return f'{f1}.SetEnemy({f2}, true, true)'
        return f'{f1}.SetAlly({f2}, true, true)'

    def _convert_function_call(self, line: str, extends: str) -> str:
        """Convert an Oblivion function call line to Papyrus."""
        stripped = line.strip()
        # Fix space after dot in ref. function patterns (TES4 typo).  The
        # closing quote of a quoted receiver counts as the left-hand side —
        # `"SomeRef". Disable` is legal TES4 and appears in Nehrim, and leaving
        # the gap made the ref patterns below miss, so the call fell through to
        # `Ref(., Disable())`.
        stripped = re.sub(r'([\w"])\.\s+(\w)', r'\1.\2', stripped)

        # `"EditorID".Function args` — see the matching note in
        # _convert_expression.  Drop the quotes so the ref patterns below match.
        stripped = re.sub(r'^"([^"]+)"\s*\.', r'\1.', stripped)

        # ref.function pattern
        ref_m = re.match(r'^(\w+)\.(\w+)\s*(.*)', stripped, re.IGNORECASE)
        if ref_m:
            return self._emit_function(ref_m.group(1), ref_m.group(2), ref_m.group(3).strip(), extends)

        # Standalone function
        func_m = re.match(r'^(\w+)\s*(.*)', stripped, re.IGNORECASE)
        if func_m:
            return self._emit_function(None, func_m.group(1), func_m.group(2).strip(), extends)

        # Nothing matched, so `stripped` is not valid Papyrus by definition —
        # emitting it as live code just moves the failure to the compiler (TES4
        # scripts use bare `-----` rules as separators, which parse as a prefix
        # expression).  Comment it out so the surrounding code still builds.
        return f';TODO: Could not parse: {stripped}'

    def _get_action_ref_param(self) -> str:
        """Return the correct event parameter for GetActionRef/IsActionRef.
        
        TES4 GetActionRef is available in every block. Papyrus scopes event params.
        Map to the appropriate parameter based on the current event being converted.
        """
        ev = self._current_event.lower()
        if 'onactivate' in ev or 'ontrigger' in ev:
            return 'akActionRef'
        if 'onequipped' in ev or 'onunequipped' in ev:
            return 'akActor'
        if 'onhit' in ev:
            return 'akAggressor'
        if 'ondeath' in ev:
            return 'akKiller'
        if 'oncontainerchanged' in ev:
            return 'akNewContainer'
        if 'oncombatstate' in ev:
            return 'akTarget'
        # OnUpdate/OnInit/other events have no action ref - use None as fallback
        if 'onupdate' in ev or 'oninit' in ev:
            return 'None'
        # Fallback: akActionRef (may be undefined, but most common case)
        return 'akActionRef'

    # Papyrus locals/parameters that are already actors — calling an actor-only
    # function on one must never mint a property for it.
    _NON_PROPERTY_REFS = frozenset({
        'self', 'akspeakerref', 'akactionref', 'akactor', 'aktarget',
        'akcaster', 'aksource', 'akaggressor', 'akdestination',
        'game.getplayer()', 'gettargetactor()', 'getactorreference()',
        'getcasteractor()', 'getowningquest()',
    })

    def _is_bindable_property(self, ref: str) -> bool:
        """True when `ref` is a bare identifier worth recording as Actor-typed.

        The receiver reaching the actor-only cast below is already CONVERTED, so
        it can be an expression (`Game.GetPlayer()`), a cast (`(x as Actor)`) or
        a fixed event parameter.  Registering one of those as a property ref put
        it through _safe_property_name and emitted a mangled, never-referenced
        declaration — `Actor Property Game_GetPlayer__ Auto` appeared in 511
        scripts, bound to nothing.

        Script-local variables DO belong here even though they never become VMAD
        properties: _property_refs is also what marks a local as Actor-typed, so
        it drives the `as Actor` downcast and the variable's declared type
        (AmuletofKings' `TempRef.UnequipItem`).  Excluding them broke 73 scripts.
        """
        if not ref or not re.match(r'^[A-Za-z_]\w*$', ref):
            return False
        return ref.lower() not in self._NON_PROPERTY_REFS

    def _packages_of_type(self, ref_name: str, pkg_type: int) -> list:
        """PACK EditorIDs backing a `GetCurrentAIPackage == <type>` test.

        A named receiver resolves through that record's own AIPackage list; a
        bare call runs on whatever actor attaches the script being converted,
        so it resolves through SCRI instead.  Empty when nothing resolves,
        which leaves the caller on the pre-existing no-op path.
        """
        if not self.xref:
            return []
        if ref_name and ref_name.lower() not in ('self', 'myself', 'getself'):
            return self.xref.get_actor_packages_of_type(ref_name, pkg_type)
        if self._current_script_edid:
            return self.xref.get_script_owner_packages_of_type(
                self._current_script_edid, pkg_type)
        return []

    def _resolve_self_ref(self, ref_name, extends, actor_func=False):
        """Resolve the reference for a function call.

        For ActiveMagicEffect scripts, bare (no ref) or Self-prefixed actor/objref
        functions need GetTargetActor() instead of Self.
        For TopicInfo scripts, bare actor functions need akSpeakerRef.
        For PlayerAlias scripts (a TES4 script attached to the Player BASE
        record, rehosted on a quest's PlayerRef alias — see
        object_scripts._build_player_alias_plan) Self is a ReferenceAlias, not
        an actor, so the implicit subject is the alias's filled reference.
        """
        if extends == PLAYER_ALIAS_EXTENDS and (
                not ref_name or ref_name.lower() in ('self', 'myself', 'getself')):
            return 'GetActorReference()' if actor_func else 'GetReference()'
        if ref_name:
            ref_low = ref_name.lower()
            # Self in ActiveMagicEffect/TopicInfo should redirect actor functions
            if actor_func and ref_low in ('self', 'myself', 'getself'):
                if extends == 'ActiveMagicEffect':
                    return 'GetTargetActor()'
                if extends == 'TopicInfo':
                    return '(akSpeakerRef as Actor)'
            # Upgrade property type to Actor when used with actor-only functions
            canon = self._convert_ref(ref_name, extends, as_receiver=True)
            if actor_func:
                # akSpeakerRef is a fixed ObjectReference parameter; cast it rather than upgrading
                if canon == 'akSpeakerRef':
                    return '(akSpeakerRef as Actor)'
                cur = self._property_refs.get(canon, '')
                # Upgrading an existing ObjectReference entry is always right;
                # creating a NEW one is only right for a bare identifier (see
                # _is_bindable_property — `Game.GetPlayer()` must not become a
                # mangled `Game_GetPlayer__` property).
                if cur == 'ObjectReference' or (
                        cur == '' and self._is_bindable_property(canon)):
                    self._property_refs[canon] = 'Actor'
                elif cur.startswith('TES4_'):
                    # The property is typed as the SCRIPT attached to the record
                    # it names (_add_scro_ref prefers that so cross-script
                    # variable reads work).  That type is not an Actor, so an
                    # actor-only call on it does not compile — but the object it
                    # binds to IS one, so cast at the call site rather than
                    # retyping the property and breaking the variable reads.
                    # (`KreoRef.EvaluatePackage()`, `MelvinTotRef.SetGhost()`,
                    # `NQ05Soldat01Ref.StartCombat()` — all actors carrying a
                    # converted script.)
                    return f'({canon} as Actor)'
            return canon
        if actor_func:
            if extends == 'ActiveMagicEffect':
                return 'GetTargetActor()'
            if extends == 'TopicInfo':
                return '(akSpeakerRef as Actor)'
        return 'Self'

    # `(Self as Actor)` / `Self as Actor` inside a PlayerAlias script.  Matches
    # the parenthesised and bare forms; a bare `Self` on its own is left alone
    # (assigning the alias itself to an alias-typed property is legitimate).
    _PLAYER_ALIAS_SELF_RE = re.compile(
        r'\(\s*Self\s+as\s+Actor\s*\)|\bSelf\s+as\s+Actor\b', re.IGNORECASE)

    @staticmethod
    def _implicit_self(extends: str) -> str:
        """What a bare, receiver-less call acts on in this script's base type.

        `Self` everywhere except a PlayerAlias script, whose Self is the
        ReferenceAlias rather than the reference it fills.
        """
        return 'GetReference()' if extends == PLAYER_ALIAS_EXTENDS else 'Self'

    def _is_global_target(self, target: str) -> bool:
        """True when `target` names a GlobalVariable-typed property.

        A Papyrus global is an object written through SetValue(), never by
        assignment.  Shared by the `set` and `let` assignment paths so both
        spellings of a global write emit the same call.
        """
        tgt_low = target.lower().split('.')[-1]
        return self._property_refs.get(
            target, self._property_refs.get(tgt_low, '')) == 'GlobalVariable'

    def _resolve_objref_ref(self, ref_name, extends) -> str:
        """Resolve the reference for an ObjectReference-typed function call.

        Like `_resolve_self_ref(actor_func=True)` this redirects the implicit
        `Self` of ActiveMagicEffect/TopicInfo scripts (whose Self is NOT a
        reference) onto the reference they act on — but it does not add the
        `as Actor` cast, because the callee is declared on ObjectReference and
        works for actors and objects alike.
        """
        if not ref_name:
            if extends == 'ActiveMagicEffect':
                return 'GetTargetActor()'
            if extends == 'TopicInfo':
                return 'akSpeakerRef'
            if extends == PLAYER_ALIAS_EXTENDS:
                return 'GetReference()'
            return 'Self'
        if ref_name.lower() in ('self', 'myself', 'getself'):
            if extends == 'ActiveMagicEffect':
                return 'GetTargetActor()'
            if extends == 'TopicInfo':
                return 'akSpeakerRef'
            if extends == PLAYER_ALIAS_EXTENDS:
                return 'GetReference()'
        return self._convert_ref(ref_name, extends, as_receiver=True)

    def _bind_base_form_property(self, name: str) -> None:
        """Type `name` as the Papyrus type of the BASE record it names.

        Used by base-object comparisons (GetIsID), whose operand is the base
        record itself: an NPC_ is an ActorBase, a MISC is a MiscObject.  Falls
        back to Form, which compares against every base type.
        """
        rtype = ''
        if self.xref:
            fid = self.xref.edid_to_formid.get(name.lower(), '')
            rtype = self.xref.record_type.get(fid, '') if fid else ''
        self._property_refs[name] = _record_type_to_base_papyrus(rtype)

    def _dangling_cross_script_target(self, raw_target: str) -> str:
        """Return a reason string when `Owner.Var` names an undeclared variable.

        Only fires when the owner resolves to a script whose variable list is
        KNOWN and does not contain the name — an unresolved owner is left alone
        so this never suppresses a legitimate assignment.
        """
        if '.' not in raw_target or not self.xref:
            return ''
        owner, _, var = raw_target.partition('.')
        owner_low, var_low = owner.strip().lower(), var.strip().lower()
        if not owner_low or not var_low:
            return ''
        # Resolve the owner EditorID to its attached script's variable table.
        fid = self.xref.edid_to_formid.get(owner_low, '')
        script_low = ''
        if fid:
            scri = self.xref.record_scri.get(fid, '')
            if scri:
                script_low = self.xref.script_formid_to_edid.get(scri, '').lower()
        if not script_low and owner_low in self.xref.script_all_vars:
            script_low = owner_low
        if not script_low:
            return ''
        known = self.xref.script_all_vars.get(script_low)
        if not known:
            return ''
        if var_low in known:
            return ''
        return (f'NE: {owner}.{var} is not declared in {script_low} '
                f'(dangling in the original script)')

    def _actor_base_property(self, name: str, extends: str) -> str:
        """Bind `name` as an ActorBase property and return the property name.

        Commands whose operand is an actor BASE record (GetDeadCount) need the
        property typed ActorBase, which is where the method is declared.  The
        name may collide case-insensitively with one of the script's own
        variables — MQ19Script has both an `Int narel` flag and a reference to
        the NPC_ `Narel` — and Papyrus is case-insensitive, so reusing the name
        would either redeclare it or silently resolve to the local (which is
        what made `Narel.GetDeadCount()` an undefined function).  Suffix the
        property in that case.
        """
        canon = name
        if self.xref:
            fid = self.xref.edid_to_formid.get(name.lower(), '')
            if fid:
                canon = self.xref.formid_to_edid.get(fid, name)
        prop = _safe_property_name(canon)
        low = prop.lower()
        if low in self._local_vars or low in self._var_types:
            prop = f'{prop}Base'
        self._property_refs[prop] = 'ActorBase'
        return prop

    def _emit_function(self, ref_name: Optional[str], func_name: str,
                       args_str: str, extends: str) -> str:
        """Emit a converted function call."""
        fname_low = func_name.lower()

        # Oblivion's parser tolerated a comma between a command and its first
        # argument (`IsActionRef, Player`, `GetItemCount, Gold001`, `MessageBox,
        # "text"`) and Nehrim's scripts use the style constantly.  Nothing
        # downstream expects it, so a stray leading comma ends up emitted inside
        # the generated argument list — `If (IsActionRef, Game.GetPlayer())`.
        # Strip it once, here, so every handler sees a clean argument string.
        had_leading_comma = args_str.lstrip().startswith(',')
        args_str = args_str.lstrip().lstrip(',').lstrip()

        # ...but for a command that takes NO arguments, the token after that
        # comma is the RECEIVER, not an argument: Oblivion's `StopCombat,
        # Player` / `IsInCombat, Player == 1` mean Player's combat state, the
        # same as `Player.StopCombat`.  Treating it as an argument emitted
        # `IsInCombat(Player)` ("function takes 0 parameters not 1") and
        # `(Self as Actor).StopCombat()` — which silently acted on the wrong
        # actor.  Promote it to the receiver when the call has none.
        if (had_leading_comma and not ref_name and args_str
                and fname_low in _ZERO_ARG_REF_FUNCTIONS
                and re.match(r'^[A-Za-z_]\w*$', args_str.strip())):
            ref_name = args_str.strip()
            args_str = ''

        # --- Special case functions ---

        # SKYRIM HAS NO ATTRIBUTES.  An actor-value call naming Strength,
        # Intelligence, Willpower, Agility, Speed, Endurance, Personality or
        # Luck has no faithful target: every TES5 actor value sits on a
        # different scale than TES4's 0-100, so aliasing one onto the nearest
        # look-alike does not preserve the authored threshold.
        #
        # This was aliased (strength->UnarmedDamage, endurance->HealRate,
        # agility/speed->SpeedMult) and it broke every Morroblivion guild.
        # fbmwFGAdvancementQuestScript gates each Fighters Guild rank on
        # `Player.GetAV Strength >= 30 && Player.GetAV Endurance >= 30`;
        # UnarmedDamage sits near 0, so no character qualified at any level and
        # the recruiter always answered "you don't have enough experience",
        # while the Thieves Guild's Agility gate read SpeedMult (~100) and
        # passed unconditionally.
        #
        # A read becomes ATTRIBUTE_STUB_VALUE (above every authored TES4
        # threshold) so the gate falls OPEN, and a write is dropped.  Falling
        # open is the faithful outcome: the gate exists to keep an
        # under-developed character out, and a Skyrim character cannot raise an
        # attribute at all, so enforcing it locks the content away permanently
        # rather than merely early.  Mirrors
        # dialog_conditions._TES4_AV_ATTRIBUTES on the record side.
        if fname_low in _ACTOR_VALUE_FUNCTIONS and args_str:
            av_first = args_str.split(None, 1)[0].rstrip(',').strip('"\'')
            if av_first.lower() in TES4_ATTRIBUTES:
                if fname_low in _ACTOR_VALUE_READ_FUNCTIONS:
                    return ATTRIBUTE_STUB_VALUE
                return (f';TES4 attribute {av_first} has no Skyrim equivalent '
                        f'-- write dropped')

        # OBSE `Call <ScriptName> arg1, arg2, ...` — invoke a user-defined
        # function.  The callee is a script, so it is reached through a property
        # typed as that script; the function itself is emitted as
        # `<Script>.TES4Call(...)`.
        #
        # OBSE accepts WHITESPACE, commas, or a mix as the argument separator:
        # `Call Foo 10, 1, -1` and `Call JDLevitate 1 0` and `Call
        # mwTransportFollowersFunc travelMarker 0 100 0` are all legal.  Splitting
        # the tail on commas alone left the whitespace-separated form glued into
        # one token and emitted `JDLevitate.TES4Call(1 0)`, which the Papyrus
        # parser rejects with "extraneous input '0' expecting RPAREN" — 487
        # Morrowind_ob scripts failed on exactly this.
        if fname_low == 'call' and args_str:
            head, _, rest = args_str.strip().partition(' ')
            target = head.strip().rstrip(',')
            if target:
                # Key the property on the CANONICAL EditorID, not the spelling
                # this call happened to use.  TES4 name lookup is
                # case-insensitive, so `Call fbmwbmWerewolfManageControlPC` and
                # the record's own `fbmwBMWerewolfManageControlPC` are the same
                # script — but keying on the local spelling created a SECOND
                # _property_refs entry differing only in case, and since Papyrus
                # is case-insensitive the two declarations collided: the generic
                # ObjectReference typing won and `.TES4Call()` became "undefined
                # function" on a property that has it.  (Same trap as the named
                # -form path below.)
                fid = self.xref.edid_to_formid.get(target.lower(), '')
                canon = self.xref.formid_to_edid.get(fid, target) if fid else target
                script_type = papyrus_script_name(canon)
                prop = _safe_property_name(canon)
                self._property_refs[prop] = script_type
                args = _split_obse_args(rest)
                conv = ', '.join(self._convert_expression(a, extends) for a in args)
                return f'{prop}.{_UDF_NAME}({conv})'

        if fname_low == 'getself':
            if extends == 'ActiveMagicEffect':
                return 'GetTargetActor()'
            if extends == 'TopicInfo':
                return 'akSpeakerRef'
            if extends == PLAYER_ALIAS_EXTENDS:
                return 'GetReference()'
            return 'Self'

        if fname_low == 'getpcissex':
            arg = args_str.strip().lower() if args_str else 'male'
            sex_val = '1' if 'female' in arg else '0'
            return f'Game.GetPlayer().GetActorBase().GetSex() == {sex_val}'

        if fname_low == 'getactionref':
            return self._get_action_ref_param()

        if fname_low == 'isactionref':
            # The operand is always a REFERENCE, never a script variable, so the
            # `player` keyword wins here even in a script that also declares a
            # local called Player (StartCelleAufzugTriggerZone01Script does):
            # `IsActionRef player` asks whether the ACTOR was the player, while
            # its own `Player` short is a separate trigger flag.  Going through
            # _convert_expression let the local-variable guard suppress the
            # keyword and emitted `akActionRef == player`, comparing an
            # ObjectReference against an Int.
            arg = ''
            if args_str:
                _a = args_str.strip()
                if _a.lower() in ('player', 'playerref'):
                    arg = 'Game.GetPlayer()'
                else:
                    arg = self._convert_expression(_a, extends)
            return f'{self._get_action_ref_param()} == {arg}'

        # OBSE `GetLocalGravity <axis>` — the per-axis gravity vector acting on
        # the calling reference.  Papyrus exposes no gravity accessor at all
        # (the value lives in the `fGravity` INI setting, which is present in
        # BOTH engines and reachable from neither script language), so the
        # literal constant IS the faithful translation: gravity in Skyrim is a
        # world constant that points straight down, so X and Y are always 0 and
        # only Z carries the magnitude.  Emitted as a signed value to match
        # OBSE, whose callers subtract it as a downward acceleration.
        if fname_low == 'getlocalgravity':
            axis = args_str.strip().upper() if args_str else 'Z'
            return '-9.81' if axis == 'Z' else '0.0'

        # GetPos/GetAngle/GetStartingAngle: axis param -> GetPositionX/Y/Z or GetAngleX/Y/Z
        if fname_low in ('getpos', 'getangle', 'getstartingangle'):
            axis = args_str.strip().upper() if args_str else 'X'
            if axis not in ('X', 'Y', 'Z'):
                axis = 'X'
            if fname_low == 'getpos':
                papyrus = f'GetPosition{axis}'
            else:
                papyrus = f'GetAngle{axis}'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.{papyrus}()' if ref_name else f'{papyrus}()'

        # SetPos/SetAngle: axis param -> SetPosition(x,y,z) / SetAngle(x,y,z)
        if fname_low in ('setpos', 'setangle'):
            parts = args_str.split(None, 1) if args_str else ['X', '0']
            axis = parts[0].upper() if parts else 'X'
            value = self._convert_expression(parts[1], extends) if len(parts) > 1 else '0'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            if fname_low == 'setpos':
                axes = {'X': (value, f'{ref}.GetPositionY()', f'{ref}.GetPositionZ()'),
                        'Y': (f'{ref}.GetPositionX()', value, f'{ref}.GetPositionZ()'),
                        'Z': (f'{ref}.GetPositionX()', f'{ref}.GetPositionY()', value)}
                x, y, z = axes.get(axis, (value, f'{ref}.GetPositionY()', f'{ref}.GetPositionZ()'))
                return f'{ref}.SetPosition({x}, {y}, {z})'
            else:
                axes = {'X': (value, f'{ref}.GetAngleY()', f'{ref}.GetAngleZ()'),
                        'Y': (f'{ref}.GetAngleX()', value, f'{ref}.GetAngleZ()'),
                        'Z': (f'{ref}.GetAngleX()', f'{ref}.GetAngleY()', value)}
                x, y, z = axes.get(axis, (value, f'{ref}.GetAngleY()', f'{ref}.GetAngleZ()'))
                return f'{ref}.SetAngle({x}, {y}, {z})'

        # GetRandomPercent -> Utility.RandomInt(0, 99)
        if fname_low == 'getrandompercent':
            return 'Utility.RandomInt(0, 99)'

        # SetStage/GetStage/GetStageDone: first arg is quest, second is stage
        if fname_low in ('setstage', 'getstage', 'getstagedone'):
            if args_str and ',' in args_str:
                parts = [p.strip() for p in args_str.split(',')]
            else:
                parts = args_str.split() if args_str else []
            if len(parts) >= 2:
                quest_ref = parts[0].rstrip(',')
                stage = parts[1].rstrip(',')
            elif len(parts) == 1:
                quest_ref = parts[0].rstrip(',')
                stage = '0'
            else:
                quest_ref = 'quest'
                stage = '0'
            # The quest EditorID is a PROPERTY name, so it goes through the same
            # sanitiser as every other ref — an Oblivion quest can be named the
            # same as a Skyrim script (MS14), and emitting it raw makes the CK
            # read it as the type rather than the property.
            quest_ref = _safe_property_name(quest_ref)
            # Always use base Quest type for SetStage/GetStage method calls.
            # The TES4 attached script (TES4_FGC01Script etc.) won't match the
            # quest's TES5 VMAD script (TES4_QF_*), so the property would be
            # null at runtime if we used the TES4 script type.
            if quest_ref not in self._property_refs or self._property_refs[quest_ref] == 'Quest':
                self._property_refs[quest_ref] = 'Quest'
            # Don't downgrade a more specific type already set via cross-script
            # variable access (e.g. FGC01Rats.someVar) — that uses the TES4 type.
            papyrus = {'setstage': 'SetStage', 'getstage': 'GetStage',
                        'getstagedone': 'GetStageDone'}[fname_low]
            if fname_low in ('getstage', 'getstagedone') and len(parts) < 2:
                return f'{quest_ref}.{papyrus}()'
            if fname_low in ('getstage', 'getstagedone') and stage == '0' and len(parts) < 2:
                return f'{quest_ref}.{papyrus}()'
            # The stage is often a VARIABLE (`setstage MQ01 tempstage`), so it has
            # to go through the expression converter like any other operand —
            # emitting it raw skipped the variable renames and left references
            # pointing at names that no longer exist.
            stage_expr = self._convert_expression(stage, extends)
            return f'{quest_ref}.{papyrus}({stage_expr})'

        # StartQuest/StopQuest/GetQuestRunning/CompleteQuest/IsQuestCompleted: arg is quest
        if fname_low in ('startquest', 'stopquest', 'getquestrunning', 'completequest', 'isquestcompleted'):
            quest_ref = _safe_property_name(args_str.strip() if args_str else 'quest')
            existing = self._property_refs.get(quest_ref, self._property_refs.get(quest_ref.lower(), ''))
            if not existing:
                # No type known yet — use Quest (base type sufficient for
                # Start/Stop/IsRunning). TES4 SCPT-derived names from xref
                # (e.g. TES4_FGC01Script) would be wrong here because in TES5
                # the quest's VMAD script is TES4_QF_<EditorID>, not the SCPT name.
                self._property_refs[quest_ref] = 'Quest'
            # else: keep existing type — if already TES4_XxxScript (extends Quest),
            # .Start()/.Stop() still work and cross-script var access still works.
            papyrus = {'startquest': 'Start', 'stopquest': 'Stop',
                        'getquestrunning': 'IsRunning',
                        'completequest': 'CompleteQuest',
                        'isquestcompleted': 'IsCompleted'}[fname_low]
            return f'{quest_ref}.{papyrus}()'

        # Message/MessageBox.  Vanilla TES4 uses the same printf convention as
        # the OBSE variants below — `Message "%.0f seconds to close Great
        # Gate!", remainingSec` — so a format string with arguments has to go
        # through the same concatenation helper.  _quote_msg keeps only the
        # first quoted string, which printed the specifier LITERALLY to the
        # player: MQ14's Great Gate countdown read "%.0f seconds to close Great
        # Gate!", and so did the bounty, the Dawnfang kill count and the Bruma
        # statue's year.  86 call sites (16 SCPT + 70 INFO).
        if fname_low in ('message', 'messagebox'):
            # A MessageBox WITH buttons becomes an authored MESG's Show():
            # Show() parks this thread on the box and returns the clicked
            # index, which TES4_TakeMsgButton() then hands to the script's
            # GetButtonPressed poll exactly once (see message_menus.py — the
            # importer writes the MESG records this property binds to).
            if fname_low == 'messagebox':
                from .message_menus import parse_button_box
                parsed = parse_button_box(args_str or '')
                if parsed:
                    mesg = self._mesg_for_box(*parsed)
                    if mesg:
                        self._property_refs[mesg] = 'Message'
                        self._uses_msg_buttons = True
                        return f'TES4_MsgButton = TES4_ShowMsg({mesg})'
                    # No planned MESG (a fragment context, or plan drift):
                    # fall through — the text-only box is still shown.
            papyrus = ('Debug.Notification' if fname_low == 'message'
                       else 'Debug.MessageBox')
            s = args_str.strip().lstrip(',').strip() if args_str else ''
            if s.startswith('"'):
                end = s.find('"', 1)
                if end >= 0 and self._OBSE_FMT_RE.search(s[1:end]):
                    return f'{papyrus}({self._format_message(s, extends)})'
            return f'{papyrus}({self._quote_msg(args_str)})'

        # OBSE printf-style variants: a format string plus its arguments.
        # printToConsole is a debug trace; MessageBoxEX is a player-facing box
        # (its `|`-separated button list has no Papyrus equivalent, so only the
        # message text survives — _format_string_call keeps the whole string,
        # which is the closest faithful rendering without a UI menu).
        if fname_low in ('printtoconsole', 'printc'):
            return f'Debug.Trace({self._format_string_call(args_str, extends)})'
        if fname_low in ('messageboxex', 'messageex'):
            return f'Debug.MessageBox({self._format_string_call(args_str, extends)})'

        # --- Compound player.Function ---
        # Functions with a dedicated handler further down must NOT be short-cut
        # here: the compound entry routes args through _convert_args, which
        # splits on commas only.  Oblivion writes `Player.PlaceAtMe SRMonster 1,
        # 256, 1` — base and count separated by a SPACE — so comma-splitting
        # yielded a first arg of `SRMonster 1` and emitted
        # `PlaceAtMe(SRMonster 1, 256, 1)`, which does not parse.  The dedicated
        # handler normalizes both separators and resolves the receiver itself.
        # moveto/movetomarker have the SAME two problems as placeatme, plus a
        # third: the compound path never registers the destination as a property,
        # so `Player.MoveTo <marker>` emitted a bare identifier that nothing
        # declared and the compiler rejected the whole script.  (Oblivion writes
        # the offsets space-separated too — `MoveTo marker 0 100 0`.)  Only the
        # `player.`-prefixed form took this path, which is why a plain
        # `ref.MoveTo` looked fine while Morroblivion's CATChargenAndTransport
        # failed on `Player.MoveTo CGPlayerStartMarker1`.
        _COMPOUND_HAS_OWN_HANDLER = ('placeatme', 'moveto', 'movetomarker')
        compound = f'{ref_name}.{func_name}'.lower() if ref_name else ''
        if compound in FUNCTION_MAP and fname_low not in _COMPOUND_HAS_OWN_HANDLER:
            entry = FUNCTION_MAP[compound]
            papyrus_func, _, note = entry
            if papyrus_func:
                args = self._convert_args(args_str, fname_low, extends) if args_str else ''
                result = f'{papyrus_func}({args})'
                return f'{result}  {note}' if note else result

        # GetPCExpelled / SetPCExpelled: faction arg.
        # Skyrim has the exact natives on both sides — vanilla Faction.psc
        # declares `bool Function IsPlayerExpelled()` and
        # `Function SetPlayerExpelled(bool abIsExpelled = true)`.  The reader
        # used to test `GetFactionRank(...) < 0` instead, which was asymmetric
        # with the setter below: SetPlayerExpelled sets the engine's expelled
        # flag and never touches rank, so nothing ever drove the rank negative
        # and every GetPCExpelled read was permanently false.
        if fname_low in ('getpcexpelled', 'ispcexpelled'):
            faction = self._convert_expression(args_str, extends) if args_str else 'None'
            if args_str:
                self._property_refs[args_str.strip()] = 'Faction'
            return f'{faction}.IsPlayerExpelled()'
        if fname_low == 'setpcexpelled':
            # `SetPCExpelled Faction, 1` — Oblivion allowed a comma between the
            # args, and splitting on whitespace leaves it glued to the faction
            # name (`Faction,`), which is then emitted as part of the call.
            parts = [p.rstrip(',') for p in args_str.split(None, 1)] if args_str else []
            faction = self._convert_expression(parts[0], extends) if parts else 'None'
            if parts:
                self._property_refs[parts[0].strip()] = 'Faction'
            val = parts[1].strip() if len(parts) > 1 else '1'
            if val == '0':
                return f'{faction}.SetPlayerExpelled(false)'
            return f'{faction}.SetPlayerExpelled(true)'

        # GotoJail → faction.SendPlayerToJail()
        if fname_low == 'gotojail':
            self._property_refs['TES4CyrodiilCrimeFaction'] = 'Faction'
            return 'TES4CyrodiilCrimeFaction.SendPlayerToJail()'

        # Crime gold functions → TES4CyrodiilCrimeFaction proxy
        if fname_low == 'getcrimegold':
            self._property_refs['TES4CyrodiilCrimeFaction'] = 'Faction'
            return 'TES4CyrodiilCrimeFaction.GetCrimeGold()'
        if fname_low == 'setcrimegold':
            arg = self._convert_expression(args_str.strip(), extends) if args_str else '0'
            self._property_refs['TES4CyrodiilCrimeFaction'] = 'Faction'
            # SetCrimeGold takes Int; TES4 float vars need cast
            arg_type = self._var_types.get(arg.lower(), '') or self._property_refs.get(arg, self._property_refs.get(arg.lower(), ''))
            if arg_type == 'Float':
                arg = f'{arg} as Int'
            return f'TES4CyrodiilCrimeFaction.SetCrimeGold({arg})'
        if fname_low == 'modcrimegold':
            arg = self._convert_expression(args_str.strip(), extends) if args_str else '0'
            self._property_refs['TES4CyrodiilCrimeFaction'] = 'Faction'
            return (f'TES4CyrodiilCrimeFaction.ModCrimeGold'
                    f'({self._cast(arg, "Int")}, false)')
        if fname_low in ('payfine', 'payfinethief'):
            self._property_refs['TES4CyrodiilCrimeFaction'] = 'Faction'
            return 'TES4CyrodiilCrimeFaction.PlayerPayCrimeGold(false, false)'
        # "Is the player serving a jail sentence" — NOT faction expulsion, which
        # is what all four spellings used to emit. Skyrim has the exact native:
        # vanilla Actor.psc declares `bool Function IsArrested() native`,
        # documented "Is this actor currently arrested?" (the condition-function
        # form is GetArrestedState, index 656).
        #
        # All 9 TES4 sites are jail mechanics — the prison cell doors, the
        # Leyawiin jailor, Amusei (whom you meet in a cell), the tutorial's
        # prison start, and TG00FindThievesGuildScript, whose stage 10 is the
        # ENTRY POINT of the Thieves Guild questline. Expulsion is never set on
        # TES4CyrodiilCrimeFaction for the player, so every one read false.
        if fname_low in ('isplayerinjail', 'getplayerinjail', 'isplayerinprison',
                         'senttojail'):
            return 'Game.GetPlayer().IsArrested()'

        # Fame/Infamy → GlobalVariable
        if fname_low in ('getpcfame',):
            self._property_refs['TES4Fame'] = 'GlobalVariable'
            return 'TES4Fame.GetValueInt()'
        if fname_low in ('getpcinfamy', 'getinfame'):
            self._property_refs['TES4Infamy'] = 'GlobalVariable'
            return 'TES4Infamy.GetValueInt()'
        if fname_low == 'modpcfame':
            arg = self._convert_expression(args_str.strip(), extends) if args_str else '0'
            self._property_refs['TES4Fame'] = 'GlobalVariable'
            return f'TES4Fame.Mod({self._cast(arg, "Float")})'
        if fname_low == 'modpcinfamy':
            arg = self._convert_expression(args_str.strip(), extends) if args_str else '0'
            self._property_refs['TES4Infamy'] = 'GlobalVariable'
            return f'TES4Infamy.Mod({self._cast(arg, "Float")})'
        if fname_low == 'setpcfame':
            arg = self._convert_expression(args_str.strip(), extends) if args_str else '0'
            self._property_refs['TES4Fame'] = 'GlobalVariable'
            arg_type = self._var_types.get(arg.lower(), '') or self._property_refs.get(arg, self._property_refs.get(arg.lower(), ''))
            if arg_type == 'Float':
                arg = f'{arg} as Int'
            return f'TES4Fame.SetValueInt({arg})'
        if fname_low == 'setpcinfamy':
            arg = self._convert_expression(args_str.strip(), extends) if args_str else '0'
            self._property_refs['TES4Infamy'] = 'GlobalVariable'
            arg_type = self._var_types.get(arg.lower(), '') or self._property_refs.get(arg, self._property_refs.get(arg.lower(), ''))
            if arg_type == 'Float':
                arg = f'{arg} as Int'
            return f'TES4Infamy.SetValueInt({arg})'

        # Weather functions are a deliberate NO-OP: weather conversion is broken
        # and is currently skipped.  CONVERT_CLIMATE is False, so the CLMT chain
        # (WRLD -> CNAM -> CLMT -> WLST) that is the only route to a weather is
        # never written, and Cyrodiil renders under Skyrim's default climate.
        # Forcing an unreachable weather into Skyrim's sky system divides-by-zero
        # in the weather update and hard-crashes, so neutralize the override
        # rather than emit a call.  Keep this a no-op until weather conversion
        # itself is fixed.
        if fname_low in ('forceweather', 'fw', 'setweather', 'sw'):
            arg = args_str.strip() if args_str else ''
            return f';NE: {fname_low} {arg} (Oblivion weather not converted)'
        if fname_low == 'releaseweatheroverride':
            return ';NE: ReleaseWeatherOverride (Oblivion weather not converted)'
        if fname_low in ('getiscurrentweather', 'getweatherpercent'):
            if fname_low == 'getweatherpercent':
                self._line_comments.append(';NE: GetWeatherPercent approximated')
                return '50'
            arg = self._convert_expression(args_str.strip(), extends) if args_str else 'None'
            if args_str and args_str.strip():
                self._property_refs[args_str.strip()] = 'Weather'
            return f'(Weather.GetCurrentWeather() == {arg})'

        # Sound functions.  Vanilla writes the EditorID QUOTED
        # (`PlaySound "AMBBaenlinDeath"`), and the property must be registered
        # under the name that is actually EMITTED — _convert_expression strips
        # the quotes, but registering the raw argument kept them, and
        # _safe_property_name turned each quote into an underscore.  That
        # declared a second, never-referenced `Sound Property _X_ Auto`
        # alongside the real one: 75 dead properties across 23 files, none of
        # them bindable (no record is named `"X"` with quotes).  Same class of
        # artifact as the Game_GetPlayer__ properties fixed in round 2.
        if fname_low in ('playsound', 'playsound3d'):
            raw = (args_str.strip() if args_str else '').strip('"\'')
            arg = self._convert_expression(raw, extends) if raw else 'None'
            if raw:
                self._property_refs[raw] = 'Sound'
            if fname_low == 'playsound':
                return f'{arg}.Play(Game.GetPlayer())'
            ref = self._resolve_self_ref(ref_name, extends) if ref_name \
                else self._implicit_self(extends)
            return f'{arg}.Play({ref})'
        if fname_low == 'stopsound':
            self._line_comments.append(';NE: StopSound has no Papyrus equivalent')
            return '0'

        # Music playback by FILE PATH: vanilla `StreamMusic "data\music\..."` and
        # Nehrim's emc* plugin commands.  Skyrim's music system is form-driven
        # (MusicType.Add()/Remove() on a MUSC record) and neither Papyrus nor
        # SKSE can start a track from a path, so there is nothing to call — the
        # MUSC records would have to be authored first.  Emit an inert marker
        # rather than a call that cannot compile.
        # `emc*` is Nehrim's bundled music-control plugin (emcPlayTrack,
        # emcSetMusicType, emcIsBattleOverridden, ...); match the whole family by
        # prefix rather than chasing each name.  `emcount` is a local variable in
        # some scripts, not a command, so require a longer name.
        if fname_low in ('streammusic',) or (
                fname_low.startswith('emc') and fname_low != 'emcount'
                and len(fname_low) > 5):
            self._line_comments.append(
                f';NE: {func_name} — Skyrim music is MusicType-based, '
                f'no path playback ({args_str.strip()})')
            return '0'

        # OBSE IsCasting: "is this actor playing a cast animation".  Skyrim
        # exposes exactly that natively through the animation graph, so no SKSE
        # dependency is needed.
        if fname_low == 'iscasting':
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return (f'({ref}.GetAnimationVariableBool("bIsCastingRight") || '
                    f'{ref}.GetAnimationVariableBool("bIsCastingLeft"))')

        # Vanilla TES4 GetPlayerHasLastRiddenHorse — no Skyrim equivalent (the
        # engine tracks no "last ridden" horse), and SKSE adds none.
        if fname_low == 'getplayerhaslastriddenhorse':
            self._line_comments.append(
                ';NE: GetPlayerHasLastRiddenHorse has no Skyrim equivalent')
            return '0'

        # TES4 `PositionCell x, y, z, angle, Cell` teleports a reference to raw
        # coordinates in a named cell.  Papyrus MoveTo takes a TARGET REFERENCE,
        # not a cell plus coordinates, and Skyrim exposes no cell-coordinate
        # move — the scripts using it dump refs into a trash cell, which needs a
        # marker reference that does not exist in the conversion.
        if fname_low == 'positioncell':
            self._line_comments.append(
                f';NE: PositionCell needs a target marker; Papyrus MoveTo takes '
                f'a reference, not cell coordinates ({args_str.strip()})')
            return '0'

        # ObjectReference.IgnoreFriendlyHits is a SETTER in Skyrim; TES4's
        # GetIgnoreFriendlyHits reads the flag back and Papyrus cannot.
        if fname_low == 'getignorefriendlyhits':
            self._line_comments.append(
                ';NE: GetIgnoreFriendlyHits — Skyrim exposes only the setter')
            return '0'

        # sv_Construct is the ONE OBSE string command with an exact Papyrus
        # equivalent: it builds a string_var from a literal, and Papyrus String
        # IS that literal.  Falling through to the inert ar_/sv_ catch-all below
        # left `quizQuestion = sv_Construct "..."` in the output as an undefined
        # identifier, which failed the whole script — Morroblivion's
        # fbmwChargenQuestScript (the class quiz) is the site, and the
        # Chargen-and-Transport start menu imports it, so the Imperial City
        # transport NPC went down with it.  sv_Destruct stays a no-op: Papyrus
        # strings are garbage-collected, so there is nothing to free.
        if fname_low == 'sv_construct':
            arg = args_str.strip()
            if not arg:
                return '""'
            # A bare quoted literal passes straight through; anything else is an
            # expression (a format string plus args) the caller already handles.
            if arg.startswith('"') and arg.endswith('"') and arg.count('"') == 2:
                return arg
            return self._convert_expression(arg, extends)

        # OBSE `GetGlobalValue <Global>` / `SetGlobalValue <Global> <value>` read
        # and write a global by NAME rather than by direct reference.  Papyrus
        # reaches a global through a property of type GlobalVariable, which the
        # normal named-form path already builds — so resolve the operand the same
        # way any other global reference is resolved.  Left unmapped, the operand
        # stayed a bare name and broke the enclosing expression ("unexpected name
        # `fbmwbmclawcost`"), taking the werewolf script family down with it.
        if fname_low in ('getglobalvalue', 'setglobalvalue'):
            parts = _split_obse_args(args_str)
            if parts:
                gname = parts[0].strip()
                safe = _safe_property_name(gname)
                self._property_refs[safe] = 'GlobalVariable'
                if fname_low == 'getglobalvalue':
                    return self._global_read(safe)
                val = (self._convert_expression(parts[1], extends)
                       if len(parts) > 1 else '0')
                return f'{safe}.SetValue({val})'

        # OBSE / TES4-only commands with no VANILLA Papyrus equivalent.  Each was
        # checked against Actor.psc, ObjectReference.psc, Form.psc, Game.psc and
        # Utility.psc and exists in none of them.  Some are available through
        # SKSE (docs/skse_conversion_audit.md); nothing here targets SKSE today,
        # so they are neutralised for now.  Neutralise rather than emit: an
        # unknown name is a hard compile error that takes down the whole file
        # AND every script that imports it, whereas an inert 0 keeps the rest of
        # the script working.
        if fname_low in _OBSE_NO_EQUIV_COMMANDS:
            orig = f'{func_name} {args_str}'.strip()
            self._line_comments.append(
                f';NE: {func_name} — no Papyrus equivalent ({orig})')
            return '0'

        # OBSE `SetCurrentHealth <value>` takes only the value — the actor value
        # is implicit in the name, so it cannot map straight onto SetActorValue
        # (which would swallow the number as the AV NAME and set nothing).
        if fname_low == 'setcurrenthealth':
            ref = self._convert_ref(ref_name, extends) if ref_name else 'Self'
            val = (self._convert_expression(args_str.strip(), extends)
                   if args_str.strip() else '0')
            return f'{ref}.SetActorValue("Health", {val})'

        # OBSE `IsOnGround` is the complement of Skyrim's IsFlying.  The negation
        # has to wrap the WHOLE call, not ride in the function name: a plain
        # `('!IsFlying', True)` map entry emitted `myActor.!IsFlying()`, which is
        # not Papyrus.
        if fname_low == 'isonground':
            ref = self._convert_ref(ref_name, extends) if ref_name else 'Self'
            return f'!({ref}.IsFlying())'

        # TES4 `UncompleteQuest <Quest>` reopens a finished quest, naming the
        # quest as an ARGUMENT.  Papyrus spells it as a method on the quest
        # itself, so the argument has to become the receiver — mapping it
        # straight onto Reset emitted `Reset(fbmwEBBone)` ("function takes 0
        # parameters not 1").
        if fname_low == 'uncompletequest':
            target = args_str.strip()
            if target:
                return f'{self._convert_ref(target, extends)}.Reset()'
            ref = self._convert_ref(ref_name, extends) if ref_name else 'Self'
            return f'{ref}.Reset()'

        # `ref.Update3D` / `ref.IsThirdPerson` — written as RECEIVER methods, so
        # both must consume the receiver rather than be emitted after a dot
        # (`ActorRef.TES4Polyfill.Update3D()` / `ActorRef.False()` are not
        # Papyrus).  The receiver becomes the polyfill's argument; with no
        # receiver the call is on the player, which is the only actor whose
        # camera or first-person model these commands can concern.
        if fname_low == 'update3d':
            target = (self._convert_ref(ref_name, extends) if ref_name
                      else (args_str.strip() and
                            self._convert_expression(args_str.strip(), extends))
                      or 'Game.GetPlayer()')
            return f'TES4Polyfill.Update3D({target})'
        if fname_low == 'isthirdperson':
            # See the FUNCTION_MAP note: vanilla Papyrus can force a camera mode
            # but not read one, and Skyrim's own model-swap script refreshes
            # unconditionally, so the guard reports False and the refresh runs.
            return 'False'

        # `ToggleFirstPerson <0|1>` — Oblivion's one command with an argument is
        # two argument-free globals in Skyrim.  0 forces THIRD person, 1 forces
        # first; the bare form (no argument) is a true toggle, which Papyrus
        # cannot express because it cannot read the current mode, so it takes
        # the third-person branch (the mode every caller here is refreshing in).
        if fname_low == 'togglefirstperson':
            arg = args_str.strip()
            return ('Game.ForceFirstPerson()' if arg == '1'
                    else 'Game.ForceThirdPerson()')

        # OBSE `runScriptLine "<console command>"` compiles and runs a console
        # command at runtime.  Papyrus cannot execute the console at all, and
        # Morrowind_ob uses it exclusively to poke the OPTIONAL ObXP mod's
        # globals ("set ObXPMain.interOpGainedXP to 100") — a mod that is not
        # part of the conversion, so there is no target to write even in
        # principle.  Neutralise: the payload is a quoted console line
        # containing OBSE's `%q` escaped-quote token and apostrophes, which as
        # emitted broke the Papyrus string literal it was pasted into.
        if fname_low in ('runscriptline', 'runbatchscript'):
            orig = f'{func_name} {args_str}'.strip()
            self._line_comments.append(
                f';NE: {func_name} — OBSE console execution, no Papyrus '
                f'equivalent ({orig})')
            return '0'

        # OBSE `SetEventHandler "OnDeath" <script> "object"::Player` registers a
        # script as a callback for an engine event.  Papyrus has no registration
        # API of this shape — an event is bound by DECLARING it (`Event
        # OnDeath()`) on a script attached to the form — so there is no
        # call-for-call translation.  Neutralise rather than emit: the argument
        # syntax carries OBSE's `::` type-tag operator, which is not Papyrus
        # syntax at all and fails the parse of every script that imports this one.
        if fname_low in ('seteventhandler', 'removeeventhandler'):
            orig = f'{func_name} {args_str}'.strip()
            self._line_comments.append(
                f';NE: {func_name} — OBSE event registration; Papyrus binds '
                f'events by declaring them on the attached script ({orig})')
            return '0'

        # OBSE arrays and string-variables (ar_Construct/ar_Null/sv_Destruct,
        # `forEach x <- container`).  Papyrus has real arrays and strings but no
        # equivalent of OBSE's dynamic containers or its iterator syntax, and the
        # surrounding logic reads them element-by-element — there is nothing to
        # translate call-for-call, so keep the source visible and inert.
        if fname_low.startswith(('ar_', 'sv_')) or fname_low == 'foreach':
            orig = f'{func_name} {args_str}'.strip()
            self._line_comments.append(
                f';NE: {func_name} — OBSE array/string command, no Papyrus '
                f'equivalent ({orig})')
            return '0'

        # Vanilla TES4 HasFlames / light-state toggles on a light reference.
        # Skyrim lights carry no scriptable flame state.
        if fname_low == 'hasflames':
            self._line_comments.append(
                ';NE: HasFlames has no Skyrim equivalent')
            return '0'
        if fname_low in ('flameson', 'flamesoff', 'addflames', 'removeflames'):
            self._line_comments.append(
                f';NE: {func_name} has no Skyrim equivalent')
            return '0'

        # Magic shader/effect functions
        if fname_low in ('pms', 'playmagicshadervisuals'):
            parts = args_str.strip().split() if args_str else []
            shader_name = parts[0] if parts else None
            duration = parts[1] if len(parts) > 1 else '-1.0'
            if shader_name:
                safe = _safe_property_name(shader_name)
                self._property_refs[safe] = 'EffectShader'
                ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
                dur = self._convert_expression(duration, extends)
                return f'{safe}.Play({ref}, {dur})'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'Self.Play({ref})'
        if fname_low in ('sms', 'stopmagicshadervisuals'):
            parts = args_str.strip().split() if args_str else []
            shader_name = parts[0] if parts else None
            if shader_name:
                safe = _safe_property_name(shader_name)
                self._property_refs[safe] = 'EffectShader'
                ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
                return f'{safe}.Stop({ref})'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'Self.Stop({ref})'
        if fname_low == 'triggerhitshader':
            return 'Game.TriggerScreenBlood(3)'

        # pme/sme (PlayMagicEffectVisuals/StopMagicEffectVisuals): the argument
        # is a MAGIC EFFECT code (DSPL, STRP, ...), not a shader EditorID.  The
        # visuals Oblivion plays are the effect's EFSH — and EFSH records ARE
        # converted — so resolve code → TES4 MGEF → its shader and Play/Stop
        # that, exactly like pms/sms do for a directly-named shader.
        if fname_low in ('pme', 'playmagiceffectvisuals',
                         'sme', 'stopmagiceffectvisuals'):
            parts = args_str.strip().split() if args_str else []
            code = parts[0] if parts else ''
            shader_edid = (self.xref.get_mgef_shader_edid(code)
                           if (self.xref and code) else '')
            ref = self._resolve_objref_ref(ref_name, extends)
            if not shader_edid:
                orig = f'{ref_name}.{func_name} {args_str}'.strip() if ref_name \
                    else f'{func_name} {args_str}'.strip()
                self._line_comments.append(
                    f';NE: {orig} (no shader found for effect code)')
                return '0'
            safe = _safe_property_name(shader_edid)
            self._property_refs[safe] = 'EffectShader'
            if fname_low in ('sme', 'stopmagiceffectvisuals'):
                return f'{safe}.Stop({ref})'
            duration = parts[1] if len(parts) > 1 else '-1.0'
            dur = self._convert_expression(duration, extends)
            return f'{safe}.Play({ref}, {dur})'

        # IsSpellTarget: "is ref currently affected by spell X".  Papyrus has no
        # per-spell test, but HasMagicEffect on the effect the converted SPEL
        # actually carries (resolved through the importer's own code→MGEF
        # mapping) answers the same question at runtime.
        if fname_low == 'isspelltarget':
            spell = args_str.strip().split()[0] if args_str and args_str.strip() else ''
            fid = (self.xref.get_spell_first_skyrim_mgef(spell)
                   if (self.xref and spell) else 0)
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            if ref == 'Self' and extends not in ('Actor',):
                ref = '(Self as Actor)'
            if fid:
                return f'TES4Polyfill.HasMagicEffectByID({ref}, 0x{fid:08X})'
            orig = f'{ref_name}.{func_name} {args_str}'.strip() if ref_name \
                else f'{func_name} {args_str}'.strip()
            self._line_comments.append(f';NE: {orig} (spell has no convertible effect)')
            return 'False'

        # IsAnimPlaying: the behavior graph exposes this as an animation
        # variable.  Cast to Int because TES4 call sites compare/assign 0/1.
        if fname_low == 'isanimplaying':
            ref = self._resolve_objref_ref(ref_name, extends)
            return f'({ref}.GetAnimationVariableBool("bAnimPlaying") as Int)'

        # GetArmorRating → DamageResist actor value (what armor rating feeds)
        if fname_low == 'getarmorrating':
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            if ref == 'Self' and extends not in ('Actor',):
                ref = '(Self as Actor)'
            return f'{ref}.GetActorValue("DamageResist")'

        # GetFirstRef <type> / GetNextRef — OBSE's walk over every reference in
        # the loaded cells.  Papyrus has no such iterator, but Skyrim ships the
        # engine's own "an actor near here" primitive, so an ACTOR walk (TES4
        # form type 69) becomes repeated Game.FindRandomActorFromRef sampling
        # around the player.  Both spellings return the same expression: the
        # authored loop already re-assigns the variable each pass, so drawing a
        # fresh sample per pass is exactly the iteration it asked for.
        #
        # Only the actor walk converts.  A walk over any other form type has no
        # Actor-typed primitive behind it, so it neutralises to None and the
        # `While (<ref> != None)` the Label emits simply never runs — inert,
        # not wrong.
        if fname_low in ('getfirstref', 'getnextref'):
            type_arg = args_str.split(None, 1)[0].strip() if args_str else ''
            # 69 is TES4's ACTOR form type; GetNextRef carries no type and
            # continues whatever GetFirstRef opened.
            if fname_low == 'getnextref' or type_arg == '69':
                return ('Game.FindRandomActorFromRef(Game.GetPlayer(), '
                        f'{_REFWALK_RADIUS})')
            self._line_comments.append(
                f';NE: {func_name} over form type {type_arg or "?"} — Papyrus '
                f'iterates actors only')
            return 'None'

        # GetIsCreature: Skyrim marks people via the ActorTypeNPC race keyword;
        # converted creatures use generated races without it.
        if fname_low in ('getiscreature', 'iscreature'):
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            if ref == 'Self' and extends not in ('Actor',):
                ref = '(Self as Actor)'
            return f'TES4Polyfill.GetIsCreature({ref})'

        # AdvancePCLevel: raise the player exactly one level.  Skyrim's vanilla
        # Game.psc (Scripts.zip) has NO level setter — Game.SetPlayerLevel is a
        # mod-supplied extension, absent from the shipped headers — so the
        # writable Level actor value is the equivalent the base game does offer.
        # Nehrim drives its whole custom level-up through this call
        # (GlobaltagebuchScript's journal menu), so leaving it unmapped left the
        # player permanently at level 1.
        if fname_low == 'advancepclevel':
            return 'Game.GetPlayer().ModActorValue("Level", 1)'

        # HasVampireFed: Skyrim's PlayerVampireQuestScript.VampireStatus is 1
        # exactly while the vampire has recently fed.
        if fname_low == 'hasvampirefed':
            return 'TES4Polyfill.HasVampireFed()'

        # GetIsCurrentPackage: vanilla Actor.GetCurrentPackage() makes this an
        # exact conversion when the argument is a converted PACK record.
        if fname_low == 'getiscurrentpackage':
            arg = args_str.strip().split()[0] if args_str and args_str.strip() else ''
            fid = self.xref.edid_to_formid.get(arg.lower(), '') if (self.xref and arg) else ''
            if fid and self.xref.record_type.get(fid, '') == 'PACK':
                ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
                if ref == 'Self' and extends not in ('Actor',):
                    ref = '(Self as Actor)'
                safe = _safe_property_name(arg)
                self._property_refs[safe] = 'Package'
                return f'({ref}.GetCurrentPackage() == {safe})'
            orig = f'{ref_name}.{func_name} {args_str}'.strip() if ref_name \
                else f'{func_name} {args_str}'.strip()
            self._line_comments.append(f';NE: {orig}')
            return '0'

        # IsGuard: membership in Skyrim's guard dialogue faction
        if fname_low == 'isguard':
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            if ref == 'Self' and extends not in ('Actor',):
                ref = '(Self as Actor)'
            return f'TES4Polyfill.IsGuard({ref})'

        # SetActorRefraction: no Papyrus refraction control; a translucent
        # alpha fade is the closest visual (0 restores full opacity).
        if fname_low == 'setactorrefraction':
            val = args_str.strip().split()[0] if args_str and args_str.strip() else '0'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            if ref == 'Self' and extends not in ('Actor',):
                ref = '(Self as Actor)'
            val_conv = self._convert_expression(val, extends)
            return f'TES4Polyfill.SetActorRefraction({ref}, {val_conv})'

        # StopCombatAlarmOnActor / SCAOnActor / SCA.
        # NOT StopCombat: that "removes this actor from combat" (ends the
        # actor's OWN aggression), whereas SCAOnActor "stops all combat and
        # alarms AGAINST this actor" — the opposite direction.  Skyrim has the
        # exact native, Actor.StopCombatAlarm().  With StopCombat the whole
        # point of the call was lost: `player.SCAOnActor` is the idiom for
        # calming a mob that is attacking the player (Dark19Whispers uses it to
        # hold the player still through the Night Mother's speech), and
        # stopping only the player's own aggression left everyone still hostile.
        if fname_low in ('scaonactor', 'sca', 'stopcombatalarmonactor'):
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.StopCombatAlarm()'

        # ShowMap → marker.AddToMap(true)
        if fname_low == 'showmap':
            parts = args_str.strip().split() if args_str else []
            marker_name = parts[0] if parts else 'None'
            if marker_name != 'None':
                safe = _safe_property_name(marker_name)
                self._property_refs[safe] = 'ObjectReference'
                return f'{safe}.AddToMap(true)'
            return 'Self.AddToMap(true)'

        # Disposition (removed in Skyrim).  A full -100 drop is Oblivion's
        # "make them hostile" idiom, so it becomes StartCombat.
        #
        # DIRECTION MATTERS.  TES4's signature is
        #     <actor>.ModDisposition <target> <value>
        # and it changes the CALLING actor's disposition toward the target — so
        # `UngolimRef.ModDisposition player -100` means Ungolim now hates the
        # player and Ungolim is the aggressor.  Emitting
        # `<target>.StartCombat(<ref>)` inverted that and made the PLAYER attack
        # Ungolim, which in Dark16Kiss framed the player for the murder the
        # quest wanted Ungolim to commit.  The aggressor is the ref.
        if fname_low == 'moddisposition':
            parts = [p.strip() for p in (args_str.replace(',', ' ').split() if args_str else [])]
            if len(parts) >= 2:
                try:
                    val = int(parts[-1])
                    if val <= -100:
                        target = self._convert_expression(parts[0], extends)
                        tgt_key = target  # already canonical from _convert_expression
                        cur = self._property_refs.get(tgt_key, '')
                        if cur in ('', 'ObjectReference'):
                            self._property_refs[tgt_key] = 'Actor'
                        ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
                        return f'{ref}.StartCombat({target})'
                except (ValueError, IndexError):
                    pass
            self._line_comments.append(f';NE: ModDisposition')
            return '0'
        if fname_low == 'getdisposition':
            return '50'

        # SetAlert → Actor.SetAlert (native, same name and semantics both ways).
        # NOT DrawWeapon: Oblivion's SetAlert sets the AI combat-READINESS flag,
        # which the engine clears on its own and which does NOT block dialogue.
        # DrawWeapon puts the actor in a weapon-drawn state that suppresses the
        # force-greet, and `SetAlert 0` (the sheathe half) was a NO-OP, so an
        # actor alerted for a scripted ambush never stood down: CharacterGen
        # stage 15 alerts Uriel for the prison-cell ambush and stage 17/24
        # clears it to run the conversation, so converted Uriel drew his sword,
        # never sheathed it, and could never initiate dialogue with the player
        # — the intro soft-locked with controls disabled.
        if fname_low == 'setalert':
            arg = args_str.strip().lower() if args_str else '0'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            # SetAlert is Actor-only; cast if ref is ObjectReference
            if ref == 'Self' and extends not in ('Actor',):
                ref = '(Self as Actor)'
            alerted = 'true' if arg in ('1', 'true') else 'false'
            return f'{ref}.SetAlert({alerted})'

        # StartConversation: caller.StartConversation Target [, TopicID].
        # The topic INFO (and its result-script fragment) is the payload —
        # discarding it as Say(None) silenced every scripted NPC-NPC
        # conversation (DANocturnal's Bejeen/Nocturnal talk, MQ12's
        # Jauffre/Martin council, MS10's Llevana scene) and lost their
        # SetStage results. Route it like SayTo: speak the topic directly.
        if fname_low == 'startconversation':
            if args_str and ',' in args_str:
                pparts = [p.strip() for p in args_str.split(',')]
            else:
                pparts = args_str.split() if args_str else []
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            if len(pparts) >= 2 and pparts[1].strip():
                topic_str = pparts[1].strip().split()[0]
                topic = self._convert_expression(topic_str, extends)
                self._mark_topic_property(topic_str)
                return f'{ref}.Say({topic})'
            # No topic.  Per UESP's function table the Topic argument is
            # explicitly "(Optional)", and omitting it makes the engine open the
            # conversation on the greeting — which is a real, resolvable topic
            # (DIAL GREETING, 000000C8) rather than "nothing to say".  Dropping
            # these silenced 64 call sites, all of them the standard
            # `<npc>.StartConversation Player` walk-up beat: FGC01's Pinarus
            # after the mountain lions, and 63 more.  Say(GREETING) is the same
            # routing the 3-argument form already uses.
            self._property_refs['GREETING'] = 'Topic'
            return f'{ref}.Say(GREETING)'

        # Wait → no-op (TES4 Wait is a package instruction, not a time delay)
        if fname_low == 'wait':
            self._line_comments.append(';NE: Wait is a package instruction')
            return '0'

        # Reset3DState → MoveTo self (reloads 3D)
        if fname_low == 'reset3dstate':
            ref = self._resolve_self_ref(ref_name, extends)
            return f'{ref}.MoveTo({ref})'

        # GetDestroyed → destruction stage > 0.  Skyrim has no native bool
        # reader for the destroyed state, but GetCurrentDestructionStage() is
        # native and a destroyed ref always sits above stage 0.
        if fname_low == 'getdestroyed':
            ref = self._resolve_self_ref(ref_name, extends)
            return f'({ref}.GetCurrentDestructionStage() > 0)'

        # ClearOwnership
        if fname_low == 'clearownership':
            ref = self._resolve_self_ref(ref_name, extends)
            return f'{ref}.SetActorOwner(Game.GetPlayer().GetActorBase())'

        # SetRestrained → SetDontMove
        if fname_low == 'setrestrained':
            arg = args_str.strip() if args_str else '0'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            val = 'true' if arg in ('1', 'true') else 'false'
            return f'{ref}.SetDontMove({val})'
        if fname_low == 'getrestrained':
            self._line_comments.append(';NE: GetRestrained')
            return '0'

        # SetForceRun → SpeedMult
        if fname_low == 'setforcerun':
            arg = args_str.strip() if args_str else '0'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            if arg in ('1', 'true'):
                return f'{ref}.SetActorValue("SpeedMult", 150.0)'
            return f'{ref}.SetActorValue("SpeedMult", 100.0)'

        # Faction crime tracking.  TES4 keeps three independent per-faction
        # booleans (Murder, Attack, Steal); every call site in Oblivion.esm
        # reads them as `== 1` and writes them only as `0` (the engine is what
        # sets them).  Skyrim exposes no Papyrus native for any of the three —
        # it keeps GetPCFactionMurder/Attack only as condition functions — so
        # they are reconstructed from the crime-gold split, which IS reachable:
        #
        #   Steal  → GetCrimeGoldNonViolent() > 0
        #   Attack → violent gold in the assault band (0 < gold < murder)
        #   Murder → violent gold at or above the murder bounty
        #
        # The murder/assault threshold is what makes the last two separable.
        # Census of Skyrim.esm: all 14 real crime factions use exactly
        # murder=1000, assault=40 in CRVA, a 25x gap, and the importer writes
        # those same vanilla amounts for every converted crime faction.
        #
        # Mapping both Murder and Attack onto a bare GetCrimeGoldViolent() (the
        # previous behaviour) made them indistinguishable, so any script testing
        # both — FGExpulsionScript's Blackwood chain, TGCastOut, both
        # MGExpulsion scripts — had its murder branch shadowed by the attack
        # branch that precedes it, and `== 1` meant "exactly 1 gold of bounty",
        # which no crime ever produces.
        if fname_low in ('setpcfactionmurder', 'setpcfactionattack',
                         'setpcfactionsteal'):
            parts = [p.strip() for p in (args_str.replace(',', ' ').split() if args_str else [])]
            if not parts:
                return f';NE: {fname} missing faction arg'
            faction = self._convert_expression(parts[0], extends)
            self._property_refs[parts[0].strip()] = 'Faction'
            val = parts[1] if len(parts) > 1 else '1'
            violent = fname_low != 'setpcfactionsteal'
            setter = 'SetCrimeGoldViolent' if violent else 'SetCrimeGold'
            if val in ('0', '0.0'):
                return f'{faction}.{setter}(0)'
            # Writing the flag true means "make this crime stand": raise the
            # bounty into the matching band.  Murder must clear the threshold;
            # assault and theft sit below it.
            amount = {'setpcfactionmurder': TES4_MURDER_BOUNTY,
                      'setpcfactionattack': TES4_ASSAULT_BOUNTY}.get(
                          fname_low, TES4_STEAL_BOUNTY)
            return f'{faction}.{setter}({amount})'

        if fname_low in ('getpcfactionmurder', 'getpcfactionattack',
                         'getpcfactionsteal'):
            arg = self._convert_expression(args_str.strip(), extends) if args_str else 'None'
            if args_str and args_str.strip():
                self._property_refs[args_str.strip()] = 'Faction'
            if fname_low == 'getpcfactionsteal':
                return f'({arg}.GetCrimeGoldNonViolent() > 0) as Int'
            if fname_low == 'getpcfactionmurder':
                return (f'({arg}.GetCrimeGoldViolent() >= '
                        f'{TES4_MURDER_BOUNTY}) as Int')
            # Attack: violent bounty that is NOT big enough to be a murder.
            return (f'({arg}.GetCrimeGoldViolent() > 0 && '
                    f'{arg}.GetCrimeGoldViolent() < {TES4_MURDER_BOUNTY}) as Int')

        # GetIsReference → equality check
        if fname_low == 'getisreference':
            arg = self._convert_expression(args_str.strip(), extends) if args_str else 'None'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref} == {arg}'

        # GetInWorldSpace → WorldSpace comparison
        # GetPlayerInSEWorld takes NO argument and asks whether the player is
        # anywhere in the Shivering Isles — exteriors AND interiors.  It stays
        # the literal 0 (the bare-read fallback), deliberately:
        #
        #   * The exterior half is trivially reconstructible
        #     (GetWorldSpace() against the SE* worldspaces) but the interior
        #     half is not.  An SI interior cell has NO worldspace, carries no
        #     distinguishing climate/music (measured: SI interiors use the same
        #     music types as Cyrodiil's), and the door graph does not separate
        #     the two worlds — the SI<->Cyrodiil gate is a legitimate edge, so
        #     a flood fill from the SE worldspaces reaches 1,407 Cyrodiil
        #     interiors.  There is no sound generic invariant to key on.
        #   * Reconstructing only the exterior half would be WORSE than the
        #     no-op.  Censused over the plugin, 11 of the 16 sites test
        #     `== 0` — they are suppression guards (Lucien Lachance's sleep
        #     visit, the Gray Cowl's bounty transfer, the tutorial's jail
        #     hint), for which a constant 0 is the RIGHT answer everywhere in
        #     Cyrodiil.  An exterior-only test would flip all 11 to false the
        #     moment the player stepped into an SI interior.
        #
        # The 5 `== 1` sites do lose their behaviour; 4 of them are in SI spell
        # scripts that do not run anyway (no MGEF carries a VMAD — see the
        # audit's known gaps).
        if fname_low == 'getinworldspace':
            arg = self._convert_expression(args_str.strip(), extends) if args_str else 'None'
            if args_str and args_str.strip():
                self._property_refs[args_str.strip()] = 'WorldSpace'
            if ref_name:
                ref = self._resolve_self_ref(ref_name, extends)
                return f'{ref}.GetWorldSpace() == {arg}'
            return f'Game.GetPlayer().GetWorldSpace() == {arg}'

        # ModAmountSoldStolen — adds GOLD to the "amount fenced" counter, which
        # Skyrim exposes only as a condition function.  Backed by the synthesized
        # TES4GoldFenced global (see _create_tes4_special_records); NOT the
        # vanilla "Items Stolen" stat, which counts items and is driven by the
        # engine on every theft.
        if fname_low == 'modamountsoldstolen':
            arg = self._convert_expression(args_str.strip(), extends) if args_str else '1'
            self._property_refs['TES4GoldFenced'] = 'GlobalVariable'
            return f'TES4GoldFenced.Mod({self._cast(arg, "Float")})'

        # Reset → ref.Reset()
        if fname_low == 'reset':
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.Reset()'

        # GetDetectionLevel has the SAME shape as GetDetected — per UESP's
        # function table (opcode 0x10B4, 1 param Actor, "Actor Reference"
        # receiver) it is `<observer>.GetDetectionLevel <target>` — so it gets
        # the same receiver/argument swap onto IsDetectedBy.
        #
        # It used to be a flat `0`, which turned every call site into a
        # permanently-false threshold test.  That is safe only if scripts read
        # the level numerically; censused over the plugin, NOT ONE does.  All
        # 56 sites are `>= 2`, `>= 3` or `== 3` — pure "is the target
        # detected" questions, which is exactly what IsDetectedBy answers.
        # The dead tests gated real behaviour: all 7 of Dark04Execution's
        # guard-aggro triggers, the Dark Sanctuary assassins' reaction to the
        # player, Baenlin's and Gromm's murder-witness checks, and the bandit
        # sentries' challenge.
        #
        # The threshold must be RESCALED, not just wrapped.  TES4 levels run
        # 0=unnoticed .. 3=fully detected, but IsDetectedBy is a Bool, and the
        # generic `_BOOL_CMP_RE` pass wraps a Bool meeting a number as
        # `(... as Int) <op> N` — where `true as Int` is 1.  Left alone, the
        # `>= 2` and `>= 3` sites (the majority: DarkVicenteScript, the Dark
        # Sanctuary assassins, the SE guards) would compile but be permanently
        # FALSE, trading one dead form for another.  `== 3` would break the
        # same way.  Scaling the Bool to TES4's own top level yields 0 or 3,
        # which satisfies every threshold the plugin actually uses (>=2, >=3,
        # ==3) exactly when the target is detected and never otherwise.
        # Verified with the CK compiler: a bare `Bool >= 2` is rejected
        # outright ("cannot relatively compare variables of type bool"), so
        # the cast has to be explicit here anyway.
        if fname_low == 'getdetectionlevel':
            observer = self._resolve_self_ref(ref_name, extends, actor_func=True)
            arg = args_str.strip() if args_str else ''
            target = self._convert_expression(arg, extends) if arg else 'Game.GetPlayer()'
            for key in (target, observer):
                if re.match(r'^[A-Za-z_]\w*$', key or ''):
                    if self._property_refs.get(key, '') in ('', 'ObjectReference'):
                        self._property_refs[key] = 'Actor'
            return f'(({target}.IsDetectedBy({observer}) as Int) * 3)'

        # IsActorDetected takes no argument — "am I detected by ANYONE".  Skyrim
        # only offers IsDetectedBy(specificActor), so there is nothing to call.
        # Emitting IsDetectedBy with the default player arg produced
        # `Game.GetPlayer().IsDetectedBy(Game.GetPlayer())` (always true).
        if fname_low == 'isactordetected':
            self._line_comments.append(';NE: IsActorDetected (no Skyrim equivalent)')
            return '0'

        # GetDetected is the OBSERVER's question; IsDetectedBy is the TARGET's.
        # TES4: `<observer>.GetDetected <target>` — "does the observer detect the
        # target" (Morrowind's shared doc for the function: the argument is the
        # "target NPC used to check if the SOURCE actor can detect them").
        # Skyrim: `<target>.IsDetectedBy(<observer>)` — vanilla Actor.psc reads
        # "returns if THIS actor is detected by the other one".
        # So receiver and argument must SWAP.  Mapping them positionally made
        # every call ask the mirror-image question: CharGenQuest's
        # `GlenroyRef.getdetected player` (has Glenroy spotted the player, which
        # advances the Ambush-B stage) became "has the player spotted Glenroy",
        # true the moment the player looks down the corridor.
        if fname_low == 'getdetected':
            observer = self._resolve_self_ref(ref_name, extends, actor_func=True)
            arg = args_str.strip() if args_str else ''
            target = self._convert_expression(arg, extends) if arg else 'Game.GetPlayer()'
            for key in (target, observer):
                if re.match(r'^[A-Za-z_]\w*$', key or ''):
                    if self._property_refs.get(key, '') in ('', 'ObjectReference'):
                        self._property_refs[key] = 'Actor'
            return f'{target}.IsDetectedBy({observer})'

        # SetActorFullName → no-op (SKSE required for SetDisplayName)
        if fname_low == 'setactorfullname':
            self._line_comments.append(';NE: SetActorFullName')
            return '0'
        # SetName is the same capability as SetDisplayName (both rename a form)
        # and neither exists in vanilla Papyrus — Form.psc has no name setter.
        if fname_low in ('setdisplayname', 'setname'):
            self._line_comments.append(f';NE: {func_name}')
            return '0'

        # SetCellFullName no-op
        if fname_low in ('setcellfullname', 'setcellownership'):
            self._line_comments.append(f';NE: {func_name}')
            return '0'

        # SetCombatStyle → no-op (managed by CK/race)
        if fname_low == 'setcombatstyle':
            self._line_comments.append(';NE: SetCombatStyle')
            return '0'

        # ForceFlee → StartCombat avoidance (approximate)
        if fname_low == 'forceflee':
            self._line_comments.append(';NE: ForceFlee')
            return '0'

        # SetActorsAI → no-op
        if fname_low == 'setactorsai':
            self._line_comments.append(';NE: SetActorsAI')
            return '0'

        # GetDayOfWeek → GameDaysPassed % 7
        if fname_low == 'getdayofweek':
            self._property_refs['GameDaysPassed'] = 'GlobalVariable'
            return '(GameDaysPassed.GetValueInt() % 7)'

        # IsPlayerSleeping
        if fname_low == 'isplayersleeping':
            if getattr(self, '_in_sleep_menumode', False):
                return 'TES4_PCSleeping'
            return 'Game.GetPlayer().GetSleepState()'

        # GetIsPlayableRace
        if fname_low == 'getisplayablerace':
            return 'true'

        # DeleteFullActorCopy
        if fname_low == 'deletefullactorcopy':
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.Delete()'

        # ResetInterior → cell.Reset()
        if fname_low == 'resetinterior':
            if args_str and args_str.strip():
                cell_name = _safe_property_name(args_str.strip().split()[0])
                self._property_refs[cell_name] = 'Cell'
                return f'{cell_name}.Reset()'
            ref = self._resolve_self_ref(ref_name, extends)
            return f'{ref}.Reset()'

        # IsPCRace → Game.GetPlayer().GetRace() == arg
        if fname_low in ('ispcrace', 'getpcisrace'):
            arg = self._convert_expression(args_str.strip(), extends) if args_str else 'None'
            if args_str and args_str.strip():
                self._property_refs[args_str.strip()] = 'Race'
            return f'Game.GetPlayer().GetRace() == {arg}'

        # IsSwimming → no vanilla equivalent, approximate with submerged check
        if fname_low == 'isswimming':
            self._line_comments.append(';NE: IsSwimming')
            return '0'

        # GetTalkedToPC
        if fname_low in ('gettalkedtopc', 'gettalkedtopcp'):
            self._line_comments.append(';NE: GetTalkedToPC')
            return '0'

        # SetItemValue → no-op
        if fname_low == 'setitemvalue':
            self._line_comments.append(';NE: SetItemValue')
            return '0'

        # SetLevel → no-op
        if fname_low == 'setlevel':
            self._line_comments.append(';NE: SetLevel')
            return '0'

        # IsPCAMurderer / GetPCIsMurderer.  Must match the bare-read branch in
        # _convert_expression exactly (these take no arguments, so that branch
        # is the one that actually fires).  `> 0` was R4-1's *Attack* test —
        # any violent bounty at all — which would have made the player a
        # "murderer" for a bar brawl; murder is the 1000-gold band.
        if fname_low in ('ispcamurderer', 'ispcanmurderer', 'getpcismurderer'):
            self._property_refs['TES4CyrodiilCrimeFaction'] = 'Faction'
            return (f'(TES4CyrodiilCrimeFaction.GetCrimeGoldViolent() '
                    f'>= {TES4_MURDER_BOUNTY})')

        # SetForceSneaking
        if fname_low in ('setforcesneak',):
            self._line_comments.append(';NE: SetForceSneak')
            return '0'

        # Expel → faction.SetPlayerExpelled(true)
        if fname_low == 'expel':
            arg = self._convert_expression(args_str.strip(), extends) if args_str else 'None'
            if args_str and args_str.strip():
                self._property_refs[args_str.strip()] = 'Faction'
            return f'{arg}.SetPlayerExpelled(true)'

        # SetDoorDefaultOpen → SetOpen.  The argument is a BOOLEAN, not a flag
        # to be ignored: per UESP's function table (opcode 0x10D8, 1 Integer)
        # "a value of 1 will make the door open by default", so 0 CLOSES it.
        # Hardcoding SetOpen(true) inverted the `0` form — MQ16's endgame
        # `ICPalaceElderCouncilMainDoor.SetDoorDefaultOpen 0`, the line whose own
        # comment reads "close Elder Council door", flung it open instead.
        # `OpenDoor` takes no argument and always opens.
        if fname_low == 'setdoordefaultopen':
            ref = self._resolve_self_ref(ref_name, extends)
            arg = args_str.strip().rstrip(',').strip() if args_str else '1'
            opened = 'false' if arg in ('0', '0.0', 'false') else 'true'
            return f'{ref}.SetOpen({opened})'
        if fname_low == 'opendoor':
            ref = self._resolve_self_ref(ref_name, extends)
            return f'{ref}.SetOpen(true)'
        if fname_low == 'closedoor':
            ref = self._resolve_self_ref(ref_name, extends)
            return f'{ref}.SetOpen(false)'

        # SetScale / SetSize
        if fname_low in ('setsize',):
            arg = self._convert_expression(args_str.strip(), extends) if args_str else '1.0'
            ref = self._resolve_self_ref(ref_name, extends)
            return f'{ref}.SetScale({arg})'
        if fname_low in ('getsize',):
            ref = self._resolve_self_ref(ref_name, extends)
            return f'{ref}.GetScale()'

        # Rotate → no-op
        if fname_low == 'rotate':
            self._line_comments.append(';NE: Rotate')
            return '0'

        # SetRigidBodyMass → no-op
        if fname_low == 'setrigidbodymass':
            self._line_comments.append(';NE: SetRigidBodyMass')
            return '0'

        # ResetFallDamageTimer (OBSE) cleared the accumulated fall distance so
        # the next landing did no damage.  Skyrim has the console command
        # (opcode 4404) but exposes no Papyrus binding for it, so the faithful
        # substitute is the GMST the fall-damage formula actually reads:
        #
        #   damage = ((height - fJumpFallHeightMin) * fJumpFallHeightMult)
        #            ^ fJumpFallHeightExponent
        #
        # (Skyrim:Damage, verified against the GMSTs in Skyrim.esm —
        # fJumpFallHeightMin defaults to 600.)  Pushing the threshold beyond
        # any reachable fall makes the landing survivable, which is the whole
        # observable behaviour of the OBSE call.  The scripts that use it
        # (Icarian Flight and friends) call it every update while an effect
        # runs and stop when the effect ends, so the raise is scoped the same
        # way — TES4Polyfill restores the original on release.
        if fname_low == 'resetfalldamagetimer':
            self._suppressed_fall_damage = True
            # OnUpdate has no actor parameter, so the polyfill's None default
            # (the player) covers the common case; a handler that DOES name an
            # actor targets that one.
            return (f'TES4Polyfill.SuppressFallDamage('
                    f'{self._current_event_actor_param()})')

        # AddTopic on a GATED topic opens that topic's unlock gate.
        #
        # Skyrim has no AddTopic, so the visibility model is re-expressed as one
        # `TES4Unlock_<topic>` global per explicitly-added topic (see
        # tes5_import/dialog_unlocks.py). INFO and quest-stage fragments already
        # emit the SetValue; a script AddTopic is the THIRD reveal route and
        # reveals the topic exactly the same way, so it emits the same call.
        #
        # Load-bearing rather than cosmetic: TGReadWantedPoster and
        # TG00MysteriousNoteScript are how the player first learns of the Gray
        # Fox, MS45DarMaDiary is finding Dar Ma's diary, DAMephalaUlfgarScript
        # is Ulfgar's death. Dropped, each of those reveals waited on some later
        # quest stage or unrelated line instead.
        #
        # An UNGATED topic (never explicitly added anywhere, or bark-revealed —
        # both deliberately ungated by the plan) has no global to set and is
        # already visible, so it falls through to the no-op below.
        if fname_low == 'addtopic':
            topic_arg = (args_str or '').strip().strip(',').split()
            gname = None
            if topic_arg:
                gname = (self.topic_unlock_globals or {}).get(
                    topic_arg[0].strip().strip('"').lower())
            if gname:
                self._property_refs.setdefault(gname, 'GlobalVariable')
                return f'{gname}.SetValue(1)'
            self._line_comments.append(f';NE: {func_name} (topic not gated)')
            return '0'

        # Comprehensive no-ops (functions with no meaningful Skyrim equivalent)
        _NO_OP_FUNCS = {
            'removetopic', 'refreshtopiclist', 'setquestobject',
            'setcellpublicflag', 'disablelinkedpathpoints', 'enablelinkedpathpoints',
            'addachievement', 'closecurrentobliviongate', 'forcecloseobliviongate',
            'closeobliviongate', 'setignorefriendlyhits', 'setsceneiscomplex',
            'setnorumors', 'trapupdate', 'setdoordisabletakeoff',
            'setinvestmentgold', 'setpackduration', 'purgecellbuffers', 'pcb',
            'showdialogsubtitles', 'setpublic', 'essentialdeathreload',
            'showenchantment', 'playbink', 'showspellmaking',
            'showbirthsignmenu', 'setallreachable', 'setallvisible',
            'setshowquestitems', 'opencurrentcontainer', 'sendtrespassalarm',
            'setnoavoidance', 'respawnhorse', 'offerhorse', 'getisplayerbirthsign',
            'attachashpile', 'menumode', 'istimepassing',
            'getisalerted', 'getcrimeknown', 'isidleplaying',
            'iscurrentfurnitureref', 'iscurrentfurnitureobj',
            'getbookread', 'isonguard', 'isindangerouswater',
            'getstartingpos',
            'getcurrentaiprocedure', 'getcurrentaipackage', 'getcurrentpackage',
            'getiscurrentpackage', 'hasvariable', 'hasbeenpickedup',
            'ispcanmurderer', 'gettalkedtopc', 'gettalkedtopcp',
            'setclass', 'setcellfullname', 'modamountsoldstolen',
        }
        if fname_low in _NO_OP_FUNCS:
            self._line_comments.append(f';NE: {func_name}')
            return '0'

        # Say: ref.Say topic [force] [headRef] -> ref.Say(topic)
        # SayTo: ref.SayTo target topic [force] -> ref.Say(topic)
        if fname_low in ('say', 'sayto', 'saycustom'):
            if args_str and ',' in args_str:
                pparts = [p.strip() for p in args_str.split(',')]
            else:
                pparts = args_str.split() if args_str else []
            if fname_low == 'sayto' and len(pparts) >= 2:
                # SayTo target topic [force] -> first arg is target, second is topic
                # If topic part has a trailing number (force flag), strip it
                topic_str = pparts[1].strip().split()[0] if pparts[1].strip() else 'None'
                topic = self._convert_expression(topic_str, extends)
                self._mark_topic_property(topic_str)
            else:
                topic = self._convert_expression(pparts[0], extends) if pparts else 'None'
                if pparts:
                    self._mark_topic_property(pparts[0].strip())
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.Say({topic})'

        # Functions whose Papyrus equivalent takes fewer args - drop the extra
        _DROP_ARGS_FUNCS = {'addscriptpackage', 'removescriptpackage', 'stopcombat', 'resurrect'}
        if fname_low in _DROP_ARGS_FUNCS:
            args_str = ''

        # PushActorAway: ObjectReference.PushActorAway(Actor, force).  The
        # pushed target must be Actor-typed; promote or cast as needed.
        if fname_low == 'pushactoraway':
            parts = [p.strip() for p in
                     (args_str.replace(',', ' ').split() if args_str else [])
                     if p.strip()]
            ref = self._resolve_objref_ref(ref_name, extends)
            if parts:
                target = self._convert_expression(parts[0], extends)
                vtype = self._var_types.get(target.lower(), '')
                ptype = self._property_refs.get(target, '')
                if 'ObjectReference' in (vtype, ptype):
                    target = f'({target} as Actor)'
                elif not vtype and not ptype and re.match(r'^\w+$', target):
                    self._property_refs[target] = 'Actor'
            else:
                target = 'Game.GetPlayer()'
            force = self._convert_expression(parts[1], extends) if len(parts) > 1 else '1.0'
            return f'{ref}.PushActorAway({target}, {force})'

        # SetFactionReaction/ModFactionReaction: TES4 `setfactionreaction f1 f2 val`
        # where val is a -100..+100 DISPOSITION modifier.
        #
        # This must NOT become `f1.SetReaction(f2, val)`.  Faction.SetReaction
        # writes the XNAM 'Modifier' field, which Skyrim no longer reads: a
        # census of Skyrim.esm's 1,036 XNAM relations found 1,035 with
        # Modifier == 0 — the engine gates combat purely on the separate
        # 'Group Combat Reaction' ENUM (xEdit wbFactionRelations: 0=Neutral,
        # 1=Enemy, 2=Ally, 3=Friend), which vanilla exercises across all four
        # values (348/316/302/69).  So every converted `setfactionreaction
        # ... -100` wrote a dead field and left the factions NEUTRAL.
        #
        # The natives that DO write that enum are SetEnemy/SetAlly (both
        # present in the SSE binary alongside SetReaction).  Their bool
        # arguments pick the softer tier of each pair:
        #   SetEnemy(other, selfNeutralToOther, otherNeutralToSelf)  → Enemy/Neutral
        #   SetAlly (other, selfFriendToOther,  otherFriendToSelf)   → Ally/Friend
        # TES4's call is ONE-WAY (f1's feelings about f2), so only the first
        # bool is driven and the second is left false, matching the vanilla
        # asymmetric-relation pattern.
        #
        # This is what broke CharacterGen: stage 23 raises the Mythic Dawn vs
        # Blades/Emperor hostility with setfactionreaction, and because the
        # write was inert the assassins never turned on the Blades — leaving
        # the player as the only valid target in the room.
        if fname_low in ('setfactionreaction', 'modfactionreaction'):
            # TES4 accepts any mix of commas and spaces between the three args
            parts = [p.strip() for p in
                     (args_str.replace(',', ' ').split() if args_str else [])
                     if p.strip()]
            if len(parts) >= 3:
                f1 = self._convert_expression(parts[0], extends)
                f2 = self._convert_expression(parts[1], extends)
                self._property_refs[parts[0].strip()] = 'Faction'
                self._property_refs[parts[1].strip()] = 'Faction'
                call = self._faction_reaction_call(f1, f2, parts[2],
                                                   is_mod=(fname_low == 'modfactionreaction'),
                                                   extends=extends)
                if call is not None:
                    return call
                # Non-literal amount: bucket at runtime so a scripted variable
                # still lands on a real enum tier instead of a dead modifier.
                val = self._convert_expression(parts[2], extends)
                return (f'if ({val}) < 0\n'
                        f'  {f1}.SetEnemy({f2}, false, false)\n'
                        f'else\n'
                        f'  {f1}.SetAlly({f2}, true, true)\n'
                        f'endif')
            # Fallback: not enough args
            return f';TODO: {func_name} {args_str}  ;needs faction1.SetEnemy/SetAlly(faction2)'

        # GetGameSetting/getgs: arg is GMST name → quoted string
        if fname_low in ('getgamesetting', 'getgs'):
            setting = args_str.strip().strip('"') if args_str else 'fUnknown'
            # A setting this converter WRITES through an actor value must also
            # be READ through it, or the save/restore pattern these scripts use
            # ("remember the old value, set a new one, put it back") reads the
            # untouched global and restores a number the write never changed.
            av = self._GMST_TO_ACTOR_VALUE.get(setting.lower())
            if av:
                target = self._actor_target_for_gamesetting(extends)
                return f'{target}.GetActorValue("{av}")'
            # Use Int/Float/String variant based on naming convention (i=int, f=float, s=string)
            if setting.startswith('i'):
                return f'Game.GetGameSettingInt("{setting}")'
            elif setting.startswith('s'):
                return f'Game.GetGameSettingString("{setting}")'
            return f'Game.GetGameSettingFloat("{setting}")'

        # SetNumericGameSetting / SetGameSetting (OBSE): write a GMST at
        # runtime.  SKSE's Game.SetGameSettingFloat is the literal counterpart,
        # but it does NOT compile against the vanilla headers this pipeline
        # builds with (verified: "undefined function SetGameSettingFloat",
        # while the *getter* resolves), and requiring SKSE to build is not an
        # option.  So the settings that have a per-actor ACTOR VALUE equivalent
        # go through Actor.ModActorValue — a vanilla native that produces the
        # same observable change on the player, scoped to the actor instead of
        # the whole game, which is what these scripts actually want.
        #
        # Anything without an actor-value equivalent keeps a visible marker
        # rather than a call that silently does nothing.
        if fname_low in ('setnumericgamesetting', 'setgamesetting',
                         'setnumericgamesettingfloat'):
            # TES4 accepts any mix of commas and spaces between the two args.
            parts = [p.strip() for p in
                     (args_str.replace(',', ' ').split(None, 1) if args_str else [])
                     if p.strip()]
            if len(parts) >= 2:
                setting = parts[0].strip().strip('"')
                value = self._convert_expression(parts[1].strip().lstrip(','),
                                                 extends)
                return self._gamesetting_write(setting, value, extends)
            return f';TODO: {func_name} {args_str}  ;needs a setting name and value'

        # GetDeadCount: TES4 counts how many actors of a BASE type are dead.
        # Skyrim has the SAME function natively — ActorBase.GetDeadCount(),
        # documented in ActorBase.psc as "Gets the number of actors of this type
        # that have been killed".  The operand is a base form, so it binds as an
        # ActorBase and the call converts exactly.
        #
        # This previously emitted a literal `0` on the belief that no equivalent
        # existed, which silently disabled 152 quest gates across Nehrim (126 of
        # them plain "is at least one dead" checks like `GetDeadCount X == 1`,
        # which became `0 == 1`).
        if fname_low == 'getdeadcount':
            if args_str:
                name = args_str.strip().rstrip(',').strip()
                target = self._actor_base_property(name, extends)
                return f'{target}.GetDeadCount()'
            if ref_name:
                ref = self._convert_ref(ref_name, extends)
                if re.match(r'^\w+$', ref):
                    ref = self._actor_base_property(ref_name, extends)
                    return f'{ref}.GetDeadCount()'
                return f'({ref} as Actor).GetActorBase().GetDeadCount()'
            # A bare 0, NOT a trailing `;TODO` comment: this is an operand and
            # gets embedded mid-expression (`getdeadcount X + 3`), where a `;`
            # would comment out the rest of the line.
            return '0'

        # PositionWorld x, y, z, angleZ, worldspace — teleport to absolute world
        # coordinates.  Papyrus splits this into SetPosition + SetAngle (both on
        # ObjectReference); there is no worldspace parameter, so that operand is
        # dropped.  Emitted verbatim before, it was an undefined function and
        # every mount-recall in TeleportRueckkehr failed to compile.
        if fname_low in ('positionworld', 'positioncell'):
            normalized = args_str.replace(',', ' ') if args_str else ''
            parts = [p for p in normalized.split() if p]
            ref = self._resolve_objref_ref(ref_name, extends)
            if len(parts) >= 3:
                x, y, z = (self._convert_expression(p, extends)
                           for p in parts[:3])
                out = f'{ref}.SetPosition({x}, {y}, {z})'
                if len(parts) >= 4:
                    ang = self._convert_expression(parts[3], extends)
                    out += f'\n  {ref}.SetAngle(0.0, 0.0, {ang})'
                return out
            return f'; {func_name} {args_str or ""}  ;could not parse'

        # SkipAnim: TES4 jumps an animating object straight to its end state.
        # Papyrus has no per-frame skip; the closest faithful effect is to let
        # the animation finish instantly, which PlayAnimation to the end state
        # cannot express.  There is genuinely no equivalent, so no-op it rather
        # than emit an undefined call that fails the whole script.
        if fname_low == 'skipanim':
            return f';NE: SkipAnim  ;no Papyrus equivalent'

        # GetPackageTarget (OBSE): the current package's target reference.
        # Skyrim exposes no such read.  Comparisons against it (`X.getPackageTarget
        # == player`) previously emitted a property access that did not exist.
        # SetNumericINISetting / GetNumericINISetting (OBSE): write/read a game
        # INI key at runtime.  Papyrus has no INI access at all, so these become
        # no-ops.  Nehrim uses them only to toggle the vanilla autosave-on-wait
        # and on-travel settings, which its own autosave quest re-implements.
        if fname_low == 'setnumericinisetting':
            return f';NE: {func_name} {args_str or ""}  ;no Papyrus INI access'
        if fname_low == 'getnumericinisetting':
            self._line_comments.append(
                ';NE: GetNumericINISetting has no Papyrus equivalent (read as 0)')
            return '0'

        # con_Save / Autosave / con_SaveGame: write a save.  Papyrus exposes
        # Game.RequestSave() (a normal save) and Game.RequestAutoSave().  The
        # TES4 argument is a save-slot NAME, which Papyrus does not accept, so it
        # is dropped — the engine picks the slot.
        # (`autosave` itself already maps to Game.RequestAutoSave via FUNCTION_MAP.)
        if fname_low in ('con_save', 'con_savegame', 'savegame'):
            return 'Game.RequestSave()'

        # Every other OBSE `con_*` is a CONSOLE command invoked from script
        # (con_RunMemoryPass, con_ToggleMenus, …).  Papyrus cannot run console
        # commands, so the whole family no-ops.  Matched by PREFIX so a con_*
        # command this file never saw does not fail the build later.
        if fname_low.startswith('con_'):
            return (f';NE: {func_name} {args_str or ""}'
                    f'  ;OBSE console command, no Papyrus equivalent')

        # OBSE reads with no vanilla-Papyrus counterpart at all: raw input
        # bindings (getControl/getAltControl), UI introspection
        # (getMenuHasTrait), and inventory/form queries whose return shape has no
        # Skyrim analogue (getItems is an OBSE array, isPlayable2/
        # getFullGoldValue/getWeaponSkillType are OBSE-only form reads).
        # All are numeric/boolean in context, so 0 keeps the surrounding
        # expression well-typed.  Bare literal — these sit inside conditions and
        # arithmetic, where a trailing comment would eat the rest of the line.
        if fname_low in ('getcontrol', 'getaltcontrol', 'getmousecontrol',
                         'getitems', 'isplayable2', 'isplayable',
                         'getfullgoldvalue', 'getweaponskilltype'):
            self._line_comments.append(
                f';NE: {func_name} has no Papyrus equivalent (read as 0)')
            return '0'

        # OBSE raw-INPUT control: disableKey/enableKey/tapKey/holdKey/playback and
        # the isKeyPressed* readers.  Skyrim has no vanilla input API (it is
        # SKSE-only), so the writers no-op and the readers return 0.  Kept as one
        # family for the same reason as the menu commands above — enumerating them
        # one build at a time is how `disableKey` survived to fail on its own.
        if fname_low in ('disablekey', 'enablekey', 'tapkey', 'holdkey',
                         'releasekey', 'playback', 'playbackalt',
                         'disablecontrol', 'enablecontrol', 'tapcontrol'):
            return (f';NE: {func_name} {args_str or ""}'
                    f'  ;OBSE input command, no Papyrus equivalent')

        # The whole OBSE MENU family (`get/setMenu*Value`, `getMenuHasTrait`, …)
        # reaches into Oblivion's XML UI, which Skyrim does not have in any form.
        # Matched by PATTERN rather than a name list so the *setters* and any
        # variant this file never saw also no-op instead of failing a later build:
        # the getters returned 0 while `setMenuFloatValue` stayed an undefined
        # function, which is exactly the one-at-a-time failure mode to avoid.
        if re.match(r'^(?:get|set)menu\w*$', fname_low):
            if fname_low.startswith('set'):
                return (f';NE: {func_name} {args_str or ""}'
                        f'  ;OBSE menu/UI command, no Papyrus equivalent')
            self._line_comments.append(
                f';NE: {func_name} has no Papyrus equivalent (read as 0)')
            return '0'

        # getObjectType (OBSE): the numeric TES4 form-type code of a reference's
        # base object.  Skyrim's form-type numbering is entirely different and
        # Papyrus has no equivalent read, so comparisons against the TES4 codes
        # could not be honoured even if it did.  Reads as 0 (a bare literal — it
        # sits inside larger conditions).
        if fname_low in ('getobjecttype', 'gettype'):
            self._line_comments.append(
                f';NE: {func_name} has no Papyrus equivalent (read as 0)')
            return '0'

        # getCrosshairRef (OBSE): the reference currently under the crosshair.
        # Vanilla Papyrus has no such read (it is SKSE-only), so this resolves to
        # None.  Callers compare it against refs, which simply never matches.
        if fname_low in ('getcrosshairref', 'getcrosshairreference'):
            self._line_comments.append(
                ';NE: getCrosshairRef has no Papyrus equivalent (read as None)')
            return 'None'

        # IsInAir: TES4 "actor is off the ground".  Papyrus's nearest native read
        # is Actor.IsFlying().  Cast to Int for the usual `== 0/1` comparisons.
        if fname_low == 'isinair':
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            if ref == 'Self' and extends not in ('Actor',):
                ref = '(Self as Actor)'
            return f'({ref}.IsFlying() as Int)'

        # GetStringGameSetting (OBSE): reads a STRING GMST.  Papyrus exposes only
        # the numeric getters (Game.GetGameSettingFloat/Int), so there is no
        # equivalent — return an empty string, which keeps the concatenation that
        # consumes it well-typed.
        if fname_low == 'getstringgamesetting':
            self._line_comments.append(
                ';NE: GetStringGameSetting has no Papyrus equivalent (read as "")')
            return '""'

        # isKeyPressed / isKeyPressed2 / isControlPressed (OBSE): raw input
        # polling.  Papyrus has no key-state read outside SKSE, so these read as
        # "not pressed".  A BARE 0 — the call sits inside a larger condition
        # (`if isKeyPressed2 attackKey || isKeyPressed2 attackButton`) where a
        # trailing comment would swallow the rest of the expression.
        if fname_low in ('iskeypressed', 'iskeypressed2', 'iskeypressed3',
                         'iscontrolpressed', 'isbuttonpressed'):
            self._line_comments.append(
                f';NE: {func_name} has no Papyrus equivalent (read as 0)')
            return '0'

        # Bare operand — see the note above about trailing comments.
        if fname_low == 'getpackagetarget':
            self._line_comments.append(
                ';NE: getPackageTarget has no Papyrus equivalent (read as None)')
            return 'None'

        # UnlockAchievement (Nehrim's own Steam-achievement stub, 100 calls).
        # Skyrim has no scriptable achievement API; drop it to a no-op comment
        # so the surrounding quest logic still compiles and runs.
        if fname_low == 'unlockachievement':
            name = args_str.strip() if args_str else ''
            return f';NE: UnlockAchievement {name}  ;no Papyrus equivalent'

        # GetGameRestarted / IsPlayerMovingIntoNewSpace (OBSE): both report a
        # one-off engine transition Skyrim does not expose.  False is the safe
        # reading — the guarded body is a re-initialisation that is allowed to be
        # skipped, and the alternative (an undefined identifier) kills the script.
        # Return a BARE literal: this is an operand and gets embedded inside a
        # larger condition, where a trailing `;` comment would swallow the rest
        # of the expression (`If True  ;(False ;NE: ...)`).
        if fname_low in ('getgamerestarted', 'isplayermovingintonewspace'):
            self._line_comments.append(
                f';NE: {func_name} has no Papyrus equivalent (read as 0)')
            return '0'

        # ForceFlee / Flee: "Forces a actor to flee" (UESP function index 407,
        # both params optional and unused by Nehrim).  Skyrim has no Flee call —
        # fleeing is driven by the Confidence actor value, so dropping the actor
        # to Cowardly (0) and re-evaluating its package makes the engine itself
        # break off combat.  That is the engine's own mechanism rather than a
        # Papyrus approximation of running away.
        if fname_low in ('flee', 'forceflee'):
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            if ref == 'Self' and extends not in ('Actor',):
                ref = '(Self as Actor)'
            return (f'{ref}.SetActorValue("Confidence", 0)\n'
                    f'  {ref}.EvaluatePackage()')

        # GetAttacked: TES4 zero-arg read of "has this actor been attacked".
        # Skyrim's nearest native read is IsAlarmed() (the actor has registered
        # a hostile act).  Cast to Int because TES4 callers compare it to 0/1.
        if fname_low == 'getattacked':
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            if ref == 'Self' and extends not in ('Actor',):
                ref = '(Self as Actor)'
            return f'({ref}.IsAlarmed() as Int)'

        # ResetHealth: TES4 ResetHealth -> RestoreActorValue("Health", 9999)
        if fname_low == 'resethealth':
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.RestoreActorValue("Health", 9999)'

        # EvaluatePackage/EVP/AddScriptPackage/RemoveScriptPackage/StopWaiting:
        # Skyrim version takes no args (drop TES4 package arg)
        if fname_low in ('evaluatepackage', 'evp', 'addscriptpackage', 'removescriptpackage', 'stopwaiting'):
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.EvaluatePackage()'

        # ClearLookAt / StopLook: Skyrim version takes no args (drop TES4 target arg)
        if fname_low in ('clearlookat', 'stoplook', 'stoplooking'):
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.ClearLookAt()'

        # GetAmountSoldStolen: gold fenced, paired with ModAmountSoldStolen above.
        if fname_low == 'getamountsoldstolen':
            self._property_refs['TES4GoldFenced'] = 'GlobalVariable'
            return 'TES4GoldFenced.GetValue()'

        # Player-controls state.  Skyrim exposes the two WRITERS as natives
        # (Game.DisablePlayerControls/EnablePlayerControls) but no getter, so
        # the writers also shadow the state into a synthesized global and the
        # read returns that.  Flattening the read to 0 was actively wrong
        # rather than merely inert: MG18Script polls it three times to sequence
        # Mannimarco's confrontation, and a constant 0 made the force-greet
        # branch (`== 1`) permanently false while the combat branch (`== 0`)
        # fired immediately — so Mannimarco never spoke and attacked at once.
        if fname_low in ('getplayercontrolsdisabled',
                         'getplayercontrolsdisabled_'):
            self._property_refs['TES4ControlsDisabled'] = 'GlobalVariable'
            return 'TES4ControlsDisabled.GetValue()'
        # The two writers stay single-expression (a trailing source comment is
        # appended to whatever they return, so a second line here would be
        # orphaned behind it); the shadow write is spliced in as its own line
        # by _shadow_controls_writes during post-processing.
        if fname_low in ('disableplayercontrols', 'enableplayercontrols'):
            self._property_refs['TES4ControlsDisabled'] = 'GlobalVariable'
            verb = 'Disable' if fname_low.startswith('disable') else 'Enable'
            return f'Game.{verb}PlayerControls()'

        # GetPCMiscStat: genuine stat query, named by its argument.
        if fname_low == 'getpcmiscstat':
            stat = args_str.strip().strip('"') if args_str else 'Items Stolen'
            return f'Game.QueryStat("{stat}")'

        # GetEquippedItemType: Skyrim requires hand param (0=left, 1=right)
        if fname_low in ('getweaponanimtype', 'getequippeditemtype'):
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.GetEquippedItemType(1)'

        # IsActorUsingATorch: check if left hand has torch equipped (type 11)
        if fname_low == 'isactorusingatorch':
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'({ref}.GetEquippedItemType(0) == 11)'

        # IsRidingHorse: Actor.IsOnMount() in Skyrim
        if fname_low == 'isridinghorse':
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.IsOnMount()'

        # GetRace: ref.GetRace() -> ref.GetRace()
        if fname_low == 'getrace':
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.GetRace()'

        # Expel: ref.Expel(faction) -> faction.SetPlayerExpelled(true).
        # Must write the same flag IsPlayerExpelled() reads; the old rank -1
        # write was invisible to every expelled test.
        if fname_low == 'expel':
            arg = self._convert_expression(args_str.strip(), extends) if args_str else 'None'
            if args_str:
                self._property_refs[args_str.strip()] = 'Faction'
            return f'{arg}.SetPlayerExpelled(true)'

        # IsExpelled/IsPCExpelled/GetPCExpelled — see the GetPCExpelled handler
        # above; Faction.IsPlayerExpelled() is the exact native.
        if fname_low in ('isexpelled', 'ispcexpelled', 'getpcexpelled'):
            arg = self._convert_expression(args_str.strip(), extends) if args_str else 'None'
            if args_str:
                self._property_refs[args_str.strip()] = 'Faction'
            return f'{arg}.IsPlayerExpelled()'

        # IsInInterior: ref.IsInInterior -> ref.GetParentCell().IsInterior()
        if fname_low == 'isininterior':
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.GetParentCell().IsInterior()'

        # Unlock: ref.Unlock -> ref.Lock(false).  Lock() is declared on
        # ObjectReference and its TES4 targets are doors/containers, so this
        # must NOT promote the property to Actor: doing so made CGAmbushBDoor /
        # CGDungeon02Exit / CGPrisonSecretWallRef (all REFRs) declare as
        # `Actor Property`, which the VM refuses to bind ("cannot be bound
        # because <fid> is not the right type") — the property comes back None
        # and the door never unlocks.
        if fname_low == 'unlock':
            ref = self._resolve_objref_ref(ref_name, extends)
            return f'{ref}.Lock(false)'

        # IsDoor / IsContainer / IsWeapon / ... — OBSE form-TYPE tests.  Vanilla
        # Papyrus cannot ask a form its type (Form.GetType is SKSE), so these
        # answer 0.  Handled here rather than by the blanket neutraliser so the
        # DOTTED spelling (`crosshairRef.IsDoor == 1`) is covered too: that path
        # only recognises a name as a function when it is a FUNCTION_MAP key,
        # and otherwise emitted a raw member access that failed the compile.
        if fname_low in _FORM_TYPE_TESTS:
            self._line_comments.append(
                f';NE: {func_name} — Papyrus cannot read a form\'s type')
            return '0'

        # GetModIndex "<plugin>" — the plugin's slot in load order, which
        # Papyrus cannot read.  Answers 1 (loaded, and early) for the same
        # reason FileExists answers present: every caller tests it to flag a
        # MIS-ordered install, so 0 or a large value would fire that error
        # branch on a conversion where load order is no longer the mod author's
        # problem.
        if fname_low == 'getmodindex':
            self._line_comments.append(
                ';NE: GetModIndex — Papyrus cannot read load order')
            return '1'

        # FileExists "<path>" — OBSE probes a loose file on disk, which Papyrus
        # cannot see at all.  It answers PRESENT, not absent.
        #
        # Polarity is the whole point.  Every TES4 caller uses it as an
        # installation check (`if FileExists "Data\Morrowind_ob - Meshes.bsa"
        # == 0 / "ERROR: ... is missing"`), and the paths named are Oblivion-side
        # artifacts — BSAs, ini files — that do not exist after conversion BY
        # DESIGN: the pipeline converts and deploys those assets itself.
        # Answering 0 therefore fired every "missing file" branch at once and
        # greeted the player with a bogus installation-error box on load.
        # Answering 1 states what is actually true of a converted install: the
        # content the check is looking for is there, just not under a TES4 path.
        if fname_low == 'fileexists':
            self._line_comments.append(
                ';NE: FileExists — converted assets are deployed by the '
                'pipeline, not under the TES4 path')
            return '1'

        # SetCanFastTravelFromWorld <worldspace> <flag> — Skyrim's fast-travel
        # toggle is global, so the worldspace operand has nowhere to go.  Keep
        # the flag, which is the part that actually changes behaviour, and note
        # the widened scope rather than dropping the call entirely.
        if fname_low == 'setcanfasttravelfromworld':
            parts = [p.strip() for p in args_str.replace(',', ' ').split()
                     if p.strip()] if args_str else []
            flag = (self._convert_expression(parts[-1], extends)
                    if len(parts) > 1 else 'true')
            if flag in ('0', '0.0'):
                flag = 'false'
            elif flag in ('1', '1.0'):
                flag = 'true'
            self._line_comments.append(
                ';NE: Skyrim fast travel is global, not per-worldspace')
            return f'Game.EnableFastTravel({flag})'

        # Dispel <magic item> — Skyrim's Actor.DispelSpell takes a Spell, and an
        # ENCH does not convert to one (enchantments stay ENCH; see
        # docs/magic_conversion_plan.md).  Emitting the call anyway is a hard
        # compile error that takes the whole script down, so an ENCH operand
        # neutralises instead.  A SPEL operand converts normally.
        if fname_low in ('dispel', 'dispelspell') and args_str.strip():
            arg_raw = args_str.strip().split(',')[0].strip()
            arg_fid = (self.xref.edid_to_formid.get(arg_raw.lower(), '')
                       if self.xref else '')
            arg_rtype = (self.xref.record_type.get(arg_fid, '')
                         if arg_fid else '')
            if arg_rtype == 'ENCH':
                self._line_comments.append(
                    f';NE: Dispel {arg_raw} names an enchantment, which has no '
                    f'Skyrim Spell to dispel')
                return '0'
            arg = self._convert_expression(arg_raw, extends)
            self._property_refs[arg_raw] = 'Spell'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.DispelSpell({arg})'

        # Cast: TES4 ref.cast spell [target] -> Papyrus spell.Cast(ref, target)
        # Cast is a method on Spell in Papyrus, not on ObjectReference
        if fname_low == 'cast':
            parts = args_str.split(',') if args_str and ',' in args_str else (args_str.split() if args_str else [])
            parts = [p.strip() for p in parts if p.strip()]
            spell = self._convert_expression(parts[0], extends) if parts else 'None'
            if parts:
                # Only claim the Spell typing when the name is still free.  A
                # variable that already resolved to something wider — a `ref`
                # read out of another script's variable table lands as `Form` —
                # keeps its declaration, and the cast goes on the call instead.
                _cur = (self._property_refs.get(spell, '')
                        or self._var_types.get(spell.lower(), ''))
                if _cur in ('', 'ObjectReference'):
                    self._property_refs[parts[0].strip()] = 'Spell'
                elif _cur != 'Spell':
                    spell = f'({spell} as Spell)'
            source = self._resolve_self_ref(ref_name, extends, actor_func=True)
            if len(parts) > 1:
                target = self._convert_expression(parts[1], extends)
                return f'{spell}.Cast({source}, {target})'
            return f'{spell}.Cast({source})'

        # GetIsID: ref.GetIsID baseForm -> ref.GetBaseObject() == baseForm
        #
        # TES4's GetIsID asks "is this reference's BASE record that one", and the
        # operand can be ANY base type — the SE38 oddities are MISC items, not
        # actors.  Emitting `(ref as Actor).GetActorBase()` was wrong twice: on a
        # non-actor script `Self as Actor` is a cast the CK rejects outright, and
        # typing the operand ActorBase mis-binds every non-actor base.
        # GetBaseObject() is declared on ObjectReference (so it needs no cast, and
        # still works for actors, since Actor extends ObjectReference) and returns
        # a Form, which compares against every base type.
        if fname_low == 'getisid':
            arg = self._convert_expression(args_str.strip(), extends) if args_str else 'None'
            if args_str:
                self._bind_base_form_property(args_str.strip())
            ref = self._resolve_objref_ref(ref_name, extends)
            return f'{ref}.GetBaseObject() == {arg}'

        # GetIsRace: ref.GetIsRace RaceRef -> ref.GetRace() == raceRef
        if fname_low in ('getisrace', 'getpcisrace'):
            arg = self._convert_expression(args_str.strip(), extends) if args_str else 'None'
            if args_str:
                self._property_refs[args_str.strip()] = 'Race'
            if fname_low == 'getpcisrace':
                return f'Game.GetPlayer().GetRace() == {arg}'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.GetRace() == {arg}'

        # GetIsClass / GetPCIsClass: the CLAS argument is a Class form, and
        # Skyrim reads it off the ActorBase, not the reference — Actor has no
        # GetClass().  Left untranslated, `GetPCIsClass CharactergenClass`
        # parsed as a bare name after a name and killed the whole script
        # (Morroblivion's chargen quest script, which the Chargen-and-Transport
        # start menu imports, so the transport NPCs went with it).
        if fname_low in ('getisclass', 'getpcisclass'):
            arg = self._convert_expression(args_str.strip(), extends) if args_str else 'None'
            if args_str:
                self._property_refs[args_str.strip()] = 'Class'
            if fname_low == 'getpcisclass':
                return f'Game.GetPlayer().GetActorBase().GetClass() == {arg}'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'({ref} as Actor).GetActorBase().GetClass() == {arg}'

        # GetIsRef: ref.GetIsRef otherRef -> ref == otherRef
        if fname_low == 'getisref':
            arg = self._convert_expression(args_str.strip(), extends) if args_str else 'None'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref} == {arg}'

        # GetInCell: TES4 matches the argument as an EditorID PREFIX, so
        # `GetInCell Chorrol` is true in all 86 cells named Chorrol* — the whole
        # city, interiors and exteriors.  Oblivion relies on this: 62 CELL
        # records exist only as the named anchor of such a family and contain no
        # refs at all (`FULL=Dummy cell for GetInCell`).  Translating the call as
        # a single equality against that anchor gave a condition the player can
        # never satisfy, silently killing 167 of the 396 GetInCell calls (MQ02's
        # Chorrol/Weynon Priory stage advances among them).  Expand the family
        # into an OR-chain over its member cells instead.
        if fname_low == 'getincell':
            arg = args_str.strip().strip('"') if args_str else 'None'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            # A `Cell` property binds only to an INTERIOR cell, so the family is
            # split: interiors keep the property comparison, exteriors are
            # matched by worldspace + grid coordinates instead.  See
            # CrossRefGraph.split_cell_family.
            interior, exterior = ((self.xref.split_cell_family(arg))
                                  if self.xref else ([], []))
            # Fall back to the literal name when the index knows nothing.
            if not interior and not exterior:
                interior = [arg]
            for cell in interior:
                self._property_refs[cell] = 'Cell'
            if len(interior) == 1 and not exterior:
                return f'{ref}.GetParentCell() == {interior[0]}'
            # Families run to hundreds of cells ("IC" covers 431), far too many
            # to inline at every call site, so emit one helper per family and
            # call it.  The helper evaluates GetParentCell() a single time.
            helper = self._register_cell_family(arg, interior, exterior)
            return f'{helper}({ref})'

        # GetInSameCell: ref.GetInSameCell otherRef -> ref.GetParentCell() == otherRef.GetParentCell()
        if fname_low in ('getinsamecell', 'getinsamecellas'):
            arg = self._convert_expression(args_str.strip(), extends) if args_str else 'Game.GetPlayer()'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.GetParentCell() == {arg}.GetParentCell()'

        # GetIsSex: ref.GetIsSex Male/Female -> ref.GetActorBase().GetSex() == 0/1
        if fname_low == 'getissex':
            arg = args_str.strip().lower() if args_str else 'male'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            sex_val = '1' if 'female' in arg else '0'
            return f'({ref} as Actor).GetActorBase().GetSex() == {sex_val}'

        # PlayGroup: the API depends on WHAT THE TARGET IS, not on whether the
        # call names a reference.
        #  - Animated OBJECTS (activators/doors/statics with a
        #    NiControllerManager): the converted NIF keeps its TES4 sequences
        #    ('Forward', 'Unequip', …), so PlayGroup Forward 0 ->
        #    PlayAnimation("Forward").  Debug.SendAnimationEvent only works on
        #    behavior-graph ACTORS and silently does nothing on an activator
        #    (tripwires never played their break animation, swinging traps
        #    never got kicked).
        #  - ACTORS keep the behavior-graph event mapping: PlayAnimation() on
        #    an actor corrupts its behavior graph
        #    (BShkbAnimationGraph/hkbRagdollDriver crash).
        #
        # Routing EXPLICIT-REF calls to SendAnimationEvent unconditionally was
        # wrong: TES4 aims PlayGroup at animated objects as often as at actors.
        # `CGPrisonSecretWallRef.playgroup forward 1` (CharacterGen's secret
        # door, base ACTI prisonSecretWall01, whose NIF carries the 'Forward'
        # NiControllerSequence) became SendAnimationEvent(..., "moveStart") and
        # did nothing, so Renault threw the switch and the wall never moved —
        # note the SELF-call on the very next line converted correctly, making
        # two identical TES4 statements behave differently.  Resolve the base
        # record instead and only treat real actors as actors.
        if fname_low == 'playgroup':
            parts = args_str.strip().split() if args_str else ['Idle']
            anim_name = parts[0].rstrip(',').strip('"').strip("'") if parts else 'Idle'
            target_is_actor = (extends == 'Actor') if not ref_name else False
            if ref_name:
                sig = ''
                if self.xref:
                    sig = self.xref.get_base_signature(ref_name)
                if sig:
                    target_is_actor = sig in ('NPC_', 'CREA', 'ACHR', 'ACRE')
                else:
                    # Unknown target: keep the behavior-graph event, which is
                    # inert on an object but never corrupts an actor's graph.
                    target_is_actor = True
            if not target_is_actor:
                # NiControllerSequence names in Oblivion NIFs are capitalized
                # ('Forward', 'Backward', 'Unequip', 'Open', 'Close', 'Idle').
                seq = anim_name.capitalize()
                # PlayAnimation is an ObjectReference method, so an explicit
                # ref plays on THAT object, not on Self.
                obj = self._resolve_objref_ref(ref_name, extends) if ref_name \
                    else self._implicit_self(extends)
                # HAVOK RELEASE.  Oblivion holds two families of prop rigid
                # until a script fires: break-apart props (mwallplankbreakaway01's
                # planks, IDCrumbleWall01's bricks) and whole constrained trap
                # islands (ctrapswingmacelong01, ctraplogs01, ctrigtripwire01).
                # Both are authored as keyframed bodies with real mass and
                # `Unyielding = 1` -- the clip only creaks the piece off its
                # mounting, and HAVOK does the visible part once it ends.
                # CTrapLogs01SCRIPT says so in its own header: "On activation
                # havok will turn on and logs will roll".  Skyrim keyframed
                # bodies never yield to gravity, so without a release the planks
                # hang half-broken and the tripwire never snaps.
                #
                # The release is native: SetMotionType(Motion_Dynamic) after the
                # clip has run.  Which objects get it is decided by the MESH, not
                # by the animation group: `_convert_collision` keeps a non-zero
                # mass on a keyframed body for held pieces ONLY, and records that
                # as physics-flag bit 1.  Keying off the group name was wrong --
                # 'forward' is 491 of Oblivion's 850 playgroup calls and is
                # overwhelmingly gates, doors and portcullises that must keep
                # following their clip exactly, yet it is also the tripwire's
                # break group.  The group name cannot separate them; the mesh
                # can.  The release stays inert on anything not held, because
                # every other animated object converts to a mass-0 keyframed
                # body that cannot fall even once it is dynamic.  (Shipping the
                # pieces dynamic in the NIF instead was wrong: they dropped the
                # instant the cell loaded, before the clip ever played -- which
                # is exactly what made swinging traps free-swing on cell entry.)
                held = False
                if self.xref:
                    if ref_name:
                        held = self.xref.needs_havok_release(ref_name)
                    else:
                        held = self.xref.script_owner_needs_havok_release(
                            self._current_script_edid)
                if held:
                    return (f'{obj}.PlayAnimation("{seq}")\n'
                            f'  TES4Polyfill.ReleaseBreakaway({obj})')
                return f'{obj}.PlayAnimation("{seq}")'
            # Map common Oblivion animation groups to Skyrim behavior events
            _anim_map = {
                'forward': 'moveStart', 'backward': 'moveStartBackward',
                'left': 'moveStartStrafeLeft', 'right': 'moveStartStrafeRight',
                'idle': 'IdleForceDefaultState', 'specialidle': 'SpecialIdle',
                'unequip': 'Unequip', 'equip': 'Equip',
                'torchidle': 'IdleForceDefaultState',
                'castself': 'MagicCastSelf', 'casttouch': 'attackStart',
                'casttarget': 'attackStart',
                'jumpstart': 'JumpStandingStart', 'jumpland': 'JumpLand',
                'handstohandsattack': 'attackStart',
            }
            event = _anim_map.get(anim_name.lower(), anim_name)
            # SendAnimationEvent takes an ObjectReference, and TES4 aims
            # PlayGroup at doors and animated statics as often as at actors
            # (CGPrisonSecretWallRef.playgroup backward), so promoting the
            # property to Actor would leave the VM unable to bind a REFR.
            ref = self._resolve_objref_ref(ref_name, extends)
            return f'Debug.SendAnimationEvent({ref}, "{event}")'

        # PickIdle / PlayIdle: -> Debug.SendAnimationEvent(ref, "IdleForceDefaultState")
        if fname_low in ('pickidle', 'playidle'):
            idle_name = args_str.strip() if args_str else 'IdleForceDefaultState'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'Debug.SendAnimationEvent({ref}, "{idle_name}")'

        # SetEssential: TES4's SetEssential takes a BASE id (SetEssential base 1).
        # The property must be typed to match what it is BOUND to (VMAD binds the
        # SCRO FormID, which for a base EditorID is the base record):
        #   - base arg (NPC_/CREA, or unknown) -> ActorBase property, direct
        #     `target.SetEssential(v)`. An Actor-derived-script type here would
        #     be UNBINDABLE (a base is not an Actor) and abort the whole script's
        #     init -> quest never finishes init -> aliases never fill. This was
        #     the FGC01Rats bug: QuillWeave (NPC_ base) was typed as the Actor-
        #     script TES4_FGC01QuillweaveScript.
        #   - placed reference arg (ACHR/ACRE/REFR) -> Actor, via GetActorBase().
        if fname_low == 'setessential':
            normalized = args_str.replace(',', ' ').strip() if args_str else ''
            parts = normalized.split() if normalized else []
            if len(parts) >= 2:
                target = self._convert_expression(parts[0], extends)
                val = 'true' if parts[1].strip() in ('1', 'true') else 'false'
                arg_fid = self.xref.edid_to_formid.get(parts[0].lower(), '') if self.xref else ''
                arg_rtype = self.xref.record_type.get(arg_fid, '') if arg_fid else ''
                if arg_rtype in ('ACHR', 'ACRE', 'REFR'):
                    self._property_refs[target] = 'Actor'
                    return f'({target} as Actor).GetActorBase().SetEssential({val})'
                # Base form (or unresolved): bind as ActorBase and call directly.
                # Force ActorBase even over an attached-script type, since the
                # VMAD binds this to the base and only ActorBase can bind there.
                self._property_refs[target] = 'ActorBase'
                return f'{target}.SetEssential({val})'
            elif ref_name:
                ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
                val = 'true' if args_str and args_str.strip() in ('1', 'true') else 'false'
                return f'({ref} as Actor).GetActorBase().SetEssential({val})'
            return f'; SetEssential {args_str or ""}  ;could not parse'

        # SetOwnership: ref.SetOwnership owner -> ref.SetActorOwner/SetFactionOwner
        if fname_low == 'setownership':
            ref = self._resolve_self_ref(ref_name, extends)
            if args_str:
                arg = self._convert_expression(args_str.strip(), extends)
                arg_low = args_str.strip().lower()
                # Check if arg is a faction
                arg_fid = self.xref.edid_to_formid.get(arg_low, '') if self.xref else ''
                arg_rtype = self.xref.record_type.get(arg_fid, '') if arg_fid else ''
                pref_type = self._property_refs.get(arg, self._property_refs.get(_safe_property_name(args_str.strip()), ''))
                if arg_rtype == 'FACT' or pref_type == 'Faction':
                    return f'{ref}.SetFactionOwner({arg})'
                else:
                    return f'{ref}.SetActorOwner({arg}.GetActorBase())'
            return f'{ref}.SetActorOwner(Game.GetPlayer().GetActorBase())'

        # IsOwner [owner] — the read side of SetOwnership.  Written bare it asks
        # "does the PLAYER own this reference"; with an argument it names the
        # owner to test.  Skyrim splits ownership into an actor owner and a
        # faction owner, so the comparison picks whichever the argument is,
        # exactly as SetOwnership does above.
        if fname_low == 'isowner':
            ref = self._resolve_objref_ref(ref_name, extends)
            if args_str and args_str.strip():
                arg = self._convert_expression(args_str.strip(), extends)
                arg_low = args_str.strip().lower()
                arg_fid = (self.xref.edid_to_formid.get(arg_low, '')
                           if self.xref else '')
                arg_rtype = (self.xref.record_type.get(arg_fid, '')
                             if arg_fid else '')
                pref_type = self._property_refs.get(
                    arg, self._property_refs.get(
                        _safe_property_name(args_str.strip()), ''))
                if arg_rtype == 'FACT' or pref_type == 'Faction':
                    return f'({ref}.GetFactionOwner() == {arg})'
                return f'({ref}.GetActorOwner() == {arg}.GetActorBase())'
            return (f'({ref}.GetActorOwner() == '
                    f'Game.GetPlayer().GetActorBase())')

        # MoveTo: ref.MoveTo target [X Y Z] -> ref.MoveTo(target, X, Y, Z)
        if fname_low in ('moveto', 'movetomarker'):
            normalized = args_str.replace(',', ' ') if args_str else ''
            parts = [p.strip() for p in normalized.split() if p.strip()]
            target = self._convert_expression(parts[0], extends) if parts else 'None'
            # The destination is a PLACED REFERENCE, and nothing else in the
            # script necessarily declares it.  Without registering it here the
            # call emitted a bare identifier that no property backed, and the
            # compiler rejected the whole script — Morroblivion's
            # CATChargenAndTransport dies on `Player.MoveTo CGPlayerStartMarker1`
            # (a typo in the mod: the SCRO table binds only CGPlayerStartMarker,
            # so Oblivion silently no-opped it, but Papyrus will not compile an
            # undefined name).  Register only a plain identifier: an already
            # converted expression (Game.GetPlayer(), a local, a literal) is not
            # a property and must not be declared as one.
            if parts and re.fullmatch(r'\w+', parts[0]) and target == parts[0]:
                self._property_refs.setdefault(parts[0], 'ObjectReference')
            offsets = ', '.join(parts[1:4]) if len(parts) > 1 else ''
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            if offsets:
                return f'{ref}.MoveTo({target}, {offsets})'
            return f'{ref}.MoveTo({target})'

        # PlaceAtMe: ref.PlaceAtMe base [count] [distance] -> ref.PlaceAtMe(base, count)
        if fname_low == 'placeatme':
            # Normalize: replace commas with spaces, then split on whitespace
            normalized = args_str.replace(',', ' ') if args_str else ''
            parts = [p.strip() for p in normalized.split() if p.strip()]
            base = self._convert_expression(parts[0], extends) if parts else 'None'
            count = parts[1] if len(parts) > 1 else '1'
            # PlaceAtMe is on ObjectReference — don't promote type to Actor
            ref = self._resolve_self_ref(ref_name, extends, actor_func=False)
            if ref == 'Self' and extends == 'ActiveMagicEffect':
                ref = 'GetTargetActor()'
            elif ref == 'Self' and extends == 'TopicInfo':
                ref = 'akSpeakerRef'
            return f'{ref}.PlaceAtMe({base}, {count})'

        # CreateFullActorCopy: approximate with PlaceAtMe
        if fname_low == 'createfullactorcopy':
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.PlaceAtMe({ref}.GetActorBase())'

        # WakeUpPC kicks the player OUT OF SLEEP.  It does not move them, change
        # the camera, or play an animation — the old mapping to
        # Game.ForceThirdPerson() did none of the right things.
        #
        # Skyrim genuinely has no equivalent: no native in Game/Debug/Actor/
        # ObjectReference ends an active sleep, and SKSE registers none either
        # (grepped every NativeFunction in references/skse64-master).  Vanilla's
        # closest case, the Dark Brotherhood abduction, does not wake the player
        # with a function — it runs its whole sequence inside OnSleepStart.
        #
        # That is exactly where our converted body already runs: all 5 TES4 call
        # sites sit in a MenuMode block reading isPCSleeping, which this
        # converter routes into OnSleepStart/OnSleepStop.  So the surrounding
        # code the script wanted to run on waking DOES run, at the right moment;
        # only the "cut the sleep short" part has no target.  Emitting a no-op
        # keeps that faithful and visible instead of inventing a side effect the
        # original never had.
        if fname_low == 'wakeuppc':
            self._line_comments.append(
                ';NE: WakeUpPC (no Skyrim equivalent; body runs in OnSleepStart)')
            return '0'

        # IsExpelled: faction arg -> faction rank check
        if fname_low == 'isexpelled':
            arg = self._convert_expression(args_str.strip(), extends) if args_str else 'None'
            if args_str and args_str.strip():
                self._property_refs[args_str.strip()] = 'Faction'
            return f'({arg}.IsPlayerExpelled())'

        # GetContainer: item.GetContainer -> item.GetContainer()
        if fname_low == 'getcontainer':
            ref = self._resolve_self_ref(ref_name, extends)
            return f'{ref}.GetContainer()'

        # GetBookRead -> no direct equivalent, return 0
        if fname_low in ('getbookread', 'bookread'):
            self._line_comments.append(';NE: GetBookRead')
            return '0'

        # ShowClassMenu / ShowBirthSignMenu — Skyrim has no class or birthsign
        # system, so these stay no-ops. ShowRaceMenu is NOT here: Skyrim has
        # Game.ShowRaceMenu() and FUNCTION_MAP carries the mapping — the TES4
        # chargen "revise your character" doors (CGSewerExitScript, Morroblivion's
        # census exit) depend on it actually opening.
        if fname_low in ('showclassmenu', 'showbirthsignmenu'):
            self._line_comments.append(f';NE: {func_name}')
            return '0'

        # SetInCharGen: no-op
        if fname_low == 'setinchargen':
            self._line_comments.append(';NE: SetInCharGen')
            return '0'

        # SetPlayerInSEWorld: no-op
        if fname_low == 'setplayerinseworld':
            self._line_comments.append(';NE: SetPlayerInSEWorld')
            return '0'

        # ForceCloseOblivionGate / CloseCurrentOblivionGate: no-op
        if fname_low in ('forcecloseobliviongate', 'closecurrentobliviongate'):
            self._line_comments.append(f';NE: {func_name}')
            return '0'

        # IsInFaction: ref.IsInFaction faction -> ref.IsInFaction(faction)  
        if fname_low == 'isinfaction':
            arg = self._convert_expression(args_str.strip(), extends) if args_str else 'None'
            if args_str:
                self._property_refs[args_str.strip()] = 'Faction'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.IsInFaction({arg})'

        # Activate [ActionRef] [RunOnActivateFlag] — TES4 semantics: activate
        # the object with ActionRef as activator (DEFAULT: the ORIGINAL
        # activator inside an OnActivate/OnTrigger block, else the object
        # itself), and run only the DEFAULT activation unless the flag is 1 —
        # bare `Activate` never re-enters the script's own OnActivate block.
        # The old mapping to `Activate(Game.GetPlayer())` was catastrophic the
        # moment NPCs could pathfind: Oblivion's AutoClosingDoor/
        # AutoCloseDoorLock (on doors game-wide) re-activate themselves from
        # BOTH blocks, so every door an NPC used was "activated by the
        # player" — teleporting the player through load doors, popping the
        # lockpick minigame on locked ones — and without
        # abDefaultProcessingOnly=true each Activate re-fired OnActivate in an
        # infinite loop.  akActionRef is rewritten to Self by
        # _postprocess_lines in events that have no action ref.
        if fname_low == 'activate':
            parts = ([p for p in re.split(r'[\s,]+', args_str.strip()) if p]
                     if args_str else [])
            run_flag = '0'
            if parts and parts[-1] in ('0', '1'):
                run_flag = parts[-1]
                parts = parts[:-1]
            ref = self._convert_ref(ref_name, extends) if ref_name else ''
            if parts:
                activator = self._convert_expression(parts[0], extends)
            elif ref:
                # TES4 `X.Activate` = X activates itself (quest/stage scripts
                # opening secret walls etc. — there is no action ref there).
                activator = ref
            elif extends == 'TopicInfo':
                activator = 'akSpeakerRef'
            else:
                activator = 'akActionRef'
            target = ref + '.' if ref else ''
            if run_flag == '1':
                return f'{target}Activate({activator})'
            return f'{target}Activate({activator}, true)'

        # --- Standard function map lookup ---
        # Default args for TES4 functions that implicitly use "player" when
        # called with no arguments, but the Papyrus equivalent requires them.
        _DEFAULT_ARGS = {
            'startconversation': 'Game.GetPlayer()',
            'sayto': 'Game.GetPlayer()',
            'getrandompercent': '0, 99',
            'isactordetected': 'Game.GetPlayer()',
            'getdetected': 'Game.GetPlayer()',
            'isdetectedby': 'Game.GetPlayer()',
            'setownership': 'Game.GetPlayer().GetActorBase()',
            'setactorowner': 'Game.GetPlayer().GetActorBase()',
        }
        entry = FUNCTION_MAP.get(fname_low)
        if entry:
            papyrus_func, needs_self, note = entry
            if papyrus_func is None:
                orig = f'{ref_name}.{func_name} {args_str}'.strip() if ref_name else f'{func_name} {args_str}'.strip()
                if note and not note.startswith(';TODO'):
                    # No-op: function has no Skyrim equivalent
                    # Return clean value (0) for expression contexts,
                    # store comment for line-level append
                    comment = f';NE: {orig}  {note}'
                    self._line_comments.append(comment)
                    return '0'
                return f';TODO: {orig}' + (f'  {note}' if note else '')
            if not args_str and fname_low in _DEFAULT_ARGS:
                args = _DEFAULT_ARGS[fname_low]
            else:
                args = self._convert_args(args_str, fname_low, extends) if args_str else ''
            # A mapped name that is already a GLOBAL call (Game.X, Utility.X,
            # Debug.X) is not a method, so a TES4 receiver has nowhere to go.
            # `Player.DisablePlayerControls` emitted
            # `Game.GetPlayer().Game.DisablePlayerControls()` — a property named
            # `Game` on Actor, which does not exist.  Oblivion allowed the
            # receiver on these player-global commands; Papyrus does not, and it
            # carries no information (the target is always the player).
            if ref_name and papyrus_func and re.match(
                    r'^(?:Game|Utility|Debug|Math)\.', papyrus_func):
                ref_name = None
            if ref_name:
                ref = self._convert_ref(ref_name, extends, as_receiver=True)
                papyrus_low = papyrus_func.lower() if papyrus_func else ''
                is_actor_func = fname_low in _ACTOR_ONLY_FUNCTIONS or papyrus_low in _ACTOR_ONLY_FUNCTIONS
                # ActiveMagicEffect Self doesn't have actor/objref methods
                if ref == 'Self' and extends == 'ActiveMagicEffect':
                    ref = 'GetTargetActor()'
                elif ref == 'Self' and extends == 'TopicInfo' and is_actor_func:
                    ref = 'akSpeakerRef'
                # Cast ObjectReference refs to Actor for truly actor-only functions
                # (skip ObjectReference-shared methods like PlaceAtMe, AddItem, etc.)
                if is_actor_func and fname_low not in _OBJREF_SHARED_FUNCTIONS:
                    # akSpeakerRef is a fixed ObjectReference parameter in TopicInfo scripts
                    if ref == 'akSpeakerRef':
                        ref = f'(akSpeakerRef as Actor)'
                    else:
                        cur = self._property_refs.get(ref, '')
                        if cur == 'ObjectReference':
                            ref = f'({ref} as Actor)'
                        elif cur == '' and self._is_bindable_property(ref):
                            self._property_refs[ref] = 'Actor'
                        elif cur.startswith('TES4_'):
                            # Typed as the SCRIPT attached to the record it
                            # names (see _resolve_self_ref for the full note):
                            # cast at the call site so the cross-script variable
                            # reads that need that type keep working.
                            ref = f'({ref} as Actor)'
                result = f'{ref}.{papyrus_func}({args})'
            else:
                # No ref — infer implicit target based on script context
                if needs_self and fname_low in _ACTOR_ONLY_FUNCTIONS:
                    event_actor = self._current_event_actor_param()
                    if extends == 'TopicInfo':
                        result = f'(akSpeakerRef as Actor).{papyrus_func}({args})'
                    elif extends == 'ActiveMagicEffect':
                        result = f'GetTargetActor().{papyrus_func}({args})'
                    elif extends == PLAYER_ALIAS_EXTENDS:
                        # Self is the ReferenceAlias, not an actor; the alias's
                        # filled reference (the player) is the subject.
                        result = f'GetActorReference().{papyrus_func}({args})'
                    elif extends != 'Actor' and event_actor:
                        # Inside an event that hands us the actor it is about
                        # (`OnEquipped(Actor akActor)`), TES4's implicit subject
                        # for an actor-only call is that actor, not the item.
                        # `MGBloodwormHelmScript*`'s bare `addspell` is cast on
                        # the WEARER; `(Self as Actor)` on an ARMO is None, so
                        # the helm's whole effect was silently lost.
                        result = f'{event_actor}.{papyrus_func}({args})'
                    elif extends not in ('Actor',):
                        result = f'(Self as Actor).{papyrus_func}({args})'
                    else:
                        result = f'{papyrus_func}({args})'
                elif (needs_self
                      and fname_low in _OBJREF_IMPLICIT_SELF_FUNCTIONS
                      and extends in ('ActiveMagicEffect', 'TopicInfo',
                                      PLAYER_ALIAS_EXTENDS)):
                    # ObjectReference method called bare inside a script whose
                    # Self is not a reference — route it onto the reference the
                    # effect/topic acts on, with no `as Actor` cast.
                    result = (f'{self._resolve_objref_ref(None, extends)}'
                              f'.{papyrus_func}({args})')
                else:
                    result = f'{papyrus_func}({args})'
            return f'{result}  {note}' if note else result
            
        # --- Fallback: unknown function ---
        args = self._convert_args(args_str, fname_low, extends) if args_str else ''
        if ref_name:
            ref = self._convert_ref(ref_name, extends, as_receiver=True)
            if fname_low in _ACTOR_ONLY_FUNCTIONS:
                if ref == 'akSpeakerRef':
                    ref = f'(akSpeakerRef as Actor)'
                else:
                    cur = self._property_refs.get(ref, '')
                    if cur == 'ObjectReference':
                        ref = f'({ref} as Actor)'
                    elif cur == '' and self._is_bindable_property(ref):
                        self._property_refs[ref] = 'Actor'
            return f'{ref}.{func_name}({args})  ;TODO: Verify'
        if fname_low in _ACTOR_ONLY_FUNCTIONS:
            if extends == 'TopicInfo':
                return f'(akSpeakerRef as Actor).{func_name}({args})  ;TODO: Verify'
            if extends == 'ActiveMagicEffect':
                return f'GetTargetActor().{func_name}({args})  ;TODO: Verify'
            if extends == PLAYER_ALIAS_EXTENDS:
                return f'GetActorReference().{func_name}({args})  ;TODO: Verify'
        if (fname_low in _OBJREF_IMPLICIT_SELF_FUNCTIONS
                and extends in ('ActiveMagicEffect', 'TopicInfo',
                                PLAYER_ALIAS_EXTENDS)):
            ref = self._resolve_objref_ref(None, extends)
            return f'{ref}.{func_name}({args})  ;TODO: Verify'
        return f'{func_name}({args})  ;TODO: Verify'

    # An OBSE format specifier: %z (string_var), %g/%.Nf (number), %c, %x, %%.
    # The precision digits are optional on BOTH sides of the dot: authors write
    # `%0.f` as often as `%.0f` (XPKnotboneFactionFixerSCRIPT) and the engine
    # accepts it, so requiring a digit after the dot missed those and left the
    # specifier printing literally.
    _OBSE_FMT_RE = re.compile(r'%(?:%|[-+ #0]*\d*(?:\.\d*)?[a-zA-Z])')

    def _format_string_call(self, args_str: str, extends: str) -> str:
        """Convert an OBSE printf-style call into Papyrus concatenation.

        `printToConsole "attack button == %.0f" attackButton` and
        `MessageBoxEX "…%z…%g", a, b` pass a format string followed by its
        arguments.  Papyrus has no formatting, so each specifier is replaced by
        `+ (arg as String) +`.  Previously the arguments were emitted straight
        after the string with no separator, which is not parseable at all
        ("unexpected name `attackButton`").
        """
        s = args_str.strip().lstrip(',').strip()
        if not s.startswith('"'):
            return self._quote_msg(s)
        end = s.find('"', 1)
        if end < 0:
            return self._quote_msg(s)
        fmt = s[1:end]
        rest = s[end + 1:].strip().lstrip(',').strip()
        # Arguments are comma-separated, but the FIRST may be space-separated
        # from the format string (`"…%.0f" attackButton`).
        arg_srcs = [a.strip() for a in rest.split(',') if a.strip()] if rest else []
        if len(arg_srcs) == 1 and ',' not in rest:
            arg_srcs = [a for a in arg_srcs[0].split() if a]
        args = [self._convert_expression(a, extends) for a in arg_srcs]

        pieces: list[str] = []
        last = 0
        idx = 0
        for m in self._OBSE_FMT_RE.finditer(fmt):
            if m.group(0) == '%%':
                continue
            if idx >= len(args):
                # No argument left to fill this specifier, so it is not one:
                # `%` also appears as an ordinary character ("100% done", where
                # the regex sees "% d").  Consuming it swallowed the following
                # letter and split the sentence.  Leave the text untouched.
                continue
            lit = fmt[last:m.start()]
            if lit:
                pieces.append(f'"{lit}"')
            pieces.append(f'({args[idx]} as String)')
            idx += 1
            last = m.end()
        tail = fmt[last:]
        if tail or not pieces:
            pieces.append(f'"{tail}"')
        # Any argument with no matching specifier still has to appear.
        for extra in args[idx:]:
            pieces.append(f'({extra} as String)')
        return ' + '.join(pieces)

    def _format_message(self, s: str, extends: str) -> str:
        """Format a vanilla Message/MessageBox call.

        Same printf model as _format_string_call, with one TES4-only wrinkle:
        `Message` takes an optional trailing DISPLAY TIME after the format
        arguments (`message "Rank %.0f Fireball", SpellRank, 10` shows one
        value for 10 seconds).  Papyrus's Debug.Notification has no duration,
        and _format_string_call appends every unconsumed argument to the text —
        which would print "Rank 3 Fireball10".  So surplus numeric literals
        beyond the specifier count are dropped rather than concatenated.
        """
        end = s.find('"', 1)
        fmt = s[1:end]
        rest = s[end + 1:].strip().lstrip(',').strip()
        n_spec = len([m for m in self._OBSE_FMT_RE.finditer(fmt)
                      if m.group(0) != '%%'])
        arg_srcs = [a.strip() for a in rest.split(',') if a.strip()] if rest else []
        if len(arg_srcs) == 1 and ',' not in rest:
            arg_srcs = [a for a in arg_srcs[0].split() if a]
        # Drop trailing bare numeric literals that have no specifier to fill.
        while (len(arg_srcs) > n_spec and arg_srcs
                and re.match(r'^-?\d+(?:\.\d+)?$', arg_srcs[-1])):
            arg_srcs.pop()
        rebuilt = f'"{fmt}"'
        if arg_srcs:
            rebuilt += ', ' + ', '.join(arg_srcs)
        return self._format_string_call(rebuilt, extends)

    def _quote_msg(self, args_str: str) -> str:
        """Quote a message argument if not already quoted.
        For MessageBox with buttons (e.g. '"text" "Yes" "No"'), extract only the message."""
        s = args_str.strip()
        # `Message, "text"` / `MessageBox, "text"` — Oblivion tolerated a comma
        # between the command and its first argument.  Left in place it is not
        # recognised as the opening quote, so the whole thing (comma included)
        # got re-quoted into `", "text""`, which does not parse.
        s = s.lstrip(',').strip()
        if s.startswith('"'):
            # Find the end of the first quoted string
            end = s.index('"', 1) if '"' in s[1:] else len(s)
            first_str = s[:end + 1]
            # If there are more quoted strings (button labels), strip them
            return first_str
        return f'"{s}"'


