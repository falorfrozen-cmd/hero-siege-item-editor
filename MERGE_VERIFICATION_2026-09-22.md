# Local Item Editor merge — 2026-09-22

Version: `2.15.5-s10-local`. This is a source build; launch `ItemEditor.bat`.
No release EXE was rebuilt.

## Sources and destination

All three source repositories started at `a261f2ad0b3c758c0edaa839061fd6f70ffc710d`.

- Canonical destination: `<Documents>\hero siege src\hero-siege-offline-toolkit\hero-siege-item-editor`
- Working copy synchronized with the destination: `<Documents>\Hero Siege\source\_worktrees\forgepact-ore-20260921\hero-siege-item-editor`
- AFK integration source, read only during this merge: `<Documents>\Hero Siege\source\hero-siege-offline-toolkit\hero-siege-item-editor`

Pre-merge files, original hashes, source revisions, and isolated browser results
are retained in `<Documents>\hero siege src\ItemEditor-Merge-Backups\20260922-125416`.

## Combined behavior

- AFK gear imports into expedition stashes in **AFK Farm**; native stackables
  import into **AFK Materials**. The existing spool API and client stay compatible.
- Category and stash deletion show names and counts before confirmation, check
  that the preview is still current, and retain a separate database backup.
- Deleted AFK import identities commit atomically with deletion. Retrying the
  same expedition after deletion or an editor restart creates no replacement items.
- Persistent page indexes and expedition placement work together: deleted page
  gaps stay absent and new expeditions can still create their own pages.
- Existing Miner helmet template and Forge UI changes are preserved. Its mechanic
  still requires the matching experimental ForgePact plugin.
- Schema 7 upgrades schemas 2–6 sequentially after backup. Older editors reject
  schema 7. Previous deletions without retained identities cannot be reconstructed.

## Automated verification

```powershell
py -3 -m unittest test_infinite_vault test_vault_integration test_http_security test_vault_deletion test_vault_ingest test_custom_item_forge test_launch_readiness -q
```

Result: **203 tests, 201 passed, two pre-existing failures**:

1. `test_tooltip_identity_is_enriched_with_the_verified_subskill_name`: missing
   `selectedName`; reproduced on the unchanged destination before merging.
2. `test_undecoded_ids_are_hidden_from_the_normal_player_list`: external research
   fixture `CLAUDE_CUSTOM_FORGE_STAT_DECODE_REQUEST.md` is absent; reproduced in
   the unchanged AFK source copy.

The 28 deletion/ingest tests all pass. Added regression cases first reproduced
deleted-item resurrection, then verified deletion/retry across restart, stable
page gaps, direct duplicate rejection, transactional rollback, and schema 6 to 7
migration with a valid pre-migration backup. Existing schema migration fixtures
were updated to represent their original schemas accurately.

## Browser and real client verification

A temporary save directory and SQLite database were used; the user's real save
files and Vault were untouched. Through the English Infinite Vault UI:

1. Imported four fixture items across two expeditions, with gear and material
   separated into their categories.
2. Deleted the first expedition stash (two gear items). Retried both expeditions
   using AFK Farm's actual `tools/ingest_spool.py`: **0 new, 4 duplicates**.
   The other expedition stash and material stayed present.
3. Deleted the remaining AFK Farm category (one gear item). Retried both again:
   **0 new, 4 duplicates**. AFK Farm stayed absent and the material remained.
4. Checked the displayed version, UI counts/fallback, empty browser error log,
   and both dedicated backups (`PRAGMA integrity_check = ok`, with the expected
   pre-deletion contents).

The temporary server was stopped and its browser tab closed after verification.
