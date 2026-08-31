"""
Dialog, quest, and miscellaneous record types: DIAL, INFO, QUST, PACK, SCPT,
GLOB, GMST, SOUN, CLMT, WATR, EFSH, LSCR, LVLI, LVLC, LVSP, WTHR.

Pure TES4 data dump - no transformations.
"""

import struct

from ..tes4_reader import Record, get_all_subrecords, get_formid_str, get_string, get_subrecord
from .common import (
    emit_conditions,
    emit_float,
    emit_formid,
    emit_icon,
    emit_model,
    emit_raw_hex,
    emit_script,
    emit_string,
    emit_u8,
    escape_value,
)


def export_DIAL(rec: Record) -> list:
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))
    # QSTI - quest associations
    qstis = get_all_subrecords(rec, "QSTI")
    if qstis:
        lines.append(f"QuestCount={len(qstis)}")
        for i, q in enumerate(qstis):
            if len(q.data) >= 4:
                lines.append(f"Quest[{i}]={get_formid_str(struct.unpack_from('<I', q.data, 0)[0])}")
    emit_string(lines, "FULL", get_subrecord(rec, "FULL"))
    data = get_subrecord(rec, "DATA")
    if data and len(data.data) >= 1:
        lines.append(f"DATA.Type={data.data[0]}")
    return lines


def export_INFO(rec: Record) -> list:
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))
    data = get_subrecord(rec, "DATA")
    if data and len(data.data) >= 3:
        d = data.data
        lines.append(f"DATA.DialogType={d[0]}")
        lines.append(f"DATA.NextSpeaker={d[1]}")
        lines.append(f"DATA.Flags={d[2]}")

    emit_formid(lines, "QSTI.Quest", get_subrecord(rec, "QSTI"))
    emit_formid(lines, "TPIC.Topic", get_subrecord(rec, "TPIC"))
    emit_formid(lines, "PNAM.PrevInfo", get_subrecord(rec, "PNAM"))

    # NAME - added topics
    names = get_all_subrecords(rec, "NAME")
    if names:
        lines.append(f"AddTopicCount={len(names)}")
        for i, n in enumerate(names):
            if len(n.data) >= 4:
                lines.append(f"AddTopic[{i}]={get_formid_str(struct.unpack_from('<I', n.data, 0)[0])}")

    # Responses (TRDT + NAM1 + NAM2)
    trdts = get_all_subrecords(rec, "TRDT")
    nam1s = get_all_subrecords(rec, "NAM1")
    nam2s = get_all_subrecords(rec, "NAM2")
    if trdts:
        lines.append(f"ResponseCount={len(trdts)}")
        for i, trdt in enumerate(trdts):
            pfx = f"Response[{i}]"
            if len(trdt.data) >= 16:
                lines.append(f"{pfx}.EmotionType={struct.unpack_from('<I', trdt.data, 0)[0]}")
                lines.append(f"{pfx}.EmotionValue={struct.unpack_from('<i', trdt.data, 4)[0]}")
                lines.append(f"{pfx}.ResponseNumber={trdt.data[12]}")
            if i < len(nam1s):
                lines.append(f"{pfx}.ResponseText={escape_value(get_string(nam1s[i]))}")
            if i < len(nam2s):
                lines.append(f"{pfx}.ActorNotes={escape_value(get_string(nam2s[i]))}")

    emit_conditions(lines, rec)

    # TCLT - choices (multiple, as indexed array)
    tclts = get_all_subrecords(rec, "TCLT")
    if tclts:
        lines.append(f"ChoiceCount={len(tclts)}")
        for i, tclt in enumerate(tclts):
            if len(tclt.data) >= 4:
                lines.append(f"Choice[{i}]={get_formid_str(struct.unpack_from('<I', tclt.data, 0)[0])}")

    # TCLF - link-from topics (multiple, as indexed array)
    tclfs = get_all_subrecords(rec, "TCLF")
    if tclfs:
        lines.append(f"LinkFromCount={len(tclfs)}")
        for i, tclf in enumerate(tclfs):
            if len(tclf.data) >= 4:
                lines.append(f"LinkFrom[{i}]={get_formid_str(struct.unpack_from('<I', tclf.data, 0)[0])}")

    # Result script (SCHR/SCDA/SCTX)
    sctx = get_subrecord(rec, "SCTX")
    if sctx:
        lines.append(f"ResultScript={escape_value(get_string(sctx))}")

    return lines


