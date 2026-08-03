"""Build TESGameSelect.esp — the "Threads of Prophecy" new-game game selector.

A standalone, redistributable Skyrim SE plugin. On a new game it detects which
converted TES games are present in the load order (Oblivion, Nehrim,
Morroblivion) and offers the player a choice of which game to begin; picking one
hands control to that game's own character generation. Picking Skyrim leaves the
vanilla opening untouched.

Structure (all authored from scratch — no TES4 source):

  GLOB x4   TESGS_HasSkyrim / HasOblivion / HasNehrim / HasMorroblivion.
            Set by the quest script from its detection pass. The three
            converted-game globals each gate one menu button so absent games
            are not offered; TESGS_HasSkyrim gates nothing (Skyrim's button is
            unconditional) and exists so the detection state is inspectable
            in-game with `sqv` / `getglobalvalue` when diagnosing a menu that
            offered the wrong set.
  MESG      TESGSGameSelectMSG — the prompt. All four buttons are declared; the
            three converted-game buttons carry a GetGlobalValue(<its global>)
            == 1 condition, which is how vanilla builds a menu whose buttons
            vary at runtime (dunMiddenNamesMenuMSG uses the same pattern).
  QUST      TESGSGameSelect — Start Game Enabled, script-only (no stages, no
            aliases), carrying the VMAD that attaches TESGameSelectQuest.psc
            with its Message/GlobalVariable properties bound.

Only Skyrim.esm is a master: every foreign form is resolved at runtime with
Game.GetFormFromFile(), so the plugin loads with any subset of the games
installed, in any order.

Usage:
  python tools/make_game_select_esp.py                    # -> output/TESGameSelect/
  python tools/make_game_select_esp.py --outdir some/dir
  python tools/make_game_select_esp.py --no-compile       # skip Papyrus compile
"""
import argparse
import os
import shutil
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tes5_import.writer import (pack_record, pack_subrecord, pack_tes4_header,
                                pack_top_group, pack_string_subrecord,
                                pack_formid_subrecord, pack_uint32_subrecord,
                                _count_records_and_groups)
from tes5_import.dialog_conditions import build_ctda
from script_convert.pipeline import build_vmad_object_script

PLUGIN_NAME = 'TESGameSelect.esp'
SCRIPT_NAME = 'TESGameSelectQuest'

# This plugin's own FormIDs (mod index is assigned by load order at runtime;
# 0x01 here is the placeholder the engine rewrites, matching how every ESP is
# authored — the low 24 bits are what identify the record).
FID_GLOB_SKYRIM       = 0x01000800
FID_GLOB_OBLIVION     = 0x01000801
FID_GLOB_NEHRIM       = 0x01000802
FID_GLOB_MORROBLIVION = 0x01000803
FID_MESG              = 0x01000810
FID_QUST              = 0x01000820

# QUST DNAM flags: StartGameEnabled (0x01) | StartsEnabled (0x10).
SGE_FLAGS = 0x0011
# Journal-invisible control quest — this quest has no stages and must not be
# listed in the player's journal (vanilla type 0; see dialog_converter._quest_dnam).
QUEST_TYPE_NONE = 0

# CTDA function index 74 = GetGlobalValue(Global). Operator 0x00 is '=='.
FUNC_GET_GLOBAL_VALUE = 74

PROLOGUE = (
    "The threads of prophecy gather, and fate has not yet chosen its weave.\n\n"
    "Countless worlds turn upon this moment, each with a door standing open and "
    "no one yet walking through it. An Emperor dreams of a stranger in a cell. A "
    "prisoner wakes to the smell of ash and salt. A cart rolls toward Helgen. A "
    "land without gods waits for someone who owes them nothing.\n\n"
    "All of them are true until you choose. Where do the threads of prophecy "
    "bind you?"
)

