# Hero Siege Item Editor v2.13.0

This release adds the ForgePact-backed **Custom Item Forge**.

## What changed

- Right-click any item in a character inventory, Shared Stash, or Infinite
  Vault and open **Custom Item Forge**.
- Search and add any of 330 numeric stat keys observed in the current Season 10
  runtime model. Each shows its observed range and a recommended value; the
  final number remains editable.
- Search all 932 active unique donor records and 8,877 property presets. Linked chance/proc
  mechanics copy every required identity, level, and chance key together.
- The normal list shows only 172 human-named stats. The 158 still-undecoded
  numeric IDs are clearly labeled and hidden behind an explicit technical
  toggle instead of pretending that `Stat #N` is a useful player-facing name.
- Copy a donor's complete observed property set when a mechanic uses new or
  still-unnamed numeric keys.
- Keep native stats and overlay the selected properties by default, or
  explicitly replace the native runtime stat structure.
- Forged items remain configured while moving between Shared Stash, character
  inventory, and Infinite Vault. Perfect/Best, random reroll, and verified
  skill/class seed changes retarget the configuration to the new identity.
- Forged items display an `F` badge and a tooltip status line.

## Runtime and safety

- The editor does not inject fake fields into `.hss` and does not patch
  `Hero_Siege.exe`. It writes an atomic, backed-up sidecar under
  `%LOCALAPPDATA%\Hero_Siege`.
- ForgePact applies the selected numeric keys to the real `itemStatStruct` after
  Hero Siege constructs the item. With no configured items, release builds do
  not install the item-construction hooks.
- Malformed schemas, unknown stat IDs, non-finite values, excessive entries,
  and writes while Hero Siege is running fail closed.
- Exact duplicate native item definitions are explicitly reported because the
  runtime cannot distinguish them without changing a native seed. The editor
  never silently rerolls an item to hide this limitation.

See [CUSTOM_ITEM_FORGE_RESEARCH.md](CUSTOM_ITEM_FORGE_RESEARCH.md) for the data
format, reverse-engineering evidence, rebuild steps, and update checklist.

## Verification

- 339 Item Editor tests pass; one environment-dependent test is skipped.
- 45 ForgePact contract/unit tests pass.
- The release ForgePact plugin compiles successfully with MSVC.
- Embedded JavaScript parses successfully. The live local UI was exercised
  without modifying saves: right-click menu, 330-stat catalog, unique-property
  search, linked `116/117/118` insertion, running-game write lock, and console
  error check all passed.