def export_QUST(rec: Record) -> list:
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))
    emit_script(lines, rec)
    emit_string(lines, "FULL", get_subrecord(rec, "FULL"))
    emit_icon(lines, "ICON", rec)

    data = get_subrecord(rec, "DATA")
    if data and len(data.data) >= 2:
        lines.append(f"DATA.Flags={data.data[0]}")
        lines.append(f"DATA.Priority={data.data[1]}")

    # QUST holds CTDAs in THREE distinct positions (xEdit wbDefinitionsTES4
    # QUST record): quest-level (before the first Stage/Target), per-log-entry
    # (inside a Stage's Log Entry — result-script gate), and per-Target (after
    # each QSTA — this is what gates the compass/map marker per stage). A flat
    # get_all_subrecords(rec,'CTDA') conflates all three and destroys the
    # QSTA->condition association that decides which objective's marker shows.
    # So walk the subrecord stream positionally and bucket CTDAs by context.
    #
    # State: 'quest' until the first INDX or QSTA; 'log' while inside a Stage
    # log entry; 'target' after a QSTA. Quest-level CTDAs are emitted as the
    # top-level Condition[] list the importer already reads.
    quest_ctdas = []      # top-level quest conditions
    stages = []           # (index, [{'flags','text','script','refs','ctdas'}, ...])
    targets = []          # [{'formid','flags','ctdas'}, ...]
    current_idx = None
    current_logs = []
    state = "quest"       # quest | log | target
    for sub in rec.subrecords:
        if sub.type == "INDX":
            if current_idx is not None:
                stages.append((current_idx, current_logs))
            current_idx = struct.unpack_from('<h', sub.data, 0)[0] if len(sub.data) >= 2 else 0
            current_logs = []
            state = "stage"
        elif sub.type == "QSDT":
            qsdt_flags = sub.data[0] if sub.data else 0
            current_logs.append({'flags': qsdt_flags, 'text': '', 'script': '',
                                 'refs': [], 'ctdas': []})
            state = "log"
        elif sub.type == "CNAM" and current_logs:
            current_logs[-1]['text'] = get_string(sub)
        elif sub.type == "SCTX" and current_logs:
            current_logs[-1]['script'] = get_string(sub)
        elif sub.type == "SCRO" and current_logs:
            if len(sub.data) >= 4:
                current_logs[-1]['refs'].append(get_formid_str(struct.unpack_from('<I', sub.data, 0)[0]))
        elif sub.type == "QSTA":
            if len(sub.data) >= 8:
                targets.append({
                    'formid': get_formid_str(struct.unpack_from('<I', sub.data, 0)[0]),
                    'flags': struct.unpack_from('<I', sub.data, 4)[0],
                    'ctdas': [],
                })
            state = "target"
        elif sub.type == "CTDA" and len(sub.data) >= 20:
            if state == "target" and targets:
                targets[-1]['ctdas'].append(sub.data.hex())
            elif state == "log" and current_logs:
                current_logs[-1]['ctdas'].append(sub.data.hex())
            elif state == "quest":
                quest_ctdas.append(sub.data.hex())
            # CTDAs seen in 'stage' state (between INDX and first QSDT) are rare;
            # fold them into the quest bucket so nothing is silently lost.
            else:
                quest_ctdas.append(sub.data.hex())
    if current_idx is not None:
        stages.append((current_idx, current_logs))

    # Quest-level conditions (top-level Condition[] the importer reads).
    if quest_ctdas:
        lines.append(f"ConditionCount={len(quest_ctdas)}")
        for i, raw in enumerate(quest_ctdas):
            lines.append(f"Condition[{i}].Raw={raw}")

    if stages:
        lines.append(f"StageCount={len(stages)}")
        for i, (stage_idx, log_entries) in enumerate(stages):
            lines.append(f"Stage[{i}].Index={stage_idx}")
            log_entries = log_entries or [{'flags': 0, 'text': '', 'script': '',
                                           'refs': [], 'ctdas': []}]
            lines.append(f"Stage[{i}].LogCount={len(log_entries)}")
            for j, entry in enumerate(log_entries):
                lines.append(f"Stage[{i}].Log[{j}].Flags={entry['flags']}")
                if entry.get('text'):
                    lines.append(f"Stage[{i}].Log[{j}].Text={escape_value(entry['text'])}")
                if entry.get('script'):
                    lines.append(f"Stage[{i}].Log[{j}].ResultScript={escape_value(entry['script'])}")
                for k, ref in enumerate(entry.get('refs', [])):
                    lines.append(f"Stage[{i}].Log[{j}].SCRO[{k}]={ref}")
                # Per-log-entry conditions.  These were parsed above and then
                # silently dropped, which lost 950 CTDAs across 71 quests.
                # They are Oblivion's OWN answer to "when does this journal
                # entry stop being shown": MS48 stage 40's two entries both
                # carry `GetStage MS48 < 50`, i.e. stage 50 supersedes them.
                # Skyrim has no equivalent display gate, so the importer turns
                # them into the SetObjectiveCompleted the journal needs.
                ctdas = entry.get('ctdas', [])
                if ctdas:
                    lines.append(
                        f"Stage[{i}].Log[{j}].ConditionCount={len(ctdas)}")
                    for k, raw in enumerate(ctdas):
                        lines.append(
                            f"Stage[{i}].Log[{j}].Condition[{k}].Raw={raw}")

    # Targets (QSTA) with their per-target conditions. These CTDAs (typically
    # GetStage/GetStageDone bounds) are what the importer replays on each
    # Skyrim objective's QSTA so the marker shows for the right stage.
    if targets:
        lines.append(f"TargetCount={len(targets)}")
        for i, tgt in enumerate(targets):
            lines.append(f"Target[{i}].FormID={tgt['formid']}")
            lines.append(f"Target[{i}].Flags={tgt['flags']}")
            if tgt['ctdas']:
                lines.append(f"Target[{i}].ConditionCount={len(tgt['ctdas'])}")
                for k, raw in enumerate(tgt['ctdas']):
                    lines.append(f"Target[{i}].Condition[{k}].Raw={raw}")

    return lines


