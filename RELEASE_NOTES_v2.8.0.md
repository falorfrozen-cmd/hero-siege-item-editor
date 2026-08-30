# Hero Siege Item Editor v2.8.0

## Infinite Vault

The editor now has a separate, effectively unlimited local item library.
Right-click an item in any numbered, Material, or Socket Shared Stash grid,
choose **Store in Infinite Vault**, and place it in a named collection. The
Infinite Vault workspace can search and page through the library, move items
between collections, and return an item to any compatible grid with free
space. The current Season 10 save exposes numbered tabs 1-19 plus the Material
and Socket grids; the editor discovers the grids that actually exist instead
of hardcoding nine tabs.

The database lives under Hero Siege's local application-data folder and is
opened by the Item Editor. It is not a game mod and does not change the game's
native stash limit.

## Exact item preservation

The vault stores the complete native stash entry, including fields the editor
does not understand. Perfect/Best seeds, Loaded/Overloaded Dice selection,
socket payloads, stacks, runewords, original keys/positions, and unknown future
fields round-trip without catalog reconstruction.

## Crash and retry safety

Transfers use a durable two-phase journal:

- Deposit stores a hidden exact database copy before removing the stash copy.
- Withdrawal reserves the database copy before writing the stash copy.
- Whole-stash before/after hashes prove which side of an interrupted operation
  completed.
- If an unrelated stash change alters that whole-file hash, recovery checks
  the exact journaled source or destination key and complete native entry
  before deciding which copy won.
- A repeated request ID returns the first result instead of duplicating an
  item.
- An ambiguous state keeps the exact database copy and blocks another transfer
  instead of guessing or deleting data.

Every database mutation has a consistent pre-mutation `.bak`; every changed
stash still gets the editor's normal `.guibak_<timestamp>` backup. Both SQLite
and every stash mutation are serialized across cooperating editor processes.
The launcher refuses to run beside a different/legacy Item Editor, and a
runtime peer check stops writes if another instance appears later. While open,
v2.8 also reserves the editor's complete local port range so v2.7.2 cannot
start afterward by silently choosing the next port.

The local HTTP interface now accepts only its exact loopback Host. Mutating
requests require same-origin JSON plus the editor's non-simple request header,
which blocks browser CSRF and DNS-rebinding attempts from reaching save code.

## Deliberate transfer boundary

All grid-backed Shared Stash containers are transfer endpoints in v2.8.0:
numbered tabs, the Material tab, and the Socket tab. Character bags/equipment
and the auto-sorted `unique_items` collection remain readable but cannot be
sent to Infinite Vault. `unique_items` has no native grid position, so it must
not be routed through the grid withdrawal algorithm without a separate proven
format and recovery contract.

Hero Siege must be closed for deposits and withdrawals. Viewing and searching
the vault remains available while the game is open.

## Verification

The release suite covers SQLite integrity, malformed input, inter-process
writers, ordinary-save/Vault serialization, full tabs, game-running refusal
including the backup window, opaque records, idempotent retries, hostile HTTP
boundaries, peer instances, and simulated crashes on both sides of each
transfer. See
[`INFINITE_VAULT_DESIGN.md`](INFINITE_VAULT_DESIGN.md) for the complete
reproduction and recovery design.
