# Hero Siege Item Editor

A save editor for **Hero Siege** (Pixel Prone Games). Manage your unique items, set pieces, and runewords — runs entirely on your PC, no internet required.

## Download

**→ [Releases page](../../releases) — download `HeroSiegeItemEditor.exe`**

Single file, no install, no Python needed. Just run it.

## Features

- 2062 unique items with icons, stats, rarity, and tier (S/A/B)
- Paper-doll character view with drag-and-drop equip/unequip
- Add any unique, set piece, or runeword to your stash
- Set browser — 69 sets with owned/missing counters; add all missing in one click
- Runeword Forge — 100 runewords (equipment path + codex path)
- Socket editor — add, remove, swap runes (207 runes, autocomplete)
- Stat tooltip — hover any item to see all stats and level requirements
- Loadout save/load/export system
- Automatic backups before every write; one-click restore

## How to use

1. **Close Hero Siege** (the editor locks writes while the game is running)
2. Run `HeroSiegeItemEditor.exe` — a browser tab opens automatically showing the UI
3. Pick a character or stash tab on the left
4. Browse items, drag to equip, right-click for options, click **Save** when done

Backups are saved next to your save files as `*.guibak_*`.

## Run from source (optional, for developers)

Requires Python 3.8+, no external packages.

```
py -3 hs_item_editor_gui.py
```

The repo contains the Python source (`hs_item_editor_gui.py`) and the data files the editor needs. The exe on the Releases page has all of this bundled in — end users only need the exe.

## Notes

- Reads and writes local save files only — no internet, no game process injection, no anti-cheat interaction
- Always keep a backup before bulk operations (the editor does this automatically)

## Credits

Built with Python stdlib. Item data extracted from the game's own asset repository via YYToolkit.