def export_PACK(rec: Record) -> list:
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))

    # PKDT — Package Data.
    # Two formats exist in TES4:
    #   Old (length=4): Flags U16, Type U8, Unused U8
    #   New (length=8): Flags U32, Type U8, Unused U8 U8 U8
    # Emit PKDT.Format so the importer knows which flag width to expect.
    pkdt = get_subrecord(rec, "PKDT")
    if pkdt and len(pkdt.data) >= 4:
        d = pkdt.data
        if len(d) >= 8:
            lines.append(f"PKDT.Format=new")
            lines.append(f"PKDT.Flags={struct.unpack_from('<I', d, 0)[0]}")
            lines.append(f"PKDT.Type={d[4]}")
        else:
            lines.append(f"PKDT.Format=old")
            lines.append(f"PKDT.Flags={struct.unpack_from('<H', d, 0)[0]}")
            lines.append(f"PKDT.Type={d[2]}")

    # PLDT — Location data (12 bytes: Type S32, Value 4 bytes, Radius S32)
    # Type 0 = Near reference (Value = FormID)
    # Type 1 = In cell        (Value = FormID)
    # Type 2 = Near current location (Value = ignored)
    # Type 3 = Near editor location  (Value = ignored)
    # Type 4 = Object ID      (Value = FormID)
    # Type 5 = Object type    (Value = U32 type enum)
    pldt = get_subrecord(rec, "PLDT")
    if pldt and len(pldt.data) >= 12:
        d = pldt.data
        pldt_type = struct.unpack_from('<i', d, 0)[0]
        lines.append(f"PLDT.Type={pldt_type}")
        if pldt_type in (0, 1, 4):
            lines.append(f"PLDT.Location={get_formid_str(struct.unpack_from('<I', d, 4)[0])}")
        else:
            lines.append(f"PLDT.Location={struct.unpack_from('<I', d, 4)[0]}")
        lines.append(f"PLDT.Radius={struct.unpack_from('<i', d, 8)[0]}")

    # PSDT — Schedule (Month S8, DayOfWeek S8, Date U8, Time S8, Duration S32)
    # Note: TES4 PSDT is 8 bytes (Time at offset 3 is S8, Duration at offset 4).
    # TES5 adds a Minute field (offset 4, S8) before Duration (offset 8, S32).
    psdt = get_subrecord(rec, "PSDT")
    if psdt and len(psdt.data) >= 8:
        d = psdt.data
        lines.append(f"PSDT.Month={struct.unpack_from('<b', d, 0)[0]}")
        lines.append(f"PSDT.DayOfWeek={struct.unpack_from('<b', d, 1)[0]}")
        lines.append(f"PSDT.Date={d[2]}")
        lines.append(f"PSDT.Time={struct.unpack_from('<b', d, 3)[0]}")
        lines.append(f"PSDT.Duration={struct.unpack_from('<i', d, 4)[0]}")

    # PTDT — Target data (12 bytes: Type S32, Target 4 bytes, Count S32)
    # Type 0 = Specific reference (Target = FormID)
    # Type 1 = Object ID          (Target = FormID)
    # Type 2 = Object type        (Target = U32 type enum)
    ptdt = get_subrecord(rec, "PTDT")
    if ptdt and len(ptdt.data) >= 12:
        d = ptdt.data
        ptdt_type = struct.unpack_from('<i', d, 0)[0]
        lines.append(f"PTDT.Type={ptdt_type}")
        if ptdt_type in (0, 1):
            lines.append(f"PTDT.Target={get_formid_str(struct.unpack_from('<I', d, 4)[0])}")
        else:
            lines.append(f"PTDT.Target={struct.unpack_from('<I', d, 4)[0]}")
        lines.append(f"PTDT.Count={struct.unpack_from('<i', d, 8)[0]}")

    # Conditions (CTDAs) — needed for proper package behaviour in TES5
    emit_conditions(lines, rec)
    return lines


