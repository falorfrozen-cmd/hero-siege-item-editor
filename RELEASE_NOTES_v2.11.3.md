# Hero Siege Item Editor v2.11.3

## What's fixed

- Poison Ivy is created with four sockets and St. Ahto's Diamond Hands with three.
- Perfect/Best repairs older covered copies by applying the measured native seed;
  Edit Sockets uses the same measured capacity while preserving payloads.
- Existing socketed runes, gems, seeds, and unrelated item metadata are preserved.
- Max/Best uses a replay-verified, max-socket `a` seed when the normal best-stat
  seed would sacrifice a socket roll. St. Ahto and Poison Ivy were searched
  over the full seed domain.
