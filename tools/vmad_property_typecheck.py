"""Predict VMAD property binding failures WITHOUT running the game.

The engine binds a VMAD object property only when the target record's type is
compatible with the type the script DECLARES. When it is not, the property
reads None for the whole session and silently aborts every function that
touches it -- and the only evidence is a line in the Papyrus log, which costs a
full play session to obtain.

This does the same check statically: for every `<Type> Property <Name>` in the
converted sources, resolve <Name> to its record in the output plugin and report
the pairs that cannot bind. Run it after `--scripts-only` to catch a whole class
of runtime failures before shipping.

Usage:
  python tools/vmad_property_typecheck.py --plugin Oblivion.esm
  python tools/vmad_property_typecheck.py --plugin Morrowind_ob.esm --verbose

KNOWN LIMITATION: properties are resolved by NAME against this plugin only, so
a property that binds to a MASTER's record (a vanilla Skyrim Race named Orc, a
Weather named Rain) is reported against whatever same-named record this plugin
happens to own. Ambiguity inside one plugin is filtered; cross-master collisions
are not. Confirm a small residue against the record's actual VMAD FormID before
treating it as a real defect.
"""
import argparse
import os
import re
import sys
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, ROOT)

# Papyrus type -> record signatures that satisfy it. Only the types the
# converter actually emits need an entry; anything else is reported as unknown
# rather than guessed at.
_ACCEPTS = {
    'Armor': {'ARMO'}, 'Weapon': {'WEAP'}, 'Book': {'BOOK'},
    'Potion': {'ALCH'}, 'Ingredient': {'INGR'}, 'Light': {'LIGH'},
    'MiscObject': {'MISC'}, 'Key': {'KEYM'}, 'Ammo': {'AMMO'},
    'SoulGem': {'SLGM'}, 'Scroll': {'SCRL'}, 'Activator': {'ACTI'},
    'Flora': {'FLOR'}, 'Furniture': {'FURN'}, 'Static': {'STAT'},
    'Container': {'CONT'}, 'Door': {'DOOR'},
    'LeveledItem': {'LVLI'}, 'LeveledActor': {'LVLN'},
    'LeveledSpell': {'LVSP'},
    'Quest': {'QUST'}, 'Faction': {'FACT'}, 'GlobalVariable': {'GLOB'},
    'Spell': {'SPEL'}, 'Enchantment': {'ENCH'}, 'MagicEffect': {'MGEF'},
    'Race': {'RACE'}, 'Class': {'CLAS'}, 'Package': {'PACK'},
    'Sound': {'SOUN', 'SNDR'}, 'Topic': {'DIAL'}, 'FormList': {'FLST'},
    'Keyword': {'KYWD'}, 'EffectShader': {'EFSH'}, 'Weather': {'WTHR'},
    'WorldSpace': {'WRLD'}, 'Message': {'MESG'}, 'Outfit': {'OTFT'},
    'VoiceType': {'VTYP'}, 'ImageSpaceModifier': {'IMAD'},
    'Explosion': {'EXPL'}, 'Projectile': {'PROJ'}, 'Hazard': {'HAZD'},
    'ImpactDataSet': {'IPDS'}, 'Idle': {'IDLE'}, 'Shout': {'SHOU'},
    'ActorBase': {'NPC_'}, 'Location': {'LCTN'},
    # A Cell property binds ONLY to an interior cell -- an exterior grid cell
    # is not addressable this way (verified: all 43 vanilla Skyrim Cell
    # properties name interiors). Checked specially below.
    'Cell': {'CELL'},
    # Reference types: a placed ref, or anything for the permissive ones.
    'ObjectReference': {'REFR', 'ACHR', 'ACRE'},
    'Actor': {'ACHR', 'ACRE', 'REFR'},
}
# These accept anything; never report them.
_PERMISSIVE = {'Form', 'ScriptObject', 'Alias', 'ReferenceAlias'}