def export_SCPT(rec: Record) -> list:
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))
    schr = get_subrecord(rec, "SCHR")
    if schr and len(schr.data) >= 20:
        d = schr.data
        lines.append(f"SCHR.RefCount={struct.unpack_from('<I', d, 4)[0]}")
        lines.append(f"SCHR.CompiledSize={struct.unpack_from('<I', d, 8)[0]}")
        lines.append(f"SCHR.VariableCount={struct.unpack_from('<I', d, 12)[0]}")
        lines.append(f"SCHR.Type={struct.unpack_from('<H', d, 16)[0]}")
    sctx = get_subrecord(rec, "SCTX")
    if sctx:
        lines.append(f"SCTX={escape_value(get_string(sctx))}")
    # Local variables: SLSD (index) is always followed by SCVR (name). The index
    # is what a GetScriptVariable condition stores in its param2, so the pair is
    # the only way to turn such a condition back into a named variable.
    i = 0
    for j, sub in enumerate(rec.subrecords):
        if sub.type != "SLSD" or len(sub.data) < 4:
            continue
        index = struct.unpack_from('<I', sub.data, 0)[0]
        name = ""
        if j + 1 < len(rec.subrecords):
            nxt = rec.subrecords[j + 1]
            if nxt.type == "SCVR":
                name = get_string(nxt)
        if not name:
            continue
        lines.append(f"Variable[{i}].Index={index}")
        lines.append(f"Variable[{i}].Name={escape_value(name)}")
        i += 1
    if i:
        lines.append(f"VariableCount={i}")
    # Script references (FormIDs referenced in the compiled script)
    scros = get_all_subrecords(rec, "SCRO")
    for i, scro in enumerate(scros):
        if len(scro.data) >= 4:
            lines.append(f"SCRO[{i}]={get_formid_str(struct.unpack_from('<I', scro.data, 0)[0])}")
    return lines


def export_GLOB(rec: Record) -> list:
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))
    fnam = get_subrecord(rec, "FNAM")
    if fnam and len(fnam.data) >= 1:
        type_char = chr(fnam.data[0]) if fnam.data[0] < 128 else str(fnam.data[0])
        lines.append(f"FNAM.Type={type_char}")
    emit_float(lines, "FLTV.Value", get_subrecord(rec, "FLTV"))
    return lines


def export_GMST(rec: Record) -> list:
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))
    data = get_subrecord(rec, "DATA")
    edid = get_string(get_subrecord(rec, "EDID")) if get_subrecord(rec, "EDID") else ""
    if data and len(data.data) >= 4:
        # Type determined by first char of EditorID: s=string, f=float, i=int
        if edid.startswith("s"):
            lines.append(f"DATA.Value={escape_value(get_string(data))}")
        elif edid.startswith("f"):
            lines.append(f"DATA.Value={struct.unpack_from('<f', data.data, 0)[0]}")
        else:
            lines.append(f"DATA.Value={struct.unpack_from('<I', data.data, 0)[0]}")
    return lines


