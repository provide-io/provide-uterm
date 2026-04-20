# Fleet Management

Spawn multiple workers, have them self-register with the fleet manager, and then coordinate them as a group. Workers check in automatically on startup, so you get a live inventory of what's running and where — no manual registration required.

**What you'll see:** Nine fleet workers start up and register themselves. A deploy command is broadcast to all nine simultaneously. Each worker executes it and responds. The recording shows the full lifecycle: workers coming online, grouping under a single fleet, and receiving the coordinated command.

## Files

| File                           | Description                             |
| ------------------------------ | --------------------------------------- |
| [grid.mp4](grid.mp4)           | Combined 3x3 grid view of all 9 workers |
| [grid_trim.mp4](grid_trim.mp4) | Highlight clip                          |
| [terminal.cast](terminal.cast) | Terminal session (asciinema)            |
