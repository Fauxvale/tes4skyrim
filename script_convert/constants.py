"""Constant lookup tables for TES4→Papyrus script conversion."""

import hashlib
import os
import re

# ===========================================================================
# Constants
# ===========================================================================

# Papyrus base class for a TES4 script attached to the PLAYER BASE record
# (NPC_ 0x00000007).  Oblivion let a plugin script the player that way -- Nehrim
# puts its whole XP/level/gold system AND `SetStage MQ00 1` (the intro's only
# starter) there.  Skyrim cannot: the acting player is PlayerRef 0x14, whose
# signature is PLYR (not ACHR, so a plugin cannot author an override of it), and
# its base is Skyrim's own Player 0x07 -- never the converted plugin's shifted
# copy, which no actor ever instantiates.  Vanilla's mechanism for "code that
# runs on the player forever" is a start-game-enabled quest holding a reference
# alias forced to 0x14 (71 vanilla QUSTs do exactly this); the script rides that
# alias.  `Self` there is the ReferenceAlias, so every implicit-self call is
# routed through GetReference()/GetActorReference() -- see
# ScriptConverter._implicit_self and tes5_import.object_scripts.
PLAYER_ALIAS_EXTENDS = 'ReferenceAlias'

# Oblivion block type -> Papyrus event mapping
# (event_signature, end_keyword)
BLOCK_MAP = {
    'gamemode':           ('Event OnUpdate()', 'EndEvent'),
    'menumode':           ('Event OnUpdate()', 'EndEvent'),
    'onactivate':         ('Event OnActivate(ObjectReference akActionRef)', 'EndEvent'),
    'onadd':              ('Event OnContainerChanged(ObjectReference akNewContainer, ObjectReference akOldContainer)', 'EndEvent'),
    'ondrop':             ('Event OnContainerChanged(ObjectReference akNewContainer, ObjectReference akOldContainer)', 'EndEvent'),
    'onequip':            ('Event OnEquipped(Actor akActor)', 'EndEvent'),
    'onunequip':          ('Event OnUnequipped(Actor akActor)', 'EndEvent'),
    'ondeath':            ('Event OnDeath(Actor akKiller)', 'EndEvent'),
    'onhit':              ('Event OnHit(ObjectReference akAggressor, Form akSource, Projectile akProjectile, bool abPowerAttack, bool abSneakAttack, bool abBashAttack, bool abHitBlocked)', 'EndEvent'),
    'onhitwith':          ('Event OnHit(ObjectReference akAggressor, Form akSource, Projectile akProjectile, bool abPowerAttack, bool abSneakAttack, bool abBashAttack, bool abHitBlocked)', 'EndEvent'),
    'onload':             ('Event OnLoad()', 'EndEvent'),
    'onreset':            ('Event OnReset()', 'EndEvent'),
    'onsell':             ('Event OnSell(Actor akSeller)', 'EndEvent'),
    # TES4 `Begin OnTrigger` runs EVERY FRAME an object is inside the volume,
    # not once on entry — Nehrim's Magieverbot (magic-ban) scripts count 25 and
    # 100 *executions* in it, which is only meaningful under repeat semantics.
    # Skyrim keeps the same three-way split (all three are distinct engine
    # events in SkyrimSE.exe): OnTrigger = "trigger is tripped", sent
    # repeatedly while inside; OnTriggerEnter/Leave are the edges.  Mapping
    # OnTrigger -> OnTriggerEnter froze every such state machine on its first
    # state, which left the Erothin bell latch stuck and re-ringing.
    'ontrigger':          ('Event OnTrigger(ObjectReference akActionRef)', 'EndEvent'),
    'ontriggerenter':     ('Event OnTriggerEnter(ObjectReference akActionRef)', 'EndEvent'),
    'ontriggerleave':     ('Event OnTriggerLeave(ObjectReference akActionRef)', 'EndEvent'),
    'onmagiceffectapply': ('Event OnMagicEffectApply(ObjectReference akCaster, MagicEffect akEffect)', 'EndEvent'),
    'oninit':             ('Event OnInit()', 'EndEvent'),
    'onpackagestart':     ('Event OnPackageStart(Package akNewPackage)', 'EndEvent'),
    'onpackagedone':      ('Event OnPackageEnd(Package akOldPackage)', 'EndEvent'),
    'onpackageend':       ('Event OnPackageEnd(Package akOldPackage)', 'EndEvent'),
    'onpackagechange':    ('Event OnPackageChange(Package akOldPackage)', 'EndEvent'),
    # OnTriggerActor/OnTriggerMob differ from OnTrigger only in WHAT trips them
    # (any actor / any creature), not in edge-vs-repeat — they are per-frame
    # too, so they take the repeating event as well.  Skyrim has no
    # actor-vs-creature split, so the filter is left to the block body.
    'ontriggeractor':     ('Event OnTrigger(ObjectReference akActionRef)', 'EndEvent'),
    'ontriggermob':       ('Event OnTrigger(ObjectReference akActionRef)', 'EndEvent'),
    'onmagiceffecthit':   ('Event OnMagicEffectApply(ObjectReference akCaster, MagicEffect akEffect)', 'EndEvent'),
    'onactorequip':       ('Event OnEquipped(Actor akActor)', 'EndEvent'),
    # OnAlarm (actor noticed a crime/attack) has no Papyrus event; entering
    # combat/search via OnCombatStateChanged is the closest trigger.  The block
    # loop adds an aeCombatState guard per block type (alarm: != 0, start
    # combat: == 1) so the two merge cleanly into one event.
    'onalarm':            ('Event OnCombatStateChanged(Actor akTarget, int aeCombatState)', 'EndEvent'),
    'onstartcombat':      ('Event OnCombatStateChanged(Actor akTarget, int aeCombatState)', 'EndEvent'),
    # Signatures are fixed by ActiveMagicEffect.psc — an invented one fails to
    # compile ("the parameter types of function oneffectstart ... do not match
    # the parent script activemagiceffect").
    'scripteffectstart':  ('Event OnEffectStart(Actor akTarget, Actor akCaster)', 'EndEvent'),
    'scripteffectfinish': ('Event OnEffectFinish(Actor akTarget, Actor akCaster)', 'EndEvent'),
    'scripteffectupdate': ('Event OnUpdate()', 'EndEvent'),
}

# Oblivion block filters (`begin OnEquip player`, `begin OnTrigger player`,
# `begin OnPackageDone SomePackage`) restrict the block to fire only for that
# object.  Papyrus has no such filter, so the block body must be wrapped in an
# equivalent guard on the event parameter that carries the filtered object.
#
# Maps block type -> (event parameter name, Papyrus type of that parameter).
# A block type absent from this table has no parameter to filter on, so its
# filter cannot be expressed and is dropped (with a TODO).
BLOCK_FILTER_PARAM = {
    'onactivate':         ('akActionRef', 'ObjectReference'),
    'onadd':              ('akNewContainer', 'ObjectReference'),
    'ondrop':             ('akOldContainer', 'ObjectReference'),
    'onequip':            ('akActor', 'Actor'),
    'onactorequip':       ('akActor', 'Actor'),
    'onunequip':          ('akActor', 'Actor'),
    'onsell':             ('akSeller', 'Actor'),
    'ontrigger':          ('akActionRef', 'ObjectReference'),
    'ontriggerenter':     ('akActionRef', 'ObjectReference'),
    'ontriggerleave':     ('akActionRef', 'ObjectReference'),
    'ontriggeractor':     ('akActionRef', 'ObjectReference'),
    'ontriggermob':       ('akActionRef', 'ObjectReference'),
    'onhit':              ('akAggressor', 'ObjectReference'),
    'onhitwith':          ('akSource', 'Form'),
    'ondeath':            ('akKiller', 'Actor'),
    'onstartcombat':      ('akTarget', 'Actor'),
    'onmagiceffecthit':   ('akEffect', 'MagicEffect'),
    'onmagiceffectapply': ('akEffect', 'MagicEffect'),
    'onpackagestart':     ('akNewPackage', 'Package'),
    'onpackagedone':      ('akOldPackage', 'Package'),
    'onpackageend':       ('akOldPackage', 'Package'),
    'onpackagechange':    ('akOldPackage', 'Package'),
}

# Oblivion type -> Papyrus type mapping
TYPE_MAP = {
    'short': 'Int',
    'long':  'Int',
    'int':   'Int',
    'float': 'Float',
    'ref':   'ObjectReference',
    'reference': 'ObjectReference',
    # OBSE types.  Without these the variable got NO declaration at all and
    # every use was an undefined identifier (HMSfromFloat24h builds its return
    # value in a `string_var sTime`).  Papyrus String is the direct equivalent;
    # array_var has none, so it falls back to a String the script can at least
    # declare and assign.
    'string_var': 'String',
    'array_var':  'String',
}

# Actor value name mapping (TES4 -> TES5)
# TES4 attribute names. SKYRIM HAS NO ATTRIBUTES — Strength, Intelligence,
# Willpower, Agility, Speed, Endurance, Personality and Luck do not exist as
# actor values, and no TES5 actor value is a faithful stand-in, because every
# candidate sits on a different scale than TES4's 0-100.
#
# They used to be aliased onto the nearest-looking AV here
# (strength->UnarmedDamage, endurance->HealRate, agility/speed/acrobatics->
# SpeedMult, personality->Speechcraft, luck->LuckModifier — which is not even
# a real AV name, so it failed silently). That broke every Morroblivion guild:
# the Fighters Guild gates each rank on `Player.GetAV Strength >= 30 &&
# Player.GetAV Endurance >= 30`, and UnarmedDamage sits near 0, so no character
# could ever qualify at any level; the Thieves Guild's Agility gate read
# SpeedMult (~100) and passed unconditionally instead.
#
# An attribute read is now a no-op that returns ATTRIBUTE_STUB_VALUE, so the
# gate falls OPEN, and an attribute write is discarded. Falling open is the
# faithful outcome: an Oblivion attribute gate exists to keep an
# under-developed character out, and a Skyrim character cannot raise an
# attribute at all, so enforcing it would lock the content away permanently
# rather than merely early. Mirrors dialog_conditions._TES4_AV_ATTRIBUTES,
# which drops the equivalent CTDA, and TES4Polyfill.IsTES4Attribute.
TES4_ATTRIBUTES = frozenset({
    'strength', 'intelligence', 'willpower', 'agility',
    'speed', 'endurance', 'personality', 'luck',
})

# Value substituted for a removed attribute read. Above every authored TES4
# attribute threshold (TES4 attributes cap at 100; the highest in the guild
# advancement scripts is 35) so `>=` gates pass, and positive so the rarer
# `> 0` / `!= 0` forms behave the same way.
ATTRIBUTE_STUB_VALUE = '100.0'