def export_SOUN(rec: Record) -> list:
    """SOUN — SNDX and SNDD share the same struct (xEdit wbDefinitionsTES4 SOUN):

        u8  Minimum attenuation distance  (multiply by 5   for game units)
        u8  Maximum attenuation distance  (multiply by 100 for game units)
        s8  Frequency adjustment %
        u8  Unused
        u16 Flags
        u16 Unused
        u16 Static Attenuation (divide by 100 for dB)
        u8  Stop time
        u8  Start time

    The trailing 4 bytes (static attenuation / stop / start) are optional;
    Oblivion.esm ships 12-byte SNDX for 1138 of 1140 SOUN records.
    """
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))
    emit_string(lines, "FNAM.Filename", get_subrecord(rec, "FNAM"))
    sndd = get_subrecord(rec, "SNDD")
    sub = sndd if (sndd and len(sndd.data) >= 8) else get_subrecord(rec, "SNDX")
    if sub and len(sub.data) >= 8:
        key = sub.type
        d = sub.data
        lines.append(f"{key}.MinAttDist={d[0]}")
        lines.append(f"{key}.MaxAttDist={d[1]}")
        lines.append(f"{key}.FreqAdj={struct.unpack_from('<b', d, 2)[0]}")
        lines.append(f"{key}.Flags={struct.unpack_from('<H', d, 4)[0]}")
        if len(d) >= 12:
            lines.append(f"{key}.StaticAttenuation={struct.unpack_from('<H', d, 8)[0]}")
            lines.append(f"{key}.StopTime={d[10]}")
            lines.append(f"{key}.StartTime={d[11]}")
    return lines


def export_CLMT(rec: Record) -> list:
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))
    # WLST - weather list
    wlst = get_subrecord(rec, "WLST")
    if wlst:
        count = len(wlst.data) // 8
        lines.append(f"WeatherCount={count}")
        for i in range(count):
            off = i * 8
            if off + 8 <= len(wlst.data):
                fid = struct.unpack_from("<I", wlst.data, off)[0]
                chance = struct.unpack_from("<I", wlst.data, off + 4)[0]
                lines.append(f"Weather[{i}].FormID={get_formid_str(fid)}")
                lines.append(f"Weather[{i}].Chance={chance}")
    emit_string(lines, "FNAM.SunTexture", get_subrecord(rec, "FNAM"))
    emit_string(lines, "GNAM.GlareTexture", get_subrecord(rec, "GNAM"))
    # MODL — the night-sky / stars mesh (TES5 CLMT keeps the same field).
    emit_model(lines, "Model", rec)
    tnam = get_subrecord(rec, "TNAM")
    if tnam and len(tnam.data) >= 6:
        d = tnam.data
        lines.append(f"TNAM.SunriseBegin={d[0]}")
        lines.append(f"TNAM.SunriseEnd={d[1]}")
        lines.append(f"TNAM.SunsetBegin={d[2]}")
        lines.append(f"TNAM.SunsetEnd={d[3]}")
        lines.append(f"TNAM.Volatility={d[4]}")
        lines.append(f"TNAM.MoonsPhaseLength={d[5]}")
    return lines


