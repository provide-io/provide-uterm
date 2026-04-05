# provide-terminal-transports

Network transport implementations for the [provide-terminal](../../README.md) platform.

## Transports

| Transport | Module | Protocol |
|-----------|--------|----------|
| Telnet | `telnet.py`, `telnet_transport.py` | RFC 854, NAWS, SGA |
| SSH | `ssh.py` | asyncssh-based |
| WebSocket | `websocket.py` | Stream reader/writer over FastAPI WS |
| Chaos | `chaos.py` | Packet loss/reorder simulator for testing |

Each transport implements the `ConnectionTransport` base class (`base.py`), providing a pluggable protocol layer for the session connector system.

## Installation

```bash
pip install provide-terminal-transports
pip install 'provide-terminal-transports[ssh]'       # SSH support
pip install 'provide-terminal-transports[websocket]'  # WebSocket support
```

## Usage

```python
from provide.terminal.transports import TelnetTransport

transport = TelnetTransport("bbs.example.com", 23)
await transport.connect()
data = await transport.read(4096)
await transport.write(b"hello\r\n")
await transport.close()
```

## Tests

408 tests, 100% branch+line coverage.

## License

AGPL-3.0-or-later. Copyright (c) 2025-2026 provide.io llc.
