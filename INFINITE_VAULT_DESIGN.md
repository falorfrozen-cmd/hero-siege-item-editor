# Infinite Vault — Reproduction and Safety Design

This document is the durable engineering record for rebuilding the Item
Editor's Infinite Vault after a future Hero Siege or editor update. It covers
the storage format, the exact transfer order, crash recovery, invariants, and
the tests that must pass before release.

## Scope

Infinite Vault is a local SQLite item library attached to the Item Editor. It
does not patch the game, keep Hero Siege open, or alter the game's stash size.
It moves an item between a grid-backed Shared Stash container and a separate
database.

Version 1 accepts numbered grids matching `stash_tab_[1-9]\d*` plus the native
Material and Socket grids (`material_tab` / `socket_tab`, including numbered
variants used by older fixtures). The current Season 10 save has numbered tabs
1-19. The UI obtains the exact existing compatible keys from `stash.hss`; it
does not assume a fixed tab count. Character inventory, equipment,
`unique_items`, metadata containers, and catalog-to-vault generation are not
transfer endpoints. `unique_items` is auto-sorted and has no native `pos`, so
it cannot safely use this grid withdrawal contract.

## Files and ownership

- Runtime database: `%LOCALAPPDATA%\Hero_Siege\hs_infinite_vault.sqlite3`
- Rolling pre-mutation database backup:
  `hs_infinite_vault.sqlite3.bak`
- SQLite process lock sidecar: `hs_infinite_vault.sqlite3.lock`
- Shared stash: `%LOCALAPPDATA%\Hero_Siege\hs2saves\stash.hss`
- Shared-stash/save-mutation lock: `stash.hss.itemeditor.lock`
- Short launcher-serialization lock: `editor-startup.itemeditor.lock`
- Normal editor backups: `stash.hss.guibak_<timestamp>`

The database path is based on `ROOT`, never the PyInstaller `_MEIPASS`
directory. This makes the one-file executable read immutable bundled assets
from `_MEIPASS` while writing user data only under Hero Siege's local data
folder.

## Process and local HTTP boundary

The lock order is always process `SAVE_WRITE_LOCK`, then the stash byte lock,
then SQLite's own transaction/sidecar lock. Ordinary save operations take the
same stash lock around their complete journal-gate/read/modify/backup/write
cycle; they cannot pass between a Vault prepare and stash replacement.
For a move spanning two native save files, every backup is completed before
one final runtime barrier and atomic replacements are destination-first. A
failure between replacements can therefore leave a recoverable duplicate,
never remove the only active copy.

Startup scans ports 8765-8774 for the editor's application identity and PID
under the short launcher lock. The same release is reused. A different or
PID-less legacy editor is rejected, not moved to another port. Startup also
fails closed if even one port is occupied by an unidentified process. While
v2.8 is alive it serves its identity on all ten reserved ports. This prevents
v2.7.2's old "try the next port" launcher from starting after v2.8. Save and
Vault POST paths still repeat the peer check before dispatch and at the final
write barrier.

The HTTP server binds only to `127.0.0.1`. It also requires the exact active
loopback `Host` on every request. POST accepts a bounded JSON object only,
requires an exact same-origin `Origin` when the browser supplies one, and
requires `X-Hero-Siege-Item-Editor: 1`. Foreign pages cannot send that
non-simple JSON request without a CORS preflight, which the editor does not
authorize; the strict Host additionally rejects DNS rebinding.

## Non-negotiable invariant

At every crash boundary, at least one durable exact copy of the item exists.

Temporary duplication is acceptable during recovery. Losing the only copy is
not. A prepared or conflicted transfer therefore retains `raw_json`; no
ambiguous state is resolved by deleting data.

The stored value is the complete native stash entry, not a catalog-based item
reconstruction:

```json
{
  "pos": [4.0, 7.0],
  "data": {
    "a": 429565.0,
    "i": 314159.0,
    "s": 271828.0,
    "s1": "<base64 socket payload>",
    "zz": {"sockets": 6.0},
    "unknown_future_field": {"must_survive": true}
  },
  "unknown_entry_field": {"must_survive": true}
}
```

This preserves Perfect/Best seeds, Dice skill seeds, sockets, stacks,
runewords, unknown future fields, key suffix/class identity, and original grid
position. A missing `data.b` is allowed for opaque special records; if `b` is
present it must be finite.

## SQLite model

`infinite_vault.py` owns schema version 7. Opening schema 2, 3, 4, 5, or 6 creates
one consistent pre-migration `.bak` first and migrates sequentially; a newer
schema is rejected.

- `collections`: unlimited, Unicode-normalized, case-insensitively unique
  category names. Creating one also creates its first stash in the same SQLite
  transaction.
