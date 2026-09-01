# Hero Siege Item Editor v2.11.2

This update removes the recurring game EXE match lock.

## What's fixed

- Steam updates and ForgePact/Aurie-patched executables no longer disable
  Max/Best, Torch, Dice, or exact-tooltip features because their EXE hash differs.
- Items covered by the measured socket table are generated with their real
  maximum active. Poison Ivy gets four sockets and St. Ahto's Diamond Hands
  gets three. Perfect/Best normalizes older covered items by applying a
  replay-proven native max-socket seed instead of synthetic socket fields.
- The bundled roll and target databases still validate their own contents.
- Save writes still create backups and remain blocked while Hero Siege is running.
