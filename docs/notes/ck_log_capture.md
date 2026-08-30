# Reading the CK log while the CK holds it open

The `ckpe.log` is held with an exclusive lock; this is how to read it live.

## Capture method

The CK holds `ckpe.log` with an **exclusive write lock**. It is still readable
with `FileShare.Read` — plain `Copy-Item`, `cp`, and `FileShare.ReadWrite` all
fail with "used by another process", which is not the same as unreadable:

```powershell
$src = "C:\Program Files (x86)\Steam\steamapps\common\Skyrim Special Edition\ckpe.log"
$fs  = New-Object System.IO.FileStream($src,'Open','Read','Read')
$out = New-Object System.IO.FileStream($dst,'Create','Write')
$fs.CopyTo($out); $out.Close(); $fs.Close()
```

`Logs\CKPE\CreationKitPlatformExtended.log` is only the ~10 KB patch-init log —
it does **not** contain the warning stream. The repo's `CK_WARNINGS` file is a
stale manual capture; always re-pull the live log.

Warnings are tagged `[MASTERFILE]`, `[FORMS]`, `[EDITOR]`, `[SCRIPTS]`,
`[DEFAULT]`, `[PATHFINDING]`, `[MAGIC]`. Split ours from vanilla by FormID
prefix: `01......` is Oblivion.esm, `00......` is Skyrim.esm.

---
