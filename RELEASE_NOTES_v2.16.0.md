# Hero Siege Item Editor 2.16.0

## Tooltips show what the game built

Until now the editor worked out an item's lines itself, from the seeds in the
save, with the rules of the 2026-08-28 game build. The game changed since, and
that calculation never covered the random affixes of ordinary items, so tooltips
often differed from the game. Measured on 755 owned items with the 2026-09-16
build: 199 matched line for line; ranges were right, rolled values often were not.

2.16.0 shows the game's own values wherever the game has built the item:

- the full name with the magic prefix and suffix, the rolled rarity (Common,
  Superior, Rare, Legendary, Satanic, …), the tier and the level requirement;
- every stat and every random affix, with its range;
- a **✓ Game verified** line, and in the details view the lines where the old
  estimate differed.

Where the numbers come from:

- **ForgePact 1.4.6 or newer** records each item the game finishes building while
  the game runs. The editor asks for this by creating
  `%LOCALAPPDATA%\Hero_Siege\itemtruth\capture.request`; the **GAME TRUTH** line
  turns it off and on.
- **AFK FARM** delivery records already hold every delivered item, so the AFK
  items in your Vault are verified as soon as 2.16.0 has read them, as long as
  those records are still on this computer (on a test Vault: 6,716 of 6,802 items).

- **The game checks the rest by itself.** While Hero Siege runs - the main menu is
  enough - the editor sends every item it has not verified yet to ForgePact, and
  the game builds each one in memory with its own save loader, records it and
  throws it away. On a test save 342 items took about 2 seconds, and afterwards
  all 7,628 owned items were verified: every character (loaded or not), the
  Shared Stash and the whole Vault. After a game update, everything is checked
  again the same way.

Until the game has checked an item, it keeps the editor's own calculation, now
labelled **Estimate** unless the game runs the build those rules were made for.
Item Forge base stats use the same records, and grid tiles take the rarity the
game rolled.

## Tooltips read exactly as in the game

The game also draws each item's tooltip for the editor, and the editor shows what
it drew, row by row and in the game's colours:

- the name, the type line (`Satanic Set Body Armor`), socket contents, Attack
  Damage, Attacks per Second or Defense, Block Chance;
- proc lines (`22% Chance when Casting [Fist of the Heavens] Level 70`), skill
  grants (`+16 to Omnislash (Samurai)`), auras (`Angel's Vibrance Aura Level 27 [20-30]`);
- every stat line in the game's order, wording and sign (`-25% to All Enemy
  Resistances`, `Ailment damage increased by 35%`, `+300 to Mana (Based on Level)`);
- sockets with their range, star level, set pieces, lore, `Unidentified` where the
  game says so, and the Tier / Requires Level footer.

The editor's roll range still follows each rolled stat. How it works: while an item
tooltip is open in the game, the game draws the tooltips of your other items off
screen, a few per frame (at most 3 ms), and ForgePact records the text. Hovering
any item for a minute or two is enough: on a test save the game drew 7,607 items in
about 2 minutes, and all 7,628 owned items then read as in the game. New items are
drawn the next time a tooltip is open. Until an item has been drawn, its stat lines
already use the game's own labels, formats and colours (on 7,628 drawn tooltips,
all 41,920 lines they share read the same); relic skills take their names from the
game's translation files.

Nothing is written into saves or into the game: items are only built in memory
and read. A check the game could not finish (for example because it closed) is
set aside and never restarted on its own.

Fixed: the editor could not find a game installed under a folder with Turkish or
other non-English letters in its path (it read the path garbled), which also kept
the Custom Forge status from finding the running game.