# Button order here MUST match the GAME_* constants in TESGameSelectQuest.psc.
# A hidden button does NOT renumber the others — Message.Show() returns the
# button's own index regardless of which conditions passed (vanilla's
# dunMiddenHandSculptureSCRIPT depends on exactly this) — so index == game id.
#
# Skyrim's button carries NO condition and is therefore always drawn, matching
# dunMiddenNamesMenuMSG, whose final "do nothing" button is likewise
# unconditional. That guarantees the menu always has at least one valid choice
# and can never trap the player with nothing to click.
BUTTONS = [
    ('Skyrim  -  the cart rolls toward Helgen',           None),
    ('Cyrodiil  -  an Emperor has dreamt of you',         FID_GLOB_OBLIVION),
    ('Nehrim  -  a land that owes the gods nothing',      FID_GLOB_NEHRIM),
    ('Vvardenfell  -  an old prophecy stirs in the ash',  FID_GLOB_MORROBLIVION),
]

GLOBALS = [
    (FID_GLOB_SKYRIM,       'TESGS_HasSkyrim'),
    (FID_GLOB_OBLIVION,     'TESGS_HasOblivion'),
    (FID_GLOB_NEHRIM,       'TESGS_HasNehrim'),
    (FID_GLOB_MORROBLIVION, 'TESGS_HasMorroblivion'),
]


def build_glob(fid: int, edid: str) -> bytes:
    """A short-typed global, value 0. FNAM 's' = short; vanilla writes the value
    as a float regardless of the declared type."""
    subs = pack_string_subrecord('EDID', edid)
    subs += pack_subrecord('FNAM', b's')
    subs += pack_subrecord('FLTV', struct.pack('<f', 0.0))
    return pack_record('GLOB', fid, 0, subs)


def build_mesg() -> bytes:
    """The message box. DNAM bit 0 = Message Box (a full modal with buttons,
    not a corner notification); bit 1 (Auto Display) stays clear because the
    script shows it explicitly and reads the button index back."""
    subs = pack_string_subrecord('EDID', 'TESGSGameSelectMSG')
    subs += pack_string_subrecord('DESC', PROLOGUE)
    subs += pack_string_subrecord('FULL', 'The Threads of Prophecy')
    # INAM is a required leftover ("Icon (unused)") and is always NULL in vanilla.
    subs += pack_formid_subrecord('INAM', 0)
    subs += pack_uint32_subrecord('DNAM', 0x00000001)   # Message Box
    for text, gate_fid in BUTTONS:
        subs += pack_string_subrecord('ITXT', text)
        if gate_fid is not None:
            subs += pack_subrecord('CTDA', build_ctda(
                FUNC_GET_GLOBAL_VALUE, param1=gate_fid, comp_value=1.0,
                operator=0x00))
    return pack_record('MESG', FID_MESG, 0, subs)


def build_qust() -> bytes:
    """Start-Game-Enabled, script-only quest carrying the selector script.

    No stages, no aliases, no objectives: the script does everything from
    OnInit. Priority 0 — this quest arbitrates nothing, it just runs once.
    """
    vmad = build_vmad_object_script(
        SCRIPT_NAME,
        object_props={
            'GameSelectMenu':  FID_MESG,
            'HasSkyrim':       FID_GLOB_SKYRIM,
            'HasOblivion':     FID_GLOB_OBLIVION,
            'HasNehrim':       FID_GLOB_NEHRIM,
            'HasMorroblivion': FID_GLOB_MORROBLIVION,
        })

    subs = pack_string_subrecord('EDID', 'TESGSGameSelect')
    subs += pack_subrecord('VMAD', vmad)
    subs += pack_string_subrecord('FULL', 'Threads of Prophecy')
    # DNAM: Flags(U16) Priority(U8) FormVer(U8) Unknown(U32) Type(U32)
    subs += pack_subrecord('DNAM', struct.pack('<HBBII', SGE_FLAGS, 0, 0, 0,
                                               QUEST_TYPE_NONE))
    subs += pack_subrecord('NEXT', b'')
    # ANAM (next alias id) is written even with no aliases — vanilla always
    # carries it, and the CK adds it on load regardless.
    subs += pack_uint32_subrecord('ANAM', 0)
    return pack_record('QUST', FID_QUST, 0, subs)


