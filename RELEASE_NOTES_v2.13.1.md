# Hero Siege Item Editor v2.13.1

## Custom Forge stat names and safety

- Custom Forge now reads stat meaning directly from the Season 10 game-code
  and localization decode. The bundled semantics database contains 392 runtime
  keys and is bound to the supported Hero Siege executable SHA-256.
- All 324 observed, decoded keys use their real in-game names, units and plain
  descriptions. Six still-unknown observed keys remain available only behind
  the technical-ID toggle.
- This replaces the old tooltip-position guesses and corrects 43 shifted or
  otherwise wrong player-facing labels.
- Multi-key mechanics are atomic. Proc triples, skill grants, sub-skill grants,
  damage-over-time pairs and other linked properties are added and removed as
  complete groups.
- Skill IDs, class IDs and generated socket count are read-only. They can no
  longer receive arbitrary numbers through the editor API; verified unique-item
  donor presets provide the required identity values.
- Five catalog items whose recorded skill-grant data lacks the required class
  key are intentionally not offered as donors. This prevents the editor from
  creating a partial property the game cannot interpret safely.
- Entries backed by code heuristics are visibly marked `heuristic`. Raw numeric
  runtime IDs remain unchanged for ForgePact compatibility.

The semantics JSON is embedded in the one-file executable and validated at
startup for schema, executable identity, key ranges and linked-key closure.
