"""ScriptConverter class — core TES4→Papyrus line-by-line conversion."""

from collections import defaultdict
import re
from typing import Optional

from script_convert.emit import expr as _expr
from script_convert.emit import script as _script
from script_convert.tes4 import lexer as _lexer
from script_convert.tes4 import nodes as _tes4_nodes
from script_convert.emit.commands import emit_row
from script_convert.constants import (
    COMMAND_ROWS, command_prefix_row, _ACTOR_VALUE_MAP_LOW,
    SELF_NAMES, PLACED_REF_SIGS,
    EVENTS_WITH_ACTIONREF, COMPOUND_HAS_OWN_HANDLER, DROP_ARGS_FUNCS,
    UDF_ARG_DOWNCASTS, DISPATCH_EVENTS, SAY_SPEAKAS_MIN_TOKENS,
    GMST_TO_ACTOR_VALUE, OBJREF_PARAMS,
    ENUM_ACTOR_VALUES, ENUM_AV_LADDERS,
    DEFAULT_ARGS,
    _PAPYRUS_VALUE_TYPES, BLOCK_MAP, BLOCK_FILTER_PARAM, TYPE_MAP,
    ACTOR_VALUE_MAP, KNOWN_GLOBALS, TES4_ATTRIBUTES, ATTRIBUTE_STUB_VALUE,
    _ACTOR_VALUE_FUNCTIONS, _ACTOR_VALUE_READ_FUNCTIONS, HANDLED_COMMANDS,
    KNOWN_COMMANDS, MAP, _BARE_BOOL_FUNCTIONS, _BOOL_VALUED_FUNCTIONS,
    PAPYRUS_BOOL_FUNCTIONS, _COMPARISON_BOOL_FUNCTIONS,
    _BARE_NO_EQUIV_COMMANDS, _ACTOR_ONLY_FUNCTIONS,
    _OBJREF_SHARED_FUNCTIONS, _ACTORBASE_ARG_FUNCTIONS, _ACTOR_ARG_FUNCTIONS,
    _OBJREF_IMPLICIT_SELF_FUNCTIONS, _ZERO_ARG_REF_FUNCTIONS,
    _FORM_TYPE_TESTS, _BRANCH_ONLY_COMMANDS, _safe_property_name,
    _canonical_global, _record_type_to_papyrus, _record_type_to_base_papyrus,
    papyrus_script_name, script_type_may_override, resolve_property_formid,
    _digit_stripped_formid, TES4_MURDER_BOUNTY, TES4_ASSAULT_BOUNTY,
    TES4_STEAL_BOUNTY, PLAYER_ALIAS_EXTENDS,
)
from script_convert.cross_ref import CrossRefGraph
from script_convert.tes4.parser import (
    Mode, is_self_contained, parse, split_call_args, split_param_names,
    split_trailing_comment,
)
from script_convert import symbols as _symbols
from script_convert.tes5.blocks import (
    Kind, classify, hoist_quest_start_above_writes, physical,
)


# A whole name wrapped in Oblivion's optional quotes: `"MQ01Tate"`.  Anchored,
# so it only ever strips a quoted IDENTIFIER handed to _convert_ref — never a
# string literal, which contains spaces/punctuation and reaches other handlers.
# `\w+` rather than an identifier shape: an EditorID may START WITH A DIGIT
# (`"1TrapFireMineWorldRef"`, `"2akulaSdoorSa"`), and leaving those quoted here
# is exactly the `_MQ01Tate_` trap described below.
_QUOTED_NAME_RE = re.compile(r'^"(\w+)"$')
#: TES4 reads of "is the player sleeping right now" -- the MenuMode sleep idiom.
_SLEEP_READS = frozenset({'ispcsleeping', 'isplayersleeping',
                          'getpcissleeping'})


_COND_LINE_RE = re.compile(r'^(\s*(?:If|ElseIf)\s+)(.*)$', re.IGNORECASE)

#: Any `_ACTOR_ONLY_FUNCTIONS` name called BARE -- a pre-filter for
#: `_infer_extends` (see there).  Longest-first so the alternation cannot
#: match a prefix of a longer name.
_ACTOR_ONLY_ANY_RE = re.compile(
    r'(?<!\.)(?<!\w)(?:'
    + '|'.join(sorted((re.escape(_f) for _f in _ACTOR_ONLY_FUNCTIONS),
                      key=len, reverse=True))
    + r')(?:\s|$|\()', re.IGNORECASE)

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
    cond, comment = split_trailing_comment(m.group(2))
    if not comment or not cond:
        return line
    if is_self_contained(cond):
        return line          # ordinary trailing comment — the condition is fine
    full = (cond + ' ' + comment).strip()
    return f'{m.group(1)}True  ;{full}'


def _reads_sleep_state(body) -> bool:
    """Does this block body read the player's sleep state?

    A bare `begin MenuMode` whose body reads isPCSleeping is the
    sleep-detection idiom and becomes RegisterForSleep; one that does not is
    frame bookkeeping and merges into the poll.  Asked of the NODES rather
    than by matching the name in source text, which also matched it inside a
    comment or a string literal.
    """
    return any(n.called in _SLEEP_READS
               for n in _tes4_nodes.walk_exprs_in(body))
# Fallback line length (seconds) a converted `set T to Say topic` assumes when
# the topic has no measured audio.  The real value comes from the engine at run
# time: TES4Polyfill.SayLine blocks until the INFO's OnBegin fragment reports
# the selected line's own length (say_durations, `info:<FID>`), and only falls
# back to this when the line has no voice file at all.  See the "Say() timers"
# section of docs/papyrus_conversion_notes.md.
SAY_LINE_SECONDS = 3.0
# SayLine's start timeout: how long it waits for the engine's OnBegin fragment
# before declaring the line dropped.  Mirrors SAY_START_WAIT in
# TES4Polyfill.psc and bounds how long a SayLine call can block, which is what
# the say-timer pre-charge has to outlast.
SAY_START_WAIT = 1.5
# Papyrus method name for an OBSE user-defined function (`begin Function{...}`).
# One fixed name per script: OBSE allowed exactly one Function block per script
# and `Call <ScriptName> args` names the SCRIPT, never the function.
_UDF_NAME = 'TES4Call'

def _split_udf_params(block_filter: str) -> list[str]:
    """Parameter names from an OBSE `begin Function{...}` header.

    Delegates to the TES4 lexer, which already treats commas and whitespace
    alike: the names are the identifier tokens between the braces.  Verified
    identical on all 20 distinct Function headers in the corpus.
    """
    return split_param_names(block_filter)


def _split_obse_args(rest: str) -> list[str]:
    """Split the argument tail of an OBSE `Call <Script> ...` invocation.

    Delegates to the TES4 parser: the lexer already knows where a string ends
    and the parser already knows where an argument ends, so the hand-rolled
    scanner that tracked quote state, bracket depth and operator context by
    character is gone.  Verified identical on all 338 real `Call` sites across
    every export, and on the argument-shape cases the old scanner documented
    (`30 * ( x - y ), 1, 1, -1` is four arguments; `"Voice Overs V002.esp"`
    stays one).
    """
    return split_call_args(rest)


# Search radius standing in for OBSE's GetFirstRef/GetNextRef walk over the
# loaded cells.  Oblivion scans the loaded-cell grid, whose interior span is one
# 4096-unit cell out from the player in each direction, so 4096 covers the same
# ground the authored loop did without reaching into cells the engine has not
# loaded.
_REFWALK_RADIUS = 4096.0

# Papyrus natives whose FIRST argument is typed narrower than `Form`, which is
# the permissive type an OBSE user-function parameter falls back to when the
# TES4 `ref` declaration carries no usage evidence.  Papyrus refuses the
# implicit downcast, so a call site passing such a parameter needs an explicit
# `as <type>` or the whole script fails to compile — and a script that fails to
# compile takes every script declaring a property of its type down with it.
# HasSpell is deliberately absent: it really does take a Form.

# The narrow-typed call sites above, capturing the function and its first
# argument.  A trailing `, ...` is allowed so the two-argument faction setters
# match as well as the one-argument tests.
# Longest name first so a dotted spelling (TES4Polyfill.Update3D) wins over any
# bare prefix of it.  The lookbehind rejects only a longer IDENTIFIER running
# into the name (`MyIsInFaction(`), NOT a receiver dot — these are method calls,
# so `NextActor.IsInFaction(x)` is the normal shape and must still match.
_UDF_DOWNCAST_RE = re.compile(
    r'(?<!\w)(' + '|'.join(
        re.escape(k) for k in sorted(UDF_ARG_DOWNCASTS, key=len, reverse=True))
    + r')\(\s*([A-Za-z_]\w*)\s*(?=[,)])',
    re.IGNORECASE)


# The names the expression path treats as COMMANDS even though they are
# absent from COMMAND_ROWS -- each has a dedicated handler in
# `_emit_function`.  Written inline as a tuple literal until 2026-08-28;
# the tree path needs the same gate, and two copies would drift.
_EXTRA_COMMAND_NAMES = frozenset({
    'bookread', 'call', 'closecurrentobliviongate', 'completequest',
    'createfullactorcopy', 'forcecloseobliviongate', 'getactionref',
    'getangle', 'getbookread', 'getcontainer', 'getcrimeknown',
    'getincell', 'getinsamecell', 'getisid', 'getisrace', 'getisref',
    'getissex', 'getpcisrace', 'getpcissex', 'getpos', 'getquestrunning',
    'getrandompercent', 'getself', 'getstage', 'getstagedone',
    'getstartingangle', 'isactionref', 'isexpelled', 'isinfaction',
    'isquestcompleted', 'message', 'messagebox', 'placeatme', 'pme', 'say',
    'saycustom', 'sayto', 'setangle', 'setdisplayname', 'setinchargen',
    'setplayerinseworld', 'setpos', 'setstage', 'showbirthsignmenu',
    'showclassmenu', 'showracemenu', 'sme', 'startquest', 'stopquest',
    'wakeuppc',
})


