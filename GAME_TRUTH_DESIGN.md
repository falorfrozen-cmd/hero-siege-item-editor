# Game truth — design

How the Item Editor shows an item exactly as the game shows it.

## The problem

A save keeps an item's compact definition: the seeds `a`/`i`/`s`, the base `b`,
the kind `c`, the sub-type `j`, socket payloads `s1..s6` and a few flags. Every
line the player sees is computed by the game's `CreateItemNew` each time the item
is built: the rolled rarity, the magic prefix and suffix, up to five generated
affixes, runeword and special tables, socket shares, the display name.

`exact_tooltip.py` replays that computation in Python with the rules of the
2026-08-28 build (`Hero_Siege.exe` 438BF484…). Measured on 2026-09-24 against
the stat structs the 2026-09-16 build actually built:

| | |
|---|---|
| Owned items compared | 755 |
| Identical line for line | 199 |
| Game value inside the editor's range | 99–100% |
| Rolled value equal | 43% of save items, 15% of that day's AFK items |
| Items badged EXACT NUMBERS that differed | 211 of 258 |
| Generated affixes shown | 0 of 147 |

The ranges are right; the rolls drift with game updates, and the generated-affix
path was never modelled. Replaying harder cannot keep up with a moving target.

## The principle

**The game is the referee.** A number is called exact only when the game itself
built it, for this exact item version, on the running build. Everything else is
an estimate and says so.

The work is split into three steps, each useful on its own.

## Step 1 — the game's records (Item Editor 2.16.0, ForgePact 1.4.5)

### Capture (ForgePact, `ItemTruth.hpp` + `ModuleMain.cpp`)

- One hook point: the **outermost** `CreateItemNew` return. Everything is done by
  then — random stats, runewords, special tables, sockets, the display name — and
  ForgePact's Custom Forge dressing has been applied, so the record is what the
  game shows. A nested `CreateItemNew` (an item built inside another) is part of
  its parent and is not recorded.
- The record (one NDJSON line): build id, time, `itemTimeStamp`, `itemType`,
  `itemDataHash`, and `itemDefinitionStruct`, `itemStatStruct` and
  `itemInfoStruct` as the game serialises them; plus the stat struct before the
  dressing (`native`) when a forge entry changed it.
- The game thread only serialises and queues; a background thread writes
  `%LOCALAPPDATA%\Hero_Siege\itemtruth\journal\live-<build>-<start>-<pid>-<part>.ndjson`
  and `status.json`. Bounded queue (drops are counted), 16 MB parts, one line per
  distinct item content per session.
- Off unless `itemtruth\capture.request` exists. The editor creates it; ForgePact
  checks it at setup and every ~10 s, so an editor started later still works and
  removing the file pauses the capture.
- Build id: `pe-<PE link stamp>-<.text size>`. AuriePatcher appends a section and
  rewrites the entry point and image size but not these two, so a clean and a
  patched exe of one build agree and every update changes the id.
- The older `bp_ipc\itemstats.json` snapshot is now taken on the same final pass
  (it used to be taken inside `CreateItemInit`/`GenerateItemRandomStats` and
  missed the socket count on 121 of 386 compared items).

### Second source: AFK FARM's spool

AFK FARM writes the complete struct of every delivered item into
`%LOCALAPPDATA%\Hero_Siege\afk\spool\*_claim.ndjson` (captured when
`CreateItemNew` returns). The spool does not name the build: a record made after
the game exe on disk last changed is credited to that exe's build, an older one
keeps an unknown build.

### Store and matching (Item Editor, `game_truth.py`)

- `itemtruth\truth.sqlite3`, separate from the Vault so Vault backups stay small.
  Files are read incrementally (byte offsets; a half-written last line waits);
  identical records are stored once; fully read journals older than three days
  are deleted.
- A saved item is matched by its `itemTimeStamp` (the middle of its key
  `x-y-<timestamp>-<class>`), its class, and **every definition field**:
  - placement and editor fields are ignored (`g` slot, `w`, `zz`, `pos`);
  - `m`/`o` equal to 1 count as absent (a single item);
  - a native stackable's `o` (stack count) is ignored: the Vault merges stacks;
  - anything else must be equal, so a new seed from MAX/BEST, a changed socket,
    a star level never borrow an older record.
- Among matches the running build wins, then the newest. A record from another
  build is shown but not promised ("Game record · older build").