def build_plugin() -> bytes:
    groups = [
        pack_top_group('GLOB', b''.join(build_glob(f, e) for f, e in GLOBALS)),
        pack_top_group('MESG', build_mesg()),
        pack_top_group('QUST', build_qust()),
    ]
    # HEDR count = records + GRUPs, matching vanilla Skyrim.esm and the main
    # writer. An undercount here is not cosmetic: the engine walks the file by
    # this number, and a wrong one silently drops records.
    count = sum(_count_records_and_groups(g) for g in groups)
    header = pack_tes4_header(
        ['Skyrim.esm'],
        num_records=count,
        next_object_id=0x900,
        author='TESConversion',
        description='Threads of Prophecy - choose which game to begin',
        is_esm=False)
    return header + b''.join(groups), count


def write_seq(outdir: str):
    """Start-Game-Enabled quests in an ESP only actually start when the plugin
    ships a .seq file listing them — a loose file under Data/seq/, never inside
    a BSA."""
    seq_dir = os.path.join(outdir, 'seq')
    os.makedirs(seq_dir, exist_ok=True)
    path = os.path.join(seq_dir, os.path.splitext(PLUGIN_NAME)[0] + '.seq')
    with open(path, 'wb') as f:
        f.write(struct.pack('<I', FID_QUST))
    return path


def compile_script(outdir: str) -> bool:
    """Compile TESGameSelectQuest.psc against the Skyrim SE headers."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    from convert import _find_skyrim_source_scripts
    headers = _find_skyrim_source_scripts()
    if not headers:
        print('  ERROR: Skyrim Papyrus source headers not found '
              '(<Skyrim SE>\\Data\\Source\\Scripts)')
        return False

    src_dir = os.path.join(outdir, 'scripts', 'source')
    out_dir = os.path.join(outdir, 'scripts')
    os.makedirs(out_dir, exist_ok=True)
    compiler = os.path.join(root, 'external', 'papyrus-compiler', 'papyrus.exe')
    if not os.path.isfile(compiler):
        print(f'  ERROR: Papyrus compiler not found at {compiler}')
        return False

    psc = os.path.join(src_dir, SCRIPT_NAME + '.psc')
    # -nocache: the compiler keys its cache on source content alone, so an
    # unchanged file silently produces no .pex without it.
    cmd = [compiler, 'compile', '-nocache', '-i', psc, '-o', out_dir,
           '-h', headers, '-h', src_dir]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90, cwd=root)
    out = (r.stdout or '') + (r.stderr or '')
    pex = os.path.join(out_dir, SCRIPT_NAME + '.pex')
    if r.returncode != 0 or not os.path.isfile(pex):
        print('  COMPILE FAILED:')
        print('   ', out.strip().replace('\n', '\n    '))
        return False
    print(f'  compiled {SCRIPT_NAME}.pex ({os.path.getsize(pex)} bytes)')
    return True


def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--outdir', default='output/TESGameSelect',
                    help='Data-folder-style output root (default: '
                         'output/TESGameSelect)')
    ap.add_argument('--no-compile', action='store_true',
                    help='Skip Papyrus compilation (plugin only)')
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    outdir = args.outdir if os.path.isabs(args.outdir) else os.path.join(root, args.outdir)
    os.makedirs(outdir, exist_ok=True)

    # Stage the hand-written script source into the output tree so the shipped
    # folder is a complete, self-contained Data folder.
    src_master = os.path.join(root, 'TESGameSelect', 'scripts', 'source',
                              SCRIPT_NAME + '.psc')
    src_dir = os.path.join(outdir, 'scripts', 'source')
    os.makedirs(src_dir, exist_ok=True)
    shutil.copyfile(src_master, os.path.join(src_dir, SCRIPT_NAME + '.psc'))

    data, count = build_plugin()
    esp_path = os.path.join(outdir, PLUGIN_NAME)
    with open(esp_path, 'wb') as f:
        f.write(data)
    print(f'Wrote {esp_path} ({len(data)} bytes, HEDR numRecords={count})')

    seq = write_seq(outdir)
    print(f'Wrote {seq}')

    ok = True
    if not args.no_compile:
        ok = compile_script(outdir)

    print('\nShip the contents of this folder as a Data folder:')
    print(f'  {PLUGIN_NAME}')
    print(f'  seq\\{os.path.splitext(PLUGIN_NAME)[0]}.seq')
    print(f'  scripts\\{SCRIPT_NAME}.pex')
    print(f'  scripts\\source\\{SCRIPT_NAME}.psc')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
