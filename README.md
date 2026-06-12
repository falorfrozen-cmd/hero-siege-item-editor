# Hero Siege Item Editor

A standalone save editor for **Hero Siege** (Pixel Prone Games). Manage your unique items, set pieces, and runewords without touching any game files directly.

![screenshot placeholder](https://i.imgur.com/placeholder.png)

## Features

- **Item Browser** — 2062 unique items with icons, stats, rarity, tier (S/A/B), and level requirements
- **Stash Manager** — view and edit all stash tabs (Unique, Normal, Runeword, personal tabs)
- **Character Equipment** — paper-doll view with drag-and-drop; equip/unequip items, manage Main/Extra bags
- **Add Items** — inject any unique or set piece directly into your stash
- **Set Browser** — 69 sets with owned/missing counters; add all missing pieces in one click
- **Runeword Forge** — 100 runewords (equipment and codex paths); creates correctly formatted runeword items
- **Socket Editor** — add, remove, and swap runes in socketed items (207 runes, autocomplete)
- **Stat Tooltip** — hover any item to see all stats, set bonuses, and flavor text
- **Loadout System** — save/load/export full equipment configurations
- **Backup Manager** — automatic backups before every write; one-click restore
- **Drag & Drop** — rearrange items between bags, stash tabs, and equipment slots

## Requirements

**Option A — Run the exe (recommended, no Python needed)**  
Download `HeroSiegeItemEditor.exe` from [Releases](../../releases) and run it directly — no extraction, no extra files.

**Option B — Run from source**  
- Python 3.8+
- No external packages required (stdlib only)

```
py -3 hs_item_editor_gui.py
```
Or double-click `ItemEditor.bat`.

## Usage

1. **Close the game** (or the editor will refuse to write, protecting your save)
2. Launch the editor — it opens `http://127.0.0.1:8765` in your browser automatically
3. Select a character or stash tab from the left panel
4. Browse, drag, add, or remove items
5. Click **Save** — the editor writes to `stash.hss` / `char_*.hss` under `%LOCALAPPDATA%\Hero_Siege\`

Backups are saved alongside your save files as `*.guibak_*`.

## Data Files

| File | Contents |
|------|----------|
| `hs_full_catalog.json` | 2062 unique items (names, stats, icons, rarity, tier) |
| `hs_runewords.json` | 100 runewords with base type and required runes |
| `hs_sets.json` | 69 sets with piece definitions |
| `item_icons/` | 1574 sprite PNGs extracted from game assets |

## Notes

- This tool reads and writes **local save files only** — no network calls, no game process injection, no anti-cheat interaction
- The only "game contact" is a write lock: if the game is running, the editor detects this and refuses to save
- Always keep a backup of your save folder before bulk operations

## Credits

Built with Python + stdlib `http.server`. Item data extracted from the game's own asset repository via YYToolkit.