### The verified tooltip

- Every number from the record; definition ranges from the replay for fixed
  stats, generated-affix ranges and tiers from the affix slots `10`–`14`
  (`[stat, min, max, tier]`).
- Lines in the game's draw order (the address of each stat block in
  `DrawInventoryItemV2`, from `hs_stat_semantics_s10.json`); values the renderers
  never draw (447, keys named `unknown`) go to the details view.
- Name (`itemInfoStruct` 28, prefix 5, suffix 4), rarity (27), tier (32), level
  requirement (1) from the record.
- Without a record the replay stays, labelled **Estimate** unless the running
  build is the 2026-08-28 one.

### Limits of step 1 alone

- An item the game has not built yet — a new catalog item, an item just rerolled
  or maxed — is an estimate until the game builds it (step 2 does that on its own
  as soon as the game runs).
- Labels are still the editor's (stat semantics names); proc and skill-grant
  lines are shown key by key (step 3).

## Step 2 — the game checks any item on request (Item Editor 2.16.0, ForgePact 1.4.5)

The records of step 1 cover what the game happens to build. Step 2 covers the
rest: every item the player owns, whether or not the game has loaded it.

- **Queue.** The editor writes `itemtruth\requests\<id>.req`, one item per line:
  `<item key>\t<save data json>` (ASCII JSON, so the game's `json_parse` reads any
  name). Ids start with a time stamp, so they sort by age.
- **The game builds.** ForgePact picks up the oldest request once its setup has
  run and capture is on, claims it (`.working`), and on each frame builds items
  for at most 4 ms (200 at most): `json_parse`, then the game's own save loader
  `InitItemFromJson(json, key)` with the global instance as self - the same call
  `BuildAngelicPool` makes to validate its probe items. `CreateItemNew` runs, the
  Item Truth hook records the finished item with `"src":"eval"` and
  `"req":"<id>"` (always written, even if seen this session), and the struct is
  left to the collector: never dropped, placed in a grid or saved. Progress lines
  (`{"kind":"eval",...,"done":N,"finished":...}`) go into the same journal, so
  the editor reads records and progress in order. The file is deleted when done.
- **Never a crash loop.** A request the game was working on when it closed or
  failed is renamed `.stopped` at the next start and never resumed on its own;
  the editor shows it and waits for the player to clear it. Clearing gives the
  item the check stopped on (the first of its items the game has still not
  built) a strike and puts it last; an item that stops the game twice is not
  asked about again on that build. Items of a finished check that still could
  not be verified are not asked about again in that editor session.
- **Automatic.** Every 30 s, while ForgePact reports that the game runs (its
  `status.json` has a 30 s heartbeat), the editor queues whatever it owns that is
  not verified on the running build: after a game update, everything again. The
  GAME TRUTH line shows the count and the progress and offers a manual check.
- **Measured 2026-09-24:** 342 unverified items (a character never loaded, old
  Vault materials) were built in about 2 s at the main menu, 342 of 342; after
  it every owned item was verified: 699 on characters, 127 in the Shared Stash,
  6,802 in the Vault.
- Grid tiles (bags, stash, Vault) are coloured by the rarity the game rolled.

Next on the same queue: **MAX/BEST verified by the game** - the replay proposes
the best candidate seeds, the game builds them, and the editor keeps the one that
is truly best on the running build (9 of 33 "100 %" items were one step short).

## Step 3 — the game's own text (Item Editor 2.16.0, ForgePact 1.4.5)

Steps 1 and 2 make the numbers the game's. Step 3 makes the text the game's: the
tooltip is not rebuilt from rules, it is recorded as the game draws it.

### Recording what the game draws (ForgePact)

- While capture is on, the first time in a session the game draws an item's
  inventory tooltip (`DrawInventoryItemV2`), ForgePact records every text draw of
  that pass - the `draw_text*` builtins, `draw_text_outline(_ext)`,
  `DrawTooltipRichText` - with its arguments, the draw colour and alignment, and
  the `DrawInventoryStatsNew` call it happened in; and every stat call that drew a
  line (`[x, y, item, stat, label, format, style, …, per level, negated, colour]`).
  One `"kind":"tooltip"` journal line per item. Nothing is drawn differently.
- Once per session one pass also records every stat call, drawn or not: the
  **tooltip table** (`"kind":"tooltip-table"`) - the 335 stat lines a tooltip can
  draw on this build, in order, with label, format and colour.

