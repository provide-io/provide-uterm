# Standalone C# Live Transport and GUI Parity Design

## Goal

Make `provide-uterm-csharp` a fully standalone implementation of the live terminal and remote-GUI platform on Linux, macOS, and Windows. C# must **observably match** Python and Go on black-box scenarios (not source layout). Capability-tagged supersets are allowed only when explicitly named; unbounded “improve upon” is out of scope for this program.

MCP is not part of the C# deliverable. The C# backend must implement the common REST, WebSocket, authentication, session, hijack, and GUI contracts well enough that the existing Go and Python MCP adapters can control it without C#-specific behavior.

## Product Boundary

The C# implementation is self-contained and requires only its .NET runtime and explicitly declared native/runtime dependencies. It does not shell out to Python or Go and does not run either language as a sidecar.

C# connects to and controls remote graphical sessions. It does not launch or host QEMU, libvirt, or litevirt VMs. QEMU may be used only as a real RFB test fixture. VM lifecycle and hosting remain the responsibility of litevirt or another remote hypervisor.

**In scope for this program:** PTY/process, sockets/WebSocket foundation, telnet client+gateway, SSH client+gateway, RFB client (raw TCP fixture first), GUI REST + lease gating, shared live harness, layered quality gates, external MCP consumer proofs.

**Out of scope:** C# MCP server/binary; embedding QEMU/litevirt/Python/Go; full tunnel multiplex / DeckMux UI / recording product parity unless a follow-on design is accepted; claiming parity from coverage % or unit tests alone.

## Current State (baseline)

