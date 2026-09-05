# Hero Siege Item Editor v2.13.2

## New: Item Forge page

- A dedicated **Item Forge** workspace replaces the right-click-only flow.
  Three steps on one page: pick the base item (create a new one straight
  into the Shared Stash, or search an item you own), give it an identity
  (rarity shown in game, description under the stats), add properties, forge.
- Rarity and description are new sidecar fields (`rarity=`, percent-encoded
  `lore=`) applied by ForgePact 1.4.1 through `itemInfoStruct` keys 27 and 29
  and a private localization key. The game's own tooltip renders them; colour
  tags are not supported in the description block.
- Selecting an already forged item loads its properties for editing;
  REMOVE FORGE clears them.
- Sockets (stat 20) can be forged, 0 to 6. The game rolls sockets from the
  seed while it builds the item; ForgePact writes the forged count after that
  roll, so it holds. Runeword bases with the exact socket count are now one
  click away.

## Custom Forge: every decoded stat is now reachable

- The stat list no longer stops after 120 rows. Every named stat is listed
  without typing a search.
- Skills and classes are chosen from lists instead of being locked. The
  talent list is the game's own `talentStructMap` (817 talents, dumped from
  the running game and verified against the 432 known class skills and
  against the skill ids seen on unique items); the class list is the 24
  native classes. Free-text numbers are still refused for these keys.
- Proc families (chance when striking / attacking / casting / spellhit /
  after kill / when struck / after blocking), skill grants and sub-skill
  grants can be added from NAMED STATS; the whole family is added and the
  skill or class is picked in the selection panel.
- Keys the game code reads but no drop ever showed (for example the Spellhit
  family 119/120/121 and the minimum/maximum weapon damage keys) can be
  applied; only keys without code evidence stay technical-only.
- `Attack Damage` (22), the base damage components (447-451) and the flask
  charge keys (392/393) are independent stats again. An earlier semantics
  build linked them into one family, which made 22 impossible to forge.
- The five uniques whose recorded skill-grant data lacked a class key are
  offered again; the class is chosen when the preset is added.
- Clicking a stat now starts from a typical value (the median of the unique
  definitions that carry it) instead of the observed maximum, which was
  dominated by outliers (+70 to All Skills, 32 attacks per second). Sockets
  keep the maximum, 6. The value stays editable.
- Only the six keys without any code evidence (1, 2, 24, 108, 110, 295) stay
  read-only; every named stat, proc family, skill grant and socket count is
  clickable in both the right-click Custom Forge and the Item Forge page.
- The 62 keys the game code reads but no drop has ever shown (the Spellhit
  proc family, minimum/maximum weapon damage, the loot-table and regeneration
  stats) are listed too, marked "Read by the game code; never seen on a
  drop". Their defaults follow the observed keys of the same kind.
- Skill and class pickers start empty instead of silently defaulting to
  "Attack" / "Viking"; saving with an empty picker says which list to use.

- Forged items now actually apply in game. The sidecar identity no longer
  includes the `i` and `s` fields: the game's runtime item definition carries
  them only for some items and ForgePact requires every field to match, so
  every earlier forge silently did nothing. Existing entries are migrated on
  load.

## Data

- New bundled file `hs_talent_table_s10.json` (id → talent slug, English
  name from `translationsTalent.csv`, class where applicable).
- `hs_stat_semantics_s10.json` regenerated (base damage and flask families
  unlinked).

## Verification

- Existing suites plus new tests for pickers, identity validation,
  code-verified keys and the unbounded stat list.
