# xEdit Scripting Reference (historical)

Linked from [CLAUDE.md](../../CLAUDE.md).

> **Status: the pipeline no longer uses xEdit scripts.** Export and import are
> both native Python (`tes4_export`, `tes5_import`). There are no `.pas` files
> outside `references/`, no `Edit Scripts/` directory, and no
> `conversion_settings.txt` protocol — the orchestrator passes options directly.
>
> This reference is kept because `references/xEdit` remains a primary source for
> record structure (`Core/wbDefinitions*.pas` documents the binary layout of
> every record type), and because ad-hoc xEdit scripts are still occasionally
> useful for verifying output by hand.

## Language environment

- **Language**: Pascal (JvInterpreter). Not full Delphi — many features
  unsupported.
- **Entry points**: `Initialize` (startup), `Process(e: IInterface)` (per-record),
  `Finalize` (cleanup).
- **Type system**: Everything is `IInterface`. Internally wraps `IwbFile`,
  `IwbMainRecord`, `IwbGroupRecord`, `IwbSubRecord`, etc.
- **Short-circuit evaluation does NOT work**: `if Assigned(x) and (x.Foo = 1)`
  will crash. Use nested ifs.
- **No object types, no constructors, no overloading, no `as`/`is` operators, no
  `with`, no `in`**.
- **`try/except` does not catch all runtime errors.**
- **No nested try blocks**: `try/except` inside `try/finally` is a syntax error.
- **TStringList cannot be passed as a function parameter** — "Type mismatch" at
  runtime. Use globals.
- **TStringList.Values[]** uses `=` as separator (same as our KEY=VALUE format).
- **TStringList.IndexOfName(key)** returns -1 if the key is absent.

## Key functions

| Function | Purpose |
|----------|---------|
| `Add(container, sigOrName, True)` | Create/find a subrecord or group |
| `ElementBySignature(rec, 'XXXX')` | Get subrecord by 4-char signature |
| `ElementByPath(rec, 'path\to\field')` | Navigate nested elements |
| `ElementByName(rec, 'Name')` | Get element by display name |
| `ElementByIndex(container, i)` | Get the i-th child element |
| `ElementCount(container)` | Number of child elements |
| `ElementExists(rec, 'path')` | Check if element exists |
| `GetElementEditValues(rec, 'path')` | Get string representation of value |
| `SetElementEditValues(rec, 'path', val)` | Set value from string |
| `GetElementNativeValues(rec, 'path')` | Get native value (int/float) |
| `SetElementNativeValues(rec, 'path', val)` | Set native value |
| `LinksTo(element)` | Follow a FormID reference to the target record |
| `Signature(rec)` | Get 4-char record type (e.g. 'NPC_') |
| `EditorID(rec)` | Get EditorID |
| `GetLoadOrderFormID(rec)` | Get load-order FormID |
| `SetLoadOrderFormID(rec, id)` | Set load-order FormID |
| `ElementAssign(array, HighInteger, nil, False)` | Append new entry to an array |
| `wbCopyElementToFile(el, file, asNew, deep)` | Copy element to file |
| `wbCopyElementToRecord(el, rec, asNew, deep)` | Copy element into a record |
| `GetFormVersion(rec)` / `SetFormVersion(rec, v)` | Form version (43=LE, 44=SSE) |
| `GroupBySignature(file, sig)` | Get top-level group from file |
| `RecordByFormID(file, id, allowInjected)` | Find record by FormID |
| `FileByIndex(i)` | Get loaded file by index |
| `AddMasterIfMissing(file, 'name')` | Add master dependency |
| `GetIsESM(file)` / `SetIsESM(file, b)` | ESM flag |

## Global variables

| Variable | Type | Description |
|----------|------|-------------|
| `DataPath` | String | Path to game's Data folder |
| `ProgramPath` | String | Path to xEdit installation |
| `ScriptsPath` | String | Path to Edit Scripts folder |
| `FileCount` | Integer | Number of loaded files |
| `wbAppName` | String | 'TES5', 'TES4', etc. |

## Retired: settings-file protocol

The old orchestrator communicated with xEdit scripts through
`Edit Scripts/conversion_settings.txt` using `MODE` (EXPORT/IMPORT/RELINK),
`SOURCE_FILE`, `EXPORT_DIR`, `IMPORT_FILE`, `OUTPUT_NAME`, `MAPPING_DIR`, and
`MASTER_MAPPINGS`. **Nothing in the repository reads or writes this file
anymore.**

## Retired: v1 export format fields

The pre-Python export emitted `TargetType=` / `OriginalType=` and prefixed model
paths with `tes4\`. The current format uses `Signature=` for the original TES4
type and applies no path prefixing — see
[pipeline.md](pipeline.md#text-export-format).