- `stash_pages`: permanent named 17×18 stashes inside a category. `page_index`
  and the normalized name are unique per category. Empty stashes remain real
  database rows, so a refresh cannot make them disappear.
- `items`: exact `raw_json`, SHA-256, source key/location, searchable text,
  collection, optional Vault-only custom name, nullable `page_index`,
  `layout_x`, and `layout_y`, and one of `deposit_pending`, `available`, or `reserved`. Layout
  columns are metadata only; native `raw_json` and its hash never change when
  the user rearranges a grid.
- `transfer_batches`: one parent journal for atomic multi-item deposits and
  withdrawals. Child `transfers` are never committed independently.
- `transfers`: durable two-phase journal. It records direction, stable
  `request_id`, canonical request hash, item ID, exact raw JSON, source and
  destination tab/key/position, expected whole-stash before/after hashes,
  observed hash, error, and status.
- `deleted_deposit_keys`: durable import identities, raw hashes, and deletion
  timestamps for intentionally deleted imported items. These survive removal of
  their category and prevent the same import from recreating them.
- `events`: append-only audit history.

Transfer statuses are `prepared`, `committed`, `cancelled`, and `conflict`.
Prepared deposits are hidden from ordinary vault listing until the stash write
commits. Prepared withdrawals remain in SQLite as `reserved` until the stash
write commits. Committed withdrawals retain their raw journal record even
after the active `items` row is removed.

Every SQLite mutation performs a consistent backup before `BEGIN IMMEDIATE`.
The backup and mutation share the same inter-process OS lock. Schema creation
and the default `Vault` collection are committed atomically.

### Named stashes, persistent grids, and metadata undo

Each category contains named 17×18 stashes. The editor resolves true catalog dimensions,
preserves every valid saved placement, and deterministically first-fit packs
only missing/invalid positions. A drop is rejected when it is out of bounds,
overlaps another rectangle, names a stale `updated_at`, or no longer belongs to
the category. Moving to another category clears the previous page metadata
so the destination can place it safely.

The main UI deliberately exposes only category creation/selection, one-stash
creation, and the concrete stash grids. Stash names are saved on blur/Enter.
Each stash has a verified MAX / Best Possible preflight and an atomic complete
return flow that requires a concrete compatible Shared Stash destination (or
the proven automatic router). The roll write validates every stored hash again
inside one backed-up database transaction; a stale or malformed member cancels
the complete stash update. The same MAX transaction also sets every
catalog-proven native stackable to `x999`; stack-like opaque fields on
equipment and known singleton repository records are never treated as stacks.

Each compatible Shared Stash header has a complete-tab transfer action. The
user chooses both the Vault category and one named stash. Preview resolves real
item dimensions, accounts for existing target-stash rectangles, prefers each
original Shared Stash position when free, and fails before mutation unless the
whole tab fits. Prepared database rows already contain the chosen page and
coordinates, so crash recovery exposes them in the same named stash rather
than leaving placement as a second non-atomic step.

Layout initialization, one-item drops, compacting, and multi-item collection
moves each run as one backed-up SQLite transaction. The event log stores both
previous and current metadata. Undo is deliberately limited to the latest
un-undone custom-name, collection-move, or layout action and first proves that
every affected item is still available and still equals the event's post-state.
Game-save transfers are not metadata-undone; their journal and save backup are
the recovery mechanism.

## Deposit algorithm: Shared Stash to Infinite Vault

1. Refuse immediately if `Hero_Siege.exe` is running or process detection is
   unavailable.
2. Validate the source as a numbered, Material, or Socket Shared Stash grid;
   then validate `requestId`, key, and collection. The named container must
   also exist as an object in the decoded stash document.
3. Reconcile older pending transfers. A conflict blocks new save transfers.
4. Take the process/thread save lock and the stash OS lock.
5. Hash the current encoded `stash.hss` bytes (`before_hash`) and decode the
   document.
6. Read the complete source entry and serialize it as raw JSON without
   reconstructing any field.
7. Build the would-be stash document with only that key removed. Encode it
   exactly as the normal writer will and calculate `after_hash`.
8. In SQLite, `prepare_deposit` inserts a hidden `deposit_pending` item and a
   `prepared` journal row containing both hashes and the exact item.
9. Re-check the live stash hash, game state, and peer-editor state. If any
   changed, do not overwrite the stash; safely reconcile the prepared row.
10. Create the normal `.guibak`, repeat those checks after the potentially
    slow backup, atomically replace `stash.hss`, and hash the written bytes.
11. `commit_deposit` accepts only the expected `after_hash`, exposes the item as
    `available`, and commits the journal.