class ScriptConverter:
    """Converts Oblivion script source to Papyrus .psc source."""

    # topic (lowercase) -> longest spoken line in seconds, and `info:<FID>` ->
    # that line's exact length, measured from the exported Oblivion voice files
    # (say_durations.scan_voice_durations).  Populated once per run by the
    # pipeline; a topic with no entry falls back to SAY_LINE_SECONDS.
    say_durations: dict = {}

    # DIAL FormIDs (upper hex) whose topic a TES4 script drives via Say/SayTo.
    # These are the only topics whose INFOs need Begin/End timing fragments --
    # TES4Polyfill.SayLine blocks until OnBegin reports the line started and
    # reads its length from OnEnd, while a line the PLAYER picks never goes
    # through SayLine at all.  Filled once per run by pipeline's
    # scan_say_topic_fids() and passed explicitly into every worker (spawned
    # processes do not inherit it); consumed by pipeline.info_needs_fragment(),
    # which the fragment emitter and the importer's VMAD writer BOTH call so
    # the two can never disagree about which INFOs carry a fragment.
    say_topics: set = set()

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

    # 'birthsign'/'class' -> {'pages': [(mesg_edid, title, buttons)],
    # 'actions': [[spell_edid, ...] per choice]}, from
    # message_menus.build_chargen_menus.  Shared with the importer, which
    # authors the page MESGs at fixed FormIDs.  Empty when the plugin has no
    # BSGN/CLAS records — the menus then stay no-ops.
    chargen_menus: dict = {}

    def __init__(self, xref: CrossRefGraph):
        self.xref = xref
        # Parsed arguments of the call being emitted, for handlers that
        # have moved off `args_str` (see _emit_function).
        self._arg_nodes: tuple = ()
        #: Did the current call's argument list open with a COMMA?  For a
        #: zero-argument command the token after it is the RECEIVER
        #: (`StopCombat, Player` is `Player.StopCombat`), which is the only
        #: thing that says so.
        self._leading_comma = False
        #: Papyrus type of the value in the assignment being emitted, read off
        #: its parse tree by `emit_assignment`.  Empty outside an assignment.
        self._value_type: str = ''
        #: Parse tree of the script being converted, set by `_parse_source`.
        self._tree = None
        self._current_event: str = ''  # Current event header for context-aware conversion
        self._line_comments: list[str] = []  # Comments accumulated during expression conversion

        # Everything else is PER-SCRIPT state, and `_reset` is its one
        # definition -- a second copy here drifts silently.
        self._reset()

    _SAY_TOPIC_RE = re.compile(r'\.?Say\(\s*([A-Za-z_]\w*)')

    def _say_fallback_seconds(self, say_expr: str) -> float:
        """Fallback length for a converted `set T to Say topic` (see SAY_LINE_SECONDS).

        The topic's longest MEASURED line, used by TES4Polyfill.SayLine only
        when the line the engine picked has no measured length of its own.
        """
        tm = self._SAY_TOPIC_RE.search(say_expr or '')
        topic = tm.group(1).lower() if tm else ''
        if not topic:
            ms = self._SPEAK_AS_CALL_RE.match(say_expr or '')
            if ms:
                topic = ms.group('topic').strip().lower()
        return float((self.say_durations or {}).get(topic) or SAY_LINE_SECONDS)

    _SAY_CALL_RE = re.compile(r'^\s*(?P<recv>.+?)\.Say\((?P<topic>[^()]*)\)\s*$')
    # The speak-as shape emitted by the Say handler (see _say_speak_as).
    _SPEAK_AS_CALL_RE = re.compile(
        r'^\s*TES4Polyfill\.SpeakAs\((?P<speaker>[^,()]+),'
        r'(?P<inhead>[^,()]+),(?P<topic>[^,()]+)\)\s*$')

    # Events that run on the engine's own dispatch path, where a blocking
    # Say would stall the engine rather than just this script's tick.  A
    # quest-stage / INFO fragment is compiled as `Function Fragment_*`, so
    # match that too.

    def _say_may_block(self) -> bool:
        """True when a blocking SayLine is safe here (a poll, not a callback)."""
        ev = (self._current_event or '').lower()
        if 'onupdate' in ev:
            return True
        if 'fragment' in ev:
            return False
        return not any(e in ev for e in DISPATCH_EVENTS)

    # `<quest>.GetStage() == N` ... `<timer> <= 0` in ONE condition.  Both
    # orders occur, and other terms may sit between them.
    _STAGE_TIMER_GUARD_RE = re.compile(
        r'^(?P<indent>\s*)If\s+(?P<cond>.*?\b(?P<q>[A-Za-z_]\w*)\.GetStage\(\)\s*=='
        r'\s*(?P<stage>\d+)\b.*?)\s*$', re.IGNORECASE)
    _TIMER_ZERO_RE = re.compile(
        r'\b(?P<timer>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s*<=\s*0(?:\.0*)?\b')

    def _guard_stage_timer(self, line: str) -> str:
        """Close the stage-arrival race on `GetStage()==N && <timer> <= 0`.

        \U0001f6d1 THE TIMER IS CHARGED BY STAGE N'S OWN FRAGMENT, and nothing makes
        that charge land before this guard is first tested.  If the previous
        beat left the timer at or below zero -- which is its NORMAL resting
        state, and it also goes negative whenever a line is dropped -- then the
        instant stage N arrives the guard is ALREADY satisfied and the body
        runs before stage N's fragment has said anything.

        Measured 2026-08-16 (temp/chargen_rec_4.log, 14:47:14-18): CharacterGen
        sat at convTimer = -0.076 for four seconds after Renault's line was
        dropped, so `GetStage()==16 && convTimer<=0` fired the moment stage 16
        was set.  SetStage(17) ran, the force-greet pulled the player into the
        menu, and the Emperor's stage-16 line was never spoken -- INFO 00032B11
        ("You ... I've seen you") is gated on `GetStage CharacterGen == 16` and
        is the ONLY CharGenVoice entry for that stage, so once the stage reads
        17 nothing qualifies at all.

        The fix is a stage-arrival latch: remember the stage this quest was on
        when we last saw it, and require at least one poll pass at stage N
        before honouring the guard.  That pass is what lets stage N's fragment
        run and charge the timer.  25 guards of this shape exist in the
        Oblivion build; the CharacterGen ones are simply the ones that show.
        """
        if 'GetStage()' not in line or '<=' not in line:
            return line
        m = self._STAGE_TIMER_GUARD_RE.match(line)
        if not m:
            return line
        if not self._TIMER_ZERO_RE.search(m.group('cond')):
            return line
        quest = m.group('q')
        stage = m.group('stage')
        # One latch variable per quest, declared once by the caller.
        var = self._stage_latch_var(quest)
        indent = m.group('indent')
        cond = m.group('cond')
        return (f'{indent}If {cond} && {var} == {stage}'
                f'  ; stage-arrival latch: stage {stage} seen a full pass, '
                f'so its fragment has run')

    def _stage_latch_var(self, quest: str) -> str:
        """Name of the "stage we saw last pass" latch for `quest`, registering
        it so the emitter declares and updates it.

        Keyed CASE-INSENSITIVELY: TES4 scripts spell the same quest both ways
        in one file (CharacterGen's poll uses `characterGen` on some lines and
        `CharacterGen` on others).  Keying on the raw spelling emitted TWO
        latches for one quest, and a guard could then compare against the one
        the poll tail never updated -- the guard would never open.  Papyrus is
        case-insensitive, so the duplicate declarations compiled and the fault
        would only have shown in game.
        """
        key = quest.lower()
        var = self._stage_latches.get(key)
        if var is None:
            var = f'TES4_LastStage_{quest}'
            self._stage_latches[key] = var
        return var

    def _emit_say_line(self, target: str, say_call: str, delay: str) -> str:
        """`set T to [ref.]Say[To] ... topic [+ n]` -> a TES4Polyfill.SayLine call.

        TES4's Say/SayTo returned the selected line's length synchronously and
        the script went on at once; every participant of a scripted
        conversation waits on that number.  TES4Polyfill.SayLine restores the
        contract: it blocks until the engine has BEGUN the line (the INFO's
        OnBegin fragment reports the exact measured length), returns that
        length plus a fixed tail, and the caller continues immediately.
        Fragments never write the timer; the owning script's plain countdown
        drains it, and any beat an End result adds lands on top, exactly as
        in Oblivion.

        The pre-charge closes this poll's own `T <= 0` guard for the ~2s a
        SayLine can take (a Say nothing qualifies for waits out its start
        timeout), so a second poll tick cannot start a duplicate; SayLine also
        keeps one waiter per speaker.
        """
        m = self._SAY_CALL_RE.match(say_call or '')
        ms = self._SPEAK_AS_CALL_RE.match(say_call or '')
        if not m and not ms:
            # Unrecognised shape: keep the line audible and the timer > 0.
            return (f'{target} = {SAY_LINE_SECONDS:g}{delay}\n'
                    f'  {say_call}')
        fallback = self._say_fallback_seconds(say_call)
        if ms:
            # A speak-as site (see _say_speak_as): the measuring variant waits
            # for the INFO's Begin fragment the same way SayLine does and
            # returns the same measured length.
            pre = SAY_START_WAIT + 0.25
            fn = 'SpeakAsLine' if self._say_may_block() else 'SpeakAsLineNoWait'
            call = (f'TES4Polyfill.{fn}({ms.group("speaker").strip()}, '
                    f'{fallback:g}, {ms.group("inhead").strip()}, '
                    f'{ms.group("topic").strip()})')
            if self._var_types.get(target.lower().split('.')[-1]) == 'Int':
                return (f'{target} = {int(pre + 0.999)}  ; TES4 Say: closed until the line is under way\n'
                        f'  {target} = Math.Ceiling({call}){delay}')
            return (f'{target} = {pre:g}  ; TES4 Say: closed until the line is under way\n'
                    f'  {target} = {call}{delay}')
        speaker = m.group('recv').strip()
        topic = m.group('topic').strip()
        # The pre-charge closes this poll's own `T <= 0` guard for as long as
        # SayLine can still be BLOCKED, so a second tick cannot re-enter.
        #
        # The bound is SayLine's own start timeout (SAY_START_WAIT, 1.5s), NOT
        # the line length: on a line the engine ACCEPTS it returns after the
        # measured 0.18s median / 0.84s max (2026-08-16, 76 lines), and on a
        # DROPPED line it waits the full timeout and returns 0.0.  The old
        # `min(fallback + 1.0, 2.0)` scaled with the line instead, so a long
        # line charged 2.0s of closed guard even though SayLine had already
        # returned -- dead air on the timer of every line in the topic.
        pre = SAY_START_WAIT + 0.25
        # ð A BLOCKING SayLine IS ONLY SAFE IN A POLL.  It waits for the
        # engine's OnBegin fragment -- 0.18s median, up to SAY_START_WAIT when
        # the line is refused.  In OnUpdate that costs this script's own tick.
        # On the ENGINE'S DISPATCH PATH (a quest-stage or INFO fragment, or an
        # OnPackageStart/End, OnHit, OnCombatStateChanged callback) it stalls
        # the transition itself: the stage cannot advance, the package cannot
        # swap.  That is the stutter that accompanies a line at a STAGE CHANGE
        # rather than a polled line.  Those sites fire and don't wait.
        call_fn = 'SayLine' if self._say_may_block() else 'SayLineNoWait'
        call = f'TES4Polyfill.{call_fn}({speaker}, {topic}, {fallback:g})'
        if self._var_types.get(target.lower().split('.')[-1]) == 'Int':
            # A TES4 `short` holding a Say length: round UP so the tail survives.
            return (f'{target} = {int(pre + 0.999)}  ; TES4 Say: closed until the line is under way\n'
                    f'  {target} = Math.Ceiling({call}){delay}')
        return (f'{target} = {pre:g}  ; TES4 Say: closed until the line is under way\n'
                f'  {target} = {call}{delay}')

    def _owning_scripts(self, ref_name: str, *, converted_only: bool = True,
                        via_record: bool = True) -> list:
        """Converted scripts `ref_name` may name, lowercase.

        Two resolution routes, written out inline at three call sites: the
        property's declared type, and EditorID -> SCRI -> script EditorID for a
        name bound to a record rather than held in a property.  The flags say
        which routes a caller trusts, because they did not agree:

        `converted_only` keeps only a `TES4_<script>` property type.  Off, a
        plain `Actor`/`Quest` type is also tried as a script name -- which only
        `_is_ref_as_int_crossscript` did.

        `via_record` enables the EditorID route, which
        `_is_ref_typed_access` did not use (it has `is_remote_ref_var` for it).
        """
        if not self.xref:
            return []
        out = []
        ptype = self._property_type_ci(ref_name)
        if ptype.startswith('TES4_'):
            out.append(ptype[5:].lower())
        elif ptype and not converted_only:
            out.append(ptype.lower())
        if not via_record:
            return out
        fid = (self.xref.edid_to_formid.get(ref_name)
               or self.xref.edid_to_formid.get(ref_name.lower(), ''))
        scri = self.xref.record_scri.get(fid, '') if fid else ''
        if scri:
            edid = self.xref.script_formid_to_edid.get(scri, '').lower()
            if edid:
                out.append(edid)
        return out

    def _is_ref_typed_access(self, dotted_expr: str) -> bool:
        """Does `Owner.Var` read a ref-typed variable on Owner's script?"""
        if '.' not in dotted_expr or not self.xref:
            return False
        owner, _, var = dotted_expr.strip().partition('.')
        if self.xref.is_remote_ref_var(owner, var):
            return True
        var_low = var.lower()
        return any(
            self.xref.script_all_vars.get(script, {}).get(var_low)
            in ('ObjectReference', 'Actor')
            and (script, var_low) not in self.xref.ref_as_int
            for script in self._owning_scripts(owner, via_record=False))

    def _ref_has_script_var(self, ref_name: str, var_name: str) -> bool:
        """Does ref_name resolve to a script declaring var_name?

        Used to disambiguate Quest.variable vs Quest.function().
        """
        var_low = var_name.lower()
        return any(var_low in self.xref.script_all_vars.get(script, {})
                   for script in self._owning_scripts(ref_name))

    def _is_ref_as_int_crossscript(self, dotted_expr: str) -> bool:
        """Was `Owner.Var` retyped to Int on the owning script?"""
        if '.' not in dotted_expr or not self.xref:
            return False
        owner, _, var = dotted_expr.strip().partition('.')
        var_low = var.lower()
        return any((script, var_low) in self.xref.ref_as_int
                   for script in self._owning_scripts(
                       owner, converted_only=False))

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
            code, _ = split_trailing_comment(line)
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
        # One alternation over every candidate name decides in a single pass
        # whether ANY of them appears.  The per-function loop below still has
        # the final say -- it subtracts this script's own locals, which vary
        # per script and so cannot be baked into the pattern -- but it now runs
        # only for scripts that can actually match.  Measured: `_infer_extends`
        # was 52% of script-conversion time, building and running one regex per
        # candidate name (81) for every script.
        if not _ACTOR_ONLY_ANY_RE.search(code):
            return extends
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
        saved_aliases = dict(self._scro_aliases)
        self._reset()
        self._property_refs = saved_refs
        self._scro_aliases = saved_aliases
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

        # Feature flags from the TREE, not the text (bugs ledger 20).
        bodies = self._tree_bodies()
        exprs = [e for b in bodies for e in _tes4_nodes.walk_exprs_in(b)]
        names = {e.called for e in exprs if e.called}
        # A DECLARATION counts too: `Float Timer` means the script has a timer
        # even before any statement reads it.
        names |= {v.name.lower() for v in self._tree.variables} if self._tree             else set()
        btypes = ({b.btype.lower() for b in self._tree.blocks}
                  if self._tree else set())
        self._uses_getsecondspassed = 'getsecondspassed' in names
        self._gsp_realtime = bool(
            (names & {'getsecondspassed', 'scripteffectelapsedseconds'})
            and (btypes & {'gamemode', 'scripteffectupdate'}))
        if self._gsp_realtime:
            # The synthesized elapsed-time variable must be typed for the
            # Float->Int coercion: TES4 `short` timers decremented by
            # getSecondsPassed (`damage = -50 * TES4_SecondsPassed`) need
            # the `as Int` cast the old float LITERAL got via its own
            # detection path.
            self._var_types['tes4_secondspassed'] = 'Float'
            self._var_types['tes4_lasttick'] = 'Float'
        self._uses_timer = 'timer' in names
        self._uses_say = bool(names & {'say', 'sayto'})
        self._uses_say_timer = any(
            isinstance(st, _tes4_nodes.Assign)
            and any(e.called in ('say', 'sayto')
                    for e in _tes4_nodes.walk_expr(st.value) if e.called)
            for b in bodies for st in _tes4_nodes.walk_stmts(b))
        self._uses_hour_window = any(
            isinstance(e, _tes4_nodes.BinOp) and e.op in ('>=', '<=')
            and e.left.called == 'gamehour'
            and isinstance(e.right, _tes4_nodes.Literal) and '.' in e.right.text
            for e in exprs)
        # A bare `begin MenuMode` is merged into the GameMode poll (see the
        # block loop below), so it needs the OnUpdate loop emitted even when the
        # script has no GameMode block of its own.
        self._has_gamemode = any(
            b[0] == 'gamemode'
            or (b[0] == 'menumode' and not str(b[1] or '').strip()
                and not _reads_sleep_state(b[2]))
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

        # Decide every `ref` variable's REAL type now, from the parse tree,
        # so each declaration below is written once and correctly.  Three
        # passes used to re-read the emitted Papyrus afterwards to patch these
        # lines, because the body was converted after they were already out.
        if self._tree is not None:
            _stmts = list(self._tree.body)
            for _blk in self._tree.blocks:
                _stmts += _blk.body
            # Scan for the AUTHORED spelling: `_var_info` holds the
            # Papyrus-safe name, but the tree holds the source, and a variable
            # colliding with a Papyrus keyword was renamed on the way out
            # (`ref weapon` is emitted `myWeapon`).
            _to_src = {v.lower(): k for k, v in self._var_renames.items()}
            # `Form` is the cross-script ref-as-base-form pre-declaration.
            # It is the right FALLBACK, but a unanimous specific base type
            # still upgrades it -- stockFX only ever holds EFSH records, and
            # as a bare Form its `.Play(...)` does not compile.
            _refs = [_to_src.get(n.lower(), n) for n, t in _var_info
                     if t in ('ObjectReference', 'Form')]
            _resolved = _symbols.resolve_ref_types(
                _stmts, _refs,
                lambda n: (self._var_types.get(n.lower().split('.')[-1], '')
                           or self._property_type_ci(n)),
                self._base_record_type)
            for _i, (_n, _t) in enumerate(_var_info):
                # A type the SCRO preload already knows (the record a property
                # binds to) beats the usage guess -- it is authored data.
                _want = (self._property_type_ci(_n)
                         if _t == 'ObjectReference' else '')
                _want = (_want if _want and _want != 'ObjectReference'
                         else _resolved.get(_to_src.get(_n.lower(), _n).lower()))
                if _want and _want != _t:
                    _var_info[_i] = (_n, _want)
                    self._var_types[_n.lower()] = _want
                    self._property_refs[_n] = _want

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
                # Published for the emitters: a `ref` parameter becomes `Form`
                # in the signature (see `_param_type`), which not every
                # ObjectReference method exists on -- but the signature is not
                # decided until after the body is emitted, so the body has to
                # be able to ask which names ARE parameters.
                self._udf_params = {p.lower() for p in udf_params}
                self._udf_params |= {_safe_property_name(p).lower()
                                     for p in udf_params}
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
                reads_sleep = _reads_sleep_state(block_lines)
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

        # Whether this script's OnActivate CONSUMES activation (see
        # _onactivate_consumes) — drives both the door preamble below and the
        # BlockActivation injection after the block loop.
        blocks_activation = (extends in ('ObjectReference', 'Actor')
                            and self._onactivate_consumes(blocks))

        # Oblivion gate: capture which gate the player is entering, before the
        # authored body clears the only variable that names it.
        gate_entry = (extends in ('ObjectReference', 'Actor')
                      and self._is_oblivion_gate_entry(blocks))

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

            # Remember the gate the player just walked into, so
            # CloseCurrentOblivionGate has somewhere to send them back to.
            # MUST precede the authored body: its very next act is to clear
            # MQ00.nearOblivionGate, the only reference to this gate there is.
            if gate_entry and merge_key == BLOCK_MAP['onactivate'][0]:
                out.append('  If akActionRef == Game.GetPlayer()')
                out.append('    TES4Polyfill.EnterOblivionGate(Self)')
                out.append('  EndIf')

            # Oblivion's AI door-open BYPASSES locks and OnActivate scripts —
            # the CharacterGen back gate is level-100 locked with a "nobody
            # can open this gate" consume script, and Glenroy still opens it
            # by pathing.  Skyrim's AI door-open is a plain activation
            # (verified in the AE exe: the actor door state machine raises
            # the open action for any closed pathing door without reading the
            # lock, then ActionActivateDoneHandler calls ActivateRef with
            # abDefaultProcessingOnly=false), so it obeys both the lock and
            # the BlockActivation this script now applies — but OnActivate is
            # dispatched before either refusal, so the script replays the
            # Oblivion bypass itself: an NPC activator gets the default open,
            # players fall through to the consume body and the authored lock
            # UI.  The relock is DEFERRED to OnClose (emitted below): locking
            # in the same script frame as the Activate risks aborting the
            # open animation mid-play, and TES4's authored lock state only
            # meaningfully applies to the closed door anyway.
            if (blocks_activation and extends == 'ObjectReference'
                    and merge_key == BLOCK_MAP['onactivate'][0]):
                out.append('  If (GetBaseObject() as Door) && '
                           '(akActionRef as Actor) && '
                           'akActionRef != Game.GetPlayer()')
                out.append('    ; TES4 parity: AI door-use ignores this '
                           'script and the lock; the authored lock is '
                           'restored when the door next closes.')
                out.append('    If GetOpenState() >= 3')
                out.append('      TES4_pendingRelock = GetLockLevel()')
                out.append('      If IsLocked()')
                out.append('        Lock(false)')
                out.append('      EndIf')
                out.append('      Activate(akActionRef, true)')
                out.append('    EndIf')
                out.append('    Return')
                out.append('  EndIf')

            for guard, block_lines in segments:
                body = _script.emit_body(self, block_lines, extends)
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
                isinstance(b, _tes4_nodes.SetFunctionValue)
                for b in _tes4_nodes.walk_stmts(udf_body))
            # Convert the body BEFORE writing the signature: a TES4 `ref` is an
            # untyped handle, and the declared type alone is too weak to pick a
            # Papyrus parameter type.  GlobalScriptAddSpellIfNotOwned takes a
            # `ref` that every caller fills with a Spell and the body feeds to
            # AddSpell/HasSpell — typing it ObjectReference (the literal
            # translation of `ref`) rejects all 170 call sites.  Converting first
            # lets the usage-driven type inference run, then read the result.
            self._block_depth = 0
            udf_lines = _script.emit_body(self, udf_body, extends)

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
            # The signature a CALLER needs to know which arguments to cast.
            # Recording it here is what lets the cross-script cast pass be a
            # lookup instead of a second read of every generated .psc.
            self.udf_signature = [_param_types[p] for p in udf_params]
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
                        want = UDF_ARG_DOWNCASTS[m.group(1).lower()]
                        return f'{m.group(1)}({arg} as {want}'
                    return m.group(0)
                udf_lines[_i] = _UDF_DOWNCAST_RE.sub(_cast_arg, _conv)
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

        # TES4 physical traps: the ENGINE dealt the contact damage.  When a
        # Havok body on layer 14 (OL_TRAP) struck an actor, Oblivion read the
        # magic variables `fTrapDamage` / `fLevelledDamage` / `fTrapPushBack`
        # off the striking object's script and applied
        # `fTrapDamage + fLevelledDamage * victimLevel` damage plus pushback —
        # UESP documents the per-trap results (swinging mace "20 + 1.5 x
        # level", swinging log "15 + 1.5 x level").  The script body itself
        # never contains a damage line, so nothing survived conversion and
        # every converted swinging mace / log / rolling rock hit for zero.
        #
        # Skyrim keeps the layer-14 contact detection (the mesh conversion
        # preserves authored OL_TRAP → SKYL_TRAP on the striking bodies, the
        # same layer vanilla trapmace01's mace head uses) but dispatches it as
        # the OnTrapHitStart script event and leaves the damage to the script:
        # vanilla TrapHitBase.psc answers it with the native
        # ObjectReference.ProcessTrapHit.  Mirror that contract by reading the
        # same authored variables at hit time.  The values are LIVE, which
        # reproduces the whole authored lifecycle for free: CTrapSwingMace01
        # sets fTrapDamage=20 only on activation (0 while the trap is still
        # armed and held, so brushing it is harmless) and lowers it to 5 six
        # seconds after release.
        def _declared_trap_var(name_low):
            for _vt, _vn in variables:
                if _vn.lower() == name_low:
                    return _safe_property_name(_vn)
            return None

        _trap_dmg = _declared_trap_var('ftrapdamage')
        if _trap_dmg and extends in ('ObjectReference', 'Actor'):
            _trap_lvl = _declared_trap_var('flevelleddamage')
            _trap_push = _declared_trap_var('ftrappushback')
            total = _trap_dmg
            if _trap_lvl:
                total += f' + {_trap_lvl} * victim.GetLevel()'
            out.append('Event OnTrapHitStart(ObjectReference akTarget, '
                       'float afXVel, float afYVel, float afZVel, '
                       'float afXPos, float afYPos, float afZPos, '
                       'int aeMaterial, bool abInitialHit, int aeMotionType)')
            out.append("  ; TES4's engine read this script's fTrapDamage "
                       'variables when an OL_TRAP')
            out.append('  ; body struck an actor.  Skyrim raises '
                       'OnTrapHitStart instead and the')
            out.append('  ; script deals the hit itself, like vanilla '
                       'TrapHitBase.psc.')
            out.append('  Actor victim = akTarget as Actor')
            out.append('  If victim == None')
            out.append('    Return')
            out.append('  EndIf')
            out.append(f'  Float totalDamage = {total}')
            out.append('  If totalDamage <= 0.0')
            out.append('    Return   ; not armed yet - TES4 variables start at 0')
            out.append('  EndIf')
            out.append(f'  akTarget.ProcessTrapHit(Self, totalDamage, '
                       f'{_trap_push if _trap_push else "0.0"}, '
                       'afXVel, afYVel, afZVel, afXPos, afYPos, afZPos, '
                       'aeMaterial, 0.0)')
            out.append('EndEvent')
            out.append('')

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
        # `needs_oninit_update`, not `gamemode_body`: an EMPTY `begin
        # ScriptEffectUpdate` / `begin GameMode` still declares the poll, and
        # 19 scripts have one (GhostEffectScript's is empty by design -- the
        # work is in ScriptEffectStart, and the update block exists to keep
        # the effect alive).  Gating on the body being non-empty dropped the
        # whole OnUpdate event for them.
        if needs_oninit_update:
            interval = self._get_update_interval()
            self._current_event = 'Event OnUpdate()'
            if self._gsp_realtime:
                # Backing state for TES4_SecondsPassed (the getSecondsPassed
                # conversion): filled by the prologue below with the real
                # elapsed time per pass.  Plain script variables, not
                # properties — nothing outside this script reads them and
                # they must not appear in the VMAD.
                out.append(f'Float TES4_SecondsPassed = {interval}')
                out.append('Float TES4_LastTick = 0.0')
                out.append('')
            out.append('Event OnUpdate()')
            # Arm the poll TWICE: an insurance arm at the TOP and the real
            # re-arm at the BOTTOM.
            #
            # The TOP arm is abort insurance ONLY, and it is LONG (5s).  A
            # runtime error anywhere in the body ("Cannot call X on a None
            # object", a bad cast) ABORTS the event at that line, and with
            # only a bottom re-register one abort silently killed the poll
            # for the rest of the game — the intermittent "the NPCs just
            # stand there" class of failure.
            #
            # 🛑 It must NOT arm at the real interval.  RegisterForSingleUpdate
            # counts from NOW, so a top arm at `interval` starts the next
            # pass `interval` after this one STARTED — and a pass whose body
            # takes longer than that (MQ01Script's tutorial poll: ~15 latent
            # natives per 0.1s tick) overlaps itself, every overlap slows the
            # VM further, and the pile grows without bound.  Measured in game
            # 2026-08-16 (Papyrus stack dump at the start of CharacterGen):
            # 251 concurrent TES4_MQ01Script.OnUpdate stacks, End fragments
            # of 1-2s lines running 19-24s late, conversations with 10s+
            # gaps and repeats.  A 5s insurance arm bounds the overlap to one
            # extra stack per 5s of blocking, and any pass that finishes (or
            # returns early — see the spliced Return below) replaces it with
            # the real interval, so a healthy script never sees it.
            #
            # The BOTTOM arm sets the CADENCE: it replaces the pending update
            # (RegisterForSingleUpdate keeps one pending update per script),
            # so a pass schedules the next tick `interval` after the body
            # FINISHES — period = interval + execution time, and passes never
            # overlap.
            def _emit_arm(indent='  ', secs=None):
                secs = interval if secs is None else secs
                if load_gated:
                    out.append(f'{indent}If ({self._GAMEMODE_GATE})')
                    out.append(f'{indent}  RegisterForSingleUpdate({secs})')
                    out.append(f'{indent}EndIf')
                else:
                    out.append(f'{indent}RegisterForSingleUpdate({secs})')
            _emit_arm(secs='5.0')
            # A TES4 `return` inside the polled body ends THIS pass only; the
            # converted `Return` must re-arm at the real interval itself,
            # since it skips the bottom arm and the top arm is 5s now.
            if load_gated:
                self._poll_return_prefix = (
                    f'If ({self._GAMEMODE_GATE})\n'
                    f'    RegisterForSingleUpdate({interval})\n'
                    f'  EndIf\n  ')
            else:
                self._poll_return_prefix = f'RegisterForSingleUpdate({interval})\n  '
            if quest_gated:
                # Not running: skip the body, but the poll above keeps
                # ticking so the loop resumes once the quest is started.
                out.append('  If (!IsRunning())')
                out.append('    Return')
                out.append('  EndIf')
            # TES4 GameMode never ran while a menu was open.  Every converted
            # poll (actor AND quest scripts) skips its pass while the player
            # is in a dialogue menu with any actor, or that actor is still
            # speaking the Goodbye line the menu closed on
            # (TES4Polyfill.PlayerIsInDialogue, stamped by the INFO Begin
            # fragments); an actor script also checks its own dialogue.
            #
            # Why (all measured in game 2026-08-16, CharacterGen 40-50):
            #  * the Emperor's `speaker == 4 && convTimer <= 0` poll fired
            #    while the player was in his stage-42 dialogue (the reply's
            #    End result is what sets speaker=0/stage 43); its Say()
            #    INTERRUPTED his 17.8s Goodbye reply and the reply's
            #    `setstage 43` was lost -- the birthsign menu never opened;
            #  * the QUEST poll's `stage 45 && convTimer <= 0 -> setstage 50`
            #    fired while the player was still in the stage-44 dialogue
            #    (Skyrim runs the reply's End result with the menu open), and
            #    stage 50's evp sent Baurus in to force-greet over it -- the
            #    "Baurus interrupts the Emperor" the user saw on most runs;
            #  * Baurus's stage-19 torch line (`getdistance player < 250 &&
            #    sayPlayer == 0 -> sayTo player CharGenVoice`) fired into the
            #    Emperor dialogue the same way.
            # In Oblivion none of these polls ran until the menu had closed.
            #
            # A 2026-08-14 attempt to freeze quest polls was reverted because
            # the OLD Say design estimated timers at the call site and was
            # tuned against a countdown that kept draining through menus.
            # TES4Polyfill.SayLine returns the engine's real line length, so
            # a countdown that pauses in a menu is now simply Oblivion's
            # behaviour.  The top arm above keeps the poll alive, and the
            # TES4_SecondsPassed clamp below treats the gap like any other
            # suspension.
            #
            # Only scripts that SPEAK (any Say/SayTo in the source) carry the
            # gate.  Applying it to every poll (2026-08-16, first attempt) put
            # PlayerIsInDialogue on ~210 quest polls at 0.1s and the Papyrus
            # VM -- 1.2ms per frame -- starved: End fragments of 1-2s lines
            # ran 11-17s late, SayLine's busy deadline expired first, and
            # "Yessir" played twice.  A non-speaking poll cannot cut a line.
            if self._uses_say and extends == 'Actor':
                out.append('  If IsInDialogueWithPlayer() || '
                           'TES4Polyfill.PlayerIsInDialogue()  '
                           '; TES4 GameMode did not run while a menu was open')
                _emit_arm(indent='    ', secs='0.5')
                out.append('    Return')
                out.append('  EndIf')
            elif self._uses_say and extends == 'Quest':
                out.append('  If TES4Polyfill.PlayerIsInDialogue()  '
                           '; TES4 GameMode did not run while a menu was open')
                _emit_arm(indent='    ', secs='0.5')
                out.append('    Return')
                out.append('  EndIf')
            if self._gsp_realtime:
                # TES4 getSecondsPassed returned the REAL time the frame
                # took.  Measure it instead of assuming the tick interval:
                # RegisterForSingleUpdate delivers late under VM load, and a
                # fixed decrement then drains every counted timer slower
                # than real time, so all conversation pacing floated with VM
                # load.  Every getSecondsPassed read this pass sees the same
                # value, exactly like TES4's per-frame constant.  The clamp
                # covers the first pass and resumption after unload, menus
                # or a save-load, where the raw delta spans the whole gap
                # TES4 never counted (GameMode did not run there).
                out.append('  Float TES4_Now = Utility.GetCurrentRealTime()')
                out.append('  TES4_SecondsPassed = TES4_Now - TES4_LastTick')
                out.append('  If TES4_SecondsPassed < 0.0 || '
                           'TES4_SecondsPassed > 2.0')
                out.append(f'    TES4_SecondsPassed = {interval}')
                out.append('  EndIf')
                out.append('  TES4_LastTick = TES4_Now')
            out += _script.emit_body(self, gamemode_body, extends, 1)
            # Stage-arrival latches: record the stage each guarded quest is on
            # NOW, so the next pass can tell "we have already seen this stage"
            # from "it just arrived".  Emitted at the very END of the body so
            # every guard above compared against the PREVIOUS pass's value.
            # See _guard_stage_timer for the race this closes.
            for _, _var in sorted(self._stage_latches.items()):
                # _var is TES4_LastStage_<quest as first spelled>; reuse that
                # spelling for the read so the emitted line matches the source.
                _qname = _var[len('TES4_LastStage_'):]
                out.append(f'  {_var} = {_qname}.GetStage()')
            self._poll_return_prefix = ''
            _emit_arm()
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
                out += _script.emit_body(self, block_lines, extends, 1)
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
            for converted in _script.emit_body(self, block_lines, extends):
                if converted.strip():
                    out.append(f';  {converted}')
            out.append('')

        # Start/stop the update loop (and the sleep listener, which shares the
        # same lifecycle: TES4 MenuMode also only ran while the script's owner
        # was loaded / its quest instantiated).
        needs_sleep_reg = bool(sleep_menumode_blocks)
        if needs_oninit_update or needs_sleep_reg:
            interval = self._get_update_interval()

            def _arm(indent='  '):
                """The body of every event that (re)starts the loop.

                Four events arm it -- OnCellAttach, OnLoad and two OnInit
                shapes -- and they arm it identically, so the lines are one
                definition rather than four copies that can drift apart.
                """
                if needs_oninit_update:
                    out.append(f'{indent}RegisterForSingleUpdate({interval})')
                if needs_sleep_reg:
                    out.append(f'{indent}RegisterForSleep()')
            if load_gated:
                # Object/actor: run only while loaded.  OnCellAttach fires each
                # time the reference streams into an active cell; OnCellDetach
                # when it streams out.  This confines the loop to when the
                # object is actually present, exactly like TES4 GameMode.
                out.append('Event OnCellAttach()')
                _arm()
                out.append('EndEvent')
                out.append('')
                # NO UnregisterForUpdate on OnCellDetach.  Cell-transition
                # events arrive in no guaranteed order, so the detach for the
                # OLD cell could land after OnLoad/OnCellAttach had already
                # re-armed the poll for the NEW one and silently kill a
                # loaded actor's loop mid-scene (the CharacterGen escort
                # NPCs went mute this way — "sometimes they talk, sometimes
                # nothing").  The arm-first Is3DLoaded() gate in OnUpdate
                # stops the loop by itself one tick after the 3D actually
                # goes away, so the unregister bought nothing but the race.
                if needs_sleep_reg:
                    out.append('Event OnCellDetach()')
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
                # An initially-disabled reference has no 3D, and on ~200 Nehrim
                # refs the poll body is the only thing that ever calls Enable()
                # on that same reference.  SafeGameModeGate is therefore
                # cell-scoped, not 3D-scoped; see _GAMEMODE_GATE.
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
                    _arm()
                    out.append('EndEvent')
                    out.append('')
                if not any(b[0] == 'oninit' for b in blocks):
                    out.append('Event OnInit()')
                    out.append(f'  If ({self._GAMEMODE_GATE})')
                    _arm('    ')
                    out.append('  EndIf')
                    out.append('EndEvent')
                    out.append('')
            else:
                has_oninit = any(b[0] == 'oninit' for b in blocks)
                if not has_oninit:
                    out.append('Event OnInit()')
                    _arm()
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

        # TES4 contract: the PRESENCE of an OnActivate block REPLACES default
        # activation — the body runs INSTEAD of open/loot/talk, and only a
        # bare `Activate` (already converted to Activate(akActionRef, true),
        # which bypasses the block) performs it.  Papyrus OnActivate is
        # notification-only, so without blocking, default processing ran
        # anyway: the dead Emperor opened a loot menu over his "speak to
        # Baurus" redirect, and every empty-body "nobody can open this" door
        # script consumed nothing.  Vanilla Skyrim's defaultBlockActivation
        # applies the same call from OnLoad.
        #
        # Only applied when some path through the TES4 body actually CONSUMES
        # the activation (no unconditional top-level `Activate`): a pure
        # passthrough like AutoClosingDoor gains nothing from blocking.  AI
        # door traffic through blocked doors is preserved by the OnActivate
        # door preamble emitted in the block loop above.
        if blocks_activation:
            out = self._inject_block_activation(out)

        # Companion to the door preamble: the deferred relock.  TES4 has no
        # OnClose event, so this can never collide with an authored block.
        if (blocks_activation and extends == 'ObjectReference'
                and BLOCK_MAP['onactivate'][0] in merged_blocks):
            out.append('Int TES4_pendingRelock = 0')
            out.append('')
            out.append('Event OnClose(ObjectReference akActionRef)')
            out.append('  ; Restore the authored lock lifted for an AI door '
                       'passage (see OnActivate).')
            out.append('  If TES4_pendingRelock > 0')
            out.append('    Lock(true)')
            out.append('    SetLockLevel(TES4_pendingRelock)')
            out.append('    TES4_pendingRelock = 0')
            out.append('  EndIf')
            out.append('EndEvent')
            out.append('')

        # Balance If/EndIf within event blocks (some TES4 scripts have extra EndIf)

        # Remove dead code after Return statements within event/function blocks

        # Apply shared post-processing (TES4-only functions, type mismatches, etc.)
        out = self._postprocess_lines(out)

        # Insert property declarations for referenced FormIDs
        if self._property_refs:
            # Names already taken by this script's own variables.
            declared = {v[0].lower() for v in _var_info}
            prop_lines = ['; --- External references (auto-linked via VMAD) ---']
            prop_lines += _symbols.property_declarations(self._property_refs,
                                                        declared)
            prop_lines.append('')
            insert_idx = 3 + len(_var_info) + (1 if _var_info else 0)
            for i, pl in enumerate(prop_lines):
                out.insert(insert_idx + i, pl)

        out.extend(self._emit_cell_family_helpers())
        out.extend(self._emit_button_helpers())
        if getattr(self, '_uses_chargen_menus', False):
            # Re-entrancy latch for the modal chargen menus: Message.Show()
            # parks only ITS calling thread, and an OnUpdate tick queued
            # behind the open menu re-enters the same body the moment it
            # closes.  Without the latch every queued tick re-showed the
            # menu ("I had to click through it multiple times").  A skipped
            # pass still runs the authored statements AFTER the menu —
            # exactly TES4's order, which executed `setstage 44` in the same
            # frame it opened the menu.
            out.extend(['', 'Bool TES4_ChargenMenuBusy = False'])
        # Stage-arrival latches used by _guard_stage_timer.  -1 so the FIRST
        # pass at a stage never satisfies `latch == N`: the guard then waits
        # one pass, which is what lets that stage's fragment charge the timer.
        if self._stage_latches:
            out.append('')
            for _, _var in sorted(self._stage_latches.items()):
                _qname = _var[len('TES4_LastStage_'):]
                out.append(f'Int {_var} = -1'
                           f'  ; stage of {_qname} on the previous poll pass')
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
        saved_aliases = dict(self._scro_aliases)
        self._reset()
        self._cell_families = saved_families
        self._property_refs = saved_refs
        # The caller installed this fragment's SCRO alias map just before the
        # call; _reset clears it for the NEXT fragment, not this one.
        self._scro_aliases = saved_aliases
        # A fragment body runs on the ENGINE'S DISPATCH PATH: a quest stage
        # cannot advance, and an INFO cannot finish, while the fragment is
        # still executing.  A blocking SayLine there stalls the transition
        # itself -- the stutter that accompanies a line at a stage change.
        # _say_may_block() reads this to emit SayLineNoWait instead.
        self._current_event = 'Fragment'
        # PARSED, not re-scanned: a fragment is a script without the block
        # wrappers, which is a parser MODE rather than a second hand-written
        # loop.  The three regexes this replaces (the `scn` skip, the
        # declaration match written twice, the `begin`/`end` skip) were a
        # fifth parser that had to agree with the other four about what a
        # declaration looks like -- and did not, since only this copy accepted
        # `reference` while `_convert_line_inner`'s copy also had to.
        #
        # A fragment declares its variables as LOCALS with an initialiser,
        # unlike a script body where they are hoisted to properties: a
        # fragment is one function, so there is nowhere else for them to live.
        try:
            tree = parse(source, Mode.FRAGMENT)
        except Exception:
            return []
        result = []
        for var in tree.variables:
            ptype = TYPE_MAP.get(var.vtype.lower(), 'Int')
            result.append('  %s %s = %s'
                          % (ptype, var.name, '0.0' if ptype == 'Float' else '0'))
        result += _script.emit_body(self, tree.body, extends, 1)
        # Apply shared post-processes to fragment lines
        result = self._postprocess_lines(result)
        return result

    def _postprocess_lines(self, lines: list[str]) -> list[str]:
        """Shared post-processing for both standalone and fragment scripts."""
        # Oblivion's measure-then-deliver idiom speaks a line ONCE:
        #     Set InfoLength to ArmandRef.Say TG01Armand1     ; returns duration
        #     ArmandRef.SayTo Player TG01Armand1              ; delivers to listener
        # Both TES4 functions speak, so the author's pair relies on the engine
        # collapsing them; converted literally it became two `Say` calls in a
        # row and every such line played TWICE (Armand's whole TG01 briefing,
        # the SE07A Sheogorath/Thadon endgame, SE03's chamber chatter — 92
        # pairs in all).  The measuring half is now the TES4Polyfill.SayLine
        # call (which both measures and delivers), so the bare delivery that
        # follows it for the same speaker+topic is the one to drop.  A plain
        # measure/deliver pair with no timer keeps the old rule: two identical
        # `ref.Say(topic)` calls collapse to the second.
        _say_call_re = re.compile(
            r'^(\s*)((?:\([^()]*\)|\S+)\.Say\((?:[^()]*)\))\s*$')
        _say_line_re = re.compile(
            r'^\s*\S+\s*=\s*(?:Math\.Ceiling\()?TES4Polyfill\.SayLine\('
            r'(?P<recv>[^,]+),\s*(?P<topic>[^,]+),', re.IGNORECASE)
        _flat = physical(lines)
        # The delivery is usually the next line, but the author may slip a
        # Look/SetLookAt between the two halves (SE07A's Sheogorath/Thadon
        # exchange), so scan a short window.  Stop at anything that changes
        # control flow or re-arms the timer, so two Says that genuinely belong
        # to different beats are never collapsed.
        _dedup_window = 3

        def _stops_the_window(text: str) -> bool:
            # `classify` is the shared barrier -- it also knows a typed
            # `Int Function` header, which this pass's own regex did not.
            # The marker test is `startswith` because the regex it
            # replaced was applied with `re.match`, which anchors: a
            # TRAILING `; TES4 Say: closed` never stopped the window.
            return (classify(text) is not Kind.OTHER
                    or text.startswith('; TES4 Say: closed'))
        _skip = set()
        for idx, line in enumerate(_flat):
            sl = _say_line_re.match(line)
            m = _say_call_re.match(line)
            if not sl and not m:
                continue
            want = (f'{sl.group("recv").strip()}.Say({sl.group("topic").strip()})'
                    if sl else m.group(2))
            for j in range(idx + 1, min(idx + 1 + _dedup_window, len(_flat))):
                if _stops_the_window(_flat[j]):
                    break
                nxt = _say_call_re.match(_flat[j])
                if nxt and nxt.group(2) == want:
                    # Same speaker+topic again: the bare delivery duplicates
                    # the SayLine, or `idx` is the measuring half of a plain
                    # pair.  Keep exactly one.
                    _skip.add(j if sl else idx)
                    break
        lines = [l for i, l in enumerate(_flat) if i not in _skip]
        # Defer SetDestroyed(1) past the clip started just above it.  TES4
        # pairs the two constantly -- `playgroup forward 0` then
        # `setDestroyed 1` (CTrigTripwire01SCRIPT, CTrapLogs01SCRIPT,
        # CTrapCaveIn01SCRIPT, MPlanksBreakAway01Script) -- because in
        # Oblivion setDestroyed on a record with no destruction data only
        # blocked re-activation, and Oblivion ships ZERO DEST subrecords
        # (censused: 0 in ACTI).
        #
        # NOTE (2026-08-06): an earlier version of this comment blamed the
        # SetDestroyed ordering for the tripwire never visibly snapping.
        # That was WRONG -- vanilla's own Tripwire.pex calls setDestroyed on
        # TrapTripwire01, which has no destruction data either (610/1870
        # vanilla ACTI carry DEST), so the call is safe on a DEST-less record
        # and was never the tripwire's problem (that was the morph-emulation
        # NiVisController swap; see nif_converter._emulate_morphs).  The
        # deferral stays because it is behaviour-preserving and cheap: the
        # destroy still runs (it is what stops the trap re-triggering), just
        # after the polyfill has waited out the animation.
        _playanim_re = re.compile(r'^(\s*)(.+?)\.PlayAnimation\("([^"]+)"\)\s*$')
        # Matches the polyfill form the setdestroyed handler now emits --
        # TES4Polyfill.SetDestroyed(<ref>, TES4DestroyedRefs, true) -- not the
        # bare native, which is no longer emitted anywhere.  Capturing the
        # FormList argument keeps it on the deferred call.
        _setdestroyed_re = re.compile(
            r'^(\s*)TES4Polyfill\.SetDestroyed\(\s*(.+?)\s*,\s*'
            r'(\w+)\s*,\s*true\s*\)\s*$',
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
                              f'{tgt}, {dm.group(3)})')
        # Fix akActionRef used in events that don't define it
        # TES4 scripts could use GetActionRef across blocks; Papyrus scopes params to events
        _event_re2 = re.compile(r'^\s*Event\s+(\w+)', re.IGNORECASE)
        _endevent_re2 = re.compile(r'^\s*EndEvent\b', re.IGNORECASE)
        current_event = None
        has_actionref = False
        for idx in range(len(lines)):
            em = _event_re2.match(lines[idx])
            if em:
                current_event = em.group(1).lower()
                has_actionref = current_event in EVENTS_WITH_ACTIONREF
                continue
            if _endevent_re2.match(lines[idx]):
                current_event = None
                has_actionref = False
                continue
            if current_event and not has_actionref and 'akActionRef' in lines[idx]:
                # Replace undefined akActionRef with Self
                lines[idx] = lines[idx].replace('akActionRef', 'Self')
        # Fix cross-script Float args in item count functions
        _item_count_re = re.compile(
            r'(\.(RemoveItem|AddItem)\s*\(\s*\w+\s*,\s*)(\w+\.\w+)(\s*\))',
            re.IGNORECASE)
        for idx in range(len(lines)):
            m = _item_count_re.search(lines[idx])
            if m and ' as Int' not in m.group(3):
                lines[idx] = lines[idx][:m.start(3)] + m.group(3) + ' as Int' + lines[idx][m.end(3):]
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
        lines = hoist_quest_start_above_writes(lines)
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

    def _is_ref_value(self, value: str) -> bool:
        """Does this converted expression evaluate to an object reference?"""
        low = value.strip().lower()
        return (low in ('self', 'akspeakerref')
                or 'gettargetactor()' in low or 'getself' in low
                or self._OBJREF_RETURNING.search(value.strip()) is not None)

    # ---- hooks for emit/expr.py ------------------------------------------
    # The tree emitter owns expression STRUCTURE; these own the TES4 SEMANTICS
    # it reaches.  Four delegate to the string phases that still exist below --
    # they are replaced by the command table in R4, at which point the string
    # path goes away entirely.

    def returns_bool(self, name: str) -> bool:
        """Does this TES4 SOURCE name return a boolean, so `X == 1` is `X`?"""
        return name.lower() in _BOOL_VALUED_FUNCTIONS

    def compares_bool(self, name: str) -> bool:
        """Does this TES4 name collapse `X == 0/1` in a COMPARISON position?

        Narrower than `returns_bool`: only the comparison-position list, which
        differs from the bare-read one (docs/script_conversion_bugs.md #6).
        """
        return name.lower() in _COMPARISON_BOOL_FUNCTIONS

    def emits_bool(self, name: str) -> bool:
        """Does this PAPYRUS name return Bool?  Asked of the emitted text, for
        a TES4 command whose own name is in no table but whose conversion is a
        bool call."""
        return name.lower() in PAPYRUS_BOOL_FUNCTIONS

    def emit_name(self, name: str, extends: str) -> str:
        """A local, a zero-argument command read, or an external property."""
        return self._resolve_name(name, extends)

    def emit_member(self, owner: str, name: str, extends: str) -> str:
        """`Owner.name` -- a cross-script variable read or a zero-argument call.

        Decides which of the two it is.  A regex used to re-split the dotted
        name out of `owner.name` text; the parser already separated them, so
        what is left is the lookup.
        """
        prop_low = name.lower()
        safe = _safe_property_name(name)
        # A variable the owner's script actually declares is a property read,
        # whatever the name happens to collide with.
        if self._ref_has_script_var(owner, name):
            return f'{self._convert_ref(owner, extends)}.{safe}'
        # A quest's own variable, likewise -- except for the quest methods,
        # which are commands on it rather than variables of it.
        if self.xref.is_quest_ref(owner) and prop_low not in _QUEST_METHODS \
                and prop_low not in KNOWN_COMMANDS:
            return f'{self._convert_ref(owner, extends)}.{safe}'
        # Not a command anywhere: a cross-script variable read.
        if (prop_low not in KNOWN_COMMANDS
                and prop_low not in _BARE_BOOL_FUNCTIONS
                and prop_low not in _MEMBER_COMMANDS):
            return f'{self._convert_ref(owner, extends)}.{safe}'
        return self._emit_function(owner, name, extends)

    def emit_call(self, node, extends: str, *,
                  promote_subject: bool = False) -> str:
        """A command call with its receiver and arguments.

        Rebuilds the argument text and hands it to `_emit_function`, which is
        still the 201-branch chain.  R4 replaces this body with a table lookup
        over the already-parsed `node.args`, deleting the rebuild with it.
        """
        recv = _expr.emit_bare(self, node.receiver) if node.receiver else None
        args = list(node.args)
        # TES4 lets a zero-argument reference function name its SUBJECT as an
        # argument instead of a receiver: `GetDead KimFermaleRef` is
        # `KimFermaleRef.IsDead()`, not `Self.IsDead(KimFermaleRef)`.  Promote
        # it, exactly as the string path does before dispatching.
        # The gate is the BOOL table, not `_ZERO_ARG_REF_FUNCTIONS`: `getlos`
        # is in the latter but genuinely takes a target, so promoting its
        # argument made the TARGET the caster -- `GetLOS player == 1` came out
        # `player.HasLOS()` instead of `(Self as Actor).HasLOS(player)`.
        if (promote_subject and recv is None and len(args) == 1
                and isinstance(args[0], _tes4_nodes.Ident)
                and node.name.lower() in _BOOL_VALUED_FUNCTIONS
                and node.name.lower() in _ZERO_ARG_REF_FUNCTIONS):
            recv, args = args[0].name, []
        # An UNKNOWN name carrying arguments is not a call.  The string path's
        # gate requires the name to be in KNOWN_COMMANDS (or one of its extra
        # lists) before it will treat `name arg` as a command; anything else
        # falls through and the whole expression becomes a `;TODO:` comment.
        # Emitting it as a call instead produced `GetFriendHit(Player)` --
        # `undefined function`, 9 Nehrim scripts that then failed to compile.
        if args and not self._is_known_command(node.name):
            return self._unknown_command_todo(node, extends)
        # `arg_text` is the argument list as the AUTHOR wrote it, kept only
        # for the `;NE:` markers that quote the source.  Nothing decides
        # anything from it any more -- the nodes do.
        self._leading_comma = node.leading_comma
        return self._emit_function(recv, node.name, extends, args=args)

    def _is_known_command(self, name: str) -> bool:
        """Would the string path treat `name <args>` as a command call?"""
        low = name.lower()
        return (low in KNOWN_COMMANDS or low in _BOOL_VALUED_FUNCTIONS
                or low in _EXTRA_COMMAND_NAMES
                # Commands with NO Papyrus equivalent are still COMMANDS: they
                # convert to a `;NE:` marker plus `0`, not to a `;TODO:` on the
                # whole line.  Omitting this list turned `GetAVModF a b != X`
                # into `If True ;TODO:` and dropped the comparison entirely.
                or low in _BARE_NO_EQUIV_COMMANDS
                or low in _BRANCH_ONLY_COMMANDS
                or low in COMMAND_ROWS
                or bool(re.match(r'^(?:get|set)menu\w*$', low))
                or low.startswith(('con_', 'ar_', 'sv_')))

    def _unknown_command_todo(self, node, extends: str) -> str:
        """What the string path emits for an unrecognised `name <args>`."""
        return f';TODO: {_expr.emit_source(node)}'

    # ---- hooks for emit/stmt.py ------------------------------------------
    # The tree owns which statement KIND a line is; these own what each kind
    # converts to.  They delegate to the string path while R3 is verified,
    # the same staging that made R2 checkable one construct at a time.

    def _string_into_object(self, stmt) -> bool:
        """Does this assignment put a String into an object-typed variable?

        The shape an OBSE array read makes: the container is declared
        `array_var`, which has no Papyrus type and lands on String.
        """
        target = self.type_of(_expr.emit_source(stmt.target))
        if not target or target in _PAPYRUS_VALUE_TYPES:
            return False
        value = _expr.emit_source(stmt.value)
        # A cross-script read resolves on the OWNING script's table, which is
        # where an `array_var` declaration actually lives.
        return (self.type_of(value) == 'String'
                or self.remote_type_of(value) == 'String')

    def _is_obse_array(self, node) -> bool:
        """Does this expression read an OBSE `array_var`?

        The declaration maps to String for want of a Papyrus equivalent, so a
        read assigns a String into whatever the target really is.  Cross-script
        reads (`OtherScript.someArray`) count too -- that is how Morroblivion's
        werewolf scripts pass their equipment list around.
        """
        text = _expr.emit_source(node)
        if text.split('.')[-1].lower() in self._obse_arrays:
            return True
        # A cross-script read names the OWNING script first.  `script_all_vars`
        # holds each script's declarations by name, keyed by ScriptName.
        parts = text.split('.')
        if len(parts) != 2 or self.xref is None:
            return False
        owner = self.xref.script_all_vars.get(parts[0].lower(), {})
        return owner.get(parts[1].lower(), '').lower() == 'array_var'

    def emit_assignment(self, stmt, extends: str) -> str:
        """`set X to Y` / `let X := Y` / `let X += Y`."""

        # An OBSE ARRAY element write (`let arr[0] := x`).  Papyrus has real
        # arrays but no equivalent of OBSE's dynamic containers, and the
        # `ar_Construct` that built this one is already inert -- so the
        # element writes are too, rather than assigning into an undeclared
        # `arr_0_` identifier that fails the whole script.
        # A cross-script READ of a member the owning script never declares is
        # dangling in the ORIGINAL mod, exactly like the write below --
        # Morroblivion's werewolf scripts read `fbmwBMAAAImAWere.equippeditem`,
        # an OBSE `array_var` that survives only as a String.  Oblivion
        # ignored it; Papyrus fails the whole file.
        if isinstance(stmt.value, _tes4_nodes.Member):
            _read_dangling = self._dangling_cross_script_target(
                _expr.emit_source(stmt.value))
            if _read_dangling:
                return (';%s = %s  ;%s'
                        % (_expr.emit_source(stmt.target),
                           _expr.emit_source(stmt.value), _read_dangling))

        # Reading an OBSE ARRAY variable is inert for the same reason writing
        # one is: `array_var` maps to String for want of anything better, so
        # the read lands a String in whatever the target is declared as -- and
        # Papyrus refuses that outright.  The cross-script case is caught by
        # the TYPES disagreeing, since only an array read produces a String
        # where an object is declared.
        # An `Index` VALUE is always one: `arr[i]` subscripts an OBSE array,
        # which Papyrus has no equivalent for -- the subscript cannot even be
        # preserved, so the read is inert regardless of what it is assigned to.
        if (isinstance(stmt.value, _tes4_nodes.Index)
                or (isinstance(stmt.value, (_tes4_nodes.Ident, _tes4_nodes.Member))
                    and (self._is_obse_array(stmt.value)
                         or self._string_into_object(stmt)))):
            return (';%s = %s  ;NE: OBSE array read, no Papyrus equivalent'
                    % (_expr.emit_source(stmt.target),
                       _expr.emit_source(stmt.value)))
        if isinstance(stmt.target, _tes4_nodes.Index):
            return (';let %s := %s  ;NE: OBSE array write, no Papyrus '
                    'equivalent' % (_expr.emit_source(stmt.target),
                                    _expr.emit_source(stmt.value)))
        target = self._convert_ref(_expr.emit_source(stmt.target), extends)
        # The VALUE'S TYPE comes off the node, not from scanning the rendered
        # text: a command name inside a string literal cannot be mistaken for
        # a call, and arithmetic is typed by its operands rather than by
        # whether the rendering happens to contain a decimal point.
        self._value_type = _symbols.type_of_expr(stmt.value, self.type_of)
        try:
            return self._assign(stmt, target, extends)
        finally:
            self._value_type = ''

    def _assign(self, stmt, target: str, extends: str) -> str:

        # `set <ref> to GetFirstRef <type>` opens an OBSE ref-walk: remember
        # which variable it drives so the `Label` that follows emits the
        # matching `While (<ref> != None)`.
        if _call_name(stmt.value) == 'getfirstref':
            self._refwalk_var = target

        # A compound `let X += Y` expands to `X = X + Y`; Papyrus has none.
        value_node = stmt.value
        value = _expr.emit(self, value_node, extends)

        if target in ('Self', 'GetTargetActor()', 'akSpeakerRef'):
            return f';{target} = {value}  ;cannot assign to Self in Papyrus'
        # A cross-script write whose variable the owner script never declares
        # is dangling in the ORIGINAL mod, not a conversion bug: three Nehrim
        # scripts write `AutoSaveQuest.ReadyForAutosave`, which
        # AutoSaveQuestScript does not define.  Oblivion ignored it; Papyrus
        # fails the whole file ("field or property not found").
        dangling = self._dangling_cross_script_target(
            _expr.emit_source(stmt.target))
        if dangling:
            return f';{target} = {value}  ;{dangling}'
        # In AME/TopicInfo scripts, Self is the target actor, not the script;
        # akSpeakerRef is an ObjectReference and needs the cast.
        if value == 'akSpeakerRef' and extends == 'TopicInfo':
            value = '(akSpeakerRef as Actor)'
        elif value == 'Self':
            if extends == 'ActiveMagicEffect':
                value = 'GetTargetActor()'
            elif extends == 'TopicInfo':
                value = '(akSpeakerRef as Actor)'

        if value.lstrip().startswith(';TODO:'):
            ttype = self.type_of(target)
            if ttype == 'GlobalVariable':
                return f'{target}.SetValue(0)  {value}'
            dflt = '0' if not ttype or ttype in _PAPYRUS_VALUE_TYPES else 'None'
            return f'{target} = {dflt}  {value}'

        if stmt.op:
            joiner = value if not stmt.op else f'{target} {stmt.op} {value}'
            if self._is_global_target(target):
                return (f'{target}.SetValue({self._global_read(target)} '
                        f'{stmt.op} {value})')
            return f'{target} = {joiner}'

        value = self._fix_ref_zero(target, value)

        # TES4 returned the LINE DURATION from Say; Papyrus returns nothing,
        # so the assignment becomes the polyfill's measured call.  The tree
        # already separates the Say call from any `+ 2` the author added to
        # it, which is what 60 lines of balanced-paren scanning over the
        # emitted text used to recover.
        say, delay = _split_say(self, value_node, extends)
        if say is not None:
            return self._emit_say_line(target, say, delay)

        if self._is_global_target(target):
            clean = value.split(';TODO')[0].rstrip() if ';TODO' in value else value
            todo = '  ;TODO' + value.split(';TODO', 1)[1] if ';TODO' in value else ''
            return f'{target}.SetValue({clean}){todo}'

        # `set X.fQuestDelayTime to N` kicks the OWNING quest script's poll.
        # NEVER RegisterForUpdate here: that is a REPEATING zero-interval
        # registration -- OnUpdate every frame until something unregisters it
        # -- and it shipped in 45 scripts.  A single update is the TES4
        # semantics anyway: the converted OnUpdate re-arms itself, and per the
        # TES4 CS a delay of 0 means "revert to the DEFAULT 5s cadence".
        if target.endswith('.fQuestDelayTime'):
            quest_ref = target.rsplit('.', 1)[0]
            try:
                fval = float(value.strip())
            except ValueError:
                return (f'{quest_ref}.RegisterForSingleUpdate({value.strip()})'
                        f'  ;fQuestDelayTime')
            if fval <= 0:
                return (f'{quest_ref}.RegisterForSingleUpdate(5.0)'
                        f'  ;fQuestDelayTime = 0 (TES4 default cadence)')
            return (f'{quest_ref}.RegisterForSingleUpdate({fval:g})'
                    f'  ;fQuestDelayTime')

        value = self._coerce_float_to_int(target, value)
        value = self._coerce_ref_to_actor(target, value)
        # TES4 allowed storing a reference in a `short`; Papyrus does not.
        if (self.remote_type_of(target) == 'Int'
                and self._is_ref_value(value)):
            return f';{target} = {value}  ;TES4 stored ref in short'
        return f'{target} = {value}'

    def emit_command_statement(self, expr, extends: str) -> str:
        """A bare command used as a STATEMENT, not as a value.

        Routed through `emit_call` so the PARSED arguments reach the command
        layer.  The difference from value position is what `0` means: as a
        value `disableLinkedPathPoints` is `0`, as a statement it is
        `;NE: disableLinkedPathPoints` -- which is what the wrapper folds.
        """
        if isinstance(expr, _tes4_nodes.Call):
            return self._wrap_command_result(self.emit_call(expr, extends))
        # A bare identifier in statement position is a zero-argument command.
        return self._wrap_command_result(_expr.emit(self, expr, extends))

    def emit_return(self, stmt, extends: str) -> str:
        """TES4 `return` ends the block; a UDF carries its value out here."""
        if self._udf_returns:
            return f'Return {self._udf_return_value or "0"}'
        return f'{self._poll_return_prefix}Return' if self._poll_return_prefix \
            else 'Return'

    def emit_set_function_value(self, stmt, extends: str) -> str:
        """OBSE `SetFunctionValue <expr>` -- a user function's return value."""
        value = _expr.emit(self, stmt.value, extends) if stmt.value else '0'
        self._udf_returns = True
        self._udf_return_value = value
        # At the body's top level the following `return` carries the value
        # out; inside a branch it has to return where it stands.
        return '' if self._block_depth == 0 else f'Return {value}'

    def emit_jump(self, stmt, extends: str) -> str:
        """OBSE `Label <n>` / `Goto <n>` -- the head and tail of a ref-walk.

        Oblivion scans the loaded cells with
            set <ref> to GetFirstRef <type>
            Label <n>
              if ( <ref> ) ... set <ref> to GetNextRef / Goto <n> ... endif
        `Label`/`Goto` are not Papyrus keywords at all, and emitting them
        verbatim is an undefined-function error that fails the ENTIRE script
        -- which then fails every other script declaring a property of its
        type.  The Label becomes the `While` header and the Goto a no-op: the
        authored `set <ref> to GetNextRef` above it already advanced the ref
        and the header re-tests it, so the jump back is implicit.  `EndWhile`
        is emitted where the enclosing block ends (see `_close_refwalk`) --
        the Goto sits deep inside the body's `if` nest and cannot close the
        loop across them.
        """
        if isinstance(stmt, _tes4_nodes.Label):
            if not self._refwalk_var:
                # A Label with no walk in flight controls something this
                # converter cannot model; drop it rather than emit a call.
                return (f';Label {stmt.number}'
                        f'  ;NE: OBSE Label has no Papyrus equivalent')
            self._refwalk_labels.add(stmt.number)
            return (f'While ({self._refwalk_var} != None)'
                    f'  ;OBSE ref-walk (Label {stmt.number})')
        if stmt.number in self._refwalk_labels:
            return f';Goto {stmt.number}  ;OBSE ref-walk continues (loop re-tests)'
        return f';Goto {stmt.number}  ;NE: OBSE Goto has no Papyrus equivalent'

    def emit_package_test(self, recv, op: str, comparand: str,
                          extends: str):
        """`GetCurrentAIPackage <op> <PACK|type-code>`, or None if not one.

        Delegates to the string path, which owns both halves of this rule --
        the named-package equality and the numeric type-code expansion over
        the actor's own AIPackage list.  Reproducing it here would duplicate
        `_packages_of_type` and its actor resolution; R4 moves the whole rule
        onto the node instead.
        """
        if op not in ('==', '!='):
            return None
        cand = comparand.strip().strip('()').strip()
        actor = self._resolve_self_ref(recv, extends, actor_func=True)
        if actor == 'Self' and extends not in ('Actor',):
            actor = '(Self as Actor)'

        # A PACK EditorID compares exactly: vanilla Papyrus has
        # Actor.GetCurrentPackage(), so the test converts one-for-one.
        if self.xref and re.match(r'^[A-Za-z_]\w*$', cand)                 and not cand.isdigit():
            fid = self.xref.edid_to_formid.get(cand.lower(), '')
            if fid and self.xref.record_type.get(fid, '') == 'PACK':
                canon = self.xref.formid_to_edid.get(fid, cand)
                prop = _safe_property_name(canon)
                self._property_refs[prop] = 'Package'
                return f'{actor}.GetCurrentPackage() {op} {prop}'

        # A numeric TES4 package TYPE has no Papyrus counterpart -- Skyrim
        # exposes the package, not its type -- so the test expands over the
        # actor's OWN packages of that type: `== N` becomes an OR chain of
        # equalities, `!= N` an AND chain of inequalities.  Resolving the list
        # needs the actor, which is why an unresolvable one falls through to
        # the caller's ordinary emission rather than inventing a constant.
        if cand.isdigit():
            packs = self._packages_of_type(recv, int(cand))
            if not packs:
                return None
            joiner = ' || ' if op == '==' else ' && '
            terms = []
            for edid in packs:
                prop = _safe_property_name(edid)
                self._property_refs[prop] = 'Package'
                terms.append(f'{actor}.GetCurrentPackage() {op} {prop}')
            return '(' + joiner.join(terms) + ')' if len(terms) > 1                 else terms[0]
        return None

    def emit_string(self, text: str, extends: str) -> str:
        """A quoted literal: a TES4 EditorID reference, or a real string.

        TES4 let a form name be quoted wherever a form was wanted, so the
        quotes have to come off when the name resolves to a record, a local,
        or the player -- otherwise Papyrus is handed a String where a Form is
        declared.  Delegates to the string path, which owns that lookup and
        registers the property it creates.
        """
        resolved = self._resolve_name(text, extends)
        # Unchanged means it is a genuine string, not a form name.
        return resolved if resolved != text else text

    def emit_array_read(self, owner: str, extends: str) -> str:
        """OBSE array element read: emit the base variable, drop the subscript.

        Papyrus has no `array_var`, and the subscript cannot be preserved.
        The base name is kept rather than a `0` marker because the comparand
        is often a typed form -- `0 == <Spell>` is a compile error, while
        `spells == <Spell>` builds (Morroblivion's blight-cure scripts).
        """
        return self._resolve_name(owner, extends)

    def remote_type_of(self, dotted: str) -> str:
        """Type of `Var` in `Owner.Var`, resolved on the script Owner is typed as.

        TES4 let one script read another's variables directly.  The owner is a
        property typed `TES4_<script>`, so the remote script's variable table
        answers what the member is -- three sites resolved this identically by
        hand.
        """
        if '.' not in dotted or not self.xref:
            return ''
        owner, _, member = dotted.partition('.')
        owner_type = self.type_of(owner.strip(), locals_first=False)
        if not owner_type.startswith('TES4_'):
            return ''
        return self.xref.script_all_vars.get(
            owner_type[5:].lower(), {}).get(member.lower(), '')

    def type_of(self, name: str, *, locals_first: bool = True) -> str:
        """Papyrus type carried by `name`, or '' if it is not declared here.

        `_property_refs` is keyed by the AUTHORED spelling but Papyrus is
        case-insensitive, so the lowercase fallback is mandatory -- 16 sites
        wrote this chain by hand and did not all agree on it.  Pass
        `locals_first=False` where a local of the same name must be ignored.
        """
        low = name.lower().split('.')[-1]
        if locals_first:
            local = self._var_types.get(low, '')
            if local:
                return local
        return self._property_refs.get(name, self._property_refs.get(low, ''))

    def _property_type_ci(self, name: str) -> str:
        """`type_of` for a property, matching ANY case spelling of the key.

        `_property_refs` is keyed by the AUTHORED spelling and mutated at 92
        sites, so a maintained side index would drift.  Only the cross-script
        resolvers want this: registering a property goes through `type_of`,
        and making THAT case-insensitive stopped a second spelling from ever
        being added -- which changed the emitted property name from the
        script's `TG02Taxes` to the record's `TG02taxes` (2 files).  Sorted so
        the answer cannot depend on insertion order.
        """
        exact = self.type_of(name, locals_first=False)
        if exact:
            return exact
        want = name.lower()
        return next((t for k, t in sorted(self._property_refs.items())
                     if k.lower() == want), '')

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

    # Where the speak-as identity sits in a TES4 Say/SayTo argument list:
    #   Say   <topic> <force-subtitles> <speak-as> [<in-players-head>]
    #   SayTo <target> <topic> <flag> <speak-as> <flag>

    def _say_speak_as(self, ref_name, pparts: list, fname_low: str) -> tuple:
        """(speaker property, in-head) for a speak-as call site.

        TES4's third `Say` argument is the identity the line belongs to; the
        receiver only emits the sound.  Skyrim has no equivalent parameter and
        keys voice lookup on the SPEAKER, so the emitting marker (a STAT, with
        no voice type) resolves to no voice folder and the line is silent.
        The importer places a TACT carrying that NPC's voice type at the
        emitter's authored position and registers it under the speaker name --
        see tes5_import/speaker_activators.py, which derives the SAME name
        from the same authored pair, so the two agree with no side-channel.

        The fourth authored argument is TES4's "speak in the player's head",
        which Skyrim exposes natively as `Say`'s third parameter
        (abSpeakInPlayersHead) -- passed straight through by
        TES4Polyfill.SpeakAs.  🛑 Never emulate it by moving the speaker.

        Returns ('', False) when this is not a speak-as site.
        """
        none = ('', False)
        if not ref_name:
            return none
        need = SAY_SPEAKAS_MIN_TOKENS.get(fname_low)
        if need is None:
            return none
        tokens = []
        for part in pparts:
            tokens.extend(str(part).split())
        if len(tokens) < need:
            return none
        # The identity is the first non-numeric token after the topic; the
        # in-head flag is the numeric token after it.
        rest = tokens[2:] if fname_low == 'sayto' else tokens[1:]
        topic = tokens[1] if fname_low == 'sayto' else tokens[0]
        voice = ''
        in_head = False
        for i, t in enumerate(rest):
            if t and not t.lstrip('-').replace('.', '').isdigit():
                voice = t
                nxt = rest[i + 1] if i + 1 < len(rest) else ''
                in_head = bool(nxt) and nxt.lstrip('-').isdigit() and int(nxt) != 0
                break
        if not voice or not re.fullmatch(r'\w+', voice):
            return none
        topic = topic.strip().strip('"')
        if not re.fullmatch(r'\w+', topic):
            return none
        # Only an actor BASE is a speak-as identity; anything else in that slot
        # is a flag or a stray token.  And only a real DIAL is a topic.
        if self.xref:
            fid = self.xref.edid_to_formid.get(voice.lower(), '')
            if not fid or self.xref.record_type.get(fid, '') not in ('NPC_',
                                                                     'CREA'):
                return none
            tfid = self.xref.edid_to_formid.get(topic.lower(), '')
            if not tfid or self.xref.record_type.get(tfid, '') != 'DIAL':
                return none
        speaker = _safe_property_name(
            f'TES4Voice_{ref_name.lower()}_{voice.lower()}')
        self._property_refs[speaker] = 'ObjectReference'
        return speaker, in_head

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
        #: Lowercased OBSE user-function parameter names of the
        #: script being converted; see `_param_type`.
        self._udf_params: set = set()
        self._property_refs = {}
        # OBSE `array_var` declarations in the script being converted; a
        # read of one is inert (see `_is_obse_array`).
        self._obse_arrays = set()
        # Scoped to ONE fragment: each caller installs the map built from that
        # fragment's own SCRO table before converting it.
        self._scro_aliases = {}
        self._cell_families = {}
        self._has_gamemode = False
        self._has_menumode = False
        self._has_scripteffectupdate = False
        self._uses_getsecondspassed = False
        self._gsp_realtime = False
        self._uses_hour_window = False
        self._uses_timer = False
        self._uses_say_timer = False
        self._uses_say = False
        # Per-script: a latch registered while converting one script must not
        # leak a declaration into the next (see _guard_stage_timer).
        self._stage_latches = {}
        # Set while a GameMode poll body is being converted: the arm a TES4
        # `return` must emit before `Return` (see the OnUpdate emitter).
        self._poll_return_prefix = ''
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
        # Chargen-menu call sites converted in this script (unique local
        # variable names per site), and whether the re-entrancy latch
        # declaration is due.  The converter instance is reused across
        # scripts/fragments, so a leaked True would emit the latch into
        # unrelated scripts; fragment assemblers (the QF writer) accumulate
        # the flag across their fragments themselves.
        self._chargen_menu_seq = 0
        self._uses_chargen_menus = False
        # Parameter types of this script's OBSE user function, in order;
        # None when it declares no TES4Call at all (see symbols.ScriptSymbols).
        self.udf_signature = None
        # Every `<prop>.TES4Call(...)` this script emits, as (property, args).
        # The callee's signature is unknown until every script has converted,
        # so the CASTS are applied afterwards -- from this list rather than by
        # re-reading the generated file (see pipeline._fix_udf_call_arg_types).
        self.udf_calls: list = []



    # The condition under which a placed reference's TES4 `begin GameMode` body
    # would run, used at every site that arms the OnUpdate poll for an
    # object/actor script.
    #
    # The gate is CELL-SCOPED (attached parent cell), matching TES4, with
    # Is3DLoaded() only as a fast path.  It must satisfy TWO independent
    # requirements, and an implementation meeting just one is silently broken:
    #
    # 1. Never throw on a held item (see the container note below).
    # 2. Stay true for a reference with no 3D.  A disabled ref, or one whose
    #    own poll body calls Disable(), keeps ticking in TES4.  Gating on 3D
    #    alone deadlocks both the self-ENABLE idiom (~200 Nehrim refs incl.
    #    Celebro) and the self-DISABLE one — Nehrim MQ00LichtScript disables
    #    itself, then five seconds later fires the plugin's only
    #    `SetStage MQ00 2`, whose result script holds the only
    #    EnablePlayerControls.  A 3D gate pins that quest at stage 1 with the
    #    player's controls locked forever.
    #
    # This was once reverted to a 3D-only gate to chase a CharacterGen
    # regression (Valen Dreth not reaching his taunt marker).  That was a
    # misattribution: Dreth is fixed by the UNGATED OnLoad emitted above, which
    # this gate does not affect.
    # 🛑 NOT a bare Is3DLoaded().  An item sitting INSIDE A CONTAINER has no
    # 3D and no parent cell, and calling Is3DLoaded() on it raises
    #   "Unable to call Is3DLoaded - no native object bound to the script
    #    object, or object is of incorrect type"
    # which ABORTS THE WHOLE OnUpdate EVENT AT THAT LINE -- including the
    # RegisterForSingleUpdate that keeps the poll alive.  The script is then
    # dead for the rest of the save.  Measured in the user's Papyrus.0.log
    # (2026-08-17): 17 aborted OnInit/OnUpdate passes on the CharacterGen
    # Blades equipment inside Glenroy (1A032A16) and Renault (1A032A15).
    #
    # TES4Polyfill.SafeGameModeGate does the container test FIRST
    # (GetParentCell() == None is safe on an inventory item and never throws)
    # and only calls Is3DLoaded() on a reference that is actually in a cell.
    # 1,111 converted scripts gate their poll re-arm on this, so a throw here
    # is a silent, permanent loop death across the whole plugin.
    _GAMEMODE_GATE = 'TES4Polyfill.SafeGameModeGate(Self)'

    def _get_update_interval(self) -> str:
        if self._uses_getsecondspassed:
            return '0.1'
        # A script that drives a spoken line with a timer (`set T to Say ...`)
        # ticks fast: its `T <= 0` guard is what starts the next line, so the
        # tick is pure dead air between lines (TES4 ran it every frame).
        #
        # 0.1s was tried on 2026-08-16 and measurably LENGTHENED the gaps, so
        # it was set to 0.25s.  That measurement was taken when every SayLine
        # also blocked on Utility.Wait(0.05) for its claim handshake and
        # Utility.Wait(0.25) after any busy wait, and when fragments blocked
        # the dispatch path -- the VM was saturated by the Say path itself and
        # extra poll passes queued behind it.  All three are gone, so the
        # contention that made a faster tick counterproductive is gone with
        # them, and 0.15s buys back most of the tick latency without
        # returning to the 0.1s that was measured as too aggressive.
        if self._uses_say_timer:
            return '0.15'
        if self._uses_timer:
            return '0.25'
        return '0.5'

    def _tree_bodies(self) -> list:
        """Every statement list in the parsed script, blocks included."""
        t = self._tree
        return [t.preamble, t.body] + [b.body for b in t.blocks] if t else []

    def _parse_source(self, source: str):
        """Parse Oblivion source into (variables, blocks).

        `blocks` is `(type, filter, STATEMENT NODES)`.  It used to hand back
        the SOURCE LINES between each `begin` and its `end`, reconstructed by
        counting keywords -- so the converter parsed the script, threw the
        body away and converted text line by line.  That is the whole reason
        the string layer existed: a line on its own carries no structure, so
        nesting, block balance and dead-code-after-return all had to be
        re-derived from the emitted output afterwards.

        The parser already owns the body, so it is handed over directly.
        """
        from script_convert.tes4.parser import Mode, parse
        try:
            tree = parse(source, Mode.SCRIPT)
        except Exception:
            self._tree = None
            return [], []
        self._tree = tree
        # OBSE `array_var` declarations, so a read of one can be neutralised:
        # the type maps to String for want of a Papyrus equivalent, which
        # otherwise lands a String in whatever the target is declared as.
        self._obse_arrays = {v.name.lower() for v in tree.variables
                             if v.vtype.lower() == 'array_var'}
        variables = [(v.vtype, v.name) for v in tree.variables]
        return variables, [(b.btype, b.filter, b.body) for b in tree.blocks]

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

    def _gamesetting_write(self, setting: str, value: str, extends: str) -> str:
        """A runtime GMST write, re-expressed as the actor value it changes."""
        av = GMST_TO_ACTOR_VALUE.get(setting.lower())
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

    @staticmethod
    def _is_oblivion_gate_entry(blocks) -> bool:
        """True when this script's OnActivate carries the player INTO a realm.

        The authored indicator is `set MQ00.nearOblivionGate to 0` inside a
        player-guarded OnActivate.  That line exists for one reason: the ref
        is an Oblivion gate, and the player who just activated it is being
        taken through to the realm, so the "player is standing near a gate"
        tracking variable no longer applies ("we aren't 'near' any gate
        anymore -- we're in Oblivion!" is Bethesda's own comment on it).

        It is the only moment in the game where the identity of the gate the
        player entered is known, and the authored code DISCARDS it on that
        very line -- so the capture has to be injected ahead of the clear.

        Matching the authored write rather than a script name keeps this
        generic: any plugin that adds its own gate follows the same idiom
        (all five vanilla gate scripts do, and nothing else in Oblivion.esm
        writes that variable to 0).
        """
        for btype, _bf, body in blocks:
            if btype != 'onactivate':
                continue
            names = {n.called for n in _tes4_nodes.walk_exprs_in(body)}
            if 'isactionref' not in names:
                continue
            for st in _tes4_nodes.walk_stmts(body):
                if not isinstance(st, _tes4_nodes.Assign):
                    continue
                tgt = st.target
                if (isinstance(tgt, _tes4_nodes.Member)
                        and tgt.name.lower() == 'nearobliviongate'
                        and isinstance(st.value, _tes4_nodes.Literal)
                        and st.value.text.strip() == '0'):
                    return True
        return False

    @staticmethod
    def _onactivate_consumes(blocks) -> bool:
        """True when an OnActivate body has a path that CONSUMES activation.

        In TES4 the block replaces default activation, so any path that does
        not execute a bare `Activate` swallows the click.  A body whose bare
        `Activate` sits at nesting depth 0 runs it on every path -- a pure
        passthrough (AutoClosingDoor et al.) that needs no blocking.  Only a
        missing or conditionally-guarded `Activate` needs BlockActivation for
        Skyrim to honour the consume.  (`X.Activate` -- activating some OTHER
        ref -- is not a passthrough and does not count.)

        "At depth 0" is a TREE question: a statement in the block's own body
        rather than inside an If or a While.  Counting `if`/`endif` keywords
        across source text answered it before.
        """
        consumes = False
        for btype, _bf, body in blocks:
            if btype != 'onactivate':
                continue
            top_level_activate = any(
                isinstance(st, _tes4_nodes.ExprStmt)
                and st.expr.called == 'activate'
                and st.expr.receiver is None
                for st in body or ())
            if not top_level_activate:
                consumes = True
        return consumes

    @staticmethod
    def _inject_block_activation(out: list) -> list:
        """Insert BlockActivation(true) on 3D load.

        Called for ObjectReference/Actor scripts whose OnActivate consumes the
        activation — see the caller comment for the TES4 semantics this
        restores.  Vanilla defaultBlockActivation.psc applies the call from
        OnLoad ("block activation upon loading"), so the call rides an
        existing OnLoad when one was emitted (inserted before any gating If,
        since blocking must be unconditional), else a fresh OnLoad is
        appended.
        """
        for i, line in enumerate(out):
            if line.strip() == 'Event OnLoad()':
                out.insert(i + 1, '  BlockActivation(true)')
                return out
        out.append('Event OnLoad()')
        out.append('  BlockActivation(true)')
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

    def _wrap_command_result(self, result: str) -> str:
        """Fold accumulated `;NE:` notes into a converted command statement.

        The tail of `_convert_line`, for the node path.  In VALUE position a
        command with no equivalent reads as `0`; in STATEMENT position that
        bare `0` is not a statement at all, so it is replaced by the note --
        which is why the two positions cannot share a return value and why the
        node path needs this wrapper rather than the raw `emit_call` result.
        """
        result = self._guard_stage_timer(result)
        if self._line_comments:
            comments = '  '.join(self._line_comments)
            self._line_comments.clear()
            if result.strip() == '0':
                return comments
            if not result.lstrip().startswith(';'):
                return f'{result}  {comments}'
        return result

    #: Statement kinds the node path owns.  Everything else -- the OBSE
    #: ref-walk, `foreach`, an array element write -- still needs converter
    #: state the tree does not carry, so it falls through to the chain.

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
        if vtype:
            # Any OBJECT type refuses an integer; the value types keep it.
            # Enumerating the object types instead missed every one the
            # pre-emission resolver now assigns (Form, Cell, Armor, Spell...),
            # and `Armor x = 0` does not compile.
            return 'None' if vtype not in _PAPYRUS_VALUE_TYPES else value
        # Check property refs (cross-script variables) only if not a declared var
        ptype = self.type_of(target, locals_first=False)
        if ptype and ptype not in _PAPYRUS_VALUE_TYPES:
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
    # DERIVED from PAPYRUS_BOOL_FUNCTIONS, not written out again.  This was a
    # second hand-kept list of the same fact and the two had drifted apart by
    # twelve names, so a Bool got its `as Int` or not depending on which list
    # the code path consulted -- `Temp = Player.IsDetectedBy(x)` compiled or
    # did not for that reason alone.  Longest-first so the alternation cannot
    # match a prefix of a longer name.
    _BOOL_FUNC_NAMES = '|'.join(
        sorted((re.escape(n) for n in PAPYRUS_BOOL_FUNCTIONS),
               key=len, reverse=True))
    _ARGS = r'(?:[^()]|\([^()]*\))*'      # args, allowing one nesting level
    _BOOL_CMP_RE = re.compile(
        r'((?:\w+(?:\(' + _ARGS + r'\))?\.)*'              # optional receiver chain
        r'(?:' + _BOOL_FUNC_NAMES + r')'
        r'\s*\(' + _ARGS + r'\))'                          # the call itself
        r'(\s*(?:>=|<=|>|<)\s*-?\d+(?:\.\d+)?)',           # relational op + number
        re.IGNORECASE)

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

    # `timer <= -5` — the chime latch's expiry test.  The sentinel is negative
    # because the countdown runs past zero; its magnitude is how many REAL
    # seconds the latch holds.
    _LATCH_EXPIRY_RE = re.compile(
        r'^(?P<head>.*?\b\w[\w.]*\s*<=\s*)-(?P<secs>\d+(?:\.\d+)?)(?P<tail>\s*\)?\s*)$')


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
            vtype = self.type_of(target, locals_first=False)
        if not vtype:
            vtype = self.remote_type_of(target)
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
        # What TYPE the value carries is a question about the expression, and
        # `symbols.type_of_expr` answered it from the parse tree before any of
        # this text existed.  The four scans this replaces -- a Float-function
        # regex, a `\d+\.\d+` literal probe, an identifier sweep looking up
        # each name, and a Bool-function regex -- were all re-deriving that
        # from the rendering, where a name inside a string literal counts and
        # arithmetic is invisible.
        if self._value_type not in ('Float', 'Bool'):
            return value
        # `as` binds tighter than the arithmetic operators, so only an
        # expression with a top-level operator needs the extra parentheses.
        if self._value_type == 'Float' and re.search(r'[+\-*/]', value):
            return f'({value}) as Int'
        return f'{value} as Int'

    # ObjectReference event parameter names that may need Actor cast

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
            val_low in OBJREF_PARAMS
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
        # `Self` in a non-Actor script, and a script-typed property, are both
        # ObjectReference-shaped values that a declared Actor refuses.
        if not is_objref_value and (val_stripped == 'Self'
                                    or self.type_of(val_stripped)
                                    .startswith('TES4_')):
            is_objref_value = True
        if not is_objref_value:
            return value
        tgt_low = target.lower().split('.')[-1]
        vtype = self._var_types.get(tgt_low, '')
        if vtype in ('Actor', 'ActorBase') or vtype.startswith('TES4_'):
            return f'{value} as Actor'
        # Check property refs too
        ptype = self.type_of(target, locals_first=False)
        if ptype in ('Actor', 'ActorBase') or (ptype and ptype.startswith('TES4_')):
            return f'{value} as Actor'
        # A remote `ref` var is only declared Actor when the remote script
        # itself calls an Actor-only method on it.  Casting unconditionally
        # "for safety" was unsafe in the other direction: MQ16 assigns two
        # static markers into MQ16OblivionGate1Script.mySpawnMarker, which that
        # script only ever calls PlaceAtMe on, so it stays ObjectReference --
        # and `marker as Actor` fails the downcast and stores None, leaving
        # both endgame Oblivion gates spawning nothing.
        if self.remote_type_of(target) == 'ObjectReference':
            owner, _, member = target.partition('.')
            owner_type = self.type_of(owner.strip(), locals_first=False)
            if member.lower() in self.xref.script_actor_vars.get(
                    owner_type[5:].lower(), ()):
                return f'{value} as Actor'
        return value

    def _resolve_name(self, expr: str, extends: str) -> str:
        """What does this IDENTIFIER mean in this script?

        A local, a quoted EditorID, a zero-argument command, a known global,
        an actor value, a record the plugin defines -- resolved once, here.

        This was the whole pre-tree expression SCANNER.  Everything structural
        in it has gone to the parser (operators, calls, nesting, the dotted
        name), and what the tree cannot answer is exactly this: a bare name is
        a variable, a form or a command depending on what the plugin declares,
        which is a LOOKUP rather than a parse.
        """
        expr = expr.strip()
        if not expr:
            return expr

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



        # Fix spaces around dots in method chains (e.g. "Player. GetItemCount" → "Player.GetItemCount")
        expr = re.sub(r'(\w)\.\s+(\w)', r'\1.\2', expr)
        # Handle ref.Func in expressions (only if no parens yet — avoid re-matching)
        # Require ref to start with a letter (not digit) to avoid matching floats like 0.5
        # The ref may START WITH A DIGIT — Papyrus forbids it but TES4 EditorIDs
        # do not, and Nehrim names many refs `1TrapFireMineWorldRef`.  A pure
        # number before the dot is excluded so float literals (0.5) stay literals.
        # Bare function names used as values (no ref, no args)
        # e.g. "getParentRef" -> "GetLinkedRef()", "GetActionRef" -> "akActionRef"
        #
        # A LEADING DIGIT is allowed: Papyrus forbids it, but TES4 EditorIDs do
        # not, and Nehrim names hundreds of forms `1Feuerball`, `01SetBonus...`.
        # Those still have to reach the EditorID lookup below, which renames them
        # via _safe_property_name to match the emitted property declaration —
        # otherwise the call site keeps the raw name and nothing resolves.
        # Pure numbers are excluded so numeric literals fall through untouched.
        # A pure NUMBER is a literal, not a name.
        if expr.isdigit():
            return expr
        low = expr.lower()
        # Local variables ALWAYS take priority over function name matching
        if low in self._local_vars:
            safe = self._var_renames.get(low, expr)
            return safe
        # Special bare identifiers
        if low in ('getactionref', 'isactionref'):
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
        if low in ('isanimplaying', 'getiscreature', 'iscreature',
                        'hasvampirefed', 'isspelltarget', 'isguard',
                        'getnextref', 'isowner', 'getbaseobject',
                        'isonground', 'isthirdperson',
                        # Verified against the export corpus as spellings
                        # that really are read bare somewhere in a plugin.
                        'isplayerinjail', 'getpcinfamy', 'getrestrained',
                        'ispcamurderer', 'getcrimegold', 'getpcfame',
                        'gettalkedtopc', 'payfine',
                        ) or low in _FORM_TYPE_TESTS:
            return self._emit_function(None, expr, extends)
        if low == 'isxbox':
            return 'False'
        if low in ('getdayofweek', 'getdayoftheweek'):
            # Route to COMMAND_ROWS instead of emitting a second
            # spelling here.  This copy emitted `GetValue() as Int` where
            # the table emits `GetValueInt()`, so ONE command had two
            # conversions depending on whether it was written bare or as a
            # call -- and the `as Int` form then attracted a SECOND cast on
            # assignment, yielding `(... as Int) % 7 as Int`.
            return self._emit_function(None, expr, extends)
        if low in ('getrandompercent', 'getrandpercent'):
            return 'Utility.RandomInt(0, 99)'
        if low in ('getcurrenttime', 'gamehour'):
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
        if low == 'getpcfame':
            self._property_refs['TES4Fame'] = 'GlobalVariable'
            return 'TES4Fame.GetValueInt()'
        if low in ('getpcinfamy', 'getinfame'):
            self._property_refs['TES4Infamy'] = 'GlobalVariable'
            return 'TES4Infamy.GetValueInt()'
        if low in ('isplayerinprison', 'getplayerinjail', 'isplayerinjail',
                        'senttojail'):
            return 'Game.GetPlayer().IsArrested()'
        if low in ('getpcissleeping', 'ispcsleeping', 'isplayersleeping'):
            # Inside a sleep-idiom MenuMode body the read means "is this a
            # sleep frame" — that's the script-managed flag.  Elsewhere
            # (GameMode) Oblivion never ran while sleeping, so a raw
            # GetSleepState() read (0 when awake) keeps the same truth.
            if getattr(self, '_in_sleep_menumode', False):
                return 'TES4_PCSleeping'
            return 'Game.GetPlayer().GetSleepState()'
        if low == 'isininterior':
            if extends == 'ActiveMagicEffect':
                return 'GetTargetActor().GetParentCell().IsInterior()'
            return 'Self.GetParentCell().IsInterior()'
        if low in ('getdisabled', 'isdisabled'):
            # Through the polyfill, not the bare native: a DESTROYED
            # reference must not report as disabled.  Oblivion keeps the
            # two as independent bits and closing a gate sets only
            # destroyed, so its poll preambles (`if getdisabled == 1 /
            # return`, above the `getdestroyed` setstage) were never meant
            # to fire for a closed gate.  Our removal has to Disable(),
            # which would otherwise strand every such setstage.
            ref = 'Self' if extends != 'ActiveMagicEffect' else 'GetTargetActor()'
            return (f'TES4Polyfill.GetDisabled({ref}, '
                    f'{self._destroyed_formlist()})')
        if low == 'getdestroyed':
            # NOT IsDisabled() and NOT GetCurrentDestructionStage().
            # Destroyed, disabled and destruction-STAGE are three different
            # engine states.  Skyrim exposes no reader for the destroyed
            # flag at all, and this conversion writes no DEST subrecord, so
            # GetCurrentDestructionStage() is 0 for every converted record
            # -- a read that can never become true.  That is what broke
            # every quest advancing off its own destruction: MS48's Kvatch
            # gate reads `if getdestroyed == 1 && getstage ms48 < 50 /
            # setstage ms48 50`, the ONLY setstage 50 in the chain, so the
            # quest pinned at stage 10 (measured live 2026-08-27).
            # The polyfill reads the FormList its SetDestroyed mirrors into.
            ref = 'Self' if extends != 'ActiveMagicEffect' else 'GetTargetActor()'
            return (f'TES4Polyfill.GetDestroyed({ref}, '
                    f'{self._destroyed_formlist()})')
        # Handle bare function references that need special handling
        if low == 'getbuttonpressed':
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
        if low in ('getcrimegold',):
            self._property_refs['TES4CyrodiilCrimeFaction'] = 'Faction'
            return 'TES4CyrodiilCrimeFaction.GetCrimeGold()'
        if low in ('getdisposition',):
            return '50'
        # Only the ARGUMENT-LESS spelling lands here, and with no target
        # named there is nothing to ask IsDetectedBy about (same reasoning
        # as IsActorDetected).  The real one-argument form — which is what
        # all 56 sites in the plugin use — is handled in _emit_function and
        # maps onto IsDetectedBy.
        if low == 'getdetectionlevel':
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
        if low == 'getcontainer':
            actor_param = self._current_event_actor_param()
            if actor_param:
                return actor_param
            # A COMPARISON against GetContainer is answered on the BinOp
            # before this operand is ever emitted (emit/expr._get_container),
            # so reaching here is a bare read -- `set ref to GetContainer`.
            # Papyrus cannot walk from an item to its container at all, so the
            # honest value is None; emitting a placeholder only moved the
            # failure to the compiler.
            self._line_comments.append(
                ';TODO: GetContainer has no Papyrus equivalent')
            return 'None'
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
        if low in ('ispcamurderer', 'ispcanmurderer',
                        'getpcismurderer'):
            self._property_refs['TES4CyrodiilCrimeFaction'] = 'Faction'
            return (f'(TES4CyrodiilCrimeFaction.GetCrimeGoldViolent() '
                    f'>= {TES4_MURDER_BOUNTY})')
        if low in ('getisalerted', 'israining', 'menumode',
                        'istimepassing', 'getplayerinseworld',
                        'getcurrentaiprocedure', 'getcurrentaipackage',
                        'getiscurrentpackage', 'isidleplaying',
                        'getbookread', 'gettalkedtopc',
                        'getcrimeknown', 'getstartingpos',
                        'getisplayerbirthsign',
                        'hasbeenpickedup',
                        'getgameloaded', 'hasvariable', 'getownership',
                        'isonguard', 'isindangerouswater',
                        'getarmorrating', 'isspelltarget', 'isswimming',
                        'isactor', 'getspellcount',
                        'getrestrained',
                        'getpcfactionattack', 'getpcfactionsteal',
                        'getpcfactionmurder'):
            return '0'
        if low == 'reset':
            if extends == 'ActiveMagicEffect':
                return 'GetTargetActor().Reset()'
            if extends == 'TopicInfo':
                return 'akSpeakerRef.Reset()'
            return 'Self.Reset()'
        # Only check the command table if NOT a declared local variable
        if low not in self._local_vars:
            # A handler-only name normally falls through on purpose: bare
            # reads like getSecondsPassed are rewritten by dedicated later
            # passes, and routing them here TODO's them mid-expression,
            # leaving `timer = timer - `.  The commands below have no such
            # pass and no same-named form, so they must be routed or they
            # survive into the output as undefined identifiers.
            if (low in COMMAND_ROWS
                    or (low in HANDLED_COMMANDS
                        and low in _BARE_NO_EQUIV_COMMANDS)):
                return self._emit_function(None, expr, extends)
            # Prefix-matched no-equivalent families (OBSE menu/UI, console
            # commands, array/string helpers).  These have handlers in
            # _emit_function but deliberately no table entry — one per
            # variant would have to be added by hand, and every one missed
            # becomes an undefined identifier at compile time.
            if (re.match(r'^(?:get|set)menu\w*$', low)
                    or low.startswith(('con_', 'ar_', 'sv_'))):
                return self._emit_function(None, expr, extends)
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
                low = edid.lower()
                expr = edid
        # Check if it's a known EditorID -> property ref.  Falls back to the
        # digit-stripped spelling for the same reason _convert_ref does: a
        # Papyrus identifier cannot start with a digit, so a Morroblivion
        # record named `0<name>` arrives here already stripped and the
        # direct lookup misses.  Without this an operand like
        # `AddItem(dwemerUshieldUbattleUunique, 1)` declared no property and
        # the name reached the compiler undefined.
        fid = self.xref.edid_to_formid.get(low, '')
        if not fid:
            fid = _digit_stripped_formid(self.xref, low)
        if not fid:
            # Stale source spelling — recover it from this record's SCRO
            # table (see register_scro_alias_pool).
            _alias = self._scro_alias_for(expr)
            if _alias:
                expr, low = _alias, _alias.lower()
                fid = self.xref.edid_to_formid.get(low, '')
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
        # Everything below is a WHOLE-NAME match.  These were `re.sub`
        # word-substitutions over an expression; the tree hands over one
        # identifier, so the substitution is an equality test.
        if low in ('player', 'playerref') and low not in self._local_vars:
            return 'Game.GetPlayer()'
        # In an ActiveMagicEffect / TopicInfo script, `Self` and `GetSelf`
        # name the reference the effect or topic acts on, not the script.
        if low in ('getself', 'this', 'self'):
            return self._self_reference(extends)
        # GetSecondsPassed is the time since the last pass.  In a script with
        # a GameMode/ScriptEffectUpdate poll it becomes TES4_SecondsPassed, a
        # variable the OnUpdate prologue fills with the MEASURED elapsed time
        # -- a fixed per-tick constant assumed every tick took exactly the
        # registration interval, so under VM load every timer drained slower
        # than real time.  Outside such a script no prologue exists, so the
        # interval constant remains, and it MUST equal the interval the script
        # actually runs at: a 0.5 literal at a 0.1s tick made every converted
        # timer run 5x fast (Valen Dreth's 10s taunt pause became 2s).
        if low in ('getsecondspassed', 'scripteffectelapsedseconds'):
            return str('TES4_SecondsPassed' if self._gsp_realtime
                       else self._get_update_interval())
        if low in KNOWN_GLOBALS:
            canonical = _canonical_global(expr)
            self._property_refs[canonical] = 'GlobalVariable'
            return f'{canonical}.GetValue()'
        if low in _ACTOR_VALUE_MAP_LOW:
            return _ACTOR_VALUE_MAP_LOW[low]
        return self._var_renames.get(low, expr)

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
        if low in SELF_NAMES:
            return self._self_reference(extends)

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
        if not fid:
            # Stale source spelling: recover the record the ORIGINAL compiler
            # bound, from this record's SCRO table (see register_scro_alias_pool).
            alias = self._scro_alias_for(name)
            if alias:
                name, low = alias, alias.lower()
                fid = self.xref.edid_to_formid.get(low, '')
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

    def arg_srcs(self) -> list:
        """Every argument as AUTHORED source text."""
        return [_expr.emit_source(a).strip() for a in self._arg_nodes]


    def arg_sources(self) -> list:
        """The current call's arguments as unconverted TES4 source text."""
        return [_expr.emit_source(a) for a in self._arg_nodes]

    def arg_src(self, n: int, default: str = '') -> str:
        """The nth argument as AUTHORED source text, or `default` if absent."""
        nodes = self._arg_nodes
        if n >= len(nodes):
            return default
        return _expr.emit_source(nodes[n]).strip() or default

    def arg_expr(self, n: int, extends: str, default: str = '') -> str:
        """The nth argument CONVERTED to Papyrus, or `default` if absent."""
        nodes = self._arg_nodes
        if n >= len(nodes):
            return default
        return _expr.emit(self, nodes[n], extends)

    def has_args(self) -> bool:
        """Was the current call written with any arguments?"""
        return bool(self._arg_nodes)

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
                        rest = f', {self.arg_expr(1, extends)}'
            return f'"{sk_av}"{rest}'

        # Default: Oblivion scripts use both "func arg1 arg2" and
        # "func arg1, arg2".  Split QUOTE-AWARE — a plain comma/whitespace split
        # tore filenames apart: `IsModLoaded "Voice Overs V002.esp"` became
        # three arguments and emitted the nonsense
        # `IsModLoaded("Voice, Overs, V002.esp(")`, which then converted to a
        # bare `If True` and fired the mod's "deprecated plugin detected"
        # warning unconditionally.  _split_obse_args already suppresses
        # splitting inside string literals, parens and brackets.
        parts = self.arg_srcs()
        converted = [self.arg_expr(i, extends) for i in range(len(parts))]
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

    # Descending (floor, tier) ladders for the actor values whose TES5 tier is
    # NOT a proportional bucket.  The last row must be a catch-all, since the
    # caller has already rejected `raw < 0`.  The reasoning for each threshold
    # is in `_scale_enum_av` -- these mirror `_convert_aidt` in
    # tes5_import/record_types/actors.py so a scripted change lands on the same
    # tier the NPC's AIDT was converted to.
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

    def _scale_enum_av(self, sk_av: str, value_src: str):
        """Map a TES4 0-100 trait value onto its TES5 enum tier.

        Returns None when this is not an enum-valued actor value, or when the
        operand is not a literal (a variable cannot be bucketed at conversion
        time), so the caller falls back to normal expression conversion.
        """
        max_tier = ENUM_ACTOR_VALUES.get(sk_av.lower())
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
        ladder = ENUM_AV_LADDERS.get(sk_av.lower())
        if ladder is None:
            # Generic 0-100 → 0..max_tier proportional bucket.
            tier = int(round((min(raw, 100.0) / 100.0) * max_tier))
        else:
            tier = next(t for floor, t in ladder if raw >= floor)
        return str(max(0, min(max_tier, tier)))

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

        A flip naming the TES4 PlayerFaction is mirrored onto the vanilla
        PlayerFaction: the runtime player was never a member of the
        converted record, so the original write reaches nobody.  The mirror
        covers all three tiers — the neutral clear included, because TES4
        uses `setfactionreaction X PlayerFaction 0` mid-scene to stand a
        group down from hunting the player (CharacterGen stage 23), and a
        clear that reaches nobody leaves the real player hunted.

        Nothing else is pushed here.  SetEnemy/SetAlly write the same Group
        Combat Reaction enum vanilla's own scripted battles run on; with the
        package interrupt flags authorising combat behaviour (see
        pack_converter.DEFAULT_INTERRUPT) the engine initiates the fight
        itself, exactly as it does for its own factions.
        """
        literal = amount_src.strip().rstrip(',').strip()
        if not re.match(r'^-?\d+(?:\.\d+)?$', literal):
            return None
        amount = float(literal)

        def _mirror(mode: int) -> str:
            if f1.lower() == 'playerfaction':
                return ('\n  TES4Polyfill.MirrorPlayerFactionRelation('
                        f'{f2}, {mode})')
            if f2.lower() == 'playerfaction':
                return ('\n  TES4Polyfill.MirrorPlayerFactionRelation('
                        f'{f1}, {mode})')
            return ''

        if is_mod:
            # A relative nudge: treat any negative shift as souring the
            # relation and any positive one as improving it.
            if amount < 0:
                return f'{f1}.SetEnemy({f2}, false, false)' + _mirror(1)
            if amount > 0:
                return f'{f1}.SetAlly({f2}, true, true)' + _mirror(2)
            return f';{f1}.ModReaction({f2}, 0)  ;no-op'
        if amount <= -50:
            return f'{f1}.SetEnemy({f2}, false, false)' + _mirror(1)
        if amount <= 0:
            # Neutral: SetEnemy with the "self is neutral to other" bool set.
            return f'{f1}.SetEnemy({f2}, true, true)' + _mirror(0)
        return f'{f1}.SetAlly({f2}, true, true)' + _mirror(2)

    def _force_combat_call(self, ref: str, target: str) -> str:
        """Emit a TES4Polyfill.ForceCombat call with the conversion-owned
        enemy-faction pair that makes the fight stick for ANY actors.

        The two factions are records the import writes at fixed FormIDs
        (record-side mutual Enemy XNAM); the property names are registered
        in _WELL_KNOWN_PROPERTIES so the VMAD fill binds them.
        """
        self._property_refs['TES4ForceCombatAttackers'] = 'Faction'
        self._property_refs['TES4ForceCombatVictims'] = 'Faction'
        return (f'TES4Polyfill.ForceCombat({ref}, {target}, '
                'TES4ForceCombatAttackers, TES4ForceCombatVictims)')

    def _destroyed_formlist(self) -> str:
        """Register and name the conversion-owned destroyed-reference FormList.

        Skyrim has ObjectReference.SetDestroyed but NO reader for the flag, so
        TES4's `getdestroyed` has nothing native to read.  The import writes a
        FormList (TES4DestroyedRefs, fixed FormID) that the polyfill's
        SetDestroyed mirrors every write into and GetDestroyed queries.  A
        FormList rather than a script AV because AVs are Actor-only and the
        references TES4 destroys are doors, activators and statics.
        """
        self._property_refs['TES4DestroyedRefs'] = 'FormList'
        return 'TES4DestroyedRefs'

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
        if ref_name and ref_name.lower() not in SELF_NAMES:
            return self.xref.get_actor_packages_of_type(ref_name, pkg_type)
        if self._current_script_edid:
            return self.xref.get_script_owner_packages_of_type(
                self._current_script_edid, pkg_type)
        return []

    # Music cues converted for THIS plugin: {source_rel -> cue EditorID}.
    # Populated by set_music_cues() from the same music_tracks.json the importer
    # builds MUSC from, so the two sides cannot drift apart.
    _music_cues: dict = {}

    @classmethod
    def set_music_cues(cls, cues: dict):
        """Register {lowercase source_rel -> MUSC EditorID} for StreamMusic."""
        cls._music_cues = dict(cues or {})

    def _music_cue_property(self, raw_path: str):
        """Papyrus property name for a StreamMusic argument, or None.

        `raw_path` is spelled as the TES4 script spells it: a backslash or
        forward-slash path, or a bare category name.  Normalise to the
        manifest's `source_rel` form (forward slashes, lowercase, no `data/`
        prefix, no extension) and look it up; a miss returns None so the caller
        emits the inert marker rather than binding a property to a record that
        does not exist.
        """
        if not raw_path or not self._music_cues:
            return None
        norm = raw_path.replace(chr(92), '/').strip().lower()
        while '//' in norm:
            norm = norm.replace('//', '/')
        norm = norm.lstrip('/')
        if norm.startswith('data/'):
            norm = norm[5:]
        if not norm.startswith('music/'):
            # A bare category (`StreamMusic dungeon`) names the whole folder.
            norm = 'music/' + norm
        stem = norm.rsplit('.', 1)[0]

        for key, edid in self._music_cues.items():
            if key.rsplit('.', 1)[0] == stem:
                self._property_refs[edid] = 'MusicType'
                return edid
        return None

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
                not ref_name or ref_name.lower() in SELF_NAMES):
            return 'GetActorReference()' if actor_func else 'GetReference()'
        if ref_name:
            ref_low = ref_name.lower()
            # Self in ActiveMagicEffect/TopicInfo should redirect actor functions
            if actor_func and ref_low in SELF_NAMES:
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
    def _self_reference(extends: str) -> str:
        """What TES4's `Self` / `GetSelf` NAMES in a script of this base type.

        Written out at four sites until 2026-08-29, and the copies agreed --
        which is the point: this is one fact about the base types, not four
        decisions.  A TES4 script is attached to a REFERENCE, so `Self` is that
        reference; the three TES5 base types that have no reference of their
        own each name theirs differently.
        """
        if extends == 'ActiveMagicEffect':
            return 'GetTargetActor()'
        if extends == 'TopicInfo':
            return 'akSpeakerRef'
        if extends == PLAYER_ALIAS_EXTENDS:
            return 'GetReference()'
        return 'Self'

    @staticmethod
    def _implicit_self(extends: str) -> str:
        """What a bare, receiver-less call acts on in this script's base type.

        `Self` everywhere except a PlayerAlias script, whose Self is the
        ReferenceAlias rather than the reference it fills.
        """
        return 'GetReference()' if extends == PLAYER_ALIAS_EXTENDS else 'Self'

    def _base_record_type(self, name: str) -> str:
        """Papyrus type of the BASE RECORD `name` refers to, or ''.

        Resolved exactly as the property binder resolves it: a bare
        `edid_to_formid` lookup misses the sanitised spellings it handles --
        `0probeUbent` is emitted as the property `probeUbent` -- so the plain
        lookup found nothing and the type came back unknown even though the
        property itself had bound as MiscObject.

        Only BASE records answer; a placed reference really is a reference, so
        ACHR/ACRE/REFR give '' and leave the variable an ObjectReference.
        """
        if not self.xref:
            return ''
        fid = resolve_property_formid(self.xref, name)
        rtype = self.xref.record_type.get(fid, '') if fid else ''
        if not rtype or rtype in PLACED_REF_SIGS:
            return ''
        return _record_type_to_base_papyrus(rtype)

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
            return self._self_reference(extends)
        if (ref_name.lower() in SELF_NAMES
                and extends in ('ActiveMagicEffect', 'TopicInfo',
                                PLAYER_ALIAS_EXTENDS)):
            return self._self_reference(extends)
        return self._convert_ref(ref_name, extends, as_receiver=True)

    def set_scro_aliases(self, aliases: dict) -> None:
        """Install the stale-name -> canonical-EditorID map for this fragment.

        Oblivion runs the COMPILED script, not the source text the CK shows, and
        the two can disagree.  Knights.esp's quest-stage result scripts still say
        `player.additem NDArmorCuirass 1` and `player.additem NDLL0WeaponSword 1`
        — names no record in the plugin carries — while the SCRO table those same
        stages ship binds 01000ECE (NDArmorHeavyCuirass1, "Cuirass of the
        Crusader") and 01000FCA (NDLL0WeaponSwordLvl100).  The records were
        renamed after the scripts were last compiled; the engine kept handing out
        the right items because it reads the SCRO FormID, so the stale spellings
        are invisible in-game.

        Converting the TEXT, those names resolve to nothing, declare no property
        and reach the compiler undefined — which fails the CHECKER, so no .pex is
        emitted for the whole script and every OTHER stage of the quest dies with
        it (these fragments are what hand out the Crusader relics).

        The map is built positionally by `resolve_scro_aliases`; see there.
        """
        self._scro_aliases = {k.lower(): v for k, v in aliases.items()}

    def _scro_alias_for(self, name: str) -> str:
        """Return the canonical EditorID a stale script-text `name` refers to."""
        low = name.strip().lower()
        if not low or not self.xref:
            return ''
        alias = self._scro_aliases.get(low, '')
        if not alias:
            return ''
        # Never redirect a name that resolves on its own.
        if (self.xref.edid_to_formid.get(low)
                or _digit_stripped_formid(self.xref, low)):
            return ''
        return alias

    def _form_operand_edid(self, raw: str) -> str:
        """Resolve a FORM-ARGUMENT operand written as a raw FormID.

        The bare-identifier path in _convert_expression only reinterprets a
        6-8 digit token as a FormID, because anywhere else in a script a short
        run of digits is an ordinary numeric literal.  In an argument slot that
        the engine reads as a FORM (GetIsID's base record) there is no such
        ambiguity: a number there is ALWAYS a FormID, and the low ids are the
        ones scripts actually write by hand — Knights.esp's ND10 time-stop
        effect tests `GetIsID 7`, i.e. the Player NPC_ at 0x00000007.

        Left unresolved the number survived as a literal and the comparison
        became `Form == Int`, which the checker rejects outright, so no .pex was
        emitted for the script at all.  Returns the canonical EditorID, or ''
        when the token is not a resolvable id.
        """
        raw = raw.strip().strip('"\'')
        if not raw or not re.fullmatch(r'[0-9A-Fa-f]{1,8}', raw):
            return ''
        if not self.xref:
            return ''
        return self.xref.formid_to_edid.get(raw.upper().zfill(8), '')

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
                       extends: str, args=()) -> str:
        """Emit a converted function call.

        `args` carries the PARSED argument nodes -- the single source of the
        argument list.  `args_str` is DERIVED from them here for the branches
        that quote the authored spelling in a `;NE:` marker; it is no longer a
        parallel channel a caller can disagree with, which is what let an
        empty string silently drop a call's arguments.
        """
        fname_low = func_name.lower()
        self._arg_nodes = tuple(args)
        args_str = ', '.join(_expr.emit_source(a) for a in args)

        # Oblivion tolerated a comma between a command and its first argument
        # (`IsActionRef, Player`, `MessageBox, "text"`) and Nehrim's scripts
        # use the style constantly.  The PARSER records it on the node, so it
        # is read rather than re-detected in text.
        had_leading_comma = self._leading_comma

        # ...but for a command that takes NO arguments, the token after that
        # comma is the RECEIVER, not an argument: Oblivion's `StopCombat,
        # Player` / `IsInCombat, Player == 1` mean Player's combat state, the
        # same as `Player.StopCombat`.  Treating it as an argument emitted
        # `IsInCombat(Player)` ("function takes 0 parameters not 1") and
        # `(Self as Actor).StopCombat()` — which silently acted on the wrong
        # actor.  Promote it to the receiver when the call has none.
        if (had_leading_comma and not ref_name
                and fname_low in _ZERO_ARG_REF_FUNCTIONS
                and len(self._arg_nodes) == 1
                and isinstance(self._arg_nodes[0], _tes4_nodes.Ident)):
            ref_name = self._arg_nodes[0].name
            self._arg_nodes = ()

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
            av_first = self.arg_src(0).strip('\"\'')
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
                args = [self.arg_expr(i, extends)
                        for i in range(1, len(self._arg_nodes))]
                self.udf_calls.append((prop, tuple(args)))
                return f'{prop}.{_UDF_NAME}({", ".join(args)})'

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
            if self.has_args():
                _a = self.arg_src(0)
                if _a.lower() in ('player', 'playerref'):
                    arg = 'Game.GetPlayer()'
                else:
                    arg = self.arg_expr(0, extends)
            return f'{self._get_action_ref_param()} == {arg}'

        # GetPos/GetAngle/GetStartingAngle: axis param -> GetPositionX/Y/Z or GetAngleX/Y/Z
        if fname_low in ('getpos', 'getangle', 'getstartingangle'):
            axis = self.arg_src(0, 'X').upper()
            if axis not in ('X', 'Y', 'Z'):
                axis = 'X'
            if fname_low == 'getpos':
                papyrus = f'GetPosition{axis}'
            else:
                papyrus = f'GetAngle{axis}'
            # GetPositionX/GetAngleX and friends are declared on
            # ObjectReference, so the subject must not be promoted to Actor --
            # TES4 reads and writes the position/angle of plain scenery
            # (SEXedPuzStatue1-5 are STATs the Xeddefen puzzle rotates).  An
            # `Actor Property` on a STAT never binds, so the read came back
            # None and the puzzle could not track its own statues.
            ref = self._resolve_objref_ref(ref_name, extends)
            return f'{ref}.{papyrus}()' if ref_name else f'{papyrus}()'

        # SetPos/SetAngle: axis param -> SetPosition(x,y,z) / SetAngle(x,y,z)
        if fname_low in ('setpos', 'setangle'):
            # TES4 separates arguments with whitespace, a comma, or both, so
            # `SetPos Z, PlacePosZ` is as legal as `SetPos Z PlacePosZ`.
            # Splitting on whitespace alone left the axis as `Z,`, which fails
            # the X/Y/Z test below and silently falls back to X -- writing the
            # Z coordinate into the X slot (27 sites in 10 scripts, including
            # Morroblivion's levitation and rotation-fix scripts).
            axis = self.arg_src(0, 'X').upper()
            value = self.arg_expr(1, extends, '0')
            ref = self._resolve_objref_ref(ref_name, extends)
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

        # SetStage/GetStage/GetStageDone: first arg is quest, second is stage
        if fname_low in ('setstage', 'getstage', 'getstagedone'):
            parts = self.arg_srcs()
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
            stage_expr = (self.arg_expr(1, extends)
                          if len(parts) >= 2 else stage)
            return f'{quest_ref}.{papyrus}({stage_expr})'

        # StartQuest/StopQuest/GetQuestRunning/CompleteQuest/IsQuestCompleted: arg is quest
        if fname_low in ('startquest', 'stopquest', 'getquestrunning', 'completequest', 'isquestcompleted'):
            _qname = self.arg_src(0, 'quest')
            # This handler names the property directly instead of going through
            # _convert_ref, so it needs its own stale-name recovery: Oblivion's
            # SE02 stage 15 reads `startQuest SE02FIN`, a name no record
            # carries, while the stage's SCRO binds the real quest SE02Conv.
            # Unrecovered it declared a property that binds to NOTHING, and the
            # first use of an unbound property ABORTS the fragment — so the
            # Shivering Isles post-quest dialogue quest was never started.
            _qname = self._scro_alias_for(_qname) or _qname
            quest_ref = _safe_property_name(_qname)
            existing = self.type_of(quest_ref, locals_first=False)
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
            # 🛑 StopQuest CONVERTS TO Stop().  A converter-owned "run bit"
            # global that leaves the quest engine-running was tried and
            # REVERTED (2026-08-19): Oblivion restages a stopped quest, and a
            # quest that never stops keeps its CURRENT STAGE, so `SetStage N`
            # on the stage it is already at does nothing and that stage's
            # reset script never runs again.  Arena has exactly ONE stage (10,
            # AllowRepeatedStages) whose script zeroes the match state; with
            # the quest left running it sat at stage 10 forever, ReadyMatch
            # was never re-zeroed, and Owyn's next-match line (gated on
            # ReadyMatch == 0) could never fire -- he behaved as though the
            # match had not happened.  Skyrim's Start() resetting properties
            # is a real difference, but it is handled where it belongs: by
            # hoisting Start() above the seed writes
            # (blocks.hoist_quest_start_above_writes), not by refusing to stop.
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
            # The MESSAGE is the first argument; any that follow are button
            # labels, which Papyrus has no equivalent for.  Read it from the
            # NODE rather than by scanning for the closing quote: a literal
            # containing the separator (`"LEVEL AUFSTEIGEN!"`) survives here
            # and did not survive the scan.
            sources = self.arg_sources()
            if sources:
                first = sources[0]
                if first.startswith('"') \
                        and self._OBSE_FMT_RE.search(first[1:-1]):
                    return (f'{papyrus}('
                            f'{self._format_message_args(sources, extends)})')
                return (f'{papyrus}({first})' if first.startswith('"')
                        else f'{papyrus}({self._quote_msg(first)})')
            # No nodes: reached from one of the string-path call sites that
            # still pass only text.  Keep the old scan until they are gone.
            s = args_str.strip().lstrip(',').strip() if args_str else ''
            if s.startswith('"'):
                end = s.find('"', 1)
                if end >= 0 and self._OBSE_FMT_RE.search(s[1:end]):
                    return f'{papyrus}({self._format_message(s, extends)})'
            return f'{papyrus}({self._quote_msg(args_str)})'
        # --- Table-driven commands ---
        # Everything whose conversion is data rather than logic: see
        # `COMMAND_ROWS` in constants.py and `emit_row`'s spec.
        row = COMMAND_ROWS.get(fname_low)
        if row is None and fname_low not in HANDLED_COMMANDS:
            # A prefix family says "any name starting with this is inert", so
            # it must never shadow a command that HAS a handler below:
            # `sv_Construct` builds a string from a literal (Papyrus String IS
            # that literal) and is the one `sv_` command with a real
            # equivalent.  Swallowed by the family, it survived as an
            # undefined identifier and failed Morroblivion's chargen quiz.
            row = command_prefix_row(fname_low)
        if row is not None and row.subj != MAP:
            return emit_row(self, row, ref_name, func_name, args_str, extends)

        # --- Compound player.Function ---
        # Functions with a dedicated handler further down must NOT be short-cut
        # here: the compound entry routes args through _convert_args, which
        # splits on commas only.  Oblivion writes `Player.PlaceAtMe SRMonster 1,
        # 256, 1` -- base and count separated by a SPACE -- so comma-splitting
        # yielded a first arg of `SRMonster 1` and emitted
        # `PlaceAtMe(SRMonster 1, 256, 1)`, which does not parse.  The dedicated
        # handler normalizes both separators and resolves the receiver itself.
        # moveto/movetomarker have the SAME two problems as placeatme, plus a
        # third: the compound path never registers the destination as a property,
        # so `Player.MoveTo <marker>` emitted a bare identifier that nothing
        # declared and the compiler rejected the whole script.
        compound = f'{ref_name}.{func_name}'.lower() if ref_name else ''
        crow = COMMAND_ROWS.get(compound)
        if crow is not None and fname_low not in COMPOUND_HAS_OWN_HANDLER:
            papyrus_func, note = crow.emit, crow.note
            if crow.subj == MAP:
                args = self._convert_args(args_str, fname_low, extends) if args_str else ''
                result = f'{papyrus_func}({args})'
                return f'{result}  {note}' if note else result

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
            raw = (self.arg_src(0, '')).strip('"\'')
            arg = self._resolve_name(raw, extends) if raw else 'None'
            if raw:
                self._property_refs[raw] = 'Sound'
            if fname_low == 'playsound':
                return f'{arg}.Play(Game.GetPlayer())'
            ref = self._resolve_self_ref(ref_name, extends) if ref_name \
                else self._implicit_self(extends)
            return f'{arg}.Play({ref})'

        # Music playback by FILE PATH.  Skyrim's music system is form-driven
        # (MusicType.Add()/Remove() on a MUSC record), so a path cannot be
        # played directly -- but the importer now authors one MUSC per Special
        # cue, named deterministically from that same path
        # (music_cue_editor_id), so the call resolves to a real record.
        #
        # `StreamMusic "data/music/special/theme_01.mp3"` -> Add() on the cue
        # MUSC.  Measured: 38 StreamMusic calls in Nehrim.esm, 35 by path and 3
        # by bare category (`StreamMusic dungeon`); Oblivion.esm has none.
        # A path with no converted file behind it (8 of Nehrim's references are
        # dead on disk -- theme_06_part01, the specialevent_05 typo) still gets
        # the inert marker, because binding a property to a record that was
        # never written would abort the whole function at runtime.
        if fname_low == 'streammusic':
            raw = (self.arg_src(0, '')).strip('"\'')
            cue = self._music_cue_property(raw)
            if cue:
                return f'{cue}.Add()'
            self._line_comments.append(
                f';NE: {func_name} — no converted music for '
                f'({args_str.strip()})')
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
            arg = self.arg_src(0)
            if not arg:
                return '""'
            # A bare quoted literal passes straight through; anything else is an
            # expression (a format string plus args) the caller already handles.
            if arg.startswith('"') and arg.endswith('"') and arg.count('"') == 2:
                return arg
            return self.arg_expr(0, extends)

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
                val = (self.arg_expr(1, extends)
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
 
        # TES4 `UncompleteQuest <Quest>` reopens a finished quest, naming the
        # quest as an ARGUMENT.  Papyrus spells it as a method on the quest
        # itself, so the argument has to become the receiver — mapping it
        # straight onto Reset emitted `Reset(fbmwEBBone)` ("function takes 0
        # parameters not 1").
        if fname_low == 'uncompletequest':
            target = self.arg_src(0)
            if target:
                return f'{self._convert_ref(target, extends)}.Reset()'
            ref = self._convert_ref(ref_name, extends) if ref_name else 'Self'
            return f'{ref}.Reset()'

        # `ToggleFirstPerson <0|1>` — Oblivion's one command with an argument is
        # two argument-free globals in Skyrim.  0 forces THIRD person, 1 forces
        # first; the bare form (no argument) is a true toggle, which Papyrus
        # cannot express because it cannot read the current mode, so it takes
        # the third-person branch (the mode every caller here is refreshing in).

        if fname_low == 'update3d':
            target = (self._convert_ref(ref_name, extends) if ref_name
                      else self.arg_expr(0, extends,
                                         'Game.GetPlayer()'))
            return f'TES4Polyfill.Update3D({target})'

        # SkipAnim / SetNumericIniSetting: the ;NE: text IS the emission,
        # not a comment beside a value -- both are written in STATEMENT
        # position (Nehrim's portcullis calls `<ref>.SkipAnim` on its own
        # line), so the whole line becomes the comment.
        if fname_low == 'skipanim':
            return ';NE: SkipAnim  ;no Papyrus equivalent'
        if fname_low == 'setnumericinisetting':
            return f';NE: {func_name} {args_str or ""}  ;no Papyrus INI access'

        # pme/sme (PlayMagicEffectVisuals/StopMagicEffectVisuals): the argument
        # is a MAGIC EFFECT code (DSPL, STRP, ...), not a shader EditorID.  The
        # visuals Oblivion plays are the effect's EFSH — and EFSH records ARE
        # converted — so resolve code → TES4 MGEF → its shader and Play/Stop
        # that, exactly like pms/sms do for a directly-named shader.
        if fname_low in ('pme', 'playmagiceffectvisuals',
                         'sme', 'stopmagiceffectvisuals'):
            code = self.arg_src(0)
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
            duration = self.arg_src(1, '-1.0')
            dur = self.arg_expr(1, extends, '-1.0')
            return f'{safe}.Play({ref}, {dur})'

        # IsSpellTarget: "is ref currently affected by spell X".  Papyrus has no
        # per-spell test, but HasMagicEffect on the effect the converted SPEL
        # actually carries (resolved through the importer's own code→MGEF
        # mapping) answers the same question at runtime.
        if fname_low == 'isspelltarget':
            spell = self.arg_src(0, '')
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
            type_arg = self.arg_src(0, '')
            # 69 is TES4's ACTOR form type; GetNextRef carries no type and
            # continues whatever GetFirstRef opened.
            if fname_low == 'getnextref' or type_arg == '69':
                return ('Game.FindRandomActorFromRef(Game.GetPlayer(), '
                        f'{_REFWALK_RADIUS})')
            self._line_comments.append(
                f';NE: {func_name} over form type {type_arg or "?"} — Papyrus '
                f'iterates actors only')
            return 'None'

        # GetIsCurrentPackage: vanilla Actor.GetCurrentPackage() makes this an
        # exact conversion when the argument is a converted PACK record.
        if fname_low == 'getiscurrentpackage':
            arg = self.arg_src(0, '')
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

        # ShowMap → marker.AddToMap(true)
        if fname_low == 'showmap':
            marker_name = self.arg_src(0, 'None')
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
            parts = self.arg_srcs()
            if len(parts) >= 2:
                try:
                    val = int(parts[-1])
                    if val <= -100:
                        target = self.arg_expr(0, extends)
                        tgt_key = target  # already canonical from _convert_expression
                        cur = self._property_refs.get(tgt_key, '')
                        if cur in ('', 'ObjectReference'):
                            self._property_refs[tgt_key] = 'Actor'
                        ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
                        # Same forcing contract as `startcombat` above: the
                        # hating actor may have Aggression 0 and no hostile
                        # relation, and then the plain native drops out.
                        if (ref_name or '').lower() in ('player', 'playerref'):
                            return f'{ref}.StartCombat({target})'
                        return self._force_combat_call(ref, target)
                except (ValueError, IndexError):
                    pass
            self._line_comments.append(f';NE: ModDisposition')
            return '0'

        # StartConversation: caller.StartConversation Target [, TopicID].
        # The topic INFO (and its result-script fragment) is the payload —
        # discarding it as Say(None) silenced every scripted NPC-NPC
        # conversation (DANocturnal's Bejeen/Nocturnal talk, MQ12's
        # Jauffre/Martin council, MS10's Llevana scene) and lost their
        # SetStage results. Route it like SayTo: speak the topic directly.
        if fname_low == 'startconversation':
            pparts = self.arg_srcs()
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            if len(pparts) >= 2 and pparts[1].strip():
                topic_str = pparts[1].strip().split()[0]
                topic = self.arg_expr(1, extends)
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

        # SetForceRun → SpeedMult
        if fname_low == 'setforcerun':
            arg = self.arg_src(0, '0')
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
            parts = self.arg_srcs()
            if not parts:
                return f';NE: {func_name} missing faction arg'
            faction = self.arg_expr(0, extends)
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
            arg = self.arg_expr(0, extends, 'None')
            if self.has_args():
                self._property_refs[self.arg_src(0)] = 'Faction'
            if fname_low == 'getpcfactionsteal':
                return f'({arg}.GetCrimeGoldNonViolent() > 0) as Int'
            if fname_low == 'getpcfactionmurder':
                return (f'({arg}.GetCrimeGoldViolent() >= '
                        f'{TES4_MURDER_BOUNTY}) as Int')
            # Attack: violent bounty that is NOT big enough to be a murder.
            return (f'({arg}.GetCrimeGoldViolent() > 0 && '
                    f'{arg}.GetCrimeGoldViolent() < {TES4_MURDER_BOUNTY}) as Int')

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
            arg = self.arg_expr(0, extends, 'None')
            if self.has_args():
                self._property_refs[self.arg_src(0)] = 'WorldSpace'
            if ref_name:
                ref = self._resolve_self_ref(ref_name, extends)
                return f'{ref}.GetWorldSpace() == {arg}'
            return f'Game.GetPlayer().GetWorldSpace() == {arg}'

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
            arg = self.arg_src(0)
            target = self.arg_expr(0, extends, 'Game.GetPlayer()')
            for key in (target, observer):
                if re.match(r'^[A-Za-z_]\w*$', key or ''):
                    if self._property_refs.get(key, '') in ('', 'ObjectReference'):
                        self._property_refs[key] = 'Actor'
            return f'(({target}.IsDetectedBy({observer}) as Int) * 3)'

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
            arg = self.arg_src(0)
            target = self.arg_expr(0, extends, 'Game.GetPlayer()')
            for key in (target, observer):
                if re.match(r'^[A-Za-z_]\w*$', key or ''):
                    if self._property_refs.get(key, '') in ('', 'ObjectReference'):
                        self._property_refs[key] = 'Actor'
            return f'{target}.IsDetectedBy({observer})'

        # IsPlayerSleeping
        if fname_low == 'isplayersleeping':
            if getattr(self, '_in_sleep_menumode', False):
                return 'TES4_PCSleeping'
            return 'Game.GetPlayer().GetSleepState()'

        # ResetInterior → cell.Reset()
        if fname_low == 'resetinterior':
            if self.has_args():
                cell_name = _safe_property_name(self.arg_src(0))
                self._property_refs[cell_name] = 'Cell'
                return f'{cell_name}.Reset()'
            ref = self._resolve_self_ref(ref_name, extends)
            return f'{ref}.Reset()'

        # IsPCRace → Game.GetPlayer().GetRace() == arg
        if fname_low in ('ispcrace', 'getpcisrace'):
            arg = self.arg_expr(0, extends, 'None')
            if self.has_args():
                self._property_refs[self.arg_src(0)] = 'Race'
            return f'Game.GetPlayer().GetRace() == {arg}'

        # Expel → faction.SetPlayerExpelled(true)
        if fname_low == 'expel':
            arg = self.arg_expr(0, extends, 'None')
            if self.has_args():
                self._property_refs[self.arg_src(0)] = 'Faction'
            return f'{arg}.SetPlayerExpelled(true)'

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





        # Say: ref.Say topic [force] [headRef] -> ref.Say(topic)
        # SayTo: ref.SayTo target topic [force] -> ref.Say(topic)
        if fname_low in ('say', 'sayto', 'saycustom'):
            pparts = self.arg_srcs()
            if fname_low == 'sayto' and len(pparts) >= 2:
                # SayTo target topic [force] -> first arg is target, second is topic
                # If topic part has a trailing number (force flag), strip it
                topic_str = pparts[1].strip().split()[0] if pparts[1].strip() else 'None'
                topic = self.arg_expr(1, extends, 'None')
                self._mark_topic_property(topic_str)
            else:
                topic = self.arg_expr(0, extends, 'None')
                if pparts:
                    self._mark_topic_property(pparts[0].strip())
            # Say is declared on ObjectReference, NOT Actor
            # (`ObjectReference.Say(Topic, Actor akActorToSpeakAs = None,
            # bool abSpeakInPlayersHead = false)`).  A census of Oblivion.esm's
            # receivers found 144 calls on 21 NON-actor references -- Daedric
            # shrines (ACTI), Clavicus' dog statue (MISC), the XMarker (STAT)
            # speakers the Arena announcer talks through.  Promoting the
            # receiver to Actor made those declare `Actor Property`, which the
            # VM refuses to bind, so the property read None and the first call
            # on it aborted the function.
            ref = self._resolve_objref_ref(ref_name, extends)

            # TES4 `Say <topic> <flag> <speak-as NPC> <flag>` names WHO is
            # speaking, separately from the reference that emits the sound.
            # Skyrim's Say has no such argument, and voice-file lookup is keyed
            # on the SPEAKER's voice type -- an XMarker STAT has none, so the
            # engine finds no folder, plays no audio, and (having no audio to
            # time against) leaves the subtitle onscreen forever.
            #
            # The importer mints the vanilla answer for each call site: a TACT
            # carrying the speak-as NPC's voice type, placed at the emitter's
            # own position, bound under this exact property name.  Speaking on
            # THAT reference gives the line a real voice type and a real
            # folder.  See tes5_import/speaker_activators.py.
            speaker, in_head = self._say_speak_as(ref_name, pparts, fname_low)
            if speaker:
                # Speak the line on the voiced TACT stand-in (see
                # _say_speak_as).  The topic rides along for the polyfill and
                # for the fallback length lookup in _emit_say_line -- unless a
                # script LOCAL
                # shadows the topic's name (DABoethiaCageOpenScript01 has
                # `Short Salutation` next to `say Salutation`; TES4 resolved
                # the argument as the topic, Papyrus would pass the Int).
                topic_name = (pparts[1] if fname_low == 'sayto' and len(pparts) >= 2
                              else (pparts[0] if pparts else '')).strip().split()[0] if pparts else ''
                topic_arg = topic
                if topic_name and self._var_types.get(topic_name.lower()):
                    topic_arg = 'None'
                return (f'TES4Polyfill.SpeakAs({speaker}, '
                        f'{"True" if in_head else "False"}, {topic_arg})')
            return f'{ref}.Say({topic})'

        if fname_low in DROP_ARGS_FUNCS:
            args_str = ''

        # PushActorAway: ObjectReference.PushActorAway(Actor, force).  The
        # pushed target must be Actor-typed; promote or cast as needed.
        if fname_low == 'pushactoraway':
            parts = self.arg_srcs()
            ref = self._resolve_objref_ref(ref_name, extends)
            if parts:
                target = self.arg_expr(0, extends)
                vtype = self._var_types.get(target.lower(), '')
                ptype = self._property_refs.get(target, '')
                if 'ObjectReference' in (vtype, ptype):
                    target = f'({target} as Actor)'
                elif not vtype and not ptype and re.match(r'^\w+$', target):
                    self._property_refs[target] = 'Actor'
            else:
                target = 'Game.GetPlayer()'
            force = self.arg_expr(1, extends, '1.0')
            return f'{ref}.PushActorAway({target}, {force})'

        # StartCombat: TES4's call FORCES the fight — aggression, disposition
        # and faction relations are all ignored (UESP Oblivion:StartCombat;
        # CharacterGen stage 74 has the final assassin, base aggression 0 and
        # a faction the Emperor's faction Friends at +50, cut the Emperor
        # down purely on the strength of this call).  Skyrim's
        # Actor.StartCombat is only a nudge the combat AI immediately
        # re-evaluates: an Aggression-0 actor exits combat at once — vanilla's
        # own turn-hostile fragment (MS08 "In My Time Of Need") pairs
        # SetEnemy with SetAV Aggression 1 for exactly this reason — and a
        # target the actor has no hostile reaction to is dropped as invalid.
        # TES4Polyfill.ForceCombat supplies both preconditions (Aggression
        # floor 1, pair hostility through the conversion-owned
        # TES4ForceCombat* enemy-faction pair — relationship ranks were
        # tried first and silently no-op on non-unique actors like the CG
        # final assassin) before the native; see the polyfill.  A
        # player-driven attack needs no forcing, so the player keeps the
        # plain native.
        if fname_low == 'startcombat' and self.has_args():
            target_src = self.arg_src(0)
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            target = self.arg_expr(0, extends)
            ptype = self._property_refs.get(target, '')
            vtype = self._var_types.get(target.lower(), '')
            if ptype.startswith('TES4_') or 'ObjectReference' in (ptype, vtype):
                target = f'({target} as Actor)'
            elif not ptype and not vtype and re.match(r'^\w+$', target):
                self._property_refs[target] = 'Actor'
            if (ref_name or '').lower() in ('player', 'playerref'):
                return f'{ref}.StartCombat({target})'
            if ref == 'Self' and extends == 'ObjectReference':
                # A bare StartCombat in a script that is NOT attached to an
                # actor (Nehrim's UNUSED MQ33Sarantha02Script: `StartCombat,
                # Player`).  ForceCombat's parameter is Actor-typed and Papyrus
                # will not pass an ObjectReference there; on a non-actor the
                # cast is None and the call is a logged no-op — what Oblivion
                # did with it too.
                ref = '(Self as Actor)'
            return self._force_combat_call(ref, target)

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
            parts = self.arg_srcs()
            if len(parts) >= 3:
                f1 = self.arg_expr(0, extends)
                f2 = self.arg_expr(1, extends)
                self._property_refs[parts[0].strip()] = 'Faction'
                self._property_refs[parts[1].strip()] = 'Faction'
                call = self._faction_reaction_call(f1, f2, parts[2],
                                                   is_mod=(fname_low == 'modfactionreaction'),
                                                   extends=extends)
                if call is not None:
                    return call
                # Non-literal amount: bucket at runtime so a scripted variable
                # still lands on a real enum tier instead of a dead modifier.
                # The war/peace pushes ride the same sign (see
                # _faction_reaction_call for why they are needed at all).
                val = self.arg_expr(2, extends)
                return (f'if ({val}) < 0\n'
                        f'  {f1}.SetEnemy({f2}, false, false)\n'
                        f'else\n'
                        f'  {f1}.SetAlly({f2}, true, true)\n'
                        f'endif')
            # Fallback: not enough args
            return f';TODO: {func_name} {args_str}  ;needs faction1.SetEnemy/SetAlly(faction2)'

        # GetGameSetting/getgs: arg is GMST name → quoted string
        if fname_low in ('getgamesetting', 'getgs'):
            setting = self.arg_src(0, 'fUnknown').strip('\"')
            # A setting this converter WRITES through an actor value must also
            # be READ through it, or the save/restore pattern these scripts use
            # ("remember the old value, set a new one, put it back") reads the
            # untouched global and restores a number the write never changed.
            av = GMST_TO_ACTOR_VALUE.get(setting.lower())
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
                     self.arg_srcs()
                     if p.strip()]
            if len(parts) >= 2:
                setting = parts[0].strip().strip('"')
                value = self.arg_expr(1, extends)
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
            if self.has_args():
                name = self.arg_src(0)
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
        if fname_low == 'positionworld':
            parts = self.arg_srcs()
            ref = self._resolve_objref_ref(ref_name, extends)
            if len(parts) >= 3:
                x, y, z = (self.arg_expr(i, extends)
                           for i in range(3))
                out = f'{ref}.SetPosition({x}, {y}, {z})'
                if len(parts) >= 4:
                    ang = self.arg_expr(3, extends)
                    out += f'\n  {ref}.SetAngle(0.0, 0.0, {ang})'
                return out
            return f'; {func_name} {args_str or ""}  ;could not parse'

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
        # The two writers stay single-expression (a trailing source comment is
        # appended to whatever they return, so a second line here would be
        # orphaned behind it); the shadow write is spliced in as its own line
        # by _shadow_controls_writes during post-processing.
        if fname_low in ('disableplayercontrols', 'enableplayercontrols'):
            self._property_refs['TES4ControlsDisabled'] = 'GlobalVariable'
            verb = 'Disable' if fname_low.startswith('disable') else 'Enable'
            return f'Game.{verb}PlayerControls()'

        # IsDoor / IsContainer / IsWeapon / ... — OBSE form-TYPE tests.  Vanilla
        # Papyrus cannot ask a form its type (Form.GetType is SKSE), so these
        # answer 0.  Handled here rather than by the blanket neutraliser so the
        # DOTTED spelling (`crosshairRef.IsDoor == 1`) is covered too: that path
        # only recognises a name as a function when it is a known command,
        # and otherwise emitted a raw member access that failed the compile.
        if fname_low in _FORM_TYPE_TESTS:
            self._line_comments.append(
                f';NE: {func_name} — Papyrus cannot read a form\'s type')
            return '0'

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
            parts = self.arg_srcs()
            flag = (self.arg_expr(len(parts) - 1, extends)
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
        if fname_low in ('dispel', 'dispelspell') and self.has_args():
            arg_raw = self.arg_src(0)
            arg_fid = (self.xref.edid_to_formid.get(arg_raw.lower(), '')
                       if self.xref else '')
            arg_rtype = (self.xref.record_type.get(arg_fid, '')
                         if arg_fid else '')
            if arg_rtype == 'ENCH':
                self._line_comments.append(
                    f';NE: Dispel {arg_raw} names an enchantment, which has no '
                    f'Skyrim Spell to dispel')
                return '0'
            arg = self.arg_expr(0, extends)
            self._property_refs[arg_raw] = 'Spell'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.DispelSpell({arg})'

        # Cast: TES4 ref.cast spell [target] -> Papyrus spell.Cast(ref, target)
        # Cast is a method on Spell in Papyrus, not on ObjectReference
        if fname_low == 'cast':
            parts = (self.arg_srcs())
            parts = [p.strip() for p in parts if p.strip()]
            spell = self.arg_expr(0, extends, 'None')
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
            # Spell.Cast(ObjectReference akSource, ObjectReference akTarget)
            # -- the caster is an ObjectReference.  TES4 fires spells from
            # invisible marker refs (SEHaskillSummonMarker, MG05ShockMark1,
            # SE05SpellMarker1-3, SEOrderPriestCastingMarker ...), all STATs;
            # promoting the source to Actor left an unbindable `Actor Property`
            # and the spell was never cast.
            source = self._resolve_objref_ref(ref_name, extends)
            if len(parts) > 1:
                target = self.arg_expr(1, extends)
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
            operand = self.arg_src(0, '')
            # A raw FormID operand (`GetIsID 7`) is a FORM here, never a number.
            edid = self._form_operand_edid(operand)
            arg = (self._resolve_name(edid, extends) if edid
                   else self.arg_expr(0, extends, 'None'))
            operand = edid or operand
            if operand:
                self._bind_base_form_property(operand)
            ref = self._resolve_objref_ref(ref_name, extends)
            return f'{ref}.GetBaseObject() == {arg}'

        # GetIsRace: ref.GetIsRace RaceRef -> ref.GetRace() == raceRef
        if fname_low == 'getisrace':
            arg = self.arg_expr(0, extends, 'None')
            if self.has_args():
                self._property_refs[self.arg_src(0)] = 'Race'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'{ref}.GetRace() == {arg}'

        # GetIsClass / GetPCIsClass: the CLAS argument is a Class form, and
        # Skyrim reads it off the ActorBase, not the reference — Actor has no
        # GetClass().  Left untranslated, `GetPCIsClass CharactergenClass`
        # parsed as a bare name after a name and killed the whole script
        # (Morroblivion's chargen quest script, which the Chargen-and-Transport
        # start menu imports, so the transport NPCs went with it).
        if fname_low in ('getisclass', 'getpcisclass'):
            arg = self.arg_expr(0, extends, 'None')
            if self.has_args():
                self._property_refs[self.arg_src(0)] = 'Class'
            if fname_low == 'getpcisclass':
                return f'Game.GetPlayer().GetActorBase().GetClass() == {arg}'
            ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
            return f'({ref} as Actor).GetActorBase().GetClass() == {arg}'

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
            arg = self.arg_src(0, 'None').strip('\"')
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
            parts = self.arg_srcs() or ['Idle']
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
            idle_name = self.arg_src(0, 'IdleForceDefaultState')
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
            parts = self.arg_srcs()
            if len(parts) >= 2:
                target = self.arg_expr(0, extends)
                val = 'true' if parts[1].strip() in ('1', 'true') else 'false'
                arg_fid = self.xref.edid_to_formid.get(parts[0].lower(), '') if self.xref else ''
                arg_rtype = self.xref.record_type.get(arg_fid, '') if arg_fid else ''
                if arg_rtype in PLACED_REF_SIGS:
                    self._property_refs[target] = 'Actor'
                    return f'({target} as Actor).GetActorBase().SetEssential({val})'
                # Base form (or unresolved): bind as ActorBase and call directly.
                # Force ActorBase even over an attached-script type, since the
                # VMAD binds this to the base and only ActorBase can bind there.
                self._property_refs[target] = 'ActorBase'
                return f'{target}.SetEssential({val})'
            elif ref_name:
                ref = self._resolve_self_ref(ref_name, extends, actor_func=True)
                val = 'true' if self.arg_src(0) in ('1', 'true') else 'false'
                return f'({ref} as Actor).GetActorBase().SetEssential({val})'
            return f'; SetEssential {args_str or ""}  ;could not parse'

        # SetOwnership: ref.SetOwnership owner -> ref.SetActorOwner/SetFactionOwner
        if fname_low == 'setownership':
            ref = self._resolve_self_ref(ref_name, extends)
            if self.has_args():
                arg = self.arg_expr(0, extends)
                arg_low = self.arg_src(0).lower()
                # Check if arg is a faction
                arg_fid = self.xref.edid_to_formid.get(arg_low, '') if self.xref else ''
                arg_rtype = self.xref.record_type.get(arg_fid, '') if arg_fid else ''
                pref_type = self._property_refs.get(arg, self._property_refs.get(_safe_property_name(self.arg_src(0)), ''))
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
            if self.has_args():
                arg = self.arg_expr(0, extends)
                arg_low = self.arg_src(0).lower()
                arg_fid = (self.xref.edid_to_formid.get(arg_low, '')
                           if self.xref else '')
                arg_rtype = (self.xref.record_type.get(arg_fid, '')
                             if arg_fid else '')
                pref_type = self._property_refs.get(
                    arg, self._property_refs.get(
                        _safe_property_name(self.arg_src(0)), ''))
                if arg_rtype == 'FACT' or pref_type == 'Faction':
                    return f'({ref}.GetFactionOwner() == {arg})'
                return f'({ref}.GetActorOwner() == {arg}.GetActorBase())'
            return (f'({ref}.GetActorOwner() == '
                    f'Game.GetPlayer().GetActorBase())')

        # MoveTo: ref.MoveTo target [X Y Z] -> ref.MoveTo(target, X, Y, Z)
        if fname_low in ('moveto', 'movetomarker'):
            parts = self.arg_srcs()
            target = self.arg_expr(0, extends, 'None')
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
            # MoveTo is declared on ObjectReference (`MoveTo(ObjectReference
            # akTarget, ...)`), and TES4 moves scenery with it as readily as
            # actors -- SEHaskillSummonMarker is a STAT the summon spell
            # relocates.  Promoting the subject to Actor left an `Actor
            # Property` the VM refuses to bind on a STAT, so the marker stayed
            # None and never moved.
            ref = self._resolve_objref_ref(ref_name, extends)
            if offsets:
                return f'{ref}.MoveTo({target}, {offsets})'
            return f'{ref}.MoveTo({target})'

        # PlaceAtMe: ref.PlaceAtMe base [count] [distance] -> ref.PlaceAtMe(base, count)
        if fname_low == 'placeatme':
            # Normalize: replace commas with spaces, then split on whitespace
            parts = self.arg_srcs()
            base = self.arg_expr(0, extends, 'None')
            count = parts[1] if len(parts) > 1 else '1'
            # PlaceAtMe is on ObjectReference — don't promote type to Actor
            ref = self._resolve_self_ref(ref_name, extends, actor_func=False)
            if ref == 'Self' and extends == 'ActiveMagicEffect':
                ref = 'GetTargetActor()'
            elif ref == 'Self' and extends == 'TopicInfo':
                ref = 'akSpeakerRef'
            return f'{ref}.PlaceAtMe({base}, {count})'

        # ShowClassMenu / ShowBirthSignMenu — modal Message pages built from
        # the plugin's own BSGN/CLAS records (message_menus.
        # build_chargen_menus; the importer authors the MESG pages at fixed
        # FormIDs).  TES4's menus PAUSED THE GAME, and scripted scenes rely
        # on that beat: CharacterGen's Emperor carries an authored Goodbye at
        # the birthsign point and re-force-greets afterwards, so a no-op here
        # dumped the player into a free-roam gap mid-scene where Baurus's
        # pending torch force-greet could steal them.  Message.Show() parks
        # this thread and pauses gameplay exactly like the original.
        # Birthsign choices grant the sign's converted spells; classes have
        # no expressible effect (Skyrim has no attributes/skills) so that
        # menu is choice-and-pacing only.  ShowRaceMenu is NOT here: Skyrim
        # has Game.ShowRaceMenu() and COMMAND_ROWS carries the mapping.
        if fname_low in ('showclassmenu', 'showbirthsignmenu'):
            key = ('birthsign' if fname_low == 'showbirthsignmenu'
                   else 'class')
            plan = (self.chargen_menus or {}).get(key)
            if not plan:
                self._line_comments.append(f';NE: {func_name}')
                return '0'
            from .message_menus import PAGE_OPTIONS
            pages = plan['pages']
            actions = plan['actions']
            self._uses_chargen_menus = True
            self._chargen_menu_seq = getattr(self, '_chargen_menu_seq', 0) + 1
            var = f'TES4_menuPick{self._chargen_menu_seq}'
            first = _safe_property_name(pages[0][0])
            self._property_refs[first] = 'Message'
            # The busy latch (declared script-scope by _emit_chargen state,
            # see generate()) keeps a queued OnUpdate tick from re-showing
            # the menu.
            #
            # 🛑 A latched-out pass must RETURN, not fall through.  TES4's
            # menu was modal to the WHOLE GameMode pass: `ShowBirthsignMenu`
            # blocked, and the `setstage 44` written on the next source line
            # did not run until the player had chosen.  Papyrus only parks
            # the thread that called Show(), so the poll's NEXT tick (0.1s
            # later, a different thread) re-enters this body while the menu
            # is still open, skips the menu on the latch — and then ran
            # every authored statement after it.  For CharacterGen that
            # fired `setstage 44` mid-menu, whose fragment force-greets the
            # Emperor (`UrielSeptimRef.evp`) against a player still locked
            # in the menu: the greet is evaluated and consumed with nobody
            # able to receive it, so the menu closes onto an Emperor with
            # nothing pending and the scene dead.  Verified live through the
            # game bridge (2026-08-15): driving stage 43 advanced to 44
            # instantly while TES4ChargenBirthsignChoice was still 0.
            #
            # Returning reproduces the original block: the queued tick does
            # nothing, and the pass that owns the menu runs the authored
            # statements itself once Show() returns.
            #
            # ONLY in the polled body.  A ONE-SHOT site (a quest-stage
            # fragment, an OnActivate handler) has no repeating caller to
            # re-enter it, so its latch can only trip on a genuine race —
            # and there a Return would DROP the authored tail rather than
            # defer it.  CharacterGen stage 87 is exactly that shape: the
            # class menu is followed by `MQ02.SetStage(20)`, the end-of-
            # chargen topic unlocks and the autosave.  Skipping those would
            # be a worse failure than showing the menu twice, so a one-shot
            # site keeps the fall-through form.
            polled = self._current_event == 'Event OnUpdate()'
            retry = f'TES4_menuRetry{self._chargen_menu_seq}'
            lines = ['TES4_ChargenMenuBusy = True',
                     f'Int {var} = {first}.Show()'
                     '  ; TES4 modal chargen menu — pauses the game like the original',
                     # Show() returns -1 when the box could not display (a
                     # menu/dialogue transition still in flight — this menu
                     # opens 0.1s after an authored Goodbye closes the
                     # conversation).  Retry briefly instead of swallowing
                     # the choice.
                     f'Int {retry} = 0',
                     f'While {var} < 0 && {retry} < 20',
                     '  Utility.Wait(0.5)',
                     f'  {var} = {first}.Show()',
                     f'  {retry} += 1',
                     'EndWhile']
            for k, (medid, _title, _btns) in enumerate(pages[1:], start=1):
                safe = _safe_property_name(medid)
                self._property_refs[safe] = 'Message'
                # Slot PAGE_OPTIONS on a non-final page is "More ...": global
                # choice index = 9*page + button (see message_menus._paged).
                lines.append(f'If {var} == {PAGE_OPTIONS * k}')
                lines.append(f'  {var} = {PAGE_OPTIONS * k} + {safe}.Show()')
                lines.append('EndIf')
            branch_kw = 'If'
            any_action = False
            for idx, spell_edids in enumerate(actions):
                if not spell_edids:
                    continue
                lines.append(f'{branch_kw} {var} == {idx}')
                branch_kw = 'ElseIf'
                for sp in spell_edids:
                    sp_safe = _safe_property_name(sp)
                    self._property_refs[sp_safe] = 'Spell'
                    lines.append(
                        f'  Game.GetPlayer().AddSpell({sp_safe}, false)')
                any_action = True
            if any_action:
                lines.append('EndIf')
            # Persist the pick (index+1; 0 = unchosen) so the dialogue
            # conditions the import rewrote to GetGlobalValue can match it —
            # this is what makes the Emperor's post-menu line agree with the
            # sign the player actually chose.
            gname = plan.get('choice_global')
            if gname:
                g_safe = _safe_property_name(gname)
                self._property_refs[g_safe] = 'GlobalVariable'
                # Never persist a failed pick: 0 means "unchosen" and the
                # dialogue side has an ungated fallback line for that case.
                lines.append(f'If {var} >= 0')
                lines.append(f'  {g_safe}.SetValue({var} + 1)')
                lines.append('EndIf')
            lines.append('TES4_ChargenMenuBusy = False')
            if polled:
                # The queued tick defers to the pass that owns the menu,
                # which runs the authored tail itself once Show() returns.
                lines = ['If TES4_ChargenMenuBusy',
                         '  Return'
                         '  ; menu already open — TES4 blocked the whole pass',
                         'EndIf'] + lines
            else:
                # One-shot site: skip the menu on a race but keep running,
                # so the authored tail after it is never dropped.
                lines = (['If !TES4_ChargenMenuBusy']
                         + [f'  {ln}' for ln in lines] + ['EndIf'])
            return '\n  '.join(lines)

        # IsInFaction: ref.IsInFaction faction -> ref.IsInFaction(faction)  
        if fname_low == 'isinfaction':
            arg = self.arg_expr(0, extends, 'None')
            if self.has_args():
                self._property_refs[self.arg_src(0)] = 'Faction'
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
            parts = (self.arg_srcs()
                     if args_str else [])
            run_flag = '0'
            if parts and parts[-1] in ('0', '1'):
                run_flag = parts[-1]
                parts = parts[:-1]
            ref = self._convert_ref(ref_name, extends) if ref_name else ''
            if parts:
                activator = self.arg_expr(0, extends)
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
        # The generic mapped-call rendering, for every `Cmd(..., MAP)` row.
        # It runs LAST because the dedicated handlers above convert commands
        # whose behaviour depends on their arguments, and a row cannot express
        # that.
        # A command that only a dedicated handler converts, reaching HERE, has
        # fallen past its handler -- the receiver form of a bare-only command,
        # say.  It must NOT become `ref.<name>()`: the name is TES4's, not
        # Papyrus's, so the call would be an undefined function and take the
        # whole script down with it.  Leave the source visible instead.
        if fname_low in HANDLED_COMMANDS:
            orig = (f'{ref_name}.{func_name} {args_str}'.strip() if ref_name
                    else f'{func_name} {args_str}'.strip())
            return f';TODO: {orig}'

        # The generic mapped-call rendering.  An UNKNOWN name renders the same
        # way -- receiver resolution, the Actor casts and the implicit-subject
        # rules are about the CALL, not about whether we recognise the name --
        # under its own spelling and flagged for review.  Written twice until
        # 2026-08-28, and the copies had already drifted: the fallback's
        # receiver arm never applied the TES4_-script cast, and its bare arm
        # omitted the event-actor subject.
        row = COMMAND_ROWS.get(fname_low)
        if row is not None and row.subj == MAP:
            papyrus_func, needs_self, note = row.emit, not row.bare, row.note
        else:
            papyrus_func, needs_self, note = func_name, True, ';TODO: Verify'
        if not args_str and fname_low in DEFAULT_ARGS:
            args = DEFAULT_ARGS[fname_low]
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
            # No ref — infer implicit target based on script context.
            # `_OBJREF_SHARED_FUNCTIONS` must be excluded here exactly as it
            # is at the ref'd-receiver site above: 14 of _ACTOR_ONLY_FUNCTIONS
            # are also declared on ObjectReference, and casting one of those
            # to Actor on a non-actor Self yields **None**, so the call
            # aborts at runtime instead of failing to compile.
            # MS48OblivionGateScript (an ACTI) called TES4's bare
            # `getdistance player`; emitted as `(Self as Actor).GetDistance`
            # it returned None -> the comparison read 0 -> `0 < 1000` was
            # always true, so the gate hammered
            # `OblivionStormTamriel.ForceActive()` every 0.1s.
            if (needs_self and fname_low in _ACTOR_ONLY_FUNCTIONS
                    and fname_low not in _OBJREF_SHARED_FUNCTIONS):
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
                  and (fname_low in _OBJREF_IMPLICIT_SELF_FUNCTIONS
                       or fname_low in _OBJREF_SHARED_FUNCTIONS)
                  and extends in ('ActiveMagicEffect', 'TopicInfo',
                                  PLAYER_ALIAS_EXTENDS)):
                # ObjectReference method called bare inside a script whose
                # Self is not a reference — route it onto the reference the
                # effect/topic acts on, with no `as Actor` cast.
                #
                # `_OBJREF_SHARED_FUNCTIONS` is included for the RECEIVER,
                # not the cast: dropping the bogus `(Self as Actor)` above
                # must not leave these bare, because TopicInfo/
                # ActiveMagicEffect have no implicit reference at all and a
                # bare `AddItem(...)` is an undefined function (52 scripts
                # failed to compile at exactly this point).  Route the
                # receiver, keep the type honest.
                result = (f'{self._resolve_objref_ref(None, extends)}'
                          f'.{papyrus_func}({args})')
            else:
                result = f'{papyrus_func}({args})'
        return f'{result}  {note}' if note else result

    # An OBSE format specifier: %z (string_var), %g/%.Nf (number), %c, %x, %%.
    # The precision digits are optional on BOTH sides of the dot: authors write
    # `%0.f` as often as `%.0f` (XPKnotboneFactionFixerSCRIPT) and the engine
    # accepts it, so requiring a digit after the dot missed those and left the
    # specifier printing literally.
    _OBSE_FMT_RE = re.compile(r'%(?:%|[-+ #0]*\d*(?:\.\d*)?[a-zA-Z])')

    def _format_string_call(self, args_str: str, extends: str,
                            indexes=None) -> str:
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
        if indexes is None:
            indexes = range(1, len(self._arg_nodes))
        args = [self.arg_expr(i, extends) for i in indexes]

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

    def _format_message_args(self, sources: list, extends: str) -> str:
        """`_format_message` over already-separated arguments.

        The string version re-splits its tail on commas and then on
        whitespace, which tears a literal containing either (`"LEVEL
        AUFSTEIGEN!"` became two arguments).  The parser separated them
        already, so this only has to drop the surplus display-time literal
        TES4's `Message` allows after the format arguments -- Papyrus's
        Debug.Notification has no duration, and concatenating it would print
        "Rank 3 Fireball10".
        """
        fmt = sources[0][1:-1] if sources else ''
        n_spec = len([m for m in self._OBSE_FMT_RE.finditer(fmt)
                      if m.group(0) != '%%'])
        keep = list(range(1, len(sources)))
        while (len(keep) > n_spec and keep
               and re.match(r'^-?\d+(?:\.\d+)?$', sources[keep[-1]])):
            keep.pop()
        return self._format_string_call(f'"{fmt}"', extends, keep)

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
        n_spec = len([m for m in self._OBSE_FMT_RE.finditer(fmt)
                      if m.group(0) != '%%'])
        keep = list(range(1, len(self._arg_nodes)))
        srcs = self.arg_srcs()
        while (len(keep) > n_spec and keep
               and re.match(r'^-?\d+(?:\.\d+)?$', srcs[keep[-1]])):
            keep.pop()
        return self._format_string_call(s, extends, keep)

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




_ONACTIVATE_BLOCK_RE = re.compile(
    r'^[ \t]*begin[ \t]+onactivate\b[^\r\n]*\r?\n(.*?)^[ \t]*end\b',
    re.IGNORECASE | re.MULTILINE | re.DOTALL)


def sctx_onactivate_consumes(sctx: str) -> bool:
    """True when a raw TES4 script source has a consuming OnActivate block.

    Text-level twin of ScriptConverter._onactivate_consumes for callers that
    hold the SCTX source rather than parsed blocks (tes5_import uses it to
    spot barrier doors whose lock level must stay AI-passable).  A script
    with no OnActivate block at all consumes nothing.
    """
    blocks = [('onactivate', '', m.group(1).splitlines())
              for m in _ONACTIVATE_BLOCK_RE.finditer(sctx or '')]
    if not blocks:
        return False
    return ScriptConverter._onactivate_consumes(blocks)


def _call_name(node) -> str:
    """Lowercased command name a value NAMES, or ''.

    TES4 writes a zero-argument command as a bare word, so the same call
    reaches the tree as `Call`, `Member` or `Ident` depending on how it was
    written.  The name is what matters.
    """
    return node.called


def _split_say(conv, node, extends: str):
    """`(say_call, delay)` when this value is a Say, else `(None, '')`.

    `set T to ref.Say topic + 2` assigns the LINE DURATION plus an authored
    offset.  The tree hands over the call and the offset separately; the text
    version scanned the rendered string for balanced parentheses to pull them
    apart.
    """
    delay = ''
    if isinstance(node, _tes4_nodes.BinOp) and node.op in ('+', '-'):
        inner, other = node.left, node.right
        if _call_name(inner) in _SAY_COMMANDS:
            delay = ' %s %s' % (node.op, _expr.emit(conv, other, extends))
            node = inner
    if _call_name(node) not in _SAY_COMMANDS:
        return None, ''
    return _expr.emit(conv, node, extends), delay


#: TES4 commands that SPEAK and return the spoken line's length.
_SAY_COMMANDS = frozenset({'say', 'sayto', 'saycustom'})


#: Quest METHODS -- `X.SetStage` is a command on the quest, not a variable of
#: it.  Split out of the member resolver so the two lists it used to carry
#: inline are named rather than repeated.
_QUEST_METHODS = frozenset({
    'getstage', 'setstage', 'getstagedone', 'start', 'stop', 'isrunning',
    'iscompleted', 'completequest',
})

#: Commands reached as `ref.Name` that carry no FUNCTION_MAP row of their own,
#: so the membership tests above would read them as variables.
_MEMBER_COMMANDS = frozenset({
    'evaluatepackage', 'enable', 'disable', 'delete', 'activate', 'reset',
    'kill', 'resurrect', 'moveto', 'getparentcell', 'getself', 'getactionref',
    'getlinkedref', 'getparentref', 'getbaseobject', 'getactorbase',
    'isactorusingatorch', 'isridinghorse', 'createfullactorcopy',
})