ACTOR_VALUE_MAP = {
    'armorer':      'Smithing',
    'athletics':    'Stamina',
    'blade':        'OneHanded',
    'block':        'Block',
    # Blunt is Oblivion's mace/warhammer skill and covers BOTH one- and
    # two-handed blunt weapons; Skyrim splits them. OneHanded matches Blade so
    # a script comparing the two reads one consistent scale, and it is what
    # skyrim_overrides.TES4_SKILL_TO_TES5_INDEX already uses on the record side.
    'blunt':        'OneHanded',
    'handtohand':   'UnarmedDamage',
    'heavyarmor':   'HeavyArmor',
    'alchemy':      'Alchemy',
    'alteration':   'Alteration',
    'conjuration':  'Conjuration',
    'destruction':  'Destruction',
    'illusion':     'Illusion',
    # Mysticism was folded into Illusion in Skyrim (Detect Life, Telekinesis
    # and Soul Trap all became Illusion/Conjuration spells); Alteration was a
    # mismatch with the record side, which already maps it to Illusion.
    'mysticism':    'Illusion',
    'restoration':  'Restoration',
    # Acrobatics and Athletics have no Skyrim skill at all. Stamina is the
    # athletic-capacity value the engine actually tracks, and matches the
    # 0-100 scale a TES4 skill threshold expects far better than SpeedMult
    # (which sits at ~100 for everyone and made every gate pass).
    'acrobatics':   'Stamina',
    'lightarmor':   'LightArmor',
    'marksman':     'Marksman',
    'mercantile':   'Speechcraft',
    'security':     'Lockpicking',
    'sneak':        'Sneak',
    'speechcraft':  'Speechcraft',
    'health':       'Health',
    'magicka':      'Magicka',
    'fatigue':      'Stamina',
    'encumbrance':  'CarryWeight',
    'invisibility': 'Invisibility',
    'chameleon':    'Invisibility',
    'nighteye':     'NightEye',
    'waterbreathing': 'WaterBreathing',
    'waterwalking': 'WaterWalking',
    'paralysis':    'Paralysis',
    'detectlife':   'DetectLifeRange',
    'blindness':    'Blindness',
    # Skyrim has NO silence actor value — the engine's AV name table (verified
    # against SkyrimSE.exe) runs ...Blindness, WeaponSpeedMult... with nothing
    # between, and 'MuteModifier' (what this used to emit) is not a name the
    # engine knows, so every read returned 0 and every write was rejected.
    # Silence is a spell-supplied condition in Skyrim, not a trackable value;
    # omitting it leaves the AV name unmapped, which is the honest outcome.
    'resistfire':   'FireResist',
    'resistfrost':  'FrostResist',
    'resistshock':  'ElectricResist',
    'resistmagic':  'MagicResist',
    'resistdisease':'DiseaseResist',
    'resistpoison': 'PoisonResist',
    'resistnormalweapons': 'DamageResist',
    'aggression':   'Aggression',
    'confidence':   'Confidence',
    # Energy is an AI trait in BOTH games (TES4 AV 35, TES5 AV 2), not a pool.
    # Mapping it to Magicka aliased an AI personality value onto the actor's
    # spell resource, so a scripted energy change silently drained or refilled
    # magicka instead.
    'energy':       'Energy',
    'responsibility': 'Morality',
}

# Every TES4 command whose FIRST argument is an actor-value name.  Used both to
# quote that argument and to detect calls naming a removed attribute.
_ACTOR_VALUE_FUNCTIONS = frozenset({
    'getactorvalue', 'setactorvalue', 'modactorvalue', 'forceactorvalue',
    'getav', 'setav', 'modav', 'forceav', 'getbaseactorvalue', 'getbaseav',
    'modpcskill', 'advancepcskill',
    'modav2', 'modactorvalue2', 'getav2', 'setav2',
})

# The subset that READS an actor value, so a call naming a removed attribute
# has to yield a value.  The rest write, and are dropped instead.
_ACTOR_VALUE_READ_FUNCTIONS = frozenset({
    'getactorvalue', 'getav', 'getbaseactorvalue', 'getbaseav', 'getav2',
})

# TES4 global variables that exist in Skyrim — these need GlobalVariable property access
KNOWN_GLOBALS = {
    'gamehour', 'gamedayspassed', 'gameday', 'gamemonth', 'gameyear',
    'timescale',
}