def export_WATR(rec: Record) -> list:
    """Water Type.

    DATA is a 102-byte struct, but it is authored at five different lengths in
    Oblivion.esm alone (2, 42, 62, 86 and 102 bytes -- CamoranLava ships just
    the 2-byte tail), so every field is emitted only when the source is long
    enough to hold it.  Field order and offsets follow the xEdit TES4
    definition and were verified against a real Oblivion.esm.

    Note TES4 carries a Scroll X/Y Speed pair at offsets 28-35 that TES5 has
    no field for; that is why the colors sit at 44/48/52 here but at 40/44/48
    in the TES5 DNAM.  The import side does the shift, not this dump.
    """
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))
    emit_string(lines, "TNAM.Texture", get_subrecord(rec, "TNAM"))
    emit_u8(lines, "ANAM.Opacity", get_subrecord(rec, "ANAM"))
    emit_u8(lines, "FNAM.Flags", get_subrecord(rec, "FNAM"))
    emit_string(lines, "MNAM.MaterialID", get_subrecord(rec, "MNAM"))
    emit_formid(lines, "SNAM.Sound", get_subrecord(rec, "SNAM"))
    data = get_subrecord(rec, "DATA")
    if data and len(data.data) >= 2:
        d = data.data
        n = len(d)
        lines.append(f"DATA.Size={n}")

        def f(name, off):
            if off + 4 <= n:
                lines.append(f"DATA.{name}={struct.unpack_from('<f', d, off)[0]}")

        def rgb(name, off):
            if off + 3 <= n:
                lines.append(f"DATA.{name}R={d[off]}")
                lines.append(f"DATA.{name}G={d[off + 1]}")
                lines.append(f"DATA.{name}B={d[off + 2]}")

        f("WindVelocity", 0)
        f("WindDirection", 4)
        f("WaveAmplitude", 8)
        f("WaveFrequency", 12)
        f("SunPower", 16)
        f("ReflectivityAmount", 20)
        f("FresnelAmount", 24)
        f("ScrollXSpeed", 28)
        f("ScrollYSpeed", 32)
        f("FogNear", 36)
        f("FogFar", 40)
        rgb("ShallowColor", 44)
        rgb("DeepColor", 48)
        rgb("ReflectionColor", 52)
        if n >= 57:
            lines.append(f"DATA.TextureBlend={d[56]}")
        # 60-99 are the rain / displacement simulator blocks: TES5 keeps the
        # displacement struct but reorders it and marks rain unused, and no
        # vanilla Skyrim record varies them meaningfully, so they are not
        # carried across.
        if n >= 102:
            lines.append(f"DATA.Damage={struct.unpack_from('<H', d, 100)[0]}")
    return lines


def export_EFSH(rec: Record) -> list:
    """Effect Shader.

    TES4 DATA is 224 bytes and prefix-compatible with TES5's 400-byte one:
    flags, membrane blend state, the fill and edge blocks, the two full-alpha
    ratios, membrane dest blend, the whole particle block and the three color
    keys all sit at identical offsets in both games (verified against the
    xEdit TES4/TES5 definitions and a real Oblivion.esm).  Everything past
    offset 224 is TES5-only (holes, addon models, rotation, animated frames).

    Two vanilla records ship a truncated 96-byte DATA, so every field past the
    membrane block is emitted only when the source is long enough to hold it.
    """
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))
    emit_icon(lines, "ICON", rec, "ICON")
    emit_icon(lines, "ICO2", rec, "ICO2")
    data = get_subrecord(rec, "DATA")
    if not data:
        return lines
    d = data.data
    n = len(d)

    def f(name, off):
        if off + 4 <= n:
            lines.append(f"DATA.{name}={struct.unpack_from('<f', d, off)[0]}")

    def u(name, off):
        if off + 4 <= n:
            lines.append(f"DATA.{name}={struct.unpack_from('<I', d, off)[0]}")

    def rgb(name, off):
        if off + 3 <= n:
            lines.append(f"DATA.{name}R={d[off]}")
            lines.append(f"DATA.{name}G={d[off + 1]}")
            lines.append(f"DATA.{name}B={d[off + 2]}")

    if n >= 1:
        lines.append(f"DATA.Flags={d[0]}")
    # Membrane shader blend state
    u("MemSBlend", 4)
    u("MemBlendOp", 8)
    u("MemZFunc", 12)
    # Fill/texture effect
    rgb("FillColor", 16)
    f("FillAlphaFadeInTime", 20)
    f("FillAlphaFull", 24)
    f("FillAlphaFadeOutTime", 28)
    f("FillAlphaPersistPercent", 32)
    f("FillAlphaPulseAmp", 36)
    f("FillAlphaPulseFreq", 40)
    f("FillTextureAnimSpeedU", 44)
    f("FillTextureAnimSpeedV", 48)
    # Edge effect.  Offset 52 is the fall-off ("Edge Effect Width" in older
    # docs); the remaining edge alpha ramp mirrors the fill block.
    f("EdgeEffectWidth", 52)
    rgb("EdgeColor", 56)
    f("EdgeAlphaFadeInTime", 60)
    f("EdgeAlphaFull", 64)
    f("EdgeAlphaFadeOutTime", 68)
    f("EdgeAlphaPersistPercent", 72)
    f("EdgeAlphaPulseAmp", 76)
    f("EdgeAlphaPulseFreq", 80)
    f("FillFullAlphaRatio", 84)
    f("EdgeFullAlphaRatio", 88)
    u("MemDestBlend", 92)
    # Particle shader
    u("PartSBlend", 96)
    u("PartBlendOp", 100)
    u("PartZFunc", 104)
    u("PartDestBlend", 108)
    f("PartBirthRampUp", 112)
    f("PartFullBirthTime", 116)
    f("PartBirthRampDown", 120)
    f("PartFullBirthRatio", 124)
    f("PartPersistBirthRatio", 128)
    f("PartLifetime", 132)
    f("PartLifetimeDelta", 136)
    f("PartInitSpeedNormal", 140)
    f("PartAccelNormal", 144)
    f("PartInitVel1", 148)
    f("PartInitVel2", 152)
    f("PartInitVel3", 156)
    f("PartAccel1", 160)
    f("PartAccel2", 164)
    f("PartAccel3", 168)
    f("PartScaleKey1", 172)
    f("PartScaleKey2", 176)
    f("PartScaleKey1Time", 180)
    f("PartScaleKey2Time", 184)
    # Color keys
    rgb("ColorKey1", 188)
    rgb("ColorKey2", 192)
    rgb("ColorKey3", 196)
    f("ColorKey1Alpha", 200)
    f("ColorKey2Alpha", 204)
    f("ColorKey3Alpha", 208)
    f("ColorKey1Time", 212)
    f("ColorKey2Time", 216)
    f("ColorKey3Time", 220)
    return lines


