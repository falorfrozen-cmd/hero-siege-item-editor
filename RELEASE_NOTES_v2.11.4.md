# Hero Siege Item Editor v2.11.4

This release integrates the measured native socket chain into item creation and
Perfect/Best rolls.

## What's fixed

- New items and right-click Perfect now use the same socket-aware `a` seed, so
  applying Perfect no longer reduces a covered item's socket count.
- The bundled table contains 267 replay-verified item addresses. Poison Ivy is
  created with four sockets, St. Ahto's Diamond Hands with three, and Zephy's
  Gown keeps four after Perfect.
- Measured socket capacities override stale catalog/profile values while
  existing rune and gem payloads remain intact.
- All 24 Torch of Shadows class targets now combine the requested class with
  maximum variable stats and two native sockets.
- Infinite Vault now auto-arranges stored items into the same 17×18 grid design
  used by Shared Stash. Positions persist per collection; drag-and-drop,
  multi-select move/return, empty grids, compacting, custom names, comparison,
  a side-by-side Transfer Desk, and state-checked metadata undo are included.
- Shared Stash tabs, the visible character bag, and equipped gear now have an
  atomic MAX / Best Possible batch preview. Unsupported identities are skipped,
  malformed records block the whole batch, and stale previews cannot write.
- Simple Mode is now the default. Advanced Mode keeps Random Reroll, Duplicate,
  Edit Stack, and technical tooltip evidence available without crowding the
  normal workflow.
- Infinite Vault has one primary **Transfer Items** action. Collection/grid
  maintenance and history are tucked into a compact `…` menu, and the selection
  bar shows only actions relevant to the current selection.
- The verified socket table is packaged with the standalone editor and is
  validated at startup. Missing or malformed data fails safely.

## Verification

- 317 automated tests pass; one environment-dependent test is skipped.
- All 267 shipped socket seeds replay to their measured maximum without
  regressing the recorded stat-roll quality.

The measured input set originally contained 270 candidates. Three unidentified
candidates still need a new runtime capture, so this release deliberately does
not claim universal socket coverage for every game item.
