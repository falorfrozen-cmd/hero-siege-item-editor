# Hero Siege Item Editor — Season 10

A local/offline save editor for **Hero Siege** (Pixel Prone Games). Manage items, set pieces, runewords, relics, and dedicated inventory tabs without connecting to the game process.

## Download

**→ [Releases page](../../releases) — download `HeroSiegeItemEditor.exe`**

Single file, no install, no Python needed. Just run it.

## What's new in v2.11.4

- Hero Siege **7.0.5 support** for 5,059 verified Max/Best roll profiles and
  exact numeric tooltips.
- **Detailed item tooltips**, side-by-side Vault comparison, and searchable
  Vault-only custom names.
- Move all items or one selected Shared Stash tab into Infinite Vault, then
  return them automatically or to a selected tab.
- One-click fill buttons for the **Unique, Material, and Socket** stash tabs.
- Choose the class skill when creating **Torch of Shadows**.
- Every item covered by the 267-address measured socket table is created with
  its native maximum active: for example **Poison Ivy** gets four and **St.
  Ahto's Diamond Hands** gets three. The editor now selects the real definition
  `a` roll used by the game instead of writing unrelated synthetic socket
  fields.
- Small Charms use native random rolls. The confusing Grade labels were
  removed.

Steam updates and ForgePact/Aurie-patched executables no longer disable
Max/Best, Torch, Dice, or exact-tooltip features just because the EXE hash
changed. The bundled profile databases still validate their own contents.

See [the v2.11.4 release notes](RELEASE_NOTES_v2.11.4.md) for the short summary.

## Previously added in v2.8.2

- **HSS Recovery:** Save Health Check can now recognize the exact Season 10
  `stash.hss` serializer-corruption signatures seen in affected saves: damaged
  or blank Unique-tab metadata together with the invalid terminal
  `NUL/U+FFFF` code units, plus the proven terminal-only variant.
- The read-only analysis binds the matched profile and exact metadata changes
  to the source/output hashes, per-container item counts, and an item-manifest
  hash before recovery is offered. The confirmation card shows the profile,
  changes, preserved item count, and source-hash identity.
- A confirmed recovery creates a byte-for-byte `stash.hss.pre_recovery_*`
  backup, writes and verifies a temporary candidate, atomically replaces the
  active stash, then reopens it and proves that every native item record is
  unchanged. A failed final verification restores the original automatically.
- Unknown high bytes, malformed or non-canonical envelopes, unexpected stash
  schemas, structural item errors, source-file races, a running game, and
  pending Infinite Vault transfers all fail closed without modifying the stash.

See [the v2.8.2 release notes](RELEASE_NOTES_v2.8.2.md) for the supported
signatures, safety gates, and recovery procedure.

## Previously added in v2.8.1

- **ForgePact/Aurie compatibility:** the editor now recognizes the exact
  AuriePatcher layout shipped by ForgePact while still proving that the complete
  underlying Season 10 executable is the verified `438B...A7DE4` build.
- Patched full-file hashes are never allowlisted. The verifier reconstructs the
  clean PE header in memory, excludes only the strictly validated final
  `.aurie` loader section, and hashes every byte of the original executable.
- A changed game section, malformed loader layout, unexpected overlay, wrong
  entry point, or verification-time file change still disables build-specific
  Perfect/Best and Dice seeds without touching the item.

See [the v2.8.1 release notes](RELEASE_NOTES_v2.8.1.md) for the compatibility
and verification details.

## Previously added in v2.8.0

- **Infinite Vault:** move items from every grid-backed Shared Stash tab — all
  numbered tabs (currently 1-19), Material, and Socket — into an unlimited,
  searchable local library with named collections, then return them to any of
  those grids.
- Crash-safe two-phase transfers keep an exact SQLite copy until the matching
  stash write is proven. Interrupted operations recover on the next vault
  open; ambiguous states preserve the item and stop for inspection.
- Stable request IDs prevent double-clicks and network retries from creating
  duplicate vault or stash items.
- The Global Item Finder now includes Infinite Vault results and can locate
  their collection card.
- All save writes and Vault recovery use the same cross-process stash lock;
  another/older editor instance is detected and blocked before mutation.
- The localhost API now rejects foreign Host/Origin, non-JSON, and requests
  missing the editor-only header before they can reach save operations.

The v2.7.2 Perfect/Best Roll, Dice skill selection, and shared-stash drag-scroll
features remain included.

See [the v2.8.0 release notes](RELEASE_NOTES_v2.8.0.md) and the
[Infinite Vault engineering record](INFINITE_VAULT_DESIGN.md).

## Previously added in v2.7.2

- Give supported equipment its real **Exact Max** roll, or the mathematically
  **Best Possible** roll when the game cannot max every stat at the same time.
- Choose the skill on **Loaded Dice** and the sub-skill on **Overloaded Dice**
  by name, class, or ID.
- Move through the full Shared Stash while holding an item: use the mouse wheel
  or hold the pointer near the top/bottom edge to auto-scroll.
- Safer cancelled drags, naturally ordered stash tabs, and strict validation of
  the bundled profile databases before any build-specific seed is applied.

See [the v2.7.2 release notes](RELEASE_NOTES_v2.7.2.md) for the Perfect/Best
Roll and Dice explanation.

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
- One-click, idempotent fill controls on the Unique, Material, and Socket
  Shared Stash tabs, with real-size grid placement and a single atomic write
- Save Health Check — read-only preflight scan for malformed files, invalid item addresses, grid collisions, equipment-slot mismatches, sockets, and stack values
- HSS Recovery — a separate, explicitly confirmed repair for narrowly proven
  Season 10 Shared Stash serializer corruption; it previews exact changes and
  item-preservation evidence before creating a permanent source backup
