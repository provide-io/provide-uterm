# PTY Sessions

Spawn a local pseudo-terminal (PTY) and serve it through the browser with full terminal
emulation. The shell runs as a real process on the host — not a simulation — with proper
resize handling, ANSI color support, and live snapshot capture.

**What you'll see:** A PTY session starts in a real shell. Commands run and their output
streams to the browser in real time. The terminal resizes correctly when the viewport
changes, and a snapshot captures the current screen state.

## Files

| File | Description |
|------|-------------|
| [browser.mp4](browser.mp4) | Full browser recording |
| [browser_trim.mp4](browser_trim.mp4) | Highlight clip |
| [terminal.cast](terminal.cast) | Terminal session (asciinema) |
