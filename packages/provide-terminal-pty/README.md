# provide-terminal-pty

Local PTY connector with PAM authentication and LD_PRELOAD capture for the [provide-terminal](../../README.md) platform.

## Components

| Module | Purpose |
|--------|---------|
| `connector.py` | PTY session connector — forks a child process with a pseudo-terminal |
| `capture_connector.py` | Session connector fed by `libuterm_capture.so` (no fork, observes existing shell) |
| `pam.py` | PAM lifecycle wrapper — direct libpam ctypes, no pamela dependency |
| `pam_listener.py` | Unix socket listener for PAM session events from `pam_uterm.so` |
| `uid_map.py` | UID/GID resolution and user switching |

## Native Libraries

| Library | Purpose |
|---------|---------|
| `native/capture/libuterm_capture.so` | LD_PRELOAD library that intercepts write/read/connect and sends frames to a Unix socket |
| `native/pam_uterm/pam_uterm.so` | PAM session module that sends login/logout events to the daemon |

Build with `make -C native/capture/ && make -C native/pam_uterm/`.

## Installation

```bash
pip install provide-terminal-pty
```

This package is Linux/macOS only (requires PTY support).

## Tests

192 tests, 100% branch+line coverage. Full PAM lifecycle tests run in Docker (`Dockerfile.test`).

## License

AGPL-3.0-or-later. Copyright (c) 2025-2026 provide.io llc.