Crash behavior:

| Crash point | Durable state | Recovery |
| --- | --- | --- |
| Before SQLite prepare | Item remains in stash | Nothing to recover |
| After prepare, before stash replace | Stash item + hidden DB copy | Current hash equals `before_hash`; cancel and remove only the hidden copy |
| After stash replace, before DB commit | Item absent from stash + hidden DB copy | Current hash equals `after_hash`; commit and expose DB item |
| Unrelated whole-file hash | DB raw copy remains | Check the exact journaled source tab/key/entry: present means cancel; absent means commit |
| Malformed/unprovable state | DB raw copy remains | Mark `conflict`; never delete the copy |

## Withdrawal algorithm: Infinite Vault to Shared Stash

1. Refuse immediately while Hero Siege is running.
2. Validate `itemId`, stable `requestId`, and target tab.
3. Reconcile older pending transfers; block on conflict.
4. Lock the stash and read/hash it.
5. Read the available vault item without mutating SQLite.
6. Prefer the original item key when free; otherwise generate and persist a
   collision-free key.
7. Prefer the original grid position when it still fits. Otherwise find the
   first legal free rectangle using the catalog width/height. If no rectangle
   exists, return an error before any SQLite or stash mutation.
8. Build the exact destination entry, changing only `pos` when relocation is
   necessary. Calculate the would-be `after_hash`.
9. `prepare_withdrawal` marks the item `reserved` and persists the exact target
   tab/key/position and both stash hashes.
10. Re-check the live stash hash, game, and peer state; create `.guibak`;
    repeat the checks and atomically replace the stash.
11. `commit_withdrawal` accepts only `after_hash`, removes the active item row,
    and retains the committed raw journal record.

Crash behavior mirrors deposit:

| Crash point | Durable state | Recovery |
| --- | --- | --- |
| Before prepare | Item remains available in DB | Nothing to recover |
| After prepare, before stash replace | Reserved DB copy only | Hash equals `before_hash`; cancel reservation and restore availability |
| After stash replace, before DB commit | Stash copy + reserved DB copy | Hash equals `after_hash`; commit withdrawal and remove active DB row |
| Unrelated whole-file hash | Reserved DB raw copy remains | Exact prepared target entry present means commit; absent/different means cancel and restore DB availability |
| Malformed/unprovable state | Reserved DB raw copy remains | Mark `conflict`; never discard it |

## Idempotency

The browser creates one stable `requestId` per button attempt and reuses it
after a network interruption. The backend hashes only the immutable request
meaning (direction, item/source, collection or target). Repeating the same ID
and body returns the original committed result without creating a second item.
Reusing an ID with a different body is a hard conflict.

The journal also persists the chosen withdrawal key and position. A retry can
never choose a second destination and duplicate the item.

## API contract

- `GET /api/vault/meta`: collection counts, total, game lock, recovery state.
- `GET /api/vault/items`: paged available items with catalog display metadata.
- `GET /api/vault/history`: append-only event history and latest safe undo preview.
- `POST /api/vault/deposit`: shared-stash source, key, collection, request ID.
- `POST /api/vault/withdraw`: vault item, shared-stash target, request ID.
- `POST /api/vault/collections`: create, rename, `previewDelete`, or `delete`.
- `POST /api/vault/stashes`: add, rename, `previewDelete`, or `delete` one stash.
  Deletion requires an integer `collectionId`, plus integer `pageIndex` for a
  stash. `previewDelete` returns name, stash/item counts, and `previewToken`;
  `delete` must send that exact token. Full contents are removed on confirmation.
- `POST /api/vault/item`: move an available item between collections, change a
  Vault-only name, or add an exact positive amount to a proven native stack.
- `POST /api/vault/layout`: initialize, place, or compact persistent pages.
- `POST /api/vault/roll`: preview/apply verified rolls and native `x999`
  stack maxima to one concrete stash.
- `POST /api/vault/selection-preview`: exact non-mutating plan for selected return.
- `POST /api/vault/bulk`: complete or selected atomic stash transfer. A
  Shared Stash deposit may bind `destinationPageIndex` to one named Vault
  stash; the page and every item coordinate are part of the preview hash.
- `POST /api/vault/undo`: state-checked latest metadata rollback.
- `POST /api/vault/ingest`: HS AFK Expedition spool records, keyed by
  `(expedition_id, seq)` through `deposit_key`; gear to the expedition's own
  category on stashes named by rarity group (an expedition already in the older
  **AFK Farm** category continues on its page there), native stackables to
  **AFK Materials**; SQLite only. `deposit_many` stores a batch in one
  transaction with one pre-mutation backup; `layout: "defer"` plus a final
  `finalize: true` lays the expedition out once.
