# Hero Siege Item Editor 2.16.3

## What's fixed

- **White bases come out white.** A white base's seed was chosen for its best
  stat rolls only, and the game rolls rarity from the same seed. Built by the
  game, 160 of the 363 white equipment bases the editor made came out Superior,
  Rare or better (the yellow "white" bases players reported). Every white base
  now takes a seed the running game itself built as Common, with its stats at
  their top. Checked in the game: 308 of 308 Common. Amulets and rings are never
  Common in the game, so nothing changes for them.
- **Forged runewords form.** A runeword forms only on a Common base, and about a
  third of the recipe x base pairs landed on a Superior or Rare one. Every
  runeword is now forged on a Common seed. Checked in the game: 3,687 of 3,715
  pairs formed; the other 28 are below.
- **Sockets match the game.**
  - A white base shows exactly the socket count you set, up to the most the game
    rolls for that base (for example 5 on a Royal Shield, none on gloves or
    belts).
  - A unique has exactly the sockets its seed rolls; the game ignores any other
    saved count. The editor now writes the game's count and keeps the socket
    editor to it. 59 uniques used to show more sockets than they have in the
    game (Zephy's Gown 4 instead of 3, most unique charms 2 instead of none),
    which is why a gem could go into some sockets and not the rest. Seven of
    them now get a seed that rolls the count the editor used to promise.
- **Runeword bases the game refuses are closed.** Disaster and Celestus do not
  form on some of the bases their targets name, even on a Common base with the
  right sockets (20 and 8 bases). Those bases now show as unavailable instead of
  forging an item that is not a runeword.
- **A white Great Helm no longer becomes the Miner's Helmet.** A new item never
  takes a seed that a Custom Forge item on the same base already uses.
- **Perfect** on a white base gives it the best Common seed; **Reroll** on a white
  base or a runeword stays on Common seeds. A dropped magic or rare item still
  rerolls at random.
- **Items you made with an earlier version keep their seed.** Use **Perfect** on a
  white base or a runeword to move it to a Common seed; its runes and sockets
  stay.
- **The game's own view of an item notices a socket change.** A verified tooltip
  of a non-unique item stops counting as the game's once its saved socket count
  changes, until the game builds it again (the game reads that count).

## How it was measured

ForgePact's Item Truth had the running game build about 135,000 items at the
main menu, through its own save loader, without touching a save: every white
base with 120 stat-ranked and 212 random seeds, every unique the editor claims
sockets on, every runeword recipe x base. The results are in
`hs_game_seeds.json`, keyed to the game build they were measured on
(`pe-6aaa6779-0cad4fc8`); `build_game_seed_table.py` measures them again after
a game update. Details: GAME_TRUTH_DESIGN.md, step 4.

Everything else is as in 2.16.2. Download the exe and run it; no install.
