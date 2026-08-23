"""AI package wiring for converted actors.

TES4 PACK records are now really converted (see pack_converter.py), so an actor
keeps its OWN packages: the PKID list is the TES4 AIPackage list, in TES4 order,
because Skyrim — like Oblivion — runs the first package whose conditions pass.
Order is behaviour, not decoration.

Two things still come from vanilla:

* DPLT — the default package LIST every vanilla actor carries underneath its
  own packages (the fallback that keeps an actor doing *something* when none of
  its own packages apply).
* Creatures — creature AI is driven by the generated behaviour graph, not by
  TES4 packages (see docs/creature_conversion.md), and every vanilla creature
  carries exactly one package: DefaultMasterPackageCreature.  Keep that.

Quest packages are NOT in the actor's PKID list: they hang off a QUST reference
alias (ALPC), which is how they outrank the standing schedule.  See pack_aliases.
"""

from .text_reader import get_int

# Vanilla Skyrim.esm records (master index 0 — written unremapped)
PKID_CREATURE_MASTER = 0x0010F2A5   # PACK DefaultMasterPackageCreature
DPLT_CREATURE_LIST = 0x0010F2A6     # FLST DefaultMasterPackageListCreature
PKID_NPC_SANDBOX = 0x000BFB6B       # PACK DefaultSandboxCurrentLocation1024
DPLT_NPC_LIST = 0x00021E81          # FLST DefaultMasterPackageList
CSTY_DEFAULT = 0x0000003D           # CSTY DefaultCombatstyle
CSTY_ANIMAL = 0x00057BE8            # CSTY csWolf (vanilla wolf/dog ZNAM)
CLAS_CREATURE_PREDATOR = 0x000131E6  # CLAS EncClassAnimalPredator (wolf...)
CLAS_CREATURE_CASTER = 0x00039D30    # CLAS EncClassBanditWizard (atronach)

# fid_low24 -> TES4 PKDT.Type, built once per import run (Phase 0g)
_PACK_TYPES = {}

# Packages that belong to a quest and therefore live on a QUST alias (ALPC)
# instead of the actor's PKID list.  Populated from the PackagePlan.
_QUEST_PACKAGES = set()


def load_package_types(by_type: dict, master_export: dict = None) -> None:
    """Phase 0g: index the TES4 PACK records by PKDT.Type.

    `master_export` is the MASTERS' export records ({FormID hex -> record},
    OverrideContext.master_export) and is REQUIRED for a plugin with masters:
    an actor in a dependent plugin routinely carries MASTER-owned packages
    (Morrowind_ob's chargen guard ends on Oblivion.esm's
    aaaDefaultStayAtCurrentLocation).  Indexing only the current plugin leaves
    every such package with type -1, so it can be neither classified nor
    correctly ordered against the actor's own.

    Keys are the REMAPPED FormID, matching `PackagePlan.owner_quest` and the
    actor's converted AIPackage list (both come from get_formid()).
    """
    from .text_reader import get_formid
    _PACK_TYPES.clear()
    sources = [by_type.get('PACK', [])]
    if master_export:
        sources.append([r for r in master_export.values()
                        if r.get('Signature') == 'PACK'])
    n_master = 0
    for i, src in enumerate(sources):
        for rec in src:
            try:
                fid = get_formid(rec, 'FormID')
            except ValueError:
                continue
            # The plugin's own record wins when it overrides a master's.
            if i and fid in _PACK_TYPES:
                continue
            _PACK_TYPES[fid] = get_int(rec, 'PKDT.Type', -1)
            n_master += i
    print(f'  Package types: {len(_PACK_TYPES)} TES4 packages indexed'
          + (f' ({n_master} from masters)' if n_master else ''))


def set_quest_packages(pack_fids) -> None:
    """Register the packages that are attached via QUST aliases."""
    _QUEST_PACKAGES.clear()
    _QUEST_PACKAGES.update(pack_fids)


# source pack fid -> [chain pack fid, ...]: a hunt expanded into a Follow
# chain (pack_converter.hunt_chain_targets).  The chain runs BEFORE its source
# in every list that carried the source.
_PACKAGE_CHAINS = {}


def set_package_chains(chains: dict) -> None:
    _PACKAGE_CHAINS.clear()
    _PACKAGE_CHAINS.update(chains)


def npc_packages(pack_fids) -> list:
    """The PKID list for a converted NPC: its own packages, in TES4 order.

    Quest packages are filtered out — they reach the actor through the quest's
    reference alias (ALPC).  Leaving them here as well would let a quest package
    run outside its quest.

    Compared on the full remapped FormID: masking to the low 24 bits made a
    MASTER-owned package collide with a same-low-24 plugin quest package and be
    dropped from the actor's schedule entirely.
    """
    out = []
    for f in pack_fids:
        if not f or f in _QUEST_PACKAGES:
            continue
        out.extend(c for c in _PACKAGE_CHAINS.get(f, ()) if c not in
                   _QUEST_PACKAGES)
        out.append(f)
    return out
