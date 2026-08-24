# Hero Siege Item Editor — Season 10

A local/offline save editor for **Hero Siege** (Pixel Prone Games). Manage items, set pieces, runewords, relics, and dedicated inventory tabs without connecting to the game process.

## Download

**→ [Releases page](../../releases) — download `HeroSiegeItemEditor.exe`**

Single file, no install, no Python needed. Just run it.

## Features

- Season 10 catalog profile: 944 current unique identities, including all 24 new S10 Heroic boss items
- Season 10 inventory support: Relics, Tarot, Essence Vaults, Keys, Materials, and Runes/Gems/Orbs
- Legacy entries removed from the S10 repository remain readable but cannot be generated
- Essence Vaults are readable/movable; generation stays locked because their rolled `v1/v2/d` payload is not a generic item seed
- Paper-doll character view with drag-and-drop equip/unequip
- Add verified items to the stash, character bags, potion belt, or compatible equipment slots
- Set browser — 69 sets with owned/missing counters; add all missing in one click
- Runeword Forge — 100 runewords (equipment path + codex path)
- Socket editor — add, remove, swap runes (207 runes, autocomplete)
- Season 10 Access Kits — generate all three new Uber tablets (Phantom Leviathan, Captain Grimtide, Blood Maiden) or all five Act IX dungeon keys in one backed-up operation
- Relic Lab and bulk stackable tools for Season 10 repository classes
- Save Health Check — read-only preflight scan for malformed files, invalid item addresses, grid collisions, equipment-slot mismatches, sockets, and stack values
- Safe repair mode only relocates deterministic grid conflicts or resets invalid stack amounts; every changed file is backed up first
- Global Item Finder — search every character, equipment slot, bag, potion belt, personal stash, and shared stash, then jump to the item
- Stat tooltip — hover any item to see all stats and level requirements
- Loadout save/apply/import plus reliable build export: portable `.hsbuild.json` and a self-contained, human-readable `.html` item report in `Downloads/HeroSiegeBuilds`
- Automatic backups before every write; one-click restore

## How to use

1. **Close Hero Siege** (the editor locks writes while the game is running)
2. Run `HeroSiegeItemEditor.exe` — a browser tab opens automatically showing the UI
3. Pick a character or stash tab on the left

Your characters and stash are detected automatically (standard Windows save folder) — no setup.

### Controls

- **Right-click any item** → menu with **Edit sockets**, **Reroll stats**, **Duplicate**, **Edit stack**, **Delete**
- **Drag** an item to move it within/between tabs, or drop it onto an equipment slot to equip
- **Drag from the Item Catalog** (right panel) onto a tab or slot to add a new item
- **Hover** an item to see its full stats and rarity
- **Runeword Builder** (left) → forge any of the 100 runewords into a stash tab
- **Sets** (left) → see owned/missing pieces, add all missing in one click
- **Loadouts** bar (character view) → save/apply/import full gear sets; **Export Build** writes both a re-importable JSON and a shareable HTML equipment report
- **Backups** (left) → restore any automatic backup
- **Global Item Finder** (left) → search all owned items and locate them in their character or stash tab
- **Save Health Check** (left) → scan without writing; apply only explicitly marked safe fixes when the game is closed
- **Season 10 Access & Materials** (left) → create a complete S10 Uber Access Kit, Act IX Dungeon Key Kit, or an individual verified key/material/gem stack

Every change auto-backs up first (saved next to your save files as `*.guibak_*`).

## Run from source (optional, for developers)

Requires Python 3.8+, no external packages.

```
py -3 hs_item_editor_gui.py
```

Run the offline regression suite with:

```
py -3 -m unittest -v test_hs_item_editor.py
```

The repo contains the Python source (`hs_item_editor_gui.py`) and the data files the editor needs. The exe on the Releases page has all of this bundled in — end users only need the exe.

## Notes

- Reads and writes local save files only — no game process injection and no anti-cheat interaction
- The game must be closed before a write; viewing is allowed while it is open
- Always keep a backup before bulk operations (the editor does this automatically)

## Credits

Built with Python stdlib. Item data extracted from the game's own asset repository via YYToolkit.
