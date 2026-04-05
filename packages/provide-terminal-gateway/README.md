# provide-terminal-gateway

Protocol conversion gateway for the [provide-terminal](../../README.md) platform. Bridges between browser WebSocket connections and traditional terminal protocols.

## Gateways

| Class | Direction | Use case |
|-------|-----------|----------|
| `TelnetWsGateway` | telnet client -> WS server | `uterm proxy` |
| `SshWsGateway` | SSH client -> WS server | `uterm listen --ssh` |

Both gateways handle:
- Bidirectional byte-stream relay
- Control channel frame encoding/decoding
- ANSI color mode negotiation (passthrough, truecolor, 256, 16)
- WebSocket reconnection with resume tokens (Cloudflare DO hibernation)

## Installation

```bash
pip install provide-terminal-gateway
pip install 'provide-terminal-gateway[ssh]'  # SSH gateway support
```

## Usage

```python
from provide.terminal.gateway import TelnetWsGateway

gateway = TelnetWsGateway("wss://server.example.com/ws/terminal")
server = await gateway.start("0.0.0.0", 2323)
# Telnet clients connect to port 2323, traffic relayed to WS
```

## Tests

122 tests, 100% branch+line coverage.

## License

AGPL-3.0-or-later. Copyright (c) 2025-2026 provide.io llc.
