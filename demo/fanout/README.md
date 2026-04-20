# Fan-out Broadcast

Send a single command to many sessions at once and collect all their responses. Useful for
fleet-wide config changes, health checks, or any operation where you need the same command
run on multiple nodes and want to see each node's output in one place.

**What you'll see:** A broadcast command goes out to 9 sessions simultaneously. Each session
executes it independently and returns its output. The results are displayed per-node so you
can spot differences across the fleet at a glance.

## Files

| File | Description |
|------|-------------|
| [grid.mp4](grid.mp4) | Combined 3x3 grid view of all 9 sessions |
| [grid_trim.mp4](grid_trim.mp4) | Highlight clip |
| [terminal.cast](terminal.cast) | Terminal session (asciinema) |