def export_LSCR(rec: Record) -> list:
    """Loading Screen."""
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))
    emit_icon(lines, "ICON", rec)
    emit_string(lines, "DESC", get_subrecord(rec, "DESC"))
    # LNAM - locations
    lnams = get_all_subrecords(rec, "LNAM")
    if lnams:
        lines.append(f"LocationCount={len(lnams)}")
        for i, lnam in enumerate(lnams):
            if len(lnam.data) >= 12:
                lines.append(f"Location[{i}].Direct={get_formid_str(struct.unpack_from('<I', lnam.data, 0)[0])}")
                lines.append(f"Location[{i}].Indirect={get_formid_str(struct.unpack_from('<I', lnam.data, 4)[0])}")
                lines.append(f"Location[{i}].GridX={struct.unpack_from('<h', lnam.data, 8)[0]}")
                lines.append(f"Location[{i}].GridY={struct.unpack_from('<h', lnam.data, 10)[0]}")
    return lines


def _emit_leveled_entries(lines: list, rec: Record, sig: str = "LVLO"):
    """Emit entries for leveled lists.

    LVLO is Level(s16) + Unused(2) + FormID(4) + Count(s16) + Unused(2), but
    the Count/pad tail is OPTIONAL (xEdit wbStructExSK optional-from-element
    3): Oblivion.esm ships 8-byte LVLOs (e.g. Dark03RewardDagger) whose count
    defaults to 1. Skipping them exported EntryCount without the entries and
    the import wrote null (00000000) leveled entries.
    """
    lvlos = get_all_subrecords(rec, sig)
    lines.append(f"EntryCount={len(lvlos)}")
    for i, lvlo in enumerate(lvlos):
        d = lvlo.data
        if len(d) >= 8:
            lines.append(f"Entry[{i}].Level={struct.unpack_from('<H', d, 0)[0]}")
            lines.append(f"Entry[{i}].FormID={get_formid_str(struct.unpack_from('<I', d, 4)[0])}")
            count = struct.unpack_from('<h', d, 8)[0] if len(d) >= 10 else 1
            lines.append(f"Entry[{i}].Count={count}")


def export_LVLI(rec: Record) -> list:
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))
    emit_u8(lines, "LVLD.ChanceNone", get_subrecord(rec, "LVLD"))
    emit_u8(lines, "LVLF.Flags", get_subrecord(rec, "LVLF"))
    _emit_leveled_entries(lines, rec)
    return lines


def export_LVLC(rec: Record) -> list:
    """Leveled Creature."""
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))
    emit_u8(lines, "LVLD.ChanceNone", get_subrecord(rec, "LVLD"))
    emit_u8(lines, "LVLF.Flags", get_subrecord(rec, "LVLF"))
    emit_script(lines, rec)
    emit_formid(lines, "TNAM.Template", get_subrecord(rec, "TNAM"))
    _emit_leveled_entries(lines, rec)
    return lines


