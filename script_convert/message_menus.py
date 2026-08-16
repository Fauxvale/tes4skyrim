"""TES4 multi-button MessageBox → Skyrim MESG menu plan.

Oblivion scripts drive every in-world choice menu with

    MessageBox "Would you like a blessing?" "Yes" "No"
    ...
    set button to GetButtonPressed          ; polled from GameMode

Skyrim has no dynamic message boxes: buttons live on an authored MESG record
and `Message.Show()` parks its calling thread until the player clicks, then
returns the button index. So every button-carrying MessageBox call site needs
a MESG record in the output plugin, and the script needs the Show()/consume
idiom instead of the poll.

This module is the SHARED analysis both sides run so they agree exactly:

  * the importer (tes5_import.import_main) builds the plan and writes one MESG
    per call site — EDID `TES4Msg_<Script>_<NN>`, DESC = the message text,
    ITXT per button — then registers each EDID in _WELL_KNOWN_PROPERTIES so
    the VMAD property pass can bind them;
  * the script pipeline (script_convert.pipeline) ships the plan to its
    workers, and the converter emits `TES4_MsgButton = TES4Msg_X_01.Show()`
    at the call site plus a consume-on-read helper for GetButtonPressed
    (TES4 semantics: the clicked index is returned once, then -1 again).

Sites are numbered in SOURCE order within each script, but the converter can
process blocks out of source order (MenuMode merges into the GameMode poll),
so it matches sites by (text, buttons) content and takes the next unused name
for that content — identical text twice in one script yields _01 then _02 in
either walk order.

A MessageBox with only its message string (no buttons) stays a
Debug.MessageBox, and a GetButtonPressed in a script that shows no button box
of its own (a handful poll a box some OTHER script showed — cross-script
GetButtonPressed was global state in TES4) keeps the old `-1` conversion:
those readers were dead before this plan existed and stay explicitly dead
rather than silently miswired.
"""

import re

MESG_PREFIX = 'TES4Msg_'
# Both engines cap a message box at 10 buttons.
MAX_BUTTONS = 10

_QUOTED = re.compile(r'"([^"]*)"')
# A MessageBox STATEMENT: first token on its line (TES4 is one statement per
# line, and a leading `;` comment never matches). Oblivion tolerates a comma
# straight after the command name.
_MSGBOX_LINE = re.compile(r'(?im)^[ \t]*messagebox\b(,?[^\n]*)')


def parse_button_box(args_str):
    """(text, [buttons]) from a MessageBox argument string, or None when the
    call carries no buttons (a plain notification box).

    Buttons are the quoted strings AFTER the first one; unquoted tokens in
    between are printf-style format arguments (`"...%.0f Drakes?" cost "Yes"
    "No"`) and are not part of the menu. MESG DESC text is static, so such a
    specifier survives literally — rare, and better than losing the menu.
    """
    if not args_str:
        return None
    quoted = _QUOTED.findall(args_str)
    if len(quoted) < 2:
        return None
    return quoted[0], quoted[1:1 + MAX_BUTTONS]


def mesg_edid(script_edid: str, index: int) -> str:
    base = re.sub(r'[^A-Za-z0-9_]', '_', script_edid or 'Script')
    return f'{MESG_PREFIX}{base}_{index:02d}'


def sites_for_source(script_edid: str, source: str) -> list:
    """Ordered [(mesg_edid, text, buttons)] for one script source (real
    newlines, i.e. the SCTX value parse_export_file produces)."""
    sites = []
    for m in _MSGBOX_LINE.finditer(source or ''):
        parsed = parse_button_box(m.group(1))
        if parsed:
            sites.append((mesg_edid(script_edid, len(sites) + 1),) + parsed)
    return sites


def build_message_plan(scpt_records: list) -> dict:
    """{script_edid_lower: [(mesg_edid, text, buttons)]} over SCPT records."""
    plan = {}
    for rec in scpt_records:
        edid = rec.get('EditorID', '')
        sites = sites_for_source(edid, rec.get('SCTX', ''))
        if sites and edid:
            plan[edid.lower()] = sites
    return plan


# ---------------------------------------------------------------------------
# TES4 chargen menus (ShowBirthsignMenu / ShowClassMenu)
# ---------------------------------------------------------------------------
#
# TES4's chargen menus were MODAL: the game paused until the player chose,
# and scripted scenes depend on that beat.  CharacterGen stage 43 is the
# canonical case — the Emperor's birthsign INFOs carry an authored Goodbye
# (the modal menu takes over from there) and stage 44 re-force-greets him to
# continue the conversation.  With the menus converted to no-ops the player
# was dumped into a free-roam gap in the middle of the scene, where any other
# pending force-greet (Baurus's torch GREETING, legal for stages 40-80) could
# steal them and desync the conversation.  Message.Show() is Skyrim's modal
# equivalent: it parks the calling thread AND pauses gameplay until a button
# is clicked, restoring both the choice and the authored pacing.
#
# The plan is data-driven from the plugin's own BSGN/CLAS records so any
# plugin with birthsigns/classes gets its own menus; a plugin without them
# keeps the no-op conversion.

