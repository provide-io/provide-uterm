# provide-terminal

Core shared package for the [provide-terminal](../../README.md) platform. It contains terminal I/O primitives, screen parsing, replay helpers, detection, rendering, and small operator-facing utilities used by the split companion packages.

## What's in this package

| Module                                | Purpose                                                                                            |
| ------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `ansi.py`, `colors/`, `render/`       | ANSI processing, color transforms, render helpers                                                  |
| `screen.py`, `detection/`             | Screen cleanup, prompt parsing, semantic extraction                                                |
| `telnet_session.py`, `line_editor.py` | Terminal session and local editing helpers                                                         |
| `replay/`, `session_logger.py`        | Recording and replay utilities                                                                     |
| Root package exports                  | Curated shared primitives such as `strip_ansi`, `normalize_colors`, `LineEditor`, and file loaders |

## Installation

```bash
pip install 'provide-terminal[all]'
```

See the [main README](../../README.md) for extras and quick start guides.

## Split Packages

| Package                     | Owns                                                                                                        |
| --------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `provide-terminal`          | Shared terminal primitives and root facade exports                                                          |
| `provide-terminal-server`   | `provide.terminal.bridge`, `provide.terminal.server`, `provide.terminal.gateway`, `provide.terminal.tunnel` |
| `provide-terminal-platform` | `provide.terminal.manager`, `provide.terminal.pty`                                                          |
| `provide-terminal-client`   | Client-side session control and MCP tooling                                                                 |

## Related Packages

This package depends on several companion packages via workspace symlinks. See the [package ecosystem table](../../README.md#package-ecosystem) for the full list.

## License

AGPL-3.0-or-later. Copyright (c) 2025-2026 provide.io llc.
