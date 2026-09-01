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
- The verified socket table is packaged with the standalone editor and is
  validated at startup. Missing or malformed data fails safely.

## Verification

- 306 automated tests pass; one environment-dependent test is skipped.
- All 267 shipped socket seeds replay to their measured maximum without
  regressing the recorded stat-roll quality.

The measured input set originally contained 270 candidates. Three unidentified
candidates still need a new runtime capture, so this release deliberately does
not claim universal socket coverage for every game item.
