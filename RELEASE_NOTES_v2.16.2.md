# Hero Siege Item Editor 2.16.2

## What's fixed

- **The editor starts while ForgePact's panel is open.** The editor keeps ten
  local ports, 8765-8774, so that no older copy of it can start alongside.
  ForgePact's panel prefers 8766, one of those ten. Now, when another program
  already holds one of them, the editor leaves that port to it and takes the
  rest. Before, it bound 8766 on top of the panel without noticing, or, where a
  program would not share its port, refused to start ("Local editor port … is
  occupied by an unidentified or legacy process").
- **It still refuses to run beside another Item Editor** of a different
  version. That now includes builds older than 2.7.2: before, the editor could
  start beside one of those without noticing.
- Starting the editor while the same version already runs still brings up the
  running one.

Everything else is as in 2.16.1. Download the exe and run it; no install.
