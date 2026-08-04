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
