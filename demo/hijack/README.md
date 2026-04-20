# Session Hijack

Three roles, one session. A viewer joins in read-only mode and watches. An operator acquires
an exclusive hijack lease and takes interactive control — typing commands that execute live in
the terminal. An admin can force-reclaim the lease at any time to take over. The viewer sees
everything as it happens.

**What you'll see:** Two browser windows open simultaneously: a viewer (read-only) and an
operator (interactive). The operator acquires the hijack lease and types a command. The
viewer's window shows the output appear in real time, confirming live propagation with no
page refresh.

## Files

| File | Description |
|------|-------------|
| [operator.mp4](operator.mp4) | Operator's browser perspective |
| [viewer.mp4](viewer.mp4) | Viewer's browser perspective |
| [operator_trim.mp4](operator_trim.mp4) | Highlight clip |
| [terminal.cast](terminal.cast) | Terminal session (asciinema) |
