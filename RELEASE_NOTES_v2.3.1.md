# Hero Siege Item Editor v2.3.1 — Season 10

## Season 10 support

- Added all 24 new Season 10 Heroic boss items with verified repository addresses and icons.
- Added Leviathan's Blood and the current Season 10 inventory routes.
- Added the five Act IX dungeon keys and the three new Uber access tablets.
- Added one-click **New Uber Access Kit** and **Act IX Dungeon Key Kit** generation under **Season 10 Access & Materials**.
- Corrected the native key repository order: dungeon keys use IDs 36–40 and Uber tablets use IDs 41–43.
- Added the latest verified material fragments and boss gems.

## Editor improvements

- Redesigned the interface as **Hero Siege Vault** with a clearer character, stash, and catalog layout.
- Added **Save Health Check** with read-only scanning and conservative backed-up repairs.
- Added **Global Item Finder** across every character, bag, equipment slot, potion belt, and shared stash.
- Replaced WebView-dependent loadout downloads with reliable build exports to `Downloads/HeroSiegeBuilds`: a re-importable `.hsbuild.json` and a self-contained `.html` equipment report.
- Added Season 10 inventory tabs, a fifth Relic slot, expanded target validation, and safer item generation.
- Legacy or unverified repository addresses remain readable but cannot be generated.

## Safety

- The editor refuses all save writes while Hero Siege is running.
- Every successful write creates an automatic backup.
- Access-kit generation is atomic: if the Key bag lacks space, nothing is written.

Download `HeroSiegeItemEditor.exe` below. It is standalone and does not require Python.