def _load_papyrus_script_names() -> set:
    """Every script name Skyrim ships (types AND gameplay scripts).

    The compiler rejects a variable or property named the same as ANY script it
    can see ("cannot name a variable or property the same as a known type or
    script"), and then every use of that name also fails ("Door is not a
    variable") — one bad name takes its whole dependency chain down with it.
    Oblivion EditorIDs collide freely: `Door`, `DarkBrotherhood`, `MS14`, ...

    Read from a checked-in list (generated by tools/gen_papyrus_reserved.py from
    Data/Scripts.zip) rather than the live Data/Source/Scripts, so the conversion
    is reproducible and does not shift with the user's installed mods.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'papyrus_reserved.txt')
    try:
        with open(path, encoding='utf-8') as f:
            return {ln.strip().lower() for ln in f
                    if ln.strip() and not ln.startswith('#')}
    except OSError:
        return set()


# Papyrus reserved words — cannot be used as property names
_PAPYRUS_RESERVED = {
    'self', 'parent', 'as', 'is', 'new', 'return', 'if', 'else', 'elseif',
    'endif', 'while', 'endwhile', 'function', 'endfunction', 'event',
    'endevent', 'property', 'endproperty', 'state', 'endstate', 'auto',
    'autoreadonly', 'import', 'extends', 'native', 'global', 'hidden',
    'conditional', 'int', 'float', 'bool', 'string', 'none', 'true', 'false',
    'length', 'scriptname', 'next',
} | _load_papyrus_script_names()

# Crime bounties used to reconstruct TES4's three per-faction crime booleans
# (GetPCFactionMurder / Attack / Steal) from Skyrim's crime-gold split, which is
# the only part of the system Papyrus can reach.  These are the vanilla CRVA
# amounts: every one of Skyrim.esm's 14 real crime factions uses exactly
# murder=1000, assault=40, and the steal multiplier applies to item value.  The
# importer writes the same numbers into every converted crime faction's CRVA
# (tes5_import/record_types/actors.py), so the two sides must stay in step.
TES4_MURDER_BOUNTY = 1000
TES4_ASSAULT_BOUNTY = 40
TES4_STEAL_BOUNTY = 100

# Comprehensive function mapping
# key: lowercased oblivion function name
# value: (papyrus_expression, needs_self, note_or_none)
FUNCTION_MAP = {
    # --- Actor Values ---
    'getactorvalue':     ('GetActorValue',     True,  None),
    'setactorvalue':     ('SetActorValue',     True,  None),
    'modactorvalue':     ('ModActorValue',     True,  None),
    'forceactorvalue':   ('ForceActorValue',   True,  None),
    'getav':             ('GetActorValue',     True,  None),
    'setav':             ('SetActorValue',     True,  None),
    'modav':             ('ModActorValue',     True,  None),
    'forceav':           ('ForceActorValue',   True,  None),
    'getbaseactorvalue': ('GetBaseActorValue', True,  None),
    'getbaseav':         ('GetBaseActorValue', True,  None),

    # --- Items / Inventory ---
    'additem':           ('AddItem',           True,  None),
    'removeitem':        ('RemoveItem',        True,  None),
    'getitemcount':      ('GetItemCount',      True,  None),
    'equipitem':         ('EquipItem',         True,  None),
    'unequipitem':       ('UnequipItem',       True,  None),
    'removeallitems':    ('RemoveAllItems',    True,  None),
    'getnumitems':       ('GetNumItems',       True,  None),
    'getinventoryobject':('GetNthForm',        True,  None),
    'drop':              ('DropObject',        True,  None),

    # --- Spells ---
    'addspell':          ('AddSpell',          True,  None),
    'removespell':       ('RemoveSpell',       True,  None),
    'hasspell':          ('HasSpell',          True,  None),
    'cast':              ('Cast',              True,  None),
    'dispel':            ('DispelSpell',       True,  None),
    'dispelspell':       ('DispelSpell',       True,  None),
    'dispelallspells':   ('DispelAllSpells',   True,  None),
    'getspellcount':     (None,                True,  None),  # no-op
    'getnthspell':       (None,                True,  None),  # no-op

    # --- Movement / Position ---
    'moveto':            ('MoveTo',            True,  None),
    'getdistance':       ('GetDistance',       True,  None),
    'getparentcell':     ('GetParentCell',     True,  None),
    'setposition':       ('SetPosition',       True,  None),
    'getlinkedref':      ('GetLinkedRef',      True,  None),
    'getheadingangle':   ('GetHeadingAngle',   True,  None),
    'pathtoref':         (None,                True,  None),  # no-op

    # --- Enable / Disable ---
    'enable':            ('Enable',            True,  None),
    'disable':           ('Disable',           True,  None),
    'isenabled':         ('IsEnabled',         True,  None),
    'activate':          ('Activate',          True,  None),
    'delete':            ('Delete',            True,  None),
    'markfordelete':     ('Delete',            True,  None),
    'placeatme':         ('PlaceAtMe',         True,  None),
    # TES4 SetDestroyed drives the ENGINE destruction system: the ref switches
    # to its destroyed state — geometry breaks apart and collision drops — while
    # staying present in the world.  Skyrim keeps the same system and exposes it
    # natively as ObjectReference.SetDestroyed(bool) (vanilla ObjectReference.psc
    # line 541; command 4300 / opcode 0x10CC).  An earlier mapping to
    # BlockActivation only suppressed re-activation and never broke anything,
    # which is why breakaway planks and tripwires animated but stayed solid.
    'setdestroyed': ('SetDestroyed',       True,  None),

    # --- Actor State ---
    'kill':              ('Kill',              True,  None),
    'killandresurrect':  ('Kill',              True,  None),  # then Resurrect manually
    'resurrect':         ('Resurrect',         True,  None),
    'getdead':           ('IsDead',            True,  None),
    'isdead':            ('IsDead',            True,  None),
    'isincombat':        ('IsInCombat',        True,  None),
    # SetForceSneak is neutralised (no Skyrim equivalent), so the live sneak
    # state is the closest readable value for its getter.
    'getforcesneak':     ('IsSneaking',        True,  None),
    # TES4 knocked-down state ~ Skyrim's bleedout/recovery state.
    'getknockedstate':   ('IsBleedingOut',     True,  None),
    'startcombat':       ('StartCombat',       True,  None),
    'stopcombat':        ('StopCombat',        True,  None),
    'getisid':           (None,                True,  None),  # Special handler in _emit_function
    'getisrace':         (None,                True,  None),  # Special handler in _emit_function
    'getisclass':        (None,                True,  None),  # Special handler in _emit_function
    # IsActorDetected takes NO argument (UESP opcode 0x10B5, 0 params): "is this
    # actor detected by ANYONE".  GetDetected takes 1 Actor and asks the
    # OPPOSITE question from Skyrim's IsDetectedBy: `<observer>.GetDetected
    # <target>` is "does the observer detect the target", while
    # `<target>.IsDetectedBy(<observer>)` is "is the target detected by the
    # observer".  Mapping IsActorDetected to IsDetectedBy made the argument-less
    # form default to the player (`player.IsActorDetected` →
    # Game.GetPlayer().IsDetectedBy(Game.GetPlayer()), the player detecting
    # itself); mapping GetDetected positionally kept receiver and argument in
    # place and asked the mirror-image question.  Both now have special handlers
    # in _emit_function: IsActorDetected is a no-op (Skyrim has no "detected by
    # anyone" primitive, like GetDetectionLevel), GetDetected swaps the two refs.
    'isactordetected':   (None,                True,  None),
    'getdetected':       (None,                True,  None),  # Special handler in _emit_function
    'getincell':         (None,                True,  None),  # Special handler in _emit_function
    'getinsamecell':     (None,                True,  None),  # Special handler in _emit_function
    'getissex':          (None,                True,  None),  # Special handler
    'issneaking':        ('IsSneaking',        True,  None),
    'isweaponout':       ('IsWeaponDrawn',     True,  None),
    'isswimming':        (None,                True,  None),  # Special handler
    'getsitting':        ('GetSitState',       True,  None),
    'getsleeping':       ('GetSleepState',     True,  None),
    'getequipped':       ('IsEquipped',        True,  None),
    'getweaponanimtype': ('GetEquippedItemType', True, None),
    'clearlookat':       ('ClearLookAt',       True,  None),
    'getisalerted':      (None,                True,  None),  # Special handler
    'setessential':      (None,                False, None),  # Special handler
    'getisplayablerace': (None,                True,  None),  # Special handler
    'istalking':         ('IsInDialogueWithPlayer', True, None),
    'setunconscious':    ('SetUnconscious',    True,  None),
    'setghost':          ('SetGhost',          True,  None),
    'isghost':           ('IsGhost',           True,  None),
    'setcrimegold':      (None,                False, None),  # Special handler
    'getcrimegold':      (None,                False, None),  # Special handler
    'modcrimegold':      (None,                False, None),  # Special handler
    'setalert':          (None,                True,  None),  # Special handler
    'resetai':           ('ResetAI',           True,  None),

    # --- Factions ---
    'getinfaction':      ('IsInFaction',       True,  None),
    'getfactionrank':    ('GetFactionRank',    True,  None),
    'setfactionrank':    ('SetFactionRank',    True,  None),
    'modfactionrank':    ('ModFactionRank',    True,  None),
    'addfaction':        ('AddToFaction',      True,  None),
    'removefaction':     ('RemoveFromFaction',  True,  None),
    'removefromfaction': ('RemoveFromFaction',  True,  None),

    # --- AI ---
    'evp':               ('EvaluatePackage',   True,  None),
    'evaluatepackage':   ('EvaluatePackage',   True,  None),
    # setforcerun has a dedicated handler (SpeedMult); deliberately NOT mapped
    # here.  It carried ('SetDontMove', ...) — the exact inverse of "force this
    # actor to run" — which was unreachable only because the handler runs first.
    'setforcewalk':      (None,                True,  None),  # no-op
    'wait':              (None,                False, None),  # Special handler

    # --- Quest ---
    'setstage':          ('SetStage',          False, None),
    'getstage':          ('GetStage',          False, None),
    'getstagedone':      ('GetStageDone',      False, None),
    'startquest':        ('Start',             False, None),
    'stopquest':         ('Stop',              False, None),
    'getquestrunning':   ('IsRunning',         False, None),
    'setquestobject':    (None,                False, None),  # Special handler (no-op)
    'isquestcompleted':  ('IsCompleted',       False, None),
    'completequest':     ('CompleteQuest',      False, None),

    # --- UI / Messages ---
    'message':           ('Debug.Notification', False, None),
    'messagebox':        ('Debug.MessageBox',   False, None),
    'showmessage':       ('Debug.MessageBox',   False, None),
    'getbuttonpressed':  (None,                False, None),  # Special handler

    # --- Math (OBSE) ---
    # OBSE writes these with a bare whitespace operand (`set x to sin angleZ`),
    # which is why they reached the Papyrus parser unconverted as "no viable
    # alternative at input 'sin'".  Papyrus exposes the same set as globals on
    # Math.psc, and BOTH engines take/return DEGREES, so no unit conversion is
    # needed.  `exp`/`log` have no Papyrus native — see _EXP_POLYFILL below.
    'sin':               ('Math.sin',           False, None),
    'cos':               ('Math.cos',           False, None),
    'tan':               ('Math.tan',           False, None),
    'asin':              ('Math.asin',          False, None),
    'acos':              ('Math.acos',          False, None),
    'atan':              ('Math.atan',          False, None),
    'sqrt':              ('Math.sqrt',          False, None),
    'pow':               ('Math.pow',           False, None),
    'abs':               ('Math.abs',           False, None),
    'floor':             ('Math.Floor',         False, None),
    'ceil':              ('Math.Ceiling',       False, None),
    'exp':               ('TES4Polyfill.Exp',   False, None),
    'log':               ('TES4Polyfill.Log',   False, None),

    # --- OBSE "NS"/silent variants ---
    # The OBSE `...NS` forms differ from the vanilla command ONLY in suppressing
    # the pickup/spell sound and the "item added" message.  Papyrus's plain
    # calls take an abSilent argument for exactly that, so these are the same
    # command, not a missing feature.
    'additemns':         ('AddItem',           True,  None),
    'removeitemns':      ('RemoveItem',        True,  None),
    'addspellns':        ('AddSpell',          True,  None),
    'removespellns':     ('RemoveSpell',       True,  None),
    'equipitemsilent':   ('EquipItem',         True,  None),
    'equipitemns':       ('EquipItem',         True,  None),
    'unequipitemns':     ('UnequipItem',       True,  None),
    # OBSE aliases that only widen the vanilla command's argument types.
    'modav2':            ('ModActorValue',     True,  None),
    'modactorvalue2':    ('ModActorValue',     True,  None),
    'getav2':            ('GetActorValue',     True,  None),
    'setav2':            ('SetActorValue',     True,  None),
    'setcurrenthealth':  (None,                True,  None),  # Special handler
    'rand':              ('Utility.RandomFloat', False, None),
    'islocked':          ('IsLocked',          True,  None),
    'getequippedobject': ('GetEquippedWeapon', True,  None),
    # TES4 `LoopGroup <group>` plays an idle animation on repeat;
    # PlayGamebryoAnimation is Skyrim's own looping Gamebryo-animation call.
    'loopgroup':         ('PlayGamebryoAnimation', True, None),
    # OBSE `IsOnGround` is the complement of Skyrim's IsFlying: both engines
    # only distinguish "supported by the ground" from "not".
    'isonground':        (None,                False, None),  # Special handler
    'getglobalvalue':    (None,                False, None),  # Special handler
    'setglobalvalue':    (None,                False, None),  # Special handler
    # OBSE `IsModLoaded "Foo.esp"` — Morrowind_ob guards every Oblivion XP
    # hand-off with it.  Game.GetFormFromFile returns None for an unloaded
    # file, which answers the same question in vanilla Papyrus.
    'ismodloaded':       ('TES4Polyfill.IsModLoaded', False, None),
    # Written bare as `ref.GetRace == Argonian`, so without a FUNCTION_MAP entry
    # the ref.Func branch treated it as PROPERTY access and emitted
    # `ActorRef.GetRace` with no parens ("field or property `GetRace` not
    # found").  Actor.psc has the real native.
    'getrace':           ('GetRace',           True,  None),
    # No vanilla Papyrus equivalent — see _OBSE_NO_EQUIV_COMMANDS.
    'isunderwater':      (None,                True,  None),  # Special handler
    'getvampire':        (None,                True,  None),  # Special handler
    'getweapontype':     (None,                True,  None),  # Special handler
    'iswaiting':         (None,                True,  None),  # Special handler
    'getnumfollowers':   (None,                True,  None),  # Special handler
    'getnthfollower':    (None,                True,  None),  # Special handler
    'getspells':         (None,                True,  None),  # Special handler
    'setattackdamage':   (None,                True,  None),  # Special handler
    'togglespecialanim': (None,                True,  None),  # Special handler
    'setavmod':          (None,                True,  None),  # Special handler
    'starttimer':        (None,                False, None),  # Special handler
    'getmodlocaldata':   (None,                False, None),  # Special handler
    'setaltcontrol':     (None,                False, None),  # Special handler
    'equipitem2':        ('EquipItem',         True,  None),
    # TES4 `UncompleteQuest` reopens a finished quest; Quest.Reset() is the
    # Papyrus call that returns a quest to its un-run state.
    'uncompletequest':   (None,                True,  None),  # Special handler
    # OBSE file/plugin probes and god-mode read: no VANILLA Papyrus equivalent
    # (GetGodMode exists only in third-party SKSE plugins, not Game.psc).
    'fileexists':        (None,                False, None),  # Special handler
    'getgodmode':        (None,                False, None),  # Special handler
    'getplayerbirthsign': (None,               False, None),  # Special handler
    # Same question as IsModLoaded — route to the same polyfill.
    'isplugininstalled': ('TES4Polyfill.IsModLoaded', False, None),
    # OBSE `print`/`printc` write to the console log; Debug.Trace is Papyrus's
    # own log write, which is the same capability.
    'print':             ('Debug.Trace',       False, None),

    # --- OBSE commands with no VANILLA Papyrus equivalent (neutralised) ---
    # Each has been checked against Actor/ObjectReference/Game/Form/Utility and
    # exists in none of them.  Several are reachable via SKSE — see
    # docs/skse_conversion_audit.md — and neutralising is only the current
    # behaviour, not a judgement that SKSE is off the table.
    'preloadmagiceffect': (None,               False, None),  # Special handler
    'closeallmenus':     (None,                False, None),  # Special handler
    'setmodelpath':      (None,                False, None),  # Special handler
    'getmodelpath':      (None,                False, None),  # Special handler
    'setlowlevelprocessing': (None,            False, None),  # Special handler
    'setharvested':      (None,                False, None),  # Special handler
    'selectplayerspell': (None,                False, None),  # Special handler
    'setquestitem':      (None,                False, None),  # Special handler
    'setpcamurderer':    (None,                False, None),  # Special handler
    'setcellwaterheight': (None,               False, None),  # Special handler
    'setstringinisetting': (None,              False, None),  # Special handler
    'setstringgamesettingex': (None,           False, None),  # Special handler
    'getobseversion':    (None,                False, None),  # Special handler
    'getformfrommod':    (None,                False, None),  # Special handler
    'getfirstref':       (None,                False, None),  # Special handler
    'getnextref':        (None,                False, None),  # Special handler
    'getaltcontrol2':    (None,                False, None),  # Special handler
    'sifh':              (None,                True,  None),  # SetIgnoreFriendlyHits alias
    'equipme':           (None,                True,  None),  # Special handler
    'modavmod':          (None,                True,  None),  # Special handler
    'getvelocity':       (None,                True,  None),  # Special handler
    'setvelocity':       (None,                True,  None),  # Special handler

    # --- Camera / 3D refresh (OBSE) ---
    # `ToggleFirstPerson 0/1` forces the camera into third/first person.  Skyrim
    # splits it into two argument-free globals, so the argument picks which —
    # handled in _emit_function (the bare form toggles, which has no global).
    'togglefirstperson': (None,                False, None),  # Special handler
    # Vanilla Papyrus can FORCE a camera mode but cannot QUERY one
    # (Game.psc has ForceFirstPerson/ForceThirdPerson and nothing else;
    # GetCameraState is SKSE).  Every caller here guards a model-refresh, and
    # Skyrim's own model-swap script for the same job — DLC1PlayerVampire-
    # ChangeScript, which re-skins the player exactly like the werewolf swap —
    # just calls Game.ForceThirdPerson() unconditionally rather than testing.
    # So the test is reported False and the refresh path always runs, matching
    # vanilla behaviour instead of inventing a query that does not exist.
    'isthirdperson':     (None, False, None),  # Special handler
    # OBSE `ref.Update3D` rebuilds a reference's 3D after its model changed
    # (Morrowind_ob calls it through fbmwUpdate3D after a werewolf model swap).
    # Papyrus has no direct call — QueueNiNodeUpdate is SKSE — but the engine's
    # own refresh idiom is a disable/enable cycle, which tears down and rebuilds
    # exactly the same 3D.
    'update3d':          (None,                False, None),  # Special handler

    # --- Game State ---
    'getgamesetting':    ('Game.GetGameSettingFloat', False, None),
    'getgs':             ('Game.GetGameSettingFloat', False, None),
    'getpcissex':        (None,                 False, None),  # Special handler in _emit_function
    'getpcinfaction':    ('Game.GetPlayer().IsInFaction', False, None),
    'ispcrace':          (None,                False, None),  # Special handler
    'getrandompercent':  ('Utility.RandomInt',  False, None),
    'getamountsoldstolen': (None,              False, None),  # Special handler (TES4GoldFenced)
    'showracemenu':      ('Game.ShowRaceMenu', False, None),
    'showdialogsubtitles':(None,               False, None),  # Special handler (no-op)
    'getlevel':          ('GetLevel',           True,  None),
    # 'isininterior' handled by special handler in _emit_function
    'getcurrentgametime':('Utility.GetCurrentGameTime', False, None),
    'getdayofweek':      (None,                False, None),  # Special handler
    'getcurrenttime':    ('Utility.GetCurrentGameTime', False, None),
    'getsecondspassed':  (None,                False, None),  # Special: replaced inline
    'isplayerinprison':  (None,                False, None),  # Special handler
    'getplayerinjail':   (None,                False, None),  # Special handler
    'getgameloaded':     (None,                False, None),  # no-op

    # --- Sound ---
    'playsound':         (None,                False, None),  # Special handler
    'playsound3d':       (None,                False, None),  # Special handler
    'stopsound':         (None,                False, None),  # Special handler

    # --- Animation ---
    'playgroup':         (None,                True,  None),  # Special handler
    'lookismile':        (None,                True,  None),  # no-op
    'lookat':            ('SetLookAt',         True,  None),
    'stoplook':          ('ClearLookAt',       True,  None),

    # --- Misc ---
    'getself':           (None,                False, None),  # Special: replaced with Self
    'getcontainer':      ('GetContainer',      True,  None),
    'getparentref':      ('GetLinkedRef',      True,  None),
    'showmap':           (None,                False, None),  # Special handler
    'lock':              ('Lock',              True,  None),
    'unlock':            ('Lock',              True,  None),  # handled by special handler below
    'getlocked':         ('IsLocked',          True,  None),
    'getlocklevel':      ('GetLockLevel',      True,  None),
    'setownership':      ('SetActorOwner',     True,  None),  # handled by special handler above
    'getownership':      (None,                False, None),  # no-op
    'setscale':          ('SetScale',          True,  None),
    'getscale':          ('GetScale',          True,  None),
    'purgecellbuffers':  (None,                False, None),  # Special handler (no-op)
    'pcb':               (None,                False, None),  # Special handler (no-op)
    'closeobliviongate': (None,                False, None),  # Special handler (no-op)
    'say':               ('Say',               True,  None),
    'reset3dstate':      (None,                False, None),  # Special handler
    'setactorsai':       (None,                True,  None),  # Special handler
    'addtopic':          (None,                False, None),  # Special handler (no-op)
    'setcellpublicflag': (None,                True,  None),  # Special handler (no-op)
    'moddisposition':    (None,                True,  None),  # Special handler
    'getdisposition':    (None,                True,  None),  # Special handler
    'setfactionreaction':('SetReaction',       False, None),
    'modfactionreaction':('ModReaction',       False, None),
    'isactionref':       (None,                False, None),  # Special: compare akActionRef
    'getactionref':      (None,                False, None),  # Special: returns akActionRef
    'iscurrentfurnitureref': (None,            True,  None),  # no-op
    'iscurrentfurnitureobj': (None,            True,  None),  # no-op
    'showenchantment':   (None,                False, None),  # no-op
    'triggerscreenblood': ('Game.TriggerScreenBlood', False,  None),
    'isonguard':         (None,                True,  None),  # no-op
    'setactorfullname':  (None,                True,  None),  # Special handler
    'setcellfullname':   (None,                True,  None),  # Special handler (no-op)
    'respawnhorse':      (None,                True,  None),  # no-op
    'setdoordisabletakeoff':(None,             True,  None),  # no-op
    'setdoordefaultopen':('SetOpen',           True,  None),
    'opendoor':          ('SetOpen',           True,  None),
    'closedoor':         ('SetOpen',           True,  None),
    'setweather': (None,                  False,  None),  # Special handler
    'sw': (None,                  False,  None),  # Special handler
    'forceweather': (None,                  False,  None),  # Special handler
    'fw': (None,                  False,  None),  # Special handler
    'releaseweatheroverride': (None,                  False,  None),  # Special handler
    'getbookread':       (None,                True,  None),  # Special handler
    'removeme':          ('Delete',            True,  None),

    # --- Object state ---
    'getisref':          (None,                True,  None),  # Special handler
    'hasvariable':       (None,                False, None),  # no-op
    'setdisabled':       ('Disable',           True,  None),
    'setenabled':        ('Enable',            True,  None),
    'getis3dloaded':     ('Is3DLoaded',        True,  None),
    'hasbeenpickedup':   (None,                True,  None),  # no-op

    # --- Weather ---
    'getweatherpercent': (None,                False, None),  # Special handler
    'forceweather': (None,                  False,  None),  # Special handler
    'releaseweatheroverride': (None,                  False,  None),  # Special handler

    # --- Special compound player.X ---
    'player.additem':    ('Game.GetPlayer().AddItem', False, None),
    'player.removeitem': ('Game.GetPlayer().RemoveItem', False, None),
    'player.getitemcount': ('Game.GetPlayer().GetItemCount', False, None),
    'player.addspell':   ('Game.GetPlayer().AddSpell', False, None),
    'player.removespell':('Game.GetPlayer().RemoveSpell', False, None),
    'player.moveto':     ('Game.GetPlayer().MoveTo', False, None),
    'player.placeatme':  ('Game.GetPlayer().PlaceAtMe', False, None),

    # --- Additional Actor/Combat ---
    'addscriptpackage':  ('EvaluatePackage',   True,  None),
    'removescriptpackage': ('EvaluatePackage', True,  None),
    'startconversation': (None,                True,  None),  # Special handler
    'getiscurrentpackage': (None,              True,  None),  # no-op
    'pickidle':          (None,                True,  None),  # Special handler in _emit_function
    'playidle':          (None,                True,  None),  # Special handler in _emit_function
    'isanimplaying':     (None,                True,  None),  # Special handler (anim variable)
    'getcombattarget':   ('GetCombatTarget',   True,  None),
    'isdisabled':        ('IsDisabled',        True,  None),
    'getparentcellowner':('GetParentCell',     True,  None),
    'hasmagiceffect':    ('HasMagicEffect',    True,  None),
    'isexpelled':        (None,                False, None),  # Special handler (ispcexpelled)
    'getdeadcount':      ('GetDeadCount',      True,  None),
    'getcurrentpackage': (None,                True,  None),  # no-op
    'setopendoor':       ('SetOpen',           True,  None),

    # --- Player state ---
    'getplayerinseworld': (None,               False, None),  # Special handler
    'getpcfactionmurder':(None,                False, None),  # Special handler
    'setpcfactionmurder':(None,                False, None),  # Special handler
    'getpcfactionattack':(None,                False, None),  # Special handler
    'setpcfactionattack':(None,                False, None),  # Special handler
    'getpcfactionsteal': (None,                False, None),  # Special handler
    'setpcfactionsteal': (None,                False, None),  # Special handler
    'getinworldspace':   (None,                False, None),  # Special handler
    'getiscurrentweather':(None,               False, None),  # Special handler
    'getisreference':    (None,                False, None),  # Special handler
    'senttojail':        (None,                False, None),  # Special handler
    'isplayersleeping':  (None,                False, None),  # Special handler
    'disableplayercontrols': ('Game.DisablePlayerControls', False, None),
    'enableplayercontrols': ('Game.EnablePlayerControls', False, None),
    'enablefasttravel': ('Game.EnableFastTravel', False,  None),
    'playbink':          (None,                False, None),  # no-op
    'sendtrespassalarm': (None,               True,  None),  # no-op
    'getpcisrace':       (None,                False, None),  # Special handler
    'getpcisclass':      (None,                False, None),  # Special handler
    # OBSE string_var builder; Papyrus String is the literal.  Special handler
    # in _emit_function — the inert ar_/sv_ catch-all would leave it undefined.
    'sv_construct':      (None,                False, None),
    'getinfame':         (None,                False, None),  # Special handler
    'getpcinfamy':       (None,                False, None),  # Special handler
    'getpcfame':         (None,                False, None),  # Special handler

    # --- AI/Package ---
    'setforcesneak':     (None,                True,  None),  # Special handler
    'getisalerted':      (None,                True,  None),  # no-op
    'setalert':          (None,                True,  None),  # Special handler

    # --- Object Interaction ---
    'getcontainer':      ('GetContainer',      True,  None),
    'opencurrentcontainer': (None,             True,  None),  # no-op
    'removeallitems':    ('RemoveAllItems',    True,  None),
    'getdisabled':       ('IsDisabled',        True,  None),
    # Special handlers in _emit_function (see there for why each is inert):
    # path-based music has no Skyrim API, IsCasting maps to the animation graph.
    'streammusic':       (None,                True,  None),
    'emcplaytrack':      (None,                True,  None),
    'emcmusicstop':      (None,                True,  None),
    'emcmusicresume':    (None,                True,  None),
    'emcmusicnexttrack': (None,                True,  None),
    'emcsetmusictype':   (None,                True,  None),
    'emcsetmusichold':   (None,                True,  None),
    'emcsetbattleoverride': (None,             True,  None),
    'emcisbattleoverridden': (None,            True,  None),
    'emcismusiconhold':  (None,                True,  None),
    'emcgetplaylist':    (None,                True,  None),
    'iscasting':         (None,                True,  None),
    'positioncell':      (None,                True,  None),
    'getignorefriendlyhits': (None,            True,  None),
    'hasflames':         (None,                True,  None),
    'flameson':          (None,                True,  None),
    'flamesoff':         (None,                True,  None),
    'addflames':         (None,                True,  None),
    'removeflames':      (None,                True,  None),
    'getplayerhaslastriddenhorse': (None,      True,  None),
    'attachashpile':     (None,                True,  None),  # no-op
    'setsize':           ('SetScale',          True,  None),
    'getsize':           ('GetScale',          True,  None),

    # --- Cell/Location ---
    'getincell':         (None,                True,  None),  # Special handler
    # 'isininterior' handled by special handler in _emit_function
    'getinsamecellas':   (None,                True,  None),  # Special handler

    # --- Faction/Crime ---
    'ispcexpelled':      (None,                False, None),  # Special handler in _emit_function
    'getpcexpelled':     (None,                False, None),  # Special handler in _emit_function
    'setpcexpelled':     (None,                False, None),  # Special handler in _emit_function
    'payfinethief':      (None,                False, None),  # Special handler
    'payfine':           (None,                False, None),  # Special handler
    'gotojail':          (None,                False, None),  # Special handler
    'addachievement':    (None,                False, None),  # Special handler (no-op)
    'modpcfame':         (None,                False, None),  # Special handler
    'modpcinfamy':       (None,                False, None),  # Special handler
    'getpcfame':         (None,                False, None),  # Special handler
    'getpcinfamy':       (None,                False, None),  # Special handler
    'getinfame':         (None,                True,  None),  # Special handler

    # --- Dialog/Topic ---
    'refreshtopiclist':  (None,                False, None),  # Special handler (no-op)
    'saycustom':         ('Say',               True,  None),

    # --- Look/Perception ---
    'look':              ('SetLookAt',         True,  None),
    'stoplooking':       ('ClearLookAt',       True,  None),

    # --- Display/Name ---
    # GetDisplayName is SKSE, not vanilla — Form.psc/ObjectReference.psc/
    # Actor.psc have no name accessor at all, so these emitted a call that does
    # not exist.  Neutralised via _OBSE_NO_EQUIV_COMMANDS (special handler).
    'getdisplayname':    (None,                True,  None),  # Special handler
    'getname':           (None,                True,  None),  # Special handler

    # --- Travel ---
    'movetomyeditorlocation': ('MoveToMyEditorLocation', True, None),
    'moveto':            ('MoveTo',            True,  None),
    'movetomarker':      ('MoveTo',            True,  None),

    # --- Path/Linked Points ---
    'enablelinkedpathpoints':  (None,          True,  None),  # Special handler (no-op)
    'disablelinkedpathpoints': (None,          True,  None),  # Special handler (no-op)

    # --- Shader/Visual Effects ---
    'pms':               (None,                True,  None),  # Special handler
    'sms':               (None,                True,  None),  # Special handler
    'playmagicshadervisuals':  (None,          True,  None),  # Special handler
    'stopmagicshadervisuals':  (None,          True,  None),  # Special handler
    'playmagiceffectvisuals':  (None,          True,  None),  # Special handler
    'stopmagiceffectvisuals':  (None,          True,  None),  # Special handler
    'pme':               (None,                True,  None),  # Special handler
    'sme':               (None,                True,  None),  # Special handler
    'triggerhitshader':  (None,                True,  None),  # Special handler
    'scaonactor':        (None,                True,  None),  # Special handler
    'sca':               (None,                True,  None),  # Special handler

    # --- AI/Wait ---
    'stopwaiting':       ('EvaluatePackage',   True,  None),
    'setcombatstyle':    (None,                True,  None),  # Special handler (no-op)
    'setignorefriendlyhits': (None,            True,  None),  # no-op
    'sayto':             ('Say',               True,  None),

    # --- Detection ---
    'getdetectionlevel': (None,                True,  None),  # Special handler

    # --- Door/Object State ---
    'setopenstate':      ('SetOpen',           True,  None),
    'resetinterior':     (None,                True,  None),  # Special handler

    # --- Player Skill/Misc ---
    'modpcskill': ('Game.AdvanceSkill',   False,  None),
    'modpcmiscstat': ('Game.IncrementStat',  False,  None),
    'getpcmiscstat': ('Game.QueryStat',      False,  None),

    # --- Trap/Custom functions that are quest-specific ---
    'trapupdate':        (None,                True,  None),  # Special handler (no-op)

    # --- Gold ---
    'getgold':           ('GetGoldAmount',     True,  None),

    # --- Alpha ---
    'saa':               ('SetAlpha',          True,  None),
    'setactoralpha':     ('SetAlpha',          True,  None),
    'gaa':               ('GetAlpha',          True,  None),
    'getactoralpha':     ('GetAlpha',          True,  None),

    # --- Interior ---
    # 'isininterior' handled by special handler in _emit_function

    # --- Save ---
    'autosave':          ('Game.RequestAutoSave', False, None),

    # --- Misc unmapped ---
    'modamountsoldstolen':(None,               False, None),  # Special handler
    'setcellownership':  (None,                False, None),  # Special handler (no-op)
    'setpublic':         (None,                False, None),  # no-op
    'closeCurrentOblivionGate': (None,         False, None),  # Special handler
    'setshowquestitems': (None,                False, None),  # no-op
    'setnorumors':       (None,                False, None),  # no-op
    'setsceneiscomplex': (None,                False, None),  # no-op
    'setdisplayname':    ('SetDisplayName',    True,  None),
    'setpackduration':   (None,                False, None),  # no-op
    'showbirthsignmenu': (None,                False, None),  # Special handler
    'isspelltarget':     (None,                True,  None),  # Special handler (HasMagicEffect)
    'getarmorrating':    (None,                True,  None),  # Special handler (DamageResist AV)
    'getiscreature':     (None,                True,  None),  # Special handler (polyfill)
    'isguard':           (None,                True,  None),  # Special handler (polyfill)
    'hasvampirefed':     (None,                False, None),  # Special handler (polyfill)
    'getclothingvalue':  (None,                True,  '(clothing value not tracked in Skyrim; 0)'),
    'getshouldattack':   (None,                True,  '(no Papyrus equivalent; 0 — sibling IsInCombat term carries the check)'),
    'pushactoraway':     (None,                True,  None),  # Special handler
    'isidleplaying':     (None,                True,  None),  # no-op
    'getopenstate':      ('GetOpenState',      True,  None),
    'getstartingpos':    (None,                True,  None),  # no-op
    'getcurrentaiprocedure': (None,            True,  None),  # no-op
    'getcurrentaipackage': (None,              True,  None),  # no-op
    'isessential':       ('IsEssential',       True,  None),
    'getlos':            ('HasLOS',            True,  None),
    'isactor':           (None,                True,  None),  # no-op
    'israining':         (None,                False, None),  # no-op
    'isindangerouswater':(None,                True,  None),  # no-op
    'getplayercontrolsdisabled': (None,        False, None),  # Special handler (TES4ControlsDisabled)
    'getisplayerbirthsign': (None,             False, None),  # no-op
    'isplayerinjail':    (None,                False, None),  # Special handler
    'getpcfactionattack':(None,                False, None),  # Special handler
    'getpcfactionsteal': (None,                False, None),  # Special handler
    'ispcanmurderer':    (None,                False, None),  # Special handler
    'ispcamurderer':     (None,                False, None),  # Special handler
    'getpcismurderer':   (None,                False, None),  # Special handler
    'isowner':           ('IsInFaction',        True,  None),
    'gettalkedtopc':     (None,                False, None),  # no-op
    'gettalkedtopcp':    (None,                False, None),  # no-op
    'menumode':          (None,                False, None),  # no-op
    'istimepassing':     (None,                False, None),  # no-op
    'expel':             (None,                True,  None),  # Special handler
    'setitemvalue':      (None,                True,  None),  # no-op
    'setnoavoidance':    (None,                True,  None),  # no-op
    'offerhorse':        (None,                True,  None),  # no-op
    'setactorrefraction':(None,                True,  None),  # Special handler (alpha fade)
    'setdisplayname':    (None,                True,  None),  # Special handler
    'setname':           (None,                True,  None),  # Special handler
    'getcontainer':      (None,                True,  None),  # Special handler
    'stopcombatalarmonactor': (None,           True,  None),  # Special handler (StopCombatAlarm)
    'essentialdeathreload': (None,             False, None),  # no-op
    'setallreachable':   (None,                True,  None),  # no-op
    # No native bool reader for the destroyed state, but the destruction STAGE
    # is native: stage > 0 means the ref has been destroyed.  IsDisabled() was
    # unrelated (a destroyed ref is still enabled) and always returned false.
    'getdestroyed':      (None,                True,  None),  # Special handler
    'setclass':          (None,                True,  None),  # no-op
    'setdoordefaultopen':(None,                True,  None),  # Special handler
    'setrestrained':     (None,                True,  None),  # Special handler
    'getrestrained':     (None,                True,  None),  # Special handler
    'rotate':            (None,                True,  None),  # Special handler
    'clearownership':    (None,                True,  None),  # Special handler
    'setlevel':          (None,                True,  None),  # no-op
    'showspellmaking':   (None,                False, None),  # no-op
    'setrigidbodymass':  (None,                True,  None),  # no-op
    'resetfalldamagetimer': (None,             True,  None),  # no-op
    'setpcfame':         (None,                False, None),  # Special handler
    'setpcinfamy':       (None,                False, None),  # Special handler
    'forceflee':         (None,                True,  None),  # Special handler
    'flee':              (None,                True,  None),  # Special handler
    'getattacked':       (None,                True,  None),  # Special handler
    'positionworld':     (None,                True,  None),  # Special handler
    'positioncell':      (None,                True,  None),  # Special handler
    'skipanim':          (None,                True,  None),  # Special handler
    'getpackagetarget':  (None,                True,  None),  # Special handler
    'unlockachievement': (None,                False, None),  # Special handler
    'setnumericinisetting': (None,             False, None),  # Special handler
    'printtoconsole':    (None,                False, None),  # Special handler
    'isinair':           (None,                True,  None),  # Special handler
    'con_save':          (None,                False, None),  # Special handler
    'con_savegame':      (None,                False, None),  # Special handler
    'getcrosshairref':   (None,                False, None),  # Special handler
    'getobjecttype':     (None,                True,  None),  # Special handler
    'disablekey':         (None,                False, None),  # Special handler
    'enablekey':          (None,                False, None),  # Special handler
    'tapkey':             (None,                False, None),  # Special handler
    'holdkey':            (None,                False, None),  # Special handler
    'releasekey':         (None,                False, None),  # Special handler
    'playback':           (None,                False, None),  # Special handler
    'playbackalt':        (None,                False, None),  # Special handler
    'disablecontrol':     (None,                False, None),  # Special handler
    'enablecontrol':      (None,                False, None),  # Special handler
    'tapcontrol':         (None,                False, None),  # Special handler
    'getcontrol':         (None,                False, None),  # Special handler
    'getaltcontrol':      (None,                False, None),  # Special handler
    'getmousecontrol':    (None,                False, None),  # Special handler
    'getmenuhastrait':    (None,                False, None),  # Special handler
    'getmenufloatvalue':  (None,                False, None),  # Special handler
    'getmenustringvalue': (None,                False, None),  # Special handler
    'getitems':           (None,                False, None),  # Special handler
    'isplayable2':        (None,                False, None),  # Special handler
    'isplayable':         (None,                False, None),  # Special handler
    'getfullgoldvalue':   (None,                False, None),  # Special handler
    'getweaponskilltype': (None,                False, None),  # Special handler
    'con_runmemorypass': (None,                False, None),  # Special handler
    'getstringgamesetting': (None,             False, None),  # Special handler
    'getlocalgravity':   (None,                False, None),  # Special handler
    'seteventhandler':   (None,                False, None),  # Special handler
    'removeeventhandler': (None,               False, None),  # Special handler
    'runscriptline':     (None,                False, None),  # Special handler
    'runbatchscript':    (None,                False, None),  # Special handler
    'iskeypressed':      (None,                False, None),  # Special handler
    'iskeypressed2':     (None,                False, None),  # Special handler
    'iskeypressed3':     (None,                False, None),  # Special handler
    'iscontrolpressed':  (None,                False, None),  # Special handler
    'printc':            (None,                False, None),  # Special handler
    'messageboxex':      (None,                False, None),  # Special handler
    'messageex':         (None,                False, None),  # Special handler
    'getnumericinisetting': (None,             False, None),  # Special handler
    'getgamerestarted':  (None,                False, None),  # Special handler
    'isplayermovingintonewspace': (None,       False, None),  # Special handler
    'setinvestmentgold': (None,                True,  None),  # no-op
    'setallvisible':     (None,                True,  None),  # no-op
    'getpcfame':         (None,                False, None),  # Special handler
    'getpcinfamy':       (None,                False, None),  # Special handler
    'setlookat':         ('SetLookAt',         True,  None),
}


# TES4 functions that are boolean (return 0/1) and can be used as bare checks
_BARE_BOOL_FUNCTIONS = {
    'getdead', 'isdead', 'isincombat', 'issneaking', 'isweaponout',
    'isswimming', 'isghost', 'isenabled', 'isdisabled', 'islocked',
    'getlocked', 'is3dloaded', 'getis3dloaded', 'isininterior',
    'getforcesneak', 'getknockedstate',
    # OBSE IsCasting and vanilla HasFlames are read bare/as `ref.X == 1`
    'iscasting', 'hasflames', 'getplayerhaslastriddenhorse',
    'getignorefriendlyhits',
}

# TES4 commands with NO Papyrus equivalent (FUNCTION_MAP name is None) that
# must still be routed through _emit_function when read BARE, mid-expression.
#
# Most None-named entries deliberately fall through instead: bare reads like
# getSecondsPassed are rewritten by dedicated later passes, and routing them
# here would TODO them mid-expression, leaving `timer = timer - `. The commands
# below have no such pass and no same-named Papyrus form, so without routing
# they survive into the output as undefined identifiers. _emit_function holds
# their special handlers (path-based music has no Skyrim API; the emc* family
# is matched there by prefix) and the ;NE no-op fallback.
# OBSE / TES4-only commands neutralised wholesale by _emit_function.  Verified
# absent from vanilla Papyrus (Actor/ObjectReference/Form/Game/Utility).  Some
# of these DO have an SKSE equivalent (see docs/skse_conversion_audit.md) — they
# are neutralised here only because nothing targets SKSE yet, not because SKSE
# is ruled out.  Anything moved onto an SKSE native should come off this list.
_OBSE_NO_EQUIV_COMMANDS = {
    'preloadmagiceffect', 'closeallmenus', 'setmodelpath', 'getmodelpath',
    'setlowlevelprocessing', 'setharvested', 'selectplayerspell',
    'setquestitem', 'setpcamurderer', 'setcellwaterheight',
    'setstringinisetting', 'setstringgamesettingex', 'getobseversion',
    'getformfrommod', 'getfirstref', 'getnextref', 'getaltcontrol2',
    'sifh', 'equipme', 'modavmod',
    'getvelocity', 'setvelocity',
    'isunderwater', 'getvampire', 'getweapontype', 'iswaiting',
    'getnumfollowers', 'getnthfollower', 'getspells', 'getdisplayname',
    'setattackdamage', 'togglespecialanim', 'setavmod', 'starttimer',
    'getmodlocaldata', 'setaltcontrol',
    'fileexists', 'getgodmode', 'getplayerbirthsign',
    'getdisplayname', 'getname',
    # AddActorValues (OBSE plugin) — the float-typed AV-modifier accessors that
    # sit alongside the already-listed setavmod/modavmod.  Skyrim has no such
    # plugin, and every TES4 caller already guards the block with
    # `IsPluginInstalled "AddActorValues" == 0 / return`, so the block is dead
    # by construction.
    #
    # Left unrouted they survived as undefined identifiers and failed the
    # CHECKER, so NO .pex was emitted for the owning script at all.  That is
    # what kept mwMorroDefaultQuestScript from running, and with it the
    # PlayerInMorrowind global its GameMode block maintains -- the global that
    # gates Fargoth's unique greeting and his "ring" topic.
    'getavmodf', 'setavmodf',
}

_BARE_NO_EQUIV_COMMANDS = {
    'streammusic',
    'emcplaytrack', 'emcmusicstop', 'emcmusicresume', 'emcmusicnexttrack',
    'emcsetmusictype', 'emcsetmusichold', 'emcsetbattleoverride',
    'emcisbattleoverridden', 'emcismusiconhold', 'emcgetplaylist',
    'iscasting', 'hasflames', 'flameson', 'flamesoff', 'addflames',
    'removeflames', 'getplayerhaslastriddenhorse', 'getignorefriendlyhits',
    # Read bare, mid-expression, with no same-named Papyrus form: without
    # routing they survive as undefined identifiers and fail the whole script.
    'flee', 'getattacked', 'skipanim', 'getpackagetarget',
    'getamountsoldstolen',
    # Takes no arguments, so it is ALWAYS read bare — without routing, the
    # fallback list won and the special handler (TES4ControlsDisabled) was
    # unreachable dead code.  Same trap as ispcamurderer (R6-2).
    'getplayercontrolsdisabled',
    # Zero-argument state read, so it is always bare: routed here so the
    # GetCurrentDestructionStage() handler is reachable.
    'getdestroyed',
    'isinair', 'getstringgamesetting', 'getcrosshairref', 'getobjecttype',
    'con_runmemorypass',
    'disablekey', 'enablekey', 'tapkey', 'holdkey', 'releasekey', 'playback', 'playbackalt', 'disablecontrol', 'enablecontrol', 'tapcontrol',
    'getcontrol', 'getaltcontrol', 'getmousecontrol', 'getmenuhastrait', 'getmenufloatvalue', 'getmenustringvalue', 'getitems', 'isplayable2', 'isplayable', 'getfullgoldvalue', 'getweaponskilltype',
    'iskeypressed', 'iskeypressed2',
    'iskeypressed3', 'iscontrolpressed',
    'unlockachievement', 'getgamerestarted', 'isplayermovingintonewspace',
    # OBSE event registration / console execution — no Papyrus equivalent
    # (see _emit_function).
    'seteventhandler', 'removeeventhandler',
    'runscriptline', 'runbatchscript',
} | _OBSE_NO_EQUIV_COMMANDS

# TES4 `ref.` commands that take NO arguments.  Oblivion let the receiver be
# written after a comma instead of a dot — `StopCombat, Player` and
# `IsInCombat, Player == 1` mean exactly `Player.StopCombat` /
# `Player.IsInCombat`.  Because the generic comma-stripping treats whatever
# follows as an argument, these emitted `IsInCombat(Player)` ("function takes 0
# parameters not 1") or dropped the token and acted on the wrong actor, so the
# receiver has to be promoted for precisely this set.
# Derived from the `ref.` rows with an empty argument column in
# docs/skyrim_commands.md, intersected with FUNCTION_MAP.  (IsInCombat's
# "Integer" column there is its RETURN type, not a parameter.)
_ZERO_ARG_REF_FUNCTIONS = {
    'addflames', 'clearownership', 'disablelinkedpathpoints',
    'dispelallspells', 'enablelinkedpathpoints', 'evaluatepackage',
    'getclothingvalue', 'getcombattarget', 'getcurrentaipackage',
    'getcurrentaiprocedure', 'getdead', 'getdestroyed', 'getdisabled',
    'getforcesneak', 'getgold', 'getignorefriendlyhits', 'getisalerted',
    'getiscreature', 'getisplayablerace', 'getknockedstate', 'getlevel',
    'getlocked', 'getlocklevel', 'getopenstate', 'getparentref',
    'getrestrained', 'getscale', 'getsitting', 'getsleeping',
    'gettalkedtopc', 'getweaponanimtype', 'hasflames', 'isactor',
    'iscasting', 'isessential', 'isguard', 'isidleplaying',
    'isindangerouswater', 'issneaking', 'isswimming', 'istalking',
    'isweaponout', 'markfordelete', 'pickidle', 'removeflames', 'resetai',
    'resetfalldamagetimer', 'stopcombat', 'stopcombatalarmonactor',
    'stoplook',
    # Same zero-argument shape; listed with a return type in the table.
    'isincombat', 'getattacked', 'isdead', 'getlos', 'skipanim',
    'getdisease', 'getalarmed', 'ismoving', 'isturning', 'getwantblocking',
}

# Functions that can ONLY be called on Actor (not ObjectReference)
# Used to infer correct property type for callers
_ACTOR_ONLY_FUNCTIONS = {
    'startcombat', 'stopcombat', 'getincombat', 'isincombat',
    'getdead', 'isdead', 'kill', 'resurrect',
    'addspell', 'removespell', 'hasspell', 'dispelallspells',
    'additem', 'removeitem', 'getitemcount', 'removeallitems',
    'equipitem', 'unequipitem',
    'getactorvalue', 'setactorvalue', 'modactorvalue', 'forceactorvalue',
    'getav', 'setav', 'modav', 'forceav', 'getbaseactorvalue', 'getbaseav',
    'startconversation', 'setrelationshiprank',
    'getinfaction', 'setfactionrank', 'getfactionrank',
    'modcrimegold', 'setcrimegold', 'getcrimegold',
    'evaluatepackage', 'evp', 'addscriptpackage', 'removescriptpackage', 'stopwaiting',
    'setessential', 'setghost', 'setunconscious',
    'setscale', 'getscale',
    'setforcerun', 'setforcesneak', 'getforcesneak', 'getknockedstate',
    'setrace', 'getrace',
    'getlevel', 'getclass',
    'setplayerteammate', 'pathtoref',
    'getweapondrawn', 'isweaponout',
    'setactoralpha', 'setopacity',
    'issneaking', 'isswimming', 'isghost',
    'say', 'saycustom', 'sayto',
    'getdistance', 'setcell',
    'getsitting', 'getsitstate', 'getsleeping', 'getsleepstate',
    'getequipped', 'isequipped', 'hasmagiceffect',
    'clearlookat', 'stoplook', 'stoplooking', 'setlookat', 'lookat', 'look',
    'getweaponanimtype', 'getdeadcount',
    'getgold', 'getgoldamount', 'saa', 'gaa', 'getactoralpha',
    'resethealth', 'setalpha', 'getalpha', 'getarmorrating',
    'isessential', 'getlos', 'haslos',
    'dispel', 'dispelspell', 'placeatme',
    'drawweapon', 'sheatheweapon', 'isinfaction',
}

# Functions that exist on ObjectReference (not truly Actor-only).
# These should NOT trigger type promotion from ObjectReference→Actor
# because they can be called on ObjectReference refs legally.
_OBJREF_SHARED_FUNCTIONS = {
    'placeatme', 'getdistance', 'additem', 'removeitem', 'getitemcount',
    'removeallitems', 'setscale', 'getscale', 'say', 'saycustom', 'sayto',
    'setalpha', 'getalpha', 'setcell',
    # NOT dispel/dispelspell: Actor.psc declares `bool DispelSpell(Spell)` and
    # ObjectReference has no such method, so listing them here suppressed the
    # `as Actor` cast and left an ObjectReference receiver calling an undefined
    # function (SERelmynaExperimentSpellScript).
}

# TES4 functions that name their target as an ARGUMENT rather than acting on the
# calling reference (`GetDeadCount JesanRilian`, `SetEssential SEMuurine 0`).
# Skyrim declares both on `ActorBase`, not `Actor`, so a bare occurrence says
# nothing about the enclosing script's own base type.  Used ONLY to keep
# `_infer_extends` from upgrading an ACTI/DOOR script to `extends Actor`, which
# the engine then refuses to bind ("base types do not match").  They stay in
# `_ACTOR_ONLY_FUNCTIONS` because the call-site emitter still needs the cast.
_ACTORBASE_ARG_FUNCTIONS = {
    'getdeadcount', 'setessential',
    # `saa`/`SetActorAlpha` is Actor-only in Skyrim, but Oblivion lets any
    # reference call it and simply does nothing off an actor — `SE32GhostObject`
    # rides on INGR/KEYM items.  Upgrading on its account made the script
    # unbindable and lost the `pms` shader beside it; the call site already
    # degrades the bare form to `(Self as Actor).SetAlpha(...)`, which compiles
    # and is the same no-op TES4 gave it.
    'saa', 'setactoralpha', 'gaa', 'getactoralpha',
}

# TES4 functions whose Papyrus signature declares an **Actor** parameter, taken
# from the vanilla headers in Data/Scripts.zip:
#   Actor.StartCombat(Actor akTarget)
#   Actor.IsHostileToActor(Actor akActor)
#   Actor.GetRelationshipRank(Actor akOther)
#   Actor.SetRelationshipRank(Actor akOther, int aiRank)
# An argument typed as the SCRIPT attached to the record it names (see
# pipeline._add_scro_ref) has to be cast at the call site for these, or the
# checker rejects it — `StartCombat(NQ05Soldat01nRef)` in
# NQ05StartCombatTrigBoxScript.  SetLookAt/Say/GetDistance are deliberately
# ABSENT: their parameters are ObjectReference, which a script-typed property
# converts to implicitly.
_ACTOR_ARG_FUNCTIONS = {
    'startcombat', 'ishostiletoactor',
    'getrelationshiprank', 'setrelationshiprank',
}

# Methods declared on ObjectReference that a TES4 script calls BARE, relying on
# the implicit "me".  ActiveMagicEffect and TopicInfo are not references, so an
# unqualified `Disable()` / `GetLinkedRef()` in those scripts is an undefined
# function at compile time — they must be routed onto the reference the effect
# or topic acts upon.  Distinct from _ACTOR_ONLY_FUNCTIONS: these need the
# receiver redirected but NOT an `as Actor` cast, since they are valid on any
# reference.
_OBJREF_IMPLICIT_SELF_FUNCTIONS = {
    'disable', 'enable', 'getdisabled', 'isdisabled', 'delete',
    'getlinkedref', 'getparentref', 'activate', 'reset',
    'getparentcell', 'getpos', 'setpos', 'getangle', 'setangle',
    'moveto', 'playgroup', 'playanimation', 'setopenstate',
    'getitemcount', 'isininterior',
}


# Canonical names for known TES4 globals
_GLOBAL_CANONICAL = {
    'gamehour': 'GameHour', 'gamedayspassed': 'GameDaysPassed',
    'gameday': 'GameDay', 'gamemonth': 'GameMonth', 'gameyear': 'GameYear',
    'timescale': 'TimeScale',
}


_RECORD_TYPE_PAPYRUS = {
    # NPC_/CREA are BASE records (TESNPC), so 'Actor' is technically wrong —
    # the VM type-checks VMAD object properties and an Actor-typed property
    # bound to a base form silently reads None in-game. But TES4 scripts use
    # NPC base EditorIDs in reference contexts pervasively (comparisons,
    # assignments), and a blanket ActorBase typing breaks ~1000 script
    # compilations. Instead, handlers whose TES4 argument is base-semantics
    # (SetEssential) override the individual property to ActorBase; a full fix
    # needs base-aware comparison/assignment emission (GetBaseObject()).
    'QUST': 'Quest', 'NPC_': 'Actor', 'CREA': 'Actor',
    'FACT': 'Faction', 'GLOB': 'GlobalVariable',
    'SPEL': 'Spell', 'ENCH': 'Enchantment', 'MGEF': 'MagicEffect',
    'CELL': 'Cell', 'WRLD': 'WorldSpace', 'PACK': 'Package',
    'SOUN': 'Sound', 'SNDR': 'Sound', 'DIAL': 'Topic', 'RACE': 'Race',
    'FLST': 'FormList', 'KYWD': 'Keyword', 'LVLI': 'LeveledItem',
    'LVLN': 'LeveledActor', 'LVSP': 'LeveledSpell',
    'WEAP': 'Weapon', 'ARMO': 'Armor', 'BOOK': 'Book',
    'ALCH': 'Potion', 'INGR': 'Ingredient', 'LIGH': 'Light',
    'MISC': 'MiscObject', 'KEYM': 'Key', 'AMMO': 'Ammo',
    # TES4-only item types, typed by what the IMPORTER writes them as (measured
    # over Morrowind_ob: 565 CLOT -> ARMO, 22 APPA -> MISC).  Leaving them
    # unmapped fell through to the 'ObjectReference' default, which is not a
    # base-object type -- a property bound to the converted ARMO then failed
    # with "cannot be bound because (...) is not the right type" and read None,
    # so `player.removeitem <ring>` silently did nothing.
    'CLOT': 'Armor', 'APPA': 'MiscObject', 'SLGM': 'SoulGem',
    # LVLC is Oblivion's leveled CREATURE list; the importer writes it as a
    # Skyrim LVLN (measured: 682 in Oblivion.esm). SGST (sigil stone) becomes a
    # SCRL (150). Both are base objects, so leaving them on the
    # 'ObjectReference' default made their properties fail to bind
    # (SE12GnarlSpawnerNewSCRIPT's PlaceAtMe spawners among them).
    'LVLC': 'LeveledActor', 'SGST': 'Scroll',
    'ACTI': 'Activator', 'DOOR': 'ObjectReference',
    'CONT': 'ObjectReference', 'STAT': 'ObjectReference',
    'FURN': 'ObjectReference', 'FLOR': 'ObjectReference',
    'EFSH': 'EffectShader', 'WTHR': 'Weather',
    'CSTY': 'Form', 'CLAS': 'Form',
    'EYES': 'ObjectReference', 'HAIR': 'ObjectReference',
    'TREE': 'ObjectReference', 'GRAS': 'ObjectReference',
    'ACHR': 'Actor', 'ACRE': 'Actor',
    'REFR': 'ObjectReference',
}


# ===========================================================================
# Utility functions (used by both converter.py and pipeline.py)
# ===========================================================================

def _sanitize_name(name: str) -> str:
    """Sanitize a script name for use as a filename."""
    return re.sub(r'[^\w]', '_', name)


# Papyrus caps a ScriptName at 38 characters; the compiler rejects anything
# longer outright ("...is too long, please shorten it to 38 characters or
# less"), so the script never produces a .pex and the object it is attached to
# silently does nothing in-game.  81 Oblivion script EditorIDs overflow once the
# TES4_ prefix is added.
PAPYRUS_MAX_SCRIPT_NAME = 38


def papyrus_script_name(edid: str, prefix: str = 'TES4_') -> str:
    """Return the Papyrus ScriptName for a TES4 script EditorID.

    MUST be the single source of truth: the same name is written as the .psc
    ScriptName, the .psc filename, and the ScriptName inside the VMAD that binds
    the script to its record.  If those three ever disagree the binding breaks,
    so every producer calls this rather than formatting the name itself.

    Over-long names are truncated and given a short hash of the FULL original,
    which keeps them unique (several Oblivion scripts differ only in a suffix
    past the cut, e.g. TrigZoneCloseCurrentOblivionRdCitadel0{1..5}SCRIPT).
    """
    name = prefix + _sanitize_name(edid)
    if len(name) <= PAPYRUS_MAX_SCRIPT_NAME:
        return name
    digest = hashlib.md5(name.encode('utf-8')).hexdigest()[:4].upper()
    # keep the head (it carries the recognisable quest/area prefix) + _<hash>
    keep = PAPYRUS_MAX_SCRIPT_NAME - len(digest) - 1
    return f'{name[:keep]}_{digest}'


def _safe_property_name(name: str) -> str:
    """Return a Papyrus-safe property name, renaming reserved words."""
    # Oblivion's parser accepts quotes around any EditorID and Nehrim's authors
    # use them constantly (173 sites: `SetStage "MQ01Tate" 20`,
    # `GetStage "NQ00Karick"`, `StartQuest "NQ05"`, `AddScriptPackage "..."`).
    # Left in, the `[^\w]` pass below turns each quote into an underscore, so
    # `"MQ01Tate"` became the property `_MQ01Tate_` while the SAME script's
    # unquoted `GetStage MQ01Tate` became `MQ01Tate`.  Only the unquoted
    # spelling matches an EditorID, so only it was bound in the VMAD —
    # `_MQ01Tate_` stayed None and every `_MQ01Tate_.SetStage(...)` threw.
    # MQ01Tate was stranded at stage 15, never reaching the stage 40 that is
    # the only thing that starts MQ01, so MQ00 could never complete either.
    name = name.strip()
    if len(name) > 1 and name[0] == '"' and name[-1] == '"':
        name = name[1:-1]
    safe = re.sub(r'[^\w]', '_', name)
    # A Papyrus identifier may not start with a digit. DELETING the leading
    # digits is lossy and collides: Morroblivion names ~19,000 records with a
    # leading digit, and stripping collapses 337 of them onto a shared name,
    # 155 onto a DIFFERENT record this plugin already owns, and 32 onto a
    # VANILLA SKYRIM record (`0miner` -> the Skyrim CLAS `Miner`,
    # `0banditfaction` -> the Skyrim FACT `BanditFaction`). A property bound by
    # name then resolves to the wrong record entirely, and nothing downstream
    # can tell. Prefix instead: `d` + the digits keeps the name UNIQUE and
    # REVERSIBLE (`0Blades` -> `d0Blades`, `1Necromancy` -> `d1Necromancy`), so
    # two records that differ only in their leading digits stay distinct and
    # neither can shadow an existing EditorID.
    m = re.match(r'^(\d+)(.*)$', safe)
    if m:
        safe = 'd' + m.group(1) + m.group(2)
    if not safe:
        safe = 'var_' + name.replace(' ', '_')
    # PapyrusCompiler mangles a variable `x` to the register `::x_var`, and it
    # reserves the `::temp*` namespace for its OWN scratch registers.  A user
    # variable starting with a lowercase `temp` therefore collides with the
    # compiler's free list ("Attempting to add temporary variable named
    # ::temp_var to free list multiple times") and the script does not compile.
    # Verified against PapyrusCompiler.exe: `temp`, `tempstage`, `template` and
    # `temperature` all fail; `Temp`, `tmp` and `atemp` are fine — the check is
    # case-sensitive and anchored at the start, so capitalising is enough.
    if safe.startswith('temp'):
        safe = 'T' + safe[1:]
    low = safe.lower()
    if low in _PAPYRUS_RESERVED:
        # Keep the original casing — `.capitalize()` lowercases the tail and
        # turns DarkBrotherhood into the unreadable myDarkbrotherhood.
        return 'my' + safe[0].upper() + safe[1:]
    return safe


def resolve_property_formid(xref, prop_name: str) -> str:
    """EditorID lookup for a (possibly sanitized) property name.

    _safe_property_name prefixes reserved EditorIDs with 'my' (MS14 → myMS14
    because MS14 is a vanilla Skyrim script name).  VMAD binders receive the
    SANITIZED name from the converter's property refs, so a direct EditorID
    lookup misses and the property silently stays unbound — a None quest at
    runtime, which killed every MS14 SetStage.  Reverse the rename when the
    direct lookup fails."""
    low = prop_name.lower()
    # `d<digits><rest>` is the leading-digit rename minted by
    # _safe_property_name, and it is EXACT: undo the prefix and the original
    # EditorID comes back verbatim, so it binds to that record and no other.
    # (The old scheme DELETED the digits, which made `0Blades` bind to
    # Oblivion.esm's own `Blades` faction — the player never joined
    # Morroblivion's Blades and Caius kept answering "Nobody gave me any
    # orders". Prefixing removes the whole class of collision rather than
    # ranking two ambiguous candidates.)
    fid = ''
    if low.startswith('d') and len(low) > 1 and low[1].isdigit():
        fid = xref.edid_to_formid.get(low[1:], '')
    if not fid:
        fid = xref.edid_to_formid.get(low, '')
    # Legacy fallback: a name sanitized by the OLD digit-deleting scheme still
    # has to resolve, so a stale property spelling keeps binding.
    if not fid:
        fid = _digit_stripped_formid(xref, low)
    if not fid and low.startswith('my'):
        fid = xref.edid_to_formid.get(low[2:], '')
    # `<Name>Base` is the de-collided ActorBase property minted by
    # _actor_base_property when the record's EditorID clashes
    # case-insensitively with one of the script's own variables (MQ19Script has
    # an `Int narel` alongside the NPC_ `Narel`).  Strip the suffix so the
    # property still binds to the base record.
    if not fid and len(low) > 4 and low.endswith('base'):
        fid = xref.edid_to_formid.get(low[:-4], '')
    return fid


# Record types that have no Papyrus property type at all, so a record of one of
# these is never what a script property name refers to.  Derived from
# _RECORD_TYPE_PAPYRUS (the authority on what a property CAN be) rather than
# hand-listed, minus DIAL: a topic is only ever an AddTopic argument, which the
# converter routes through its own unlock globals, never a bound property.
# Used ONLY to break digit-stripped EditorID collisions (see
# _digit_stripped_formid); the direct EditorID lookup is unaffected.
_NON_PROPERTY_SIGS = frozenset(
    {'INFO', 'LAND', 'PGRD', 'ROAD', 'NAVM', 'NAVI', 'GMST', 'LTEX', 'REGN',
     'SKIL', 'LSCR', 'ANIO', 'IDLE', 'SCPT', 'SBSP', 'LVLC', 'CLMT', 'WATR',
     'DIAL'}
)


def _digit_stripped_formid(xref, low: str) -> str:
    """FormID for a property name that came from a leading-digit EditorID.

    A Papyrus identifier may not start with a digit, so _safe_property_name
    strips any leading digits (`^\\d+`).  Morroblivion names almost every record
    with a leading `0` (`0bkUa1U1Ucaiuspackage`), so the sanitized property name
    never matches the EditorID and the property silently stays unbound.  An
    unbound property is None at runtime and the FIRST use throws, aborting the
    whole fragment: the Census greeting that hands out the Caius package ran its
    unlock/SetStage lines, then died on `AddItem(bkUa1U1Ucaiuspackage)` — so the
    player never received the package and "Report to Caius Cosades" never became
    reachable.  ~1,400 property declarations resolve only through this reversal.

    Record types that can never BE a script property (a DIAL topic, a CELL, a
    GMST...) are excluded first, which is what makes the common collision
    tractable: Morroblivion has both the `0Blades` FACT and a `1Blades` DIAL
    topic, and only the faction can be a `Faction Property`.  That filter
    resolves 315 of the 337 raw collisions.  What stays genuinely ambiguous
    binds to nothing rather than guessing, so a wrong record is never
    substituted.
    """
    if not low[:1].isalpha():
        return ''
    rev = getattr(xref, '_digit_stripped_edids', None)
    if rev is None:
        record_type = getattr(xref, 'record_type', None) or {}
        rev = {}
        for edid_low, edid_fid in xref.edid_to_formid.items():
            if not edid_low[:1].isdigit():
                continue
            if record_type.get(edid_fid, '') in _NON_PROPERTY_SIGS:
                continue
            stripped = edid_low.lstrip('0123456789')
            if stripped:
                rev[stripped] = '' if stripped in rev else edid_fid
        try:
            xref._digit_stripped_edids = rev
        except AttributeError:
            pass
    return rev.get(low, '')


def _canonical_global(name: str) -> str:
    """Return the canonical property name for a known global."""
    return _GLOBAL_CANONICAL.get(name.lower(), name)


def _record_type_to_papyrus(rtype: str) -> str:
    """Map a TES4 record type to a Papyrus property type."""
    return _RECORD_TYPE_PAPYRUS.get(rtype, 'ObjectReference')


# Record types whose Papyrus class is a BASE OBJECT (Armor, Weapon, Potion,
# ...), not a placed reference.  A VMAD property naming one of these binds to
# the base record itself, and the VM type-checks that binding: an
# `extends ObjectReference` script class is NOT a valid type for it.
#
# TES4 attaches scripts to base items freely (mwCWUItemScript rides every
# Morroblivion clothing record), and the converter preferred that script class
# over the record class so cross-script property reads would work. On a base
# item that preference is wrong and silently fatal -- measured in the game's
# own Papyrus log:
#
#   Property fbmwEngravedRingofHealing on script TES4_TIF__013236A5 ...
#     cannot be bound because (1B001677) is not the right type
#   error: Cannot add None to a container
#     [ (00000014)].Actor.RemoveItem() - "<native>"
#
# The property read None, so `player.removeitem fbmwEngravedRingofHealing 1`
# and Fargoth's matching `additem` both no-oped -- the quest still advanced to
# stage 100 (native errors are non-fatal), so the ring stayed in the player's
# inventory after handing it over.
_BASE_OBJECT_PAPYRUS = frozenset({
    'Armor', 'Weapon', 'Book', 'Potion', 'Ingredient', 'MiscObject', 'Key',
    'Ammo', 'SoulGem', 'Light', 'Activator', 'Flora', 'Furniture',
    'LeveledItem', 'LeveledActor', 'LeveledSpell', 'Scroll',
})


def script_type_may_override(record_ptype: str) -> bool:
    """Whether an attached TES4 script class may stand in for `record_ptype`.

    Reference-semantics types (ObjectReference, Actor, ...) may: the script
    extends one of those, so it binds and additionally exposes the script's own
    variables. Base-object types may NOT -- see _BASE_OBJECT_PAPYRUS.
    """
    return record_ptype not in _BASE_OBJECT_PAPYRUS


def wants_placed_reference(ptype: str) -> bool:
    """Whether a VMAD property of this Papyrus type must bind a PLACED
    reference rather than an actor BASE record.

    Oblivion resolves a unique actor's BASE EditorID to its placed instance
    (`ArenaMouth.Say ...` works even though ArenaMouth is the NPC_ record), so
    TES4 scripts name bases and mean references constantly. Skyrim's VM
    type-checks the binding: an NPC_/CREA base does NOT satisfy an
    Actor/ObjectReference(-derived) property, the bind is refused, and the
    property reads None for the whole session — measured live in the Papyrus
    log across 69 scripts (every Daedric statue voice, the Arena's ArenaMouth
    chain, the house-furnisher merchants). A TES4_* script class extends
    Actor/ObjectReference when it resolves to an actor base, so it needs the
    same treatment; script classes with other extends (Quest,
    ActiveMagicEffect) never resolve to an NPC_/CREA and fall out at the
    caller's record-type gate.
    """
    return (ptype in ('Actor', 'ObjectReference')
            or ptype.startswith('TES4_'))


def _record_type_to_base_papyrus(rtype: str) -> str:
    """Map a TES4 record type to the Papyrus type of its BASE form.

    `_record_type_to_papyrus` answers "what do I call a *reference* to this",
    which is what most TES4 script arguments mean.  Base-object comparisons
    (`GetIsID`) mean the opposite: the operand is the base record itself, so an
    NPC_ is an ActorBase (not an Actor) and a placed reference resolves to the
    base it points at.  Everything else already maps to its base type.
    """
    if rtype in ('NPC_', 'CREA', 'ACHR', 'ACRE'):
        return 'ActorBase'
    if rtype == 'REFR':
        # A REFR's base could be anything; Form compares against them all.
        return 'Form'
    mapped = _RECORD_TYPE_PAPYRUS.get(rtype, '')
    # ObjectReference is this table's fallback for base records with no
    # dedicated Papyrus class (DOOR/CONT/STAT/FLOR/...).  As a *base* operand
    # those are plain Forms, and Form compares against any base type.
    if not mapped or mapped == 'ObjectReference':
        return 'Form'
    return mapped

