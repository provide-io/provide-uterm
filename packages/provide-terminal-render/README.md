# provide-terminal-render

ANSI color rendering primitives for the [provide-terminal](../../README.md) platform. Zero required dependencies.

## Modules

| Module | Purpose |
|--------|---------|
| `palette.py` | ANSI16 and xterm256 color tables, `nearest_16()` / `nearest_256()` quantizers |
| `sgr.py` | SGR escape sequence emitters (truecolor, 256-color, 16-color) |
| `image.py` | Pillow image to ANSI art converter (optional `[images]` extra) |
| `buffer.py` | `AnsiBuffer` — pyte-based terminal buffer for screen state (optional `[emulator]` extra) |

## Installation

```bash
pip install provide-terminal-render                # core (palette + sgr)
pip install 'provide-terminal-render[images]'      # + Pillow image conversion
pip install 'provide-terminal-render[emulator]'    # + pyte terminal buffer
```

## Usage

```python
from provide.terminal.render.palette import nearest_256
from provide.terminal.render.sgr import sgr_truecolor

# Find nearest xterm-256 index for an RGB color
idx = nearest_256(200, 50, 50)

# Emit a truecolor SGR escape
esc = sgr_truecolor(fg=(200, 50, 50), bg=(0, 0, 0))
```

## Tests

97 tests, 100% branch+line coverage.

## License

AGPL-3.0-or-later. Copyright (c) 2025-2026 MindTenet LLC.