- Safe repair mode only relocates deterministic grid conflicts or resets invalid stack amounts; every changed file is backed up first
- Global Item Finder — search every character, equipment slot, bag, potion belt, personal stash, and shared stash, then jump to the item
- Infinite Vault — unlimited named SQLite collections connected to every
  numbered, Material, and Socket Shared Stash grid, with search, paging, exact
  item preservation, idempotent transfers, automatic recovery, a pre-mutation
  database backup, Vault-only custom names, two-item comparison, and an exact
  source-tab chooser for bulk deposits
- Numeric tooltip — hover saved equipment to see replayed native
  stat values and level requirements; unsupported paths are explicitly shown
  as a safe catalog preview rather than fabricated values
- Verified Dice skill targeting — choose any of the 432 skill IDs for **Loaded
  Dice** or any of the 222 game-valid sub-skill IDs for **Overloaded Dice**.
  The editor writes only the item's native RNG seed; it never injects a
  synthetic skill field into the save
- Mod/update-friendly operation — a Steam patch or ForgePact/Aurie modification
  to `Hero_Siege.exe` does not disable Perfect/Best, Torch, Dice, or tooltips
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
- **Right-click an item in any numbered, Material, or Socket Shared Stash
  grid** → **Store in Infinite Vault**; use the Infinite Vault workspace to
  search, organize, and return it to any compatible grid
- In **Infinite Vault**, use the pencil button to set or clear a searchable
  Vault-only custom name. Select two items with the compare buttons, then choose
  **Compare** for a side-by-side stat view
- While dragging through **Shared Stash**, use the mouse wheel; if the native
  browser drag suppresses wheel input, hold the pointer near the top or bottom
  edge for continuous auto-scroll. No save occurs until a valid drop
- **Drag from the Item Catalog** (right panel) onto a tab or slot to add a new item
- An ordinary **Small Charm** uses a native-random seed. Its rolled rarity and
  affixes are intentionally shown as unresolved; use reroll for a new seed, not
  the Dice skill selector
- At the top of the **Unique**, **Material**, or **Socket** Shared Stash tab,
  choose its green **Fill** button to add every missing catalog identity. Read
  and accept the confirmation; an automatic backup is created only when the
  stash actually changes
- In **Infinite Vault**, choose **Move Shared Stash to Vault**, then select
  either all item tabs or one exact numbered/Material/Socket/Unique source tab.
  The preview names the scope before confirmation; unselected tabs are not
  modified
- To return everything from **Infinite Vault**, choose **Automatic** routing or
  one exact numbered/Material/Socket/Unique destination. An exact special tab
  accepts only its native item type, and the whole transfer is cancelled if any
  item is incompatible or the selected tab has insufficient space
- **Hover** a saved item to see its build-verified numeric tooltip. Green
  **EXACT NUMBERS** means every displayed numeric path was proven for the
  installed build; **SAFE PREVIEW** means at least one path could not be proven
- **Runeword Builder** (left) → forge any of the 93 verified equipment
  runewords into a stash tab; unsafe Zone Codex synthesis fails closed
- **Sets** (left) → see owned/missing pieces, add all missing in one click
- **Loadouts** bar (character view) → save/apply/import full gear sets; **Export Build** writes both a re-importable JSON and a shareable HTML equipment report
- **Backups** (left) → restore any automatic backup
- **Global Item Finder** (left) → search all owned items and locate them in their character or stash tab
- **Save Health Check** (left) → scan without writing; apply only explicitly marked safe fixes when the game is closed
- **Season 10 Access & Materials** (left) → create a complete S10 Uber Access Kit, Act IX Dungeon Key Kit, or an individual verified key/material/gem stack

Every change auto-backs up first (saved next to your save files as `*.guibak_*`).

### Recover a supported corrupted `stash.hss`

1. Close Hero Siege and leave it closed until recovery finishes.
2. Open **Save Health Check** and run a scan. If the file matches a proven HSS
   signature, the page shows a separate **HSS Recovery available** card.
3. Review the matched profile, source SHA-256 identity, proposed metadata
   changes, and the number of item records that will be preserved.
4. Choose **Recover stash.hss** and confirm. The editor refuses if the file
   changed after the preview, another save writer is present, or Infinite Vault
   has an unfinished transfer.
5. Keep the generated `stash.hss.pre_recovery_*` file. It is the exact original
   and is intentionally not rotated with ordinary editor backups. It remains
   visible in **Backups** as a separate **Recovery source** entry for an explicit
   manual restore.

If no recovery card appears, do not force or hand-edit the file. The corruption
does not match a proven profile, so the editor leaves it untouched.

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
- HSS Recovery is deliberately not a generic byte cleaner. It repairs only
  exact, validated Season 10 signatures and rejects every unknown corruption
  pattern instead of guessing which bytes or items to discard
- Roll seeds are verified per exact item address because the game advances its
  RNG through definition stats, generated-stat pools, hidden calls, late
  sockets, and special tails. Actual `s1..s6` rune payloads are preserved;
  anything outside the proven equipment scope is left unchanged
  instead of receiving a fake universal "Perfect" seed
- The editor rechecks the installed executable identity before build-specific
  seed writes. If Hero Siege updates, regenerate and re-audit the databases for
  that build; do not bypass the hash guard
- Always keep a backup before bulk operations (the editor does this automatically)

## Credits

Built with Python stdlib. Item data extracted from the game's own asset repository via YYToolkit.
