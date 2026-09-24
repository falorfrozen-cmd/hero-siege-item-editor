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

Nothing is written into saves or into the game: items are only built in memory
and read. A check the game could not finish (for example because it closed) is
set aside and never restarted on its own.

Fixed: the editor could not find a game installed under a folder with Turkish or
other non-English letters in its path (it read the path garbled), which also kept
the Custom Forge status from finding the running game.
