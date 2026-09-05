# Custom Item Forge — engineering record

This document records the Season 10 Custom Item Forge implementation so it can
be rebuilt after a future game update without repeating the reverse engineering.

## What was proven

Hero Siege does **not** save a free-form affix list on each item. The native
save record contains a compact definition (`a`, `b`, `c`, `j`, and optional
generation seeds such as `i`/`s`). During load, `CreateItemNew`,
`CreateItemInit`, and `GenerateItemRandomStats` reconstruct a runtime item:

```text
itemType
itemDefinitionStruct
itemInfoStruct
itemStatStruct
```

`itemStatStruct` is the functional stat store. Its fields are numeric string
keys such as `"20"`, `"116"`, and `"118"`, and its values are numbers. The
game's own generated-item logs proved that ordinary stats and unique mechanics
both live here.

Example: a “Chance When Striking” effect is not one text line. It is the linked
runtime bundle:

```text
116 = spell/skill identity
117 = spell level
118 = proc chance
```

Equivalent observed proc families are `113/114/115` (attacking),
`122/123/124` (after kill), `125/126/127` (casting), `185/186/187` (struck),
and `188/189/190` (after blocking). The editor always copies those families as
complete bundles. Advanced mode still exposes every component key individually.

## Why a save-only implementation cannot work

Adding invented affix arrays to `.hss` does nothing because the game rebuilds
the computed structure from the native definition. Replacing only tooltip text
would look correct but would not change combat. Custom Item Forge therefore has
two cooperating parts:

1. Item Editor selects and validates the item and writes a sidecar.
2. ForgePact hooks the native item constructors and writes the chosen numeric
   values into the real `itemStatStruct` after native generation finishes.

No bytes in `Hero_Siege.exe` are patched by this feature.

## Files and schemas

Human-readable source of truth:

```text
%LOCALAPPDATA%\Hero_Siege\hs_custom_item_forge.json
```

Runtime input consumed by ForgePact:

```text
%LOCALAPPDATA%\Hero_Siege\hs_custom_item_forge.runtime
```

Runtime schema v1:

```text
HS_CUSTOM_ITEM_FORGE_V1
item|t=3;a=4677950;b=2;c=1;j=13|keep=1|116=167;117=25;118=15
```

- `t` is the native item type.
- `a/b/c/j/i/s`, when present, are stable generation identity fields.
- Placement fields `g/w/m` are deliberately excluded so moving or equipping an
  item does not detach its Custom Forge configuration.
- `keep=1` overlays selected keys on native stats. `keep=0` removes native
  `itemStatStruct` fields first.
- The runtime parser rejects unknown schema, malformed lines, non-finite values,
  more than 2,048 item entries, more than 512 stats per item, and stat IDs
  outside `0..9999`.
- Sidecar files are written through flushed temporary files and atomic replace.
  Existing copies are backed up under `custom_forge_backups` before replacement.

An exact duplicate has the same observable native definition and therefore
receives the same runtime setup. The editor reports this explicitly and never
silently rerolls the source item. When an editor action intentionally changes
`a`, `i`, or `s` (Random reroll, Perfect/Best, Dice/class selection), the sidecar
identity is retargeted so the selected item keeps its Custom Forge properties.

## Catalog generation

Run:

```powershell
py -3 build_custom_forge_catalog.py
```

Inputs:

- `hs_tooltip_roll_models.json`: observed runtime stat keys, values, item
  definitions, and labels.
- `hs_full_catalog.json`: exact current item identity and catalog lines.

Output:

- `hs_custom_forge_catalog.json`

Season 10 output at implementation time contains:

- 330 observed numeric stat keys;
- all 932 active unique donor records with catalog stats;
- 8,877 property presets.

The generator supplements the verified roll definitions with exact `Stat #N`
catalog fields and named fields that map to one unambiguous observed runtime
key. This closes the model gap for special charms and unique consumables while
still rejecting guessed or ambiguous mappings. Deprecated zero-stat records
are intentionally omitted.

Fixed boolean mechanics omitted from roll events are recovered when the
catalog retains their exact `Stat #N` key. The advanced stat list is the union
of the runtime model, all definition stats, and those fixed catalog keys.

## ForgePact implementation

Source: `ForgePact/plugin/ModuleMain.cpp`, section `Custom Item Forge`.

The release plugin loads the runtime sidecar during its normal delayed setup.
If there are no valid entries, it installs no item hooks. If entries exist, it
hooks the three item constructors, calls the original first, identifies a real
item by `itemType + itemDefinitionStruct`, and changes the returned/mutated
`itemStatStruct` with GameMaker's own `variable_struct_set` builtin. Repeated
constructor passes are idempotent.

Status is written to the game copy's:

```text
bin\bp_ipc\customforge_status.json
```

Release build:

```powershell
cd plugin_build
cmd /c build.bat release
```

The compiled `BloodPactPlugin_ship.dll` must replace the packaged
`modfiles_shipped\BloodPactPlugin.dll`; `build_release.py` rejects a stale DLL.

## Item Editor implementation

- Persistence/validation: `custom_item_forge.py`
- Catalog builder: `build_custom_forge_catalog.py`
- Bundled data: `hs_custom_forge_catalog.json`
- UI/API integration: `hs_item_editor_gui.py`

Right-click any item in character inventory, Shared Stash, or Infinite Vault
and choose **Custom Item Forge**. The same selector survives normal movement
between those stores because the item payload is preserved.

The modal has two searchable sources:

- **Stats** exposes all numeric keys with observed min/max and a recommended
  observed value.
- **Unique Properties** exposes donor item properties. Linked proc effects add
  every dependent key together. “Complete observed property set” copies all
  runtime stats observed on that donor definition.

Only the 172 keys with decoded player-facing names are shown in the normal
Stats list. The remaining 158 observed IDs are hidden behind a technical
toggle and explicitly marked undecoded. The exact reverse-engineering request
and required JSON schema are recorded in
`CLAUDE_CUSTOM_FORGE_STAT_DECODE_REQUEST.md`.

Forged items show an `F` marker and the tooltip states how many runtime keys are
active. Saving is disabled while Hero Siege is running. The native save record
is not rewritten when merely adding/removing Custom Forge properties.

## Update procedure after a game patch

1. Keep the old editor, sidecars, plugin DLL, and generated catalog backed up.
2. Capture fresh `itemdrops.jsonl` using the development ForgePact build while
   generating/loading representative normal and unique items.
3. Verify that computed items still expose `itemDefinitionStruct` and scalar
   numeric `itemStatStruct` fields.
4. Rebuild `hs_tooltip_roll_models.json` from the new build evidence.
5. Regenerate `hs_custom_forge_catalog.json`.
6. Review linked proc-family keys for additions or changed semantics.
7. Build both release and development ForgePact plugins.
8. Run all Item Editor and ForgePact tests, then test on a copied save with a
   harmless visible stat and at least one linked proc bundle.
9. Only after the copied-save test succeeds, package the DLL and editor EXE.

Do not claim support for a new game build merely because the old keys still
render in a tooltip. Combat behavior must be verified through the real runtime
stat structure.
