# Hero Siege Item Editor v2.8.1

This hotfix makes the verified Season 10 roll tools compatible with the
ForgePact/Aurie loader without weakening the game-build guard.

## Fixed

- ForgePact's **Install Mod Plugin** intentionally appends an executable
  `.aurie` PE section and redirects the game's entry point. Earlier Item Editor
  builds hashed the complete patched file, so every valid installation looked
  like an unknown Hero Siege build and disabled roll profiles.
- Aurie's payload contains the user's absolute `AurieCore.dll` path and mapped
  process values. Its full SHA-256 can therefore differ between machines and
  installations even when the underlying Hero Siege build is identical.

## Safe compatibility verification

The editor does not whitelist those variable patched hashes and does not trust
`Hero_Siege.exe.aurie_backup` as proof. For the current ForgePact loader it now:

1. Requires the exact AMD64 PE structure, clean executable size, original
   section count, and one final `.aurie` section at the clean file boundary.
2. Verifies the shipped Aurie section size, permissions, virtual address,
   redirected entry point, resulting image size, and mapped-PE signature.
3. Restores only Aurie's documented header changes in memory and logically
   removes the appended loader section.
4. Hashes the entire reconstructed base executable and still requires the
   proven clean Season 10 SHA-256
   `438BF4848688C5BE52AC15F26F02B46DA620D90587C28E766A9CEA190F3A7DE4`.

Any change to an original game byte, PE layout, loader shape, or trailing data
continues to fail closed. The item remains unchanged whenever proof is absent.

## Verification

- Clean `438B` build accepted through the original full-hash path.
- Real ForgePact/Aurie-patched `438B` executable normalized back to the exact
  clean SHA-256.
- Different Aurie payload/path hashes accepted only when their reconstructed
  base is byte-for-byte the proven build.
- Tampered base sections, malformed PE fields, overlays, truncated payloads,
  and files changed during verification rejected.
