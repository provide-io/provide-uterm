# provide-terminal

Core package for the [provide-terminal](../../README.md) platform. Contains the bridge session control hub, hosted server, CLI entry points, and root utilities.

## What's in this package

| Module | Purpose |
|--------|---------|
| `bridge/` | TermHub — session control hub with roles, hijack leases, presence |
| `server/` | FastAPI application factory, auth, connectors, webhooks, session runtime |
| `cli/` | `uterm` CLI entry point (proxy, listen, share, tunnel, inspect, watch) |
| Root modules | `control_channel.py`, `ansi.py`, `screen.py`, `defaults.py`, `io.py`, etc. |

## Installation

```bash
pip install 'provide-terminal[all]'
```

See the [main README](../../README.md) for extras and quick start guides.

## Entry Points

| Command | Module |
|---------|--------|
| `uterm` | `provide.terminal.cli:main` |
| `uterm-server` | `provide.terminal.server.cli:main` |

## Related Packages

This package depends on several companion packages via workspace symlinks. See the [package ecosystem table](../../README.md#package-ecosystem) for the full list.

## License

AGPL-3.0-or-later. Copyright (c) 2025-2026 MindTenet LLC.