- `GET /api/vault/items?lite=1`: grid rows without tooltip models;
  `GET /api/vault/tooltips?ids=...` returns up to 200 models on demand.
- `POST /api/vault/purge`: preview/delete every item of chosen rarity groups in
  one category; see Confirmed deletion.
- `GET /api/vault/ingest/status`: how many of one expedition's records the
  Vault holds, has already returned to the Shared Stash, or has intentionally deleted.

Collection management never edits a game save. Deposit and withdrawal always
run under `SAVE_WRITE_LOCK` and refuse while the game is running.

### Confirmed deletion

The storage layer rechecks the content hash, last-category/stash constraints,
and unresolved transfer ownership under its process lock and `BEGIN IMMEDIATE`.
The hash covers the target identity, page identities/names, and full affected
item records. Stale previews fail without deletion. Before deleting, a dedicated
SQLite snapshot is written beside the database as
`hs_infinite_vault.sqlite3.before-delete-<uuid>.bak`; backup failure aborts the
operation. Item removal, page/category removal, deleted import identities, and
the history event commit together. The API also holds `SAVE_WRITE_LOCK` and
refuses while Hero Siege runs.
Completed transfer journals remain as idempotency evidence; unresolved journals
and reserved/pending items block deletion of their category or any of its tabs.

Clean-up by rarity (`preview_item_purge` / `purge_items`) follows the same rules
for an exact set of item ids the editor selects by catalog rarity group, never
items with a Vault-only custom name: the preview token hashes the category and
every selected row, a changed selection refuses the confirmation, a dedicated
`before-delete` backup is written first, deleted imports keep their deposit keys,
stashes it empties may be removed (the category keeps at least one), and
`items_purged` is an undo barrier like the other deletions.

AFK categories are found again through a `collection_created` event marker
(`{"afkExpedition": id, "collectionId": n}`), so renaming a category never splits
an expedition. `apply_named_layout` creates or names stashes and saves positions
in one transaction; it renames only empty stashes that still carry the default
`Stash N` name.

`list_deposit_keys` includes live items, committed withdrawals, and deleted import
identities. AFK ingest checks these before creating storage; direct `deposit` also
refuses a deleted key under the write lock. A failed delete rolls back its import
identities. Schema 6 to 7 migration preserves existing payloads and forces older
editors to reject the database. Deletions made before schema 7 cannot be backfilled
because their import identities were not retained.

Page indexes are stable, so deleting a middle/first tab leaves a gap. Layout
initialization and compaction pack into existing pages and append when necessary;
they do not fill deleted gaps. Deletion is a metadata-undo barrier, preventing an
older move/layout undo from sending an item back into a removed tab.

Dedicated deletion backups are not overwritten or automatically pruned. To
recover manually, close every editor instance and Hero Siege, preserve the
current database, then replace it with the chosen pre-deletion `.bak`. This is a
whole-Vault restore, including reverting later Vault changes; it does not restore
or change game save files. There is no one-click deletion undo.

## Verification before every release

From the `HSItemEditor` directory:

```powershell
python -m unittest test_infinite_vault -v
python -m unittest test_vault_integration -v
python -m unittest test_vault_deletion -v
python -m unittest test_vault_ingest -v
python -m unittest discover -s . -p 'test*.py' -v
python -m PyInstaller --clean HeroSiegeItemEditor.spec
```

Required scenarios include exact opaque round-trip, full destination tab with
zero mutation, game-running refusal with zero mutation, request retries,
database-first deposit crash, stash-first withdrawal crash, unknown items,
socket/stack/Dice fields, malformed databases, inter-process writers, and
packaged database creation in a user-writable location.

Never test transfers on real user saves. Point `SAVES` and `VAULT_DB_FILE` at a
temporary directory, as `test_vault_integration.py` does.

## Updating for a future game build

1. Reconfirm `stash.hss` encoding and that grid entries still have the
   `{pos, data}` envelope.
2. Reconfirm valid tab names and dimensions from real, backed-up fixtures.
3. Treat every unknown item field as opaque and preserve it.
4. Add a failing round-trip and crash test for any new container before adding
   it to `_vault_stash_tab`.
5. If schema changes, bump `SCHEMA_VERSION`, implement an atomic migration, and
   retain the pre-migration backup. Never silently open a newer schema.
6. Re-run the complete suite and packaged smoke test with Hero Siege closed for
   mutation tests and open for the view-only lock test.

If a transfer is ever marked `conflict`, do not manually delete its SQLite row.
The journal is the recoverable exact copy. Diagnose the recorded before/after
hashes and source/target entry before adding a tested reconciliation rule.
