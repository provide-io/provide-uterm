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

C# is one of the four served server backends (Python, Go, C#, Cloudflare).
Parity-label definitions — `served`, `unserved`, `unsupported`, `partial`,
`N/A` — are in [`docs/parity-labels.md`](../../docs/parity-labels.md).

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
- `uterm proxy HOST PORT` — local browser WS → remote telnet/SSH (wire parity with
  Python `WsTerminalProxy`; **not** the FastAPI `mount_terminal_ui` embed helper)
- `uterm proxy --transport websocket --url wss://…` — local browser WS → remote WSS
- `proxy --once` starts Kestrel, hits `/health`, then stops

> **Terminal UI proxy embed** (`mount_terminal_ui` / library-mounted
> `WsTerminalProxy` router) is **Python FastAPI only** (permanent de-scope).
> C# operators use `uterm proxy` for the same browser WS → upstream path.

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
The gate floor is `COVER_THRESHOLD=97.9` (local/macOS after Wave10 ~98.09%;
Ubuntu ~98.02% / Windows ~97.96% residual baseline; dual-OS file-mode arms
explain the OS delta). Floor is not raised until both OS jobs show ≥0.2pt
headroom. Remaining misses match Go’s residual class: production
`Console.CancelKeyPress` wait arms (tests inject `WaitForCancel` no-ops),
Embed/session cancel races, RFB attach catch arms, dual-OS
`SetUnixFileMode` PlatformNotSupported, and rare codec/float fallbacks —
not untested pure library logic.

## Mutation testing

```bash
make mutation-gate   # NOT part of quality-gate — see below
```

Two stages: a small hand-rolled `&&`/`||` flipper (`ci/mutation_gate.py`, its
own perimeter) and real Roslyn-level mutation testing via
[Stryker.NET](https://stryker-mutator.io/docs/stryker-net/) (`ci/stryker_gate.py`,
installed as a local tool — see `.config/dotnet-tools.json`). Stryker's
perimeter (`tests/Provide.Uterm.Tests/stryker-config.json`) covers the egress
CIDR classifier, the connector and webhook egress guards, the webhook
registry/delivery loop (retry ladder, HMAC signing, auto-unregister), and the
TOML config binder — security-critical and wire-format-adjacent surfaces,
same philosophy as Python's `MUTATION_PATTERNS.md` and Go's
`mutation_equivalents.toml`. Genuinely-equivalent survivors are documented in
`mutation_equivalents.toml` (the `[[stryker_equivalent]]` entries).

Not part of `quality-gate`: Stryker recompiles an instrumented copy of the
*entire* mutated project regardless of how narrow the `mutate` glob is (C#
compiles per-assembly, not per-file), so a full run pays a fixed ~10 minute
analysis/build cost before any perimeter-specific testing even starts —
~25-30 minutes wall clock in total. Go's gremlins gate is likewise kept
separate from its own quality gate for the same reason.

## Conformance

`spec/uterm-api.yaml` lists required integrator symbols. C# is registered as a
third language (PascalCase, same as Go) via `spec/_conformance_extractors.py`.
