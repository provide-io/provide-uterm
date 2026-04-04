# SPDX-FileCopyrightText: Copyright (c) 2025-2026 MindTenet LLC. All rights reserved.
# SPDX-License-Identifier: AGPL-3.0-or-later

# provide-terminal-tunnel

Binary tunnel protocol and HTTP request interception for provide-terminal.
Implements a lightweight multiplexed frame format over WebSocket with four
channels (control, terminal data, TCP, HTTP) and an `InterceptGate` that
lets a browser pause, inspect, modify, or drop HTTP requests in flight.

## Installation

```bash
pip install provide-terminal-tunnel
pip install provide-terminal-tunnel[cli]   # adds httpx, uvicorn, websockets
```

Requires Python 3.11+. Zero required dependencies for the core protocol.

## Wire protocol

Each WebSocket binary message is a frame: `[1B channel][1B flags][N bytes payload]`.

| Channel | Constant | Purpose |
|---|---|---|
| `0x00` | `CHANNEL_CONTROL` | JSON control messages (must have `"type"` key) |
| `0x01` | `CHANNEL_DATA` | Terminal PTY bytes |
| `0x02` | `CHANNEL_TCP` | Raw TCP relay |
| `0x03` | `CHANNEL_HTTP` | Structured HTTP request/response JSON |

Flags: `FLAG_DATA` (0x00) for normal data, `FLAG_EOF` (0x01) for end-of-stream.

## Key exports

| Export | Description |
|---|---|
| `encode_frame(channel, payload, flags)` | Encode a tunnel frame |
| `decode_frame(data)` | Decode raw bytes into a `TunnelFrame` |
| `encode_control(msg)` | Encode a dict as a channel-0 control frame |
| `decode_control(payload)` | Decode control payload JSON |
| `TunnelFrame` | Frozen dataclass with `channel`, `flags`, `payload`, `is_eof`, `is_control` |
| `InterceptGate` | Async gate for pausing HTTP requests pending browser decisions |
| `InterceptDecision` | TypedDict: `action` (forward/drop/modify), optional `headers` and `body` |

## Usage

```python
from provide.terminal.tunnel import encode_frame, decode_frame, CHANNEL_DATA

frame = encode_frame(CHANNEL_DATA, b"hello")
parsed = decode_frame(frame)
assert parsed.payload == b"hello"
assert parsed.channel == CHANNEL_DATA
```

HTTP interception:

```python
from provide.terminal.tunnel.intercept import InterceptGate

gate = InterceptGate(timeout_s=10.0, timeout_action="forward")
gate.enabled = True
decision = await gate.await_decision("req-123")
```

## Key modules

- `provide.terminal.tunnel.protocol` -- `encode_frame`, `decode_frame`, `encode_control`, `decode_control`, `TunnelFrame`
- `provide.terminal.tunnel.intercept` -- `InterceptGate`, `InterceptDecision`, `parse_action_message`

## Links

- [Main repository README](../../README.md)