| Workstream | C# status | Notes |
|---|---|---|
| PTY / process | stub / non-PTY | Process pipes; native open throws; no ConPTY |
| Socket / WebSocket | partial | Loops exist; fragment reassembly / hard limits incomplete |
| Telnet client/gateway | partial | Minimal IAC; gateway drive exists; full negotiator missing |
| SSH client | partial → host-key fixed in Phase 2 slice | SSH.NET; known_hosts verification required |
| SSH gateway | missing / CLI rejected | Accept-only; pump not wired |
| RFB client | missing | Tracker raw blit only |
| GUI control | stub → REST + memory attach in Phase 5 prep | Memory session; no litevirt |
| REST session/hijack | partial production | Health, sessions, hijack, WS present |
| GUI REST | missing → added (memory attach) | Go oracle for path shapes |
| Offline codec conformance (Layer A) | production | `vectors.json` / `ConformanceVectorsTests` |
| Live harness (Layer B) | missing → scaffolded | Greenfield under `conformance/live/` |
| Coverage residuals | active | PTY + live transports excluded from 97% floor |
| Mutation (C#) | missing | Introduce Stryker perimeter later; not a live-path blocker |
| Monorepo CI (C#) | missing → ubuntu `csharp-quality` | Local `make quality-gate` exists |

**Oracle language per surface:** Python owns codec vectors; Go owns real PTY, SSH known_hosts client, SSH/telnet gateways, GUI REST paths; Python server does **not** implement `/gui/*` today (`gui_rest` capability: `go|csharp`).

## Architecture

```
┌─────────────┐   REST/WS    ┌──────────────────┐   transport    ┌────────────┐
│ MCP (Go/Py) │ ───────────► │ C# UtermServer   │ ─────────────► │ PTY/SSH/   │
│ adapters    │              │ + TermHub        │                │ Telnet/WS  │
└─────────────┘              │ + GraphicalSession│               └────────────┘
                             └────────┬─────────┘
                                      │ gui/attach (memory | rfb later)
                                      ▼
                             ┌──────────────────┐
                             │ IGraphicalSession │
                             │ RFB client / mem  │
                             └──────────────────┘
```

- **Terminal control:** hijack REST + browser/worker WebSocket with DLE/STX control frames (existing).
- **GUI control (v1):** `POST .../gui/attach` attaches an `IGraphicalSession` on `WorkerTermState`; screenshot/click/type/key/drag require an active REST hijack lease. Memory mode is the deterministic fixture; raw RFB client follows Workstream 5.
- **Litevirt gRPC dual-stream:** not required for C# v1 (Go-only path). Optional follow-on.

## Parity Contract

Parity is defined by externally observable behavior. The same black-box scenarios run against Python, Go, and C# and compare:

- bytes and control frames sent and received;
- connection, authentication, negotiation, and session state transitions;
- timeout, cancellation, half-close, reconnect, and error behavior;
- backpressure and bounded-buffer behavior;
- cleanup of processes, sockets, tasks, and leases;
- screenshots, framebuffer state, keyboard/pointer events, and GUI lease behavior;
- REST and WebSocket response shapes consumed by MCP clients.

A C# feature is complete only when required shared scenarios pass on **required OS cells for that phase** (see CI Matrix). A platform-specific exception requires a documented capability tag and an alternate scenario that is required on supported platforms.

### Contract inventory (MCP-facing)

| Method | Path | Authz | Oracle | Notes |
|---|---|---|---|---|
| GET | `/api/health` | none/any | shared | `{ok,status,service,...}` |
| GET/POST/DELETE | `/api/sessions…` | session.* | shared | |
| POST | `/worker/{id}/hijack/acquire` | hijack | shared | `{ok,hijack_id,lease_expires_at,owner}` |
| POST | `…/hijack/{hid}/heartbeat\|send\|step\|release` | hijack | shared | |
| GET | `…/hijack/{hid}/snapshot\|events` | read | shared | |
| POST | `/worker/{id}/gui/attach` | mode | **go** (+ csharp) | Body: `mode=memory\|rfb` (csharp); go uses litevirt target |
| GET | `…/hijack/{hid}/gui/screenshot` | read | **go** (+ csharp) | PNG base64 in `screenshot` |
| POST | `…/hijack/{hid}/gui/{click,type,key,drag}` | hijack | **go** (+ csharp) | Lease required |
| WS | `/ws/browser/{id}`, `/ws/worker/{id}` | auth | shared | DLE/STX control |

Error envelopes: `DetailError` → `{detail}` (HTTP 4xx/5xx); `BridgeError` → `{ok:false,error}` for hijack/GUI operational failures.

## Shared Black-Box Harness

### Layer A (exists) — offline codec differentials

`packages/provide-uterm-*/…/vectors.json` + language drivers. Pure transforms only (control frames, ANSI, emulator, HMAC, DeckMux). **Do not overload** with live I/O.

### Layer B (new) — live scenarios

Location: `conformance/live/`

- `scenarios/*.json` — fixture setup, actions, expected events, timeout, cleanup assertions, capability tags.
- `schema/result.schema.json` — ordered events + base64 payloads + `capability` tags.
- Drivers launch language binaries (`uv run uterm`, Go `bin/uterm`, C# `bin/uterm`) or in-process test hosts without changing semantics.
- Comparison: exact when deterministic; capability-tagged when OS primitives differ.
- Required outcomes: **run**, **UNSUPPORTED** (with alternate required scenario), or fail. No silent skips in required jobs.

## Workstreams

### 1. PTY and process lifecycle

Native Unix PTY + Windows ConPTY. Go is the PTY oracle (not Python’s simplified shell). Shared scenarios: interactive echo, binary, resize, Unicode, exit, cancel, process-tree kill, open/close, handle cleanup. Tag `pty.conpty` for Windows.

### 2. Socket and WebSocket foundation

Bounded async read/write loops, explicit ownership/cancellation, fragmentation, ping/pong, half-close, backpressure, structured overflow errors. Default limits published in a limit table (control payload 1 MiB, buffer 10 MiB, hub text 40k chars; WS frame and RFB max dimensions enforced).

### 3. Telnet

Full IAC negotiation (client + gateway), NAWS, TTYPE, binary-safe payloads, gateway lifecycle. Complete live path; remove residual exclusion when scenarios green.

### 4. SSH client and gateway

Secure host-key verification **by default** (OpenSSH known_hosts files; insecure only via explicit config). Password/key auth, PTY allocate/resize, streaming, cancel, cleanup. Gateway: SSH accept → remote terminal WebSocket using shared control channel. Remove CLI `listen --protocol ssh` rejection only after shared scenarios pass.

### 5. RFB/VNC and GUI control

Real RFB client: version + security negotiation for supported types, ServerInit, Raw first (CopyRect after suite green). Bounded framebuffer allocation. GUI REST + lease gating as in Contract inventory. Deterministic RFB fixture on all OS; optional QEMU only on ubuntu, never required. Memory attach remains for unit/MCP smoke without RFB.

### 6. External MCP compatibility

No C# MCP binary.

- **6a Terminal MCP:** sessions + hijack against C# (Linux job first).
- **6b GUI MCP:** blocked on Workstream 5 REST + RFB/memory attach; capability `gui_rest: go|csharp`.

## Security Requirements

### Threat model (summary)

| Boundary | Threat | Mitigation |
|---|---|---|
| SSH client | MitM / wrong host key | Fail closed without known_hosts; real key match; insecure opt-in auditable |
| SSH gateway | Credential misuse | No credential logging; ephemeral fixtures |
| WS endpoints | Oversized frames / origin abuse | Message-size limits; origin/auth checks |
| RFB peer | Alloc bombs | Max w/h + checked arithmetic before decode |
| GUI input | Confused deputy | Active hijack lease required |
| Logs/artifacts | Secret leakage | Redaction at logger + harness writer; synthetic creds in CI |
| DoS | Task/lease retention | Bounded cancel/disconnect |

- SSH host-key verification is secure by default; insecure bypass requires explicit configuration.
- Credentials and private keys never appear in logs, exception messages, trace attributes, or conformance artifacts (hashes preferred for sensitive fields in CI uploads).
- WebSocket and socket endpoints enforce scheme, host, origin/authentication, message-size, and buffer limits.
- RFB dimensions, pixel formats, rectangle counts, and allocation arithmetic are validated before allocation or decoding.
- GUI input requires an authorized active lease and is auditable.
- Cancellation and disconnect paths are bounded.
- Test fixtures use ephemeral ports and isolated temporary credentials.

## CI Matrix

**Phase-in (not all cells day one):**

0. **ubuntu `csharp-quality`** — `make -C packages/provide-uterm-csharp quality-gate` (required).
1. macOS job only for PTY scenarios when Workstream 1 lands.
2. Windows job only for ConPTY smoke when Workstream 1 lands.
3. Protocol partitions on ubuntu after Layer B harness is stable.
4. External MCP (6a) Linux-first; 6b after GUI green.
5. Optional QEMU RFB only on ubuntu; never required.

Path-filter C# jobs on `packages/provide-uterm-csharp/**` + `conformance/live/**`. Per-job timeouts and redacted artifact retention required.

### Layered quality gates

| Gate | Role |
|---|---|
| Unit / coverlet | Pure library floor **≥97%** with documented residual exclusions until live paths graduate |
| Live harness (Layer B) | Separate pass/fail; not folded into coverlet line % |
| Mutation | Future Stryker perimeter on pure codecs/state machines (Go gremlins model); **not** all OS residual arms |
| MCP consumer | Linux job after REST goldens |

Coverage exclusions for live transports must be removed only when a dedicated OS/harness job proves the path. Coverage ratcheting is not proof of live parity.

## Testing Strategy

Test-first slices: add Layer B expectation (fail) → implement → green. Unit tests remain for codecs/state machines.

**Phase completion requires (scoped per phase exit criteria):**

- required Layer B scenarios green for languages in that phase;
- required OS cells green (not necessarily all three OS until final phases);
- unit coverage floor held with honest residual policy;
- mutation only when perimeter tooling exists;
- no silent skips in required jobs (capability-tagged UNSUPPORTED only);
- 6a/6b MCP criteria only when scheduled.

## Delivery Sequence

1. **Harness + contracts + CI scaffold** — Layer B schema, sample scenario, ubuntu csharp-quality, contract appendix (this doc), residual policy documented.
2. **SSH host-key + security fixes** — real known_hosts; proxy not insecure by default; unit tests for deny/match/mismatch/insecure.
3. **GUI REST (memory attach)** — hub `GraphicalSession`, Go-compatible routes, client methods, lease denial tests (enables MCP 6b prep).
4. **PTY/process + socket foundation** — Unix then ConPTY sub-phases; limit table scenarios.
5. **Telnet + WebSocket parity**
6. **SSH gateway parity**
7. **RFB client + GUI against real RFB fixture**
8. **External MCP 6a then 6b; expand OS matrix**

Each phase leaves a working system and adds its scenarios to **required CI for that phase** before the next starts.

## Key Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Parity definition | Black-box observables | Cross-language layout match is meaningless |
| MCP in C# | Out of scope | Adapters already exist in Go/Python |
| Live harness | New Layer B, not vectors.json | Codecs ≠ OS/network |
| GUI v1 attach | Memory fixture + later RFB | Deterministic CI without litevirt |
| Litevirt dual-stream | Not C# v1 | Product boundary; Go retains path |
| PTY oracle | Go | Python is not a real PTY reference |
| SSH host-key | Fail closed + known_hosts | Matches Go security posture |
| CI growth | Ubuntu first | Avoid 15-cell matrix from day zero |
| Coverage vs live | Separate gates | Avoid dishonest residual or broken floor |
| Mutation | Deferred perimeter tool | Do not block live transport on Stryker absence |

## Alternatives considered

| Topic | Rejected | Why |
|---|---|---|
| Extend vectors.json for live I/O | Overload | Wrong abstraction; non-determinism |
| Require full 3-OS×5-job matrix immediately | Cost/flake cliff | No C# CI today |
| Implement C# MCP | Duplication | Non-goal |
| Coverage alone as parity | False confidence | Explicit non-goal |
| Leave SSH path-presence check | False security | Critical defect |

## Risks

| Risk | Mitigation |
|---|---|
| Multi-OS flake | Capability tags; OS-specialized jobs; quarantine policy |
| Three-language drift | Shared scenarios + single oracle per surface |
| ConPTY semantic gaps | Explicit Windows scenarios + tags |
| Coverlet vs live code | Separate harness gate |
| Secret leaks in artifacts | Hash-by-default; redaction CI check |

## Open Questions

1. Should C# ever implement litevirt gRPC attach, or remain raw-RFB + memory only?
2. Preferred Stryker perimeter file list for phase-2 pure logic?
3. When Windows/macOS become **required** merge gates vs nightly?

## Non-Goals

- implementing MCP in C#;
- hosting or managing local VMs;
- embedding QEMU, litevirt, Python, or Go in the C# distribution;
- claiming parity from coverage percentage or unit tests alone;
- accepting permanent silent skips for platform-specific live behavior;
- C# litevirt dual-stream unless a follow-on design is accepted.

## PR Plan

### PR1 — Design + harness scaffold + csharp-quality CI
- Files: this design, `conformance/live/**`, `.github/workflows/ci.yml` csharp-quality job, residual policy notes
- Deps: none
- Exit: CI job runs quality-gate; sample scenario present

### PR2 — SSH host-key verification
- Files: `Transports/SshTransport.cs`, `KnownHosts.cs`, Proxy/CLI flags, tests
- Deps: PR1 optional
- Exit: default deny without known_hosts; match/mismatch/insecure scenarios

### PR3 — GUI REST + hub GraphicalSession (memory attach)
- Files: Hub models, UtermServer GUI routes, HijackClient GUI methods, tests
- Deps: none hard
- Exit: attach/screenshot/click/type/key/drag with lease gating

### PR4 — PTY foundation (Unix)
- Deps: Layer B scenarios for resize/exit
- Exit: native PTY open; residual exclusion plan for ConPTY next

### PR5 — ConPTY + socket limits
### PR6 — Telnet full negotiation + WS foundation
### PR7 — SSH gateway pump
### PR8 — RFB client Raw + deterministic fixture
### PR9 — MCP 6a (terminal) Linux
### PR10 — MCP 6b (GUI) + three-OS expansion

---

*Revised after multi-agent design review (architecture, parity gap, security/CI). Review notes: `docs/superpowers/specs/2026-07-11-csharp-live-parity-design.REVIEW.md`.*
