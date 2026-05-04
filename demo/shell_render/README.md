# Shell Rendering

Render any image URL as ANSI truecolor art directly in the terminal using the built-in
`render` command. Static images display as a single frame; animated GIFs loop continuously
at the original frame rate. No external dependencies — the renderer runs entirely inside the
shell process.

**What you'll see:** First, a static rainbow PNG gradient is rendered as 24-bit ANSI color
art filling the terminal. Then a sequence of animated GIF loops is rendered in-terminal:
the color-wheel loop, a local cat-face loop, and a remote Giphy loop.

## Files

| File | Description |
|------|-------------|
| [browser.mp4](browser.mp4) | Full browser recording |
| [browser_trim.mp4](browser_trim.mp4) | Highlight clip |
| [terminal.cast](terminal.cast) | Terminal session (asciinema) |
