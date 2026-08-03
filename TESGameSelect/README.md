# Threads of Prophecy — Game Select

A small, redistributable Skyrim SE plugin. When you start a **new game**, it
detects which converted TES games are installed and asks which one you want to
play. Picking one hands control to that game's own character generation; picking
Skyrim leaves the vanilla Helgen opening completely untouched.

Supported starts:

| Choice | Starts | Requires |
|---|---|---|
| Skyrim | the vanilla opening, unmodified | — |
| Cyrodiil | Oblivion's `Charactergen` stage 5 — the Imperial Prison cell | `Oblivion.esm` |
| Nehrim | Nehrim's `Charactergen` stage 5 + `MQ00` | `Nehrim.esm` |
| Vvardenfell | Morroblivion's `fbmwChargen` stage 1 — the prison ship | `Morrowind_ob.esm` |

A game whose plugin is not in your load order simply never appears in the menu.

## Installing

Copy these into your `Data` folder (or install the folder as a mod):

```
TESGameSelect.esp
seq\TESGameSelect.seq
scripts\TESGameSelectQuest.pex
scripts\source\TESGameSelectQuest.psc   (source, optional)
```

Enable `TESGameSelect.esp`. Load order does not matter — it declares only
`Skyrim.esm` as a master and finds everything else at runtime.

**`seq\TESGameSelect.seq` is required.** A Start-Game-Enabled quest in an `.esp`
does not actually start without its `.seq` file, and it must stay a loose file —
it is read from `Data\seq\`, never from inside a BSA.

No SKSE required.

## How it works

* The quest is Start Game Enabled, so `OnInit` runs once at the start of a new
  game. It waits a few seconds for the vanilla opening to settle, then shows the
  menu.
* Detection uses `Game.GetFormFromFile()`, which returns `None` when a plugin is
  not loaded. That is why the plugin needs no masters and works with any subset
  of the games in any order.
* The menu is a single MESG with all four buttons. Each converted game's button
  carries a `GetGlobalValue(...) == 1` condition, set from the detection pass, so
  absent games are not drawn. Skyrim's button is unconditional, so the menu can
  never appear with nothing to click.
* A hidden button does **not** renumber the others — `Message.Show()` returns the
  button's own index. (Vanilla's `dunMiddenHandSculptureSCRIPT` relies on the
  same behaviour.) So the returned index is directly the game id.
* Choosing a converted game stops `MQ101` ("Unbound"), clears the opening's
  latched controls-disabled / in-chargen state, moves the player to that game's
  start marker, and sets its chargen quest to the stage its original author wrote
  as "the game begins here". Everything after that is the original game's own
  script.

## Configuring

Every plugin name, FormID and stage is a script property, editable in xEdit or
the Creation Kit without recompiling — useful if you ship renamed or translated
plugins.

| Property | Default |
|---|---|
| `OblivionPlugin` / `NehrimPlugin` / `MorroblivionPlugin` | `Oblivion.esm` / `Nehrim.esm` / `Morrowind_ob.esm` |
| `OblivionChargenID` / `OblivionStartMarkerID` / `OblivionChargenStage` | `0002466E` / `00032AB5` / `5` |
| `NehrimChargenID` / `NehrimStartMarkerID` / `NehrimMainQuestID` / `NehrimChargenStage` | `0002466E` / `00000D33` / `00000811` / `5` |
| `MorroChargenID` / `MorroStartMarkerID` / `MorroChargenStage` | `00F0A28C` / `00F0A278` / `1` |
| `StartupDelay` | `3.0` seconds |

FormIDs are the low 24 bits — the id *within that plugin's own file*, which is
what `GetFormFromFile` takes, so the load-order byte is irrelevant.

## Rebuilding

```bash
python tools/make_game_select_esp.py          # -> output/TESGameSelect/
python -m pytest tests/test_game_select_esp.py -v
```

The script source of record is `TESGameSelect/scripts/source/TESGameSelectQuest.psc`;
the build copies it into the output tree and compiles it.
