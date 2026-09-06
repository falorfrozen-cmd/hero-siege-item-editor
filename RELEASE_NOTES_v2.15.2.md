# Hero Siege Item Editor 2.15.2

A quick fix for 2.15.1.

## Fixed

- **"Shared Stash unavailable: Unsupported or corrupt HSS text payload: stash.hss".**
  The stash was fine. The editor could only read Latin letters, so a stash tab, vault or
  character name written in Chinese, Cyrillic, Turkish or any other script made it give up.
  It now reads and writes every language the game does.
- The save health check keeps its strict corruption detection; only readable text in
  another language is accepted as healthy.

Everything else is as in 2.15.1. Download the exe and run it; no install.