def export_LVSP(rec: Record) -> list:
    """Leveled Spell."""
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))
    emit_u8(lines, "LVLD.ChanceNone", get_subrecord(rec, "LVLD"))
    emit_u8(lines, "LVLF.Flags", get_subrecord(rec, "LVLF"))
    _emit_leveled_entries(lines, rec)
    return lines


def export_WTHR(rec: Record) -> list:
    lines = []
    emit_string(lines, "EditorID", get_subrecord(rec, "EDID"))
    # Cloud textures CNAM/DNAM
    emit_string(lines, "CNAM.LowerCloudLayer", get_subrecord(rec, "CNAM"))
    emit_string(lines, "DNAM.UpperCloudLayer", get_subrecord(rec, "DNAM"))
    # Model — TES4 WTHR may carry a MODL (rare; SI storm weathers).
    emit_model(lines, "Model", rec)
    # NAM0 - Colors by time of day: 10 types x 4 times x 4 bytes (rgba) = 160.
    # Dump the raw bytes; the import remaps TES4's 10 types into TES5's layout.
    nam0 = get_subrecord(rec, "NAM0")
    if nam0:
        lines.append(f"NAM0.Size={len(nam0.data)}")
        emit_raw_hex(lines, "NAM0.Data", nam0)
    # FNAM - Fog distances
    fnam = get_subrecord(rec, "FNAM")
    if fnam and len(fnam.data) >= 16:
        d = fnam.data
        lines.append(f"FNAM.FogDayNear={struct.unpack_from('<f', d, 0)[0]}")
        lines.append(f"FNAM.FogDayFar={struct.unpack_from('<f', d, 4)[0]}")
        lines.append(f"FNAM.FogNightNear={struct.unpack_from('<f', d, 8)[0]}")
        lines.append(f"FNAM.FogNightFar={struct.unpack_from('<f', d, 12)[0]}")
    # HNAM - HDR data
    hnam = get_subrecord(rec, "HNAM")
    if hnam and len(hnam.data) >= 56:
        d = hnam.data
        fields = ["EyeAdaptSpeed", "BlurRadius", "BlurPasses", "EmissiveMult",
                   "TargetLum", "UpperLumClamp", "BrightScale", "BrightClamp",
                   "LumRampNoTex", "LumRampMin", "LumRampMax", "SunlightDimmer",
                   "GrassDimmer", "TreeDimmer"]
        for i, name in enumerate(fields):
            if i * 4 + 4 <= len(d):
                lines.append(f"HNAM.{name}={struct.unpack_from('<f', d, i*4)[0]}")
    # DATA (15 bytes) — full dump. Offsets 6-14 (precipitation/thunder fades,
    # lightning frequency, weather classification, lightning color) exist in
    # TES5's DATA too and were previously dropped on the floor.
    data = get_subrecord(rec, "DATA")
    if data and len(data.data) >= 15:
        d = data.data
        lines.append(f"DATA.WindSpeed={d[0]}")
        lines.append(f"DATA.CloudSpeedLower={d[1]}")
        lines.append(f"DATA.CloudSpeedUpper={d[2]}")
        lines.append(f"DATA.TransDelta={d[3]}")
        lines.append(f"DATA.SunGlare={d[4]}")
        lines.append(f"DATA.SunDamage={d[5]}")
        lines.append(f"DATA.PrecipBeginFadeIn={d[6]}")
        lines.append(f"DATA.PrecipEndFadeOut={d[7]}")
        lines.append(f"DATA.ThunderBeginFadeIn={d[8]}")
        lines.append(f"DATA.ThunderEndFadeOut={d[9]}")
        lines.append(f"DATA.ThunderFrequency={d[10]}")
        lines.append(f"DATA.Classification={d[11]}")
        lines.append(f"DATA.LightningR={d[12]}")
        lines.append(f"DATA.LightningG={d[13]}")
        lines.append(f"DATA.LightningB={d[14]}")
    # Sound references
    snams = get_all_subrecords(rec, "SNAM")
    if snams:
        lines.append(f"SoundCount={len(snams)}")
        for i, snam in enumerate(snams):
            if len(snam.data) >= 8:
                lines.append(f"Sound[{i}].FormID={get_formid_str(struct.unpack_from('<I', snam.data, 0)[0])}")
                lines.append(f"Sound[{i}].Type={struct.unpack_from('<I', snam.data, 4)[0]}")
    return lines
