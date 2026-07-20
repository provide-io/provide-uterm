<!--
SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
SPDX-License-Identifier: AGPL-3.0-or-later
-->

# provide-uterm-csharp

C# port of the provide-uterm terminal session platform, wire-compatible with
the Python reference and aligned with the Go port's package map. Target
framework: **.NET 10**.

## Compatibility contract

Python remains the behavioral oracle. Wire formats and observable text
transforms are proven with differential corpora (same goldens as Go under
`tests/Provide.Uterm.Tests/testdata/`).

| Area | Parity evidence |
|---|---|
| `ControlChannel` | DLE/STX encode/decode, DLE escaping, incremental feed |
| `CtrlMsg` | 544-case HMAC identity-signature corpus (CPython-exact) |
| `Frames` | Frame types + builders |
| `Vt` / `Emulator` | pyte-compatible screen/stream |
| `Hub` / `Server` | Lease, presence, REST health/sessions, WS host |

## Package map (mirrors Go)

Portable surfaces live as namespaces under `src/Provide.Uterm/`:

`ControlChannel`, `CtrlMsg`, `Frames`, `Defaults`, `Ansi`, `Colors`, `Screen`,
`Vt`, `Emulator`, `DeckMux`, `Client`, `TermSession`, `Transports`, `Session`,
`Hub`, `Server`, `ServerAuth`, `ServerConfig`, `Connectors`, `Gateway`,
`Tunnel`, `TunnelClient`, `Fanout`, `Manager`, `Pty`, `Recording`,
`Replay`, `Redaction`, `Sanitizer`, `Filters`, `Channels`, `Shell`,
`Detection`, `Annotation`, `Render`, `Bridge`, `ControlPlane`, `Cli`, …

Binaries:

| Binary | Project |
|---|---|
| `uterm` | `cmd/Uterm` |
| `uterm-manager` | `cmd/Uterm.Manager` |

> **MCP** (`uterm-mcp`) is **not shipped** for the C# port (operator de-scope).
> Python and Go keep the MCP tool surface. There is no `Mcp/` namespace and no
> `Uterm.Mcp` binary in this tree.

CLI subcommands (real, not stubs): `proxy`, `listen`, `share`, `tunnel`,
`inspect`, `watch`, `audit`, `server`.

- `uterm listen WS_URL` — local telnet → remote terminal WebSocket (uwarp-compatible)
- `uterm proxy --transport websocket --url wss://…` — local browser WS → remote WSS
- `proxy --once` starts Kestrel, hits `/health`, then stops

## Build & run

```bash
cd packages/provide-uterm-csharp
export DOTNET_ROOT="$(brew --prefix dotnet)/libexec"   # Homebrew layout
make build-binaries
./bin/uterm --help
./bin/uterm server --host 127.0.0.1 --port 8780
./bin/uterm-manager --help
make quality-gate   # build + test + coverage floor + binaries
```

## Quality gate

```bash
make quality-gate
```

Coverage excludes pure Unicode/charset lookup tables (data-only) and live
OS/socket residual packages (`Pty/PtyTransport`, live telnet/SSH/WebSocket
transport bodies, Proxy/Gateway accept races — see `ci/coverage_gate.py`).
The gate floor is `COVER_THRESHOLD=98.0` (measured ~98.0% after residual
exclusions). Remaining misses match Go’s residual class: production
`Console.CancelKeyPress` wait arms (tests inject `WaitForCancel` no-ops),
Embed/session cancel races, RFB attach catch arms, Windows-only
`SetUnixFileMode` PlatformNotSupported, and rare codec/float fallbacks —
not untested pure library logic.

## Conformance

`spec/uterm-api.yaml` lists required integrator symbols. C# is registered as a
third language (PascalCase, same as Go) via `spec/_conformance_extractors.py`.
