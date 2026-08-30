# Hero Siege Item Editor — Season 10

A local/offline save editor for **Hero Siege** (Pixel Prone Games). Manage items, set pieces, runewords, relics, and dedicated inventory tabs without connecting to the game process.

## Download

**→ [Releases page](../../releases) — download `HeroSiegeItemEditor.exe`**

Single file, no install, no Python needed. Just run it.

## What's new in v2.7.2

- Give supported equipment its real **Exact Max** roll, or the mathematically
  **Best Possible** roll when the game cannot max every stat at the same time.
- Choose the skill on **Loaded Dice** and the sub-skill on **Overloaded Dice**
  by name, class, or ID.
- Move through the full Shared Stash while holding an item: use the mouse wheel
  or hold the pointer near the top/bottom edge to auto-scroll.
- Safer cancelled drags, naturally ordered stash tabs, and strict game-build
  checks so build-specific seeds are never applied to an unverified update.

See [the v2.7.2 release notes](RELEASE_NOTES_v2.7.2.md) for the plain-language
explanation and safety details.

## Features

- Season 10 catalog profile: 944 current unique identities, including all 24 new S10 Heroic boss items
- Season 10 inventory support: Relics, Tarot, Essence Vaults, Keys, Materials, and Runes/Gems/Orbs
- Legacy entries removed from the S10 repository remain readable but cannot be generated
- Essence Vaults are readable/movable; generation stays locked because their rolled `v1/v2/d` payload is not a generic item seed
- Paper-doll character view with drag-and-drop equip/unequip
- Add verified items to the stash, character bags, potion belt, or compatible equipment slots
- Set browser — 69 sets with owned/missing counters; add all missing in one click
- Runeword Forge — all 93 equipment runeword recipes across every compatible,
  verified normal base; the seven Zone Codex recipes remain visible but are
  disabled because their full save payload is not proven
- Socket editor — add, remove, swap runes (207 runes, autocomplete)
- Season 10 Access Kits — generate all three new Uber tablets (Phantom Leviathan, Captain Grimtide, Blood Maiden) or all five Act IX dungeon keys in one backed-up operation
- Relic Lab and bulk stackable tools for Season 10 repository classes
- Save Health Check — read-only preflight scan for malformed files, invalid item addresses, grid collisions, equipment-slot mismatches, sockets, and stack values
- Safe repair mode only relocates deterministic grid conflicts or resets invalid stack amounts; every changed file is backed up first
- Global Item Finder — search every character, equipment slot, bag, potion belt, personal stash, and shared stash, then jump to the item
- Stat tooltip — hover any item to see all stats and level requirements
- Verified Dice skill targeting — choose any of the 432 skill IDs for **Loaded
  Dice** or any of the 222 game-valid sub-skill IDs for **Overloaded Dice**.
  The editor writes only the item's native RNG seed; it never injects a
  synthetic skill field into the save
- Installed-build attestation — build-specific Perfect/Best and Dice seeds are
  enabled only when Steam's installed `Hero_Siege.exe` matches the clean,
  proven Season 10 SHA-256; an update or ambiguous install disables them safely
- Complete verified equipment-roll database — 423 normal addresses, all 921
  current unique **equipment** addresses, and 3,715 equipment runeword/base
  combinations (the other 23 current unique identities are consumable flasks,
  not rolled gear).
  Every profile is classified as **EXACT MAX**, exhaustive-domain
  **BEST POSSIBLE**, or inherently **FIXED**; generated-pool and late socket
  rolls are included in the same item-specific objective
- Loadout save/apply/import plus reliable build export: portable `.hsbuild.json` and a self-contained, human-readable `.html` item report in `Downloads/HeroSiegeBuilds`
- Automatic backups before every write; one-click restore

## How to use

1. **Close Hero Siege** (the editor locks writes while the game is running)
2. Run `HeroSiegeItemEditor.exe` — a browser tab opens automatically showing the UI
3. Pick a character or stash tab on the left

Your characters and stash are detected automatically (standard Windows save folder) — no setup.

### Controls

- **Right-click supported equipment** → apply **EXACT MAX / BEST POSSIBLE**;
  fixed-stat equipment needs no roll action. The same menu also offers
  **Edit sockets**, **Reroll stats**, **Duplicate**, **Edit stack**, and **Delete**
- **Right-click Loaded Dice / Overloaded Dice** → **Choose skill/sub-skill**;
  search by skill name, class, or numeric ID, then apply the verified native
  seed. Newly generated Dice items require the target skill to be selected in
  the Item Catalog first
- **Drag** an item to move it within/between tabs, or drop it onto an equipment slot to equip
- While dragging through **Shared Stash**, use the mouse wheel; if the native
  browser drag suppresses wheel input, hold the pointer near the top or bottom
  edge for continuous auto-scroll. No save occurs until a valid drop
- **Drag from the Item Catalog** (right panel) onto a tab or slot to add a new item
- **Hover** an item to see its full stats and rarity
- **Runeword Builder** (left) → forge any of the 93 verified equipment
  runewords into a stash tab; unsafe Zone Codex synthesis fails closed
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
py -3 -m unittest discover -s . -p "test*.py"
```

The standalone repository runs all application tests. The deeper generated-pool
parity class is skipped unless the separate research oracle and fixtures are
checked out in a sibling `_research` directory.

The repo contains the Python source (`hs_item_editor_gui.py`) and the data files the editor needs. The exe on the Releases page has all of this bundled in — end users only need the exe.

## Notes

- Reads and writes local save files only — no game process injection and no anti-cheat interaction
- The game must be closed before a write; viewing is allowed while it is open
- Roll seeds are verified per exact item address because the game advances its
  RNG through definition stats, generated-stat pools, hidden calls, late
  sockets, and special tails. Existing socket metadata and rune payloads are
  preserved; anything outside the proven equipment scope is left unchanged
  instead of receiving a fake universal "Perfect" seed
- The editor rechecks the installed executable identity before build-specific
  seed writes. If Hero Siege updates, regenerate and re-audit the databases for
  that build; do not bypass the hash guard
- Always keep a backup before bulk operations (the editor does this automatically)

## Credits

Built with Python stdlib. Item data extracted from the game's own asset repository via YYToolkit.
