# Hero Siege Item Editor v2.12.0

This release rebuilds Infinite Vault around the same simple mental model as
Shared Stash: a category contains real, named 17×18 stashes.

## What changed

- The main Vault toolbar is reduced to `+ CATEGORY`, category selection,
  `+ STASH`, and one compact maintenance menu.
- Creating a category and its first `Stash 1` is one atomic operation. Every
  press of `+ STASH` appends exactly one permanent empty stash.
- A stash is renamed directly in its header. Enter or leaving the field saves;
  Escape cancels the edit. Names are Unicode-normalized and unique inside the
  category.
- Every Vault stash has **MAX / BEST**. The preview uses the same verified
  Exact MAX / Best Possible profiles as Shared Stash and sets every proven
  native stackable in that stash to `x999`. Fixed/unsupported items are
  skipped, malformed items block the whole write, and a stale preview cannot
  modify anything.
- Every Vault stash has **SEND TO SHARED STASH**. The destination is selected
  first, then every item is capacity-checked. One Shared Stash backup and one
  atomic journal protect the complete transfer; if one item cannot fit,
  nothing moves.
- Items remain draggable between the named stashes in their category. Empty
  stashes survive refreshes and restarts.
- Every compatible Shared Stash header now has **TO INFINITE VAULT** beside
  its MAX button. The dialog selects an exact category and named stash, proves
  that the complete source tab fits, and moves it in one journaled operation.
- Right-clicking a catalog-proven stackable offers **Add stack** in both Shared
  Stash and Infinite Vault. The entered amount is added to the current count
  (`2 + 500 = 502`); equipment and native singleton records are rejected.
- Native Relic and Tarot collection entries can omit grid coordinates. The
  editor now gives those records a deterministic, non-overlapping display-only
  layout, preserves any real coordinates, and grows the collection grid beyond
  six rows when needed. Invented coordinates are never written to the save.
- When no previous category choice exists, the category with the most items is
  opened automatically.

## Database migration and safety

- Infinite Vault schema is now version 6 with a `stash_pages` table.
- Schema 2–5 databases are copied to `hs_infinite_vault.sqlite3.bak` before
  migration. Existing item JSON, SHA-256, categories, positions, transfer
  journals, and audit history are preserved.
- Every old page index through the highest occupied page becomes a named stash
  (`Stash 1`, `Stash 2`, and so on). Every category receives at least one stash.
- Vault-only roll updates revalidate the stored payload hash and preview hash
  inside one SQLite transaction and create the normal rolling database backup.

This remains an offline editor feature. It does not patch Hero Siege, and all
writes stay disabled while the game is running.

## Verification

- 330 automated tests pass; one environment-dependent test is skipped.
- Embedded JavaScript syntax, schema 5 → 6 migration, atomic stale-preview
  rejection, category/stash persistence, both per-stash preview flows, and
  positionless Relic/Tarot collection layouts were exercised before packaging.