### Drawing requests: tooltips of items the player never hovers

- The editor writes `itemtruth\tips\<id>.req` (lines like an evaluation request)
  for every verified item whose tooltip the game has not drawn on the running build.
- While the player has an item tooltip open, the hook of the game's own tooltip
  pass - with the tooltip's own instance, in its draw event - also builds a few of
  those items through the save loader and draws their tooltips into a 16x16 surface
  nobody sees: at most 6 items and 3 ms per frame, **before** the game draws the
  player's tooltip, so that one is always drawn last; the draw state (colour, alpha,
  font, alignment) is put back. Each drawing is journaled like one the player saw,
  with `"req":"<id>"`; progress lines are `"kind":"tipdraw"`.
- A request cut short (the game closed or failed) is set aside as `.stopped` at the
  next start. The editor clears it on its own; the item it stopped on gets a strike,
  and an item with two strikes is not asked for again on that build (hovering it
  still records it). An item a finished request drew that the editor still cannot
  tie to its record is not asked for again in that session.
- **Measured 2026-09-24:** 7,607 tooltips in about 2 minutes of an open tooltip
  (6 per frame, 0 failures), after which all 7,628 owned items showed the game's
  own text.

### Tying a drawing to its record

- A drawing is keyed by `itemTimeStamp` and `itemDataHash`. The game gives some
  items - potions, essence vaults, forged gear, a few uniques (51 of 7,628) - a new
  hash each time it builds them, with identical content. The store keeps a record's
  content once, so every other hash the same content came with is kept as an
  **alias** (`hash_aliases`), and a drawing under any of them reaches the record.
  A drawing made for a request is also tied to the content of the record on the
  journal line right before it (the item the game built for it).

### Showing it (Item Editor)

- `captured_tooltip_rows`: the draws of one pass, top to bottom. Outline copies
  (the same text within 3 px) keep the last draw; pieces on one line join left to
  right, a stat line's value and label with one space; colours are the game's
  (GameMaker keeps them as 0xBBGGRR). A stat call's line is the first line at or
  below the call that the call drew with left-aligned pieces - ForgePact's own
  centred rows for a forged item are drawn inside the same call, above it. The key
  hint (`ALT - Show Information`) is left out. A drawing made while ALT was held
  (the game's information view) keeps the ranges the game added, and the editor
  adds none of its own there.
- The tooltip shows those rows in the game's colours, with a gap wherever the game
  leaves one, and the editor's roll range after each rolled stat;
  **✓ Game verified · game text**.
- Before an item has been drawn, its stat lines take their label, value text,
  colour and order from the tooltip table (**✓ Game verified · game labels**):
  format 2 percent and 3 flat; style 8 value first and signed (`+449% Enhanced
  Damage`, `-25% to All Enemy Resistances`), style 9 label first (`Ailment damage
  increased by 35%`); per-level lines at level 100; negated lines below zero; two
  decimals for fractions (`+1.50 to Projectile Speed`); no line for a 0; skill
  grants as one line with their class (`+16 to Omnislash (Samurai)`); `to All
  Skills` and the element skill lines with the class of stat 21; relic skills named
  from the game's `translations*.csv` (`talent_name_relicMeatHook` → `Meat Hook`).
  Checked against the 7,628 drawn tooltips: all 41,920 lines they share read the
  same. The rest of such a tooltip (name, header, footer) stays the editor's until
  the game draws the item.

## Files

| Path | Written by | Read by |
|---|---|---|
| `%LOCALAPPDATA%\Hero_Siege\itemtruth\capture.request` | Item Editor | ForgePact |
| `…\itemtruth\capture.off` | Item Editor (capture turned off) | Item Editor |
| `…\itemtruth\requests\<id>.req` / `.working` / `.stopped` | Item Editor / ForgePact | ForgePact / Item Editor |
| `…\itemtruth\tips\<id>.req` / `.working` / `.stopped` | Item Editor / ForgePact | ForgePact / Item Editor |
| `…\itemtruth\journal\live-*.ndjson` | ForgePact | Item Editor |
| `…\itemtruth\status.json` | ForgePact | Item Editor |
| `…\itemtruth\truth.sqlite3` | Item Editor | Item Editor |
| `…\afk\spool\*_claim.ndjson`, `worker_*.ndjson` | AFK FARM | Item Editor |
