# PACK conversion: verified-correct behaviour

Binary layouts and behaviours confirmed against vanilla. Do not re-litigate.

## Verified correct — do NOT "fix" these

Recorded so a later session does not re-litigate them.

| Area | Verification |
|---|---|
| PKDT flag re-derivation | Matches xEdit `wbPackageFlags` (`wbDefinitionsCommon.pas:7635`) bit for bit. Both collisions handled: TES4 bit 3 `Lock Doors At Package Start` vs TES5 `Maintain Speed At Goal`; TES4 bit 20 `Armor Unequipped` vs TES5 `Ignore Combat`. |
| PKDT dual format | `export_PACK` emits `PKDT.Format`, matching `wbPACKPKDTDecider` (4-byte subrecord = U16 flags + U8 type; 8-byte = U32 + U8). Measured: **561** old-format Oblivion packages, **zero** with any flag bit above 16 — so no flag is misread. Nehrim is 100% new-format. |
| PSDT layout | 12 bytes `<bbBbb3xi>`, Duration hours→minutes, `minute=-1`. Confirmed against all **5,961** vanilla PSDTs. The nonzero bytes at `[5:8]` in some vanilla records are uninitialised garbage (`ababab`, ASCII fragments), not a field. |
| PSDT DayOfWeek | `wbPackageScheduleDayOfWeekEnum` is a **shared** enum — identical 0..10 values incl. `Weekdays (MTWTF)`, `Weekends (SS)`, `Monday, Wednesday, Friday`. Oblivion's 306 day-scheduled packages copy through correctly. |
| PSDT Date | Non-issue: 7,134/7,209 Oblivion and 1,900/1,900 Nehrim packages write 0, and all 5,961 vanilla records write 0. |
| PTDA slot 3 = 0 | Re-confirmed: all **3,740** vanilla PTDA records write 0 across every target type. |
| Speed byte | Vanilla honours `PKDT` speed only when flag `0x2000` (Preferred Speed) is set — **4,386** vanilla records carry an inert `speed=2` with the flag clear. Writing walk-unflagged is inert, not a defect. |
| PKDT byte layout | `<IBBBBHH>` confirmed: `[4]`=Type (18 ×5,857 / 19 ×104), `[5]`=interrupt override, `[6]`=speed, `[10:12]`=interrupt flags. |
| Reused CTDA indices | `_FUNC_DROP` correctly catches the index collisions, incl. **365 = `GetPlayerInSEWorld` (TES4) → `IsChild` (TES5)**, 249 `GetPCFame` → `IsInDialogueWithPlayer`, 224, 227, 258, 259, 264. |
| Structural contract | `tools/esm/pack_validate.py output/Oblivion.esm/Oblivion.esm` → **clean, 7,209 records**. Every defect below is *semantic*, which is exactly why the structural validator passes. |

---
