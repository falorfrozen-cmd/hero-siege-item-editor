# Hero Siege Item Editor v2.8.2

This release adds a fail-closed HSS Recovery path for the narrowly proven
Season 10 Shared Stash corruption found in affected `stash.hss` files.

## What the error means

An affected file can still have a valid Base64 envelope, zlib stream, and zlib
checksum while its decoded text is invalid. The observed samples contain
unexpected non-zero bytes in the UTF-16 high-byte positions, damaged or blank
metadata for the `LocalNS` tab whose ID is `-5` (the Unique tab), and an exact
terminal `NUL/U+FFFF` sentinel. The normal strict decoder correctly rejects
that payload and previously reported the stash as unsupported or corrupt.

This evidence places the damage in the decoded payload before compression. It
does not, by itself, prove which game runtime, mod, plugin, synchronization
service, or other writer produced it. Recovery therefore never guesses the
writer or applies a broad byte-cleaning rule.

## Supported recovery profiles

v2.8.2 recognizes only three fully validated signatures:

- the observed garbled Unique-tab name plus the exact terminal sentinel;
- the observed blank Unique-tab name variant plus the exact terminal sentinel;
- the exact terminal sentinel by itself when the remaining stash is already a
  valid Season 10 document.

For the two metadata profiles, recovery restores only the `LocalNS` tab `-5`
name to `Unique`. It then removes only the exact invalid terminal code units.
All item containers and native item records remain unchanged.

Any different high byte, sentinel shape, duplicate JSON key, non-finite number,
invalid or non-canonical Base64, truncated or multi-stream zlib payload,
unexpected root field, duplicate/missing tab `-5`, malformed item record, or
unproven future schema is reported as unsupported and is not written.

## Read-only proof before repair

**Save Health Check** now attempts a recovery analysis when the ordinary stash
decoder fails. The confirmation card displays the matched profile, proposed
changes, preserved item count, top-level field/anomaly counts, and abbreviated
source-hash identity. Behind that preview, the recovery plan records and binds:

- source and proposed-output SHA-256 hashes;
- all 24 expected Season 10 top-level fields;
- the exact eight proven tab-metadata namespaces, finite reset value, exact
  `{tab,name}` row shape, allowed integral tab IDs, and no duplicate tab IDs;
- item counts for every numbered, Material, Socket, and Unique container;
- a canonical SHA-256 manifest covering every native item record; and
- any ordinary read-only health warnings found in the recovered preview.

The recovery button is not enabled while Hero Siege is running. A preview that
contains structural item or independent stash-metadata errors is rejected
rather than partially salvaged.

## Transaction and rollback safety

A confirmed recovery runs under the editor's process and stash-file locks and
requires the active file to match the previewed SHA-256. It also refuses to run
while an Infinite Vault transfer is pending, because that journal may still
need the exact pre-transfer stash state.

Before replacement, the editor:

1. materializes the output from the still-matching recovery plan;
2. proves strict decode, schema, item count, per-container counts, and item
   manifest equality after a complete encode/decode round trip;
3. writes and flushes a temporary candidate in the save directory;
4. creates and verifies an exact `stash.hss.pre_recovery_<timestamp>` backup;
5. rechecks the game/Vault gates and source bytes immediately before replace;
6. atomically replaces `stash.hss`; and
7. reopens the on-disk result and repeats the strict and ordinary health gates.

If final verification fails after replacement, the editor restores the exact
original from the verified recovery backup. Rollback first proves that the
active file is still the editor's proposed output, so a newer external write is
never overwritten. The permanent source backup is not subject to ordinary
backup rotation and remains available as **Recovery source** in Backups.

## How to use it

1. Close Hero Siege.
2. Open **Save Health Check** and scan.
3. Review the **HSS Recovery available** card.
4. Select **Recover stash.hss** and confirm.
5. Keep the reported `stash.hss.pre_recovery_*` backup.

If the card is absent, the file does not match a proven v2.8.2 profile. Leave
the original untouched and keep it for a separate forensic analysis.

## Verification coverage

The recovery suite covers healthy/idempotent input, every supported profile,
exact item-manifest preservation, source-hash races, invalid envelope and zlib
variants, duplicate JSON keys, non-finite values, unknown high bytes, malformed
schemas, size limits, game-running refusal, pending Infinite Vault transfers,
verified backup creation, atomic replacement, and simulated post-write rollback.