CHARGEN_BIRTHSIGN_EDID = 'TES4Msg_ChargenBirthsign_%02d'
CHARGEN_CLASS_EDID = 'TES4Msg_ChargenClass_%02d'
# GLOB records persisting the player's menu choice as (menu index + 1); 0 =
# not chosen yet.  The converter's menu emission writes them on selection and
# the dialogue-condition conversion reads them back: TES4 GetIsPlayerBirthsign
# (func 224, dead in Skyrim) / GetPCIsClass (129, dead — the player never has
# a TES4 class) become GetGlobalValue(<choice>) == index+1, which is how the
# Emperor's post-birthsign line matches the sign actually picked.
CHARGEN_BIRTHSIGN_GLOBAL = 'TES4ChargenBirthsignChoice'
CHARGEN_CLASS_GLOBAL = 'TES4ChargenClassChoice'
# Every page but the last carries this many real choices plus a trailing
# "More ..." button in slot PAGE_OPTIONS; the last page holds up to
# MAX_BUTTONS real choices.  Global choice index = sum of prior pages'
# PAGE_OPTIONS + the clicked button — the emitted script chains pages with
# exactly this arithmetic, so page composition here is a cross-module
# contract with the converter's ShowBirthsignMenu/ShowClassMenu emission.
PAGE_OPTIONS = MAX_BUTTONS - 1


def _paged(edid_fmt: str, title: str, labels: list) -> list:
    """[(mesg_edid, title, buttons)] pages over `labels` (see PAGE_OPTIONS)."""
    pages = []
    i = 0
    n = 1
    while i < len(labels):
        rest = len(labels) - i
        if rest <= MAX_BUTTONS:
            take, more = rest, False
        else:
            take, more = PAGE_OPTIONS, True
        pages.append((edid_fmt % n, title,
                      list(labels[i:i + take]) + (['More ...'] if more else [])))
        i += take
        n += 1
    return pages


def build_chargen_menus(bsgn_records: list, clas_records: list,
                        spel_edid_by_fid24: dict) -> dict:
    """Shared birthsign/class menu plan.

    The importer authors one MESG per page; the converter emits the Show()
    chain plus, for birthsigns, the chosen sign's AddSpell calls (TES4
    ShowBirthsignMenu granted the sign's spells — BSGN lists them, and the
    spells themselves are converted SPEL records referenced here by
    EditorID).  Classes have no expressible effect in Skyrim (skills and
    attributes are gone), so the class menu is choice-and-pacing only.

    Both sides MUST derive identical page EDIDs and button order, so
    everything is sorted by display name.
    """
    plan = {}
    signs = []
    for rec in bsgn_records:
        full = rec.get('FULL') or rec.get('EditorID') or ''
        if not full:
            continue
        spells = []
        i = 0
        while True:
            fid = rec.get(f'Spell[{i}]')
            if fid is None:
                break
            edid = spel_edid_by_fid24.get(int(fid, 16) & 0xFFFFFF)
            if edid:
                spells.append(edid)
            i += 1
        try:
            fid24 = int(rec.get('FormID', '0'), 16) & 0xFFFFFF
        except ValueError:
            fid24 = 0
        signs.append((full, spells, fid24))
    signs.sort(key=lambda s: s[0].lower())
    if signs:
        plan['birthsign'] = {
            'pages': _paged(CHARGEN_BIRTHSIGN_EDID,
                            'Under which sign were you born?',
                            [s[0] for s in signs]),
            'actions': [s[1] for s in signs],
            # BSGN fid24 -> menu index, for GetIsPlayerBirthsign conditions.
            'fid_to_index': {s[2]: i for i, s in enumerate(signs) if s[2]},
            'choice_global': CHARGEN_BIRTHSIGN_GLOBAL,
        }

    classes = []
    for rec in clas_records:
        # TES4 CLAS DATA.Flags bit 0 = Playable; only those ever appear in
        # ShowClassMenu.
        try:
            playable = int(rec.get('DATA.Flags', '0')) & 0x1
        except ValueError:
            playable = 0
        full = rec.get('FULL') or ''
        if playable and full:
            try:
                fid24 = int(rec.get('FormID', '0'), 16) & 0xFFFFFF
            except ValueError:
                fid24 = 0
            classes.append((full, fid24))
    names = sorted({c[0] for c in classes}, key=str.lower)
    if classes:
        index_of = {n.lower(): i for i, n in enumerate(names)}
        plan['class'] = {
            'pages': _paged(CHARGEN_CLASS_EDID, 'Choose your class.', names),
            'actions': [[] for _ in names],
            # Two CLAS records sharing a display name share the menu slot.
            'fid_to_index': {fid: index_of[full.lower()]
                             for full, fid in classes if fid},
            'choice_global': CHARGEN_CLASS_GLOBAL,
        }
    return plan
