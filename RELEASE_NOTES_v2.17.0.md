# Hero Siege Item Editor 2.17.0

For AFK FARM's Stronghold (the Blacksmith).

## What's new

- **AFK FARM's Blacksmith reforges a Vault item.** You choose a unique piece of
  equipment in your Vault. The running game (ForgePact's Item Truth) builds 1, 2,
  4, 8 or 16 candidates: the same item with new seeds, so new values and a new
  socket count. AFK FARM shows them with the game's own numbers, and the one you
  choose replaces the item in place: the same place in the same category.
- **The game makes the item.** A candidate can be chosen only once the game has
  built it. The editor never writes an item the game did not build.
- **Nothing is lost or done twice.**
  - The Vault is backed up before the item changes (`before-afk-item-reforged-*.bak`
    next to the Vault).
  - Each reforge carries AFK FARM's request id and happens at most once. A
    cancelled request can never reforge.
  - The Vault's history lists each reforge, and History undo does not reach past it.
- **What cannot be reforged:**
  - items that are not unique (their seed also rolls their rarity);
  - runewords, and items with anything in their sockets (a new seed can change the
    socket count);
  - items whose seed chooses their skill;
  - Custom Forge items.

Hero Siege may be running (only the Vault database changes). Game truth and game
capture must be on, because the game builds the candidates.

Everything else is as in 2.16.3. Download the exe and run it; no install.
