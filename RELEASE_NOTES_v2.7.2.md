# Hero Siege Item Editor v2.7.2

This update makes it possible to create the strongest roll the game can
actually produce, choose the skill on both Dice charms, and move through a
large Shared Stash without letting go of the item.

## Strongest real equipment rolls

Right-click supported equipment and choose **Verified MAX / Best Roll**.

- **Exact Max** means every variable stat on that item reaches its maximum.
- **Best Possible** is used when Hero Siege's random-number sequence makes a
  simultaneous all-stat maximum impossible. The editor then uses the proven
  seed with the smallest possible total shortfall.
- Fixed-stat equipment is identified automatically and does not get a fake
  reroll button.

The included Season 10 database contains 5,059 verified roll profiles. Of
those, 4,954 have an actionable Exact Max or Best Possible result. Normal
equipment, current unique equipment, and valid runeword/base combinations are
covered individually because one universal "perfect seed" does not exist.
Existing sockets, inserted runes, and unrelated item data are preserved.

## Pick the skill on Dice charms

- **Loaded Dice:** choose any of the 432 verified regular skill targets.
- **Overloaded Dice:** choose any of the 222 verified sub-skill targets.

Search by skill name, class, or numeric ID. The editor changes only the charm's
native random seed; it does not add a made-up skill field to the save file.

## Easier Shared Stash navigation

You can now keep holding an item and move through the whole Shared Stash:

- Use the mouse wheel while dragging.
- If the embedded browser swallows wheel input during a native drag, hold the
  pointer near the top or bottom edge for continuous auto-scroll.
- Stash tabs are shown in natural order (`1, 2, ... 10`) instead of text order.
- Cancelling a drag clears the held-item state. No save happens until the item
  is dropped on a valid cell or equipment slot.

## Safety checks

- Perfect/Best and Dice seeds are enabled only for the exact verified Season 10
  `Hero_Siege.exe` build.
- If the game updates or the installed executable cannot be verified, these
  build-specific actions disable themselves instead of guessing.
- Hero Siege must be closed before any save write.
- Every successful change creates a recoverable backup first.
- The editor works on local save files only; it does not inject into the game.

## Download and verification

Download `HeroSiegeItemEditor.exe` from this release, close Hero Siege, and run
the file. No installation or Python setup is required.

- Application version: `2.7.2-s10`
- Offline regression suite: `97/97` passing
- Windows executable SHA-256:
  `89E1D87B5AA84DEFB0A7046FA9E1EBCE42D3177C0C15ACD63DB5C515E9B998B5`