def main():
    ap = argparse.ArgumentParser(
        description='Statically predict VMAD property binding failures')
    ap.add_argument('--plugin', default='Oblivion.esm')
    ap.add_argument('--verbose', '-v', action='store_true',
                    help='list offending properties, not just counts')
    ap.add_argument('--max', type=int, default=25)
    args = ap.parse_args()

    from dialog_emulator import read_tes5_file
    path = os.path.join(ROOT, 'output', args.plugin, args.plugin)
    hdr, recs, _loc = read_tes5_file(path)

    rec_type, interior, ambiguous = {}, {}, {}
    for r in recs:
        ed = [s for s in r.subrecords if s.type == 'EDID']
        if not ed:
            continue
        n = ed[0].data.split(b'\x00')[0].decode('latin-1').lower()
        if n in rec_type and rec_type[n] != r.type:
            ambiguous[n] = True
        rec_type.setdefault(n, r.type)
        # A leading-digit EditorID is emitted with the digits stripped, so it
        # competes for the stripped name too (DIAL `1DagothSUr` -> DagothSUr).
        stripped = n.lstrip('0123456789')
        if stripped and stripped != n:
            if stripped in rec_type and rec_type[stripped] != r.type:
                ambiguous[stripped] = True
            rec_type.setdefault(stripped, r.type)
        if r.type == 'CELL':
            fl = None
            for s in r.subrecords:
                if s.type == 'DATA':
                    fl = s.data[0]
            if fl is not None:
                interior[n] = bool(fl & 1)

    src = os.path.join(ROOT, 'output', args.plugin, 'scripts', 'source')
    decl = re.compile(r'^([A-Za-z_]\w*)\s+Property\s+(\w+)', re.M)
    bad = Counter()
    detail = defaultdict(list)
    checked = 0
    for fn in sorted(os.listdir(src)):
        if not fn.endswith('.psc'):
            continue
        text = open(os.path.join(src, fn), encoding='utf-8',
                    errors='replace').read()
        for m in decl.finditer(text):
            ptype, pname = m.group(1), m.group(2)
            if ptype in _PERMISSIVE or ptype.startswith('TES4_'):
                continue
            accepts = _ACCEPTS.get(ptype)
            if accepts is None:
                continue
            # `Player` is bound to PlayerRef (0x14), a REFR, never to the base
            # NPC_ record that shares the EditorID -- so the base record's
            # signature says nothing about whether it binds.
            if pname.lower() in ('player', 'playerref'):
                continue
            # Resolving the property NAME to a record is a guess: TES4
            # EditorIDs are not unique across types, and the binder may have
            # picked a different one (the DIAL `1DagothSUr` sanitizes to
            # `DagothSUr`, which also names a CELL). Only trust the name when
            # exactly one record answers to it.
            if ambiguous.get(pname.lower()):
                continue
            actual = rec_type.get(pname.lower())
            if actual is None:
                continue
            checked += 1
            if actual not in accepts:
                bad[(ptype, actual)] += 1
                detail[(ptype, actual)].append((fn, pname))
            elif ptype == 'Cell' and interior.get(pname.lower()) is False:
                bad[('Cell', 'CELL(exterior)')] += 1
                detail[('Cell', 'CELL(exterior)')].append((fn, pname))

    print(f'plugin: {args.plugin}')
    print(f'resolvable object properties checked: {checked}')
    print(f'predicted binding failures: {sum(bad.values())}')
    if bad:
        print()
        print(f'{"declared":<20} {"actual":<16} {"count":>7}')
        print('-' * 46)
        for (p, a), n in bad.most_common(args.max):
            print(f'{p:<20} {a:<16} {n:>7}')
    if args.verbose:
        for (p, a), n in bad.most_common(args.max):
            print()
            print(f'== {p} <- {a} ({n}) ==')
            for fn, pname in detail[(p, a)][:args.max]:
                print(f'  {fn}: {pname}')


if __name__ == '__main__':
    main()
