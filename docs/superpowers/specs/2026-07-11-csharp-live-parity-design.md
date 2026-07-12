# Standalone C# Live Transport and GUI Parity Design

## Goal

Make `provide-uterm-csharp` a fully standalone implementation of the live terminal and remote-GUI platform on Linux, macOS, and Windows. C# must **observably match** Python and Go on black-box scenarios (not source layout). Capability-tagged supersets are allowed only when explicitly named; unbounded “improve upon” is out of scope for this program.

MCP is not part of the C# deliverable. The C# backend must implement the common REST, WebSocket, authentication, session, hijack, and GUI contracts well enough that the existing Go and Python MCP adapters can control it without C#-specific behavior.

## Product Boundary

The C# implementation is self-contained and requires only its .NET runtime and explicitly declared native/runtime dependencies. It does not shell out to Python or Go and does not run either language as a sidecar.

C# connects to and controls remote graphical sessions. It does not launch or host QEMU, libvirt, or litevirt VMs. QEMU may be used only as a real RFB test fixture. VM lifecycle and hosting remain the responsibility of litevirt or another remote hypervisor.

**In scope for this program:** PTY/process, sockets/WebSocket foundation, telnet client+gateway, SSH client+gateway, RFB client (raw TCP fixture first), GUI REST + lease gating, **ushell (in-terminal REPL/connector + commands)**, shared live harness, layered quality gates, external MCP consumer proofs.

**Out of scope:** C# MCP server/binary; embedding QEMU/litevirt/Python/Go; full tunnel multiplex / DeckMux UI / recording product parity unless a follow-on design is accepted; claiming parity from coverage % or unit tests alone; **embedding a Python eval/exec sandbox in C#** (same deliberate non-port as Go — see Workstream 7).

## Current State (baseline)

| Workstream | C# status | Notes |
|---|---|---|
| PTY / process | partial | Process pipes default; native Unix openpty+posix_spawn opt-in (`PreferNativePty` / `UTERM_NATIVE_PTY=1`); no ConPTY |
| Socket / WebSocket | partial | Fragment reassembly + max size; further foundation work remains |
| Telnet client/gateway | partial | Minimal IAC; gateway drive exists; full negotiator missing |
| SSH client | partial | SSH.NET + real known_hosts matching (secure by default) |
| SSH gateway | partial | FxSsh listen → `GatewayDrive` wired; residual coverage |
| RFB client | partial | Security None + Raw client; tracker + attach `mode=rfb` |
| GUI control | partial | Memory + RFB attach; lease-gated screenshot/input REST |
| REST session/hijack | partial production | Health, sessions, hijack, WS present |
| **Ushell** | **stub only** | `Shell/ShellBasics.cs` = LineBuffer + ANSI output helpers only. **No** `UshellConnector`, dispatcher, or command suite. Go has full port under `packages/provide-uterm-go/shell` (~4k LOC incl. tests). |
| Offline codec conformance (Layer A) | production | `vectors.json` / `ConformanceVectorsTests` |
| Live harness (Layer B) | scaffolded | `conformance/live/` schema + sample scenario |
| Coverage residuals | active | Live PTY/transports/SSH gateway/RFB residual exclusions |
| Mutation (C#) | missing | Stryker perimeter later |
| Monorepo CI (C#) | ubuntu `csharp-quality` | Local `make quality-gate` exists |

**Oracle language per surface:** Python owns codec vectors and the **Python** `py` sandbox; **Go owns the portable ushell command model** (dispatcher, help/kv/fetch/storage/render/cast, connector lifecycle) and is the C# oracle for ushell; Go owns real PTY, SSH known_hosts, SSH/telnet gateways, GUI REST; Python server does **not** implement `/gui/*` today (`gui_rest` capability: `go|csharp`).

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

### 7. Ushell (in-terminal REPL + connector)

Port the **Go** ushell package (`packages/provide-uterm-go/shell`) to C# so standalone C# can host the same in-session shell connector and command surface as Go/Python (observable parity).

**Already in C# (partial):** `Shell/ShellBasics.cs` — `LineBuffer` keystroke protocol + ANSI output/format helpers, plus a **toy** `CommandDispatcher` (`help`/`clear`/`env` only — not product parity).

**Language columns (inventory date: 2026-07-12):**

| Lang | Package / location | Status |
|------|-------------------|--------|
| **Py** | `packages/provide-uterm/.../shell/` (+ CF `do/ushell`) | Full reference + Python `py` sandbox |
| **Go** | `packages/provide-uterm-go/shell/` | Full portable port (~4k LOC w/ tests); `py` stub |
| **C#** | `packages/provide-uterm-csharp/.../Shell/ShellBasics.cs` | Linebuffer + basic output + stub dispatcher |
| **Rust** | — | **Not present** in monorepo |
| **TS** | — | **Not present** (frontend is xterm UI only; no ushell package) |
| **Bun** | — | **Not present** (would share a TS package if one is added) |

Legend for matrix: **Y** = implemented and product-usable · **S** = stub / partial · **—** = absent · **N/A** = not applicable by design

#### 7.1 Stack / packaging

| Feature | Py | Go | C# | Rust | TS | Bun |
|---------|:--:|:--:|:--:|:----:|:--:|:---:|
| Dedicated ushell package/module | Y | Y | S | — | — | — |
| Unit / connector tests | Y | Y | S | — | — | — |
| CF DO / server session wiring (`ushell` connector type) | Y | Y | — | — | — | — |
| Standalone REPL entry (`__main__` / CLI) | Y | via server | — | — | — | — |

#### 7.2 Line editor + ANSI I/O

| Feature | Py | Go | C# | Rust | TS | Bun |
|---------|:--:|:--:|:--:|:----:|:--:|:---:|
| LineBuffer (CR/LF submit, BS/DEL, ESC/CSI swallow, max len) | Y | Y | Y | — | — | — |
| Ctrl-C clear / Ctrl-D EOF semantics | Y | Y | S | — | — | — |
| ANSI prompt / banner / Error/Info/Success/Heading | Y | Y | Y | — | — | — |
| FmtKV / FmtTable (Go/Python-aligned) | Y | Y | S | — | — | — |
| Exact help text / per-command `help <cmd>` | Y | Y | — | — | — | — |

#### 7.3 Commands (dispatcher)

| Command | Py | Go | C# | Rust | TS | Bun |
|---------|:--:|:--:|:--:|:----:|:--:|:---:|
| `help` / `help <cmd>` | Y | Y | S | — | — | — |
| `clear` | Y | Y | S | — | — | — |
| `exit` / `quit` / EOF | Y | Y | — | — | — | — |
| `py <expr>` | Y (sandbox) | S (unavailable stub) | — | — | — | — |
| `sessions` / `sessions kill` | Y | Y | — | — | — | — |
| `kv` list/get/set/delete | Y | Y | — | — | — | — |
| `fetch` HTTP(S) | Y | Y | — | — | — | — |
| `storage` list/get | Y | Y | — | — | — | — |
| `env` (ushell context keys) | Y | Y | S (OS env dump ≠ contract) | — | — | — |
| `render` image→ANSI (+ animation) | Y | Y | — | — | — | — |
| `cast` asciicast v2 replay | Y | Y | — | — | — | — |
| Unknown-command error frame | Y | Y | S | — | — | — |
| Injectable context (KV/storage/list sessions) | Y | Y | — | — | — | — |

#### 7.4 `py` / sandbox

| Feature | Py | Go | C# | Rust | TS | Bun |
|---------|:--:|:--:|:--:|:----:|:--:|:---:|
| Restricted eval/exec sandbox | Y | — | — | — | — | — |
| Session-persistent namespace | Y | — | — | — | — | — |
| Portable stub (`usage` + unavailable message) | N/A | Y | — (required) | — | — | — |
| Capability tag `ushell.py` | `python` | `stub` | `stub` (target) | — | — | — |

#### 7.5 UshellConnector (SessionConnector)

| Feature | Py | Go | C# | Rust | TS | Bun |
|---------|:--:|:--:|:--:|:----:|:--:|:---:|
| Start / Stop / IsConnected | Y | Y | — | — | — | — |
| HandleInput (echo + dispatch on submit) | Y | Y | — | — | — | — |
| PollMessages (pending term frames) | Y | Y | — | — | — | — |
| Welcome / banner / worker hello frames | Y | Y | — | — | — | — |
| HandleControl (flow pause/resume) | Y | Y | — | — | — | — |
| Flow-pause backpressure | Y | Y | — | — | — | — |
| AnimatedResult streaming (render/cast) | Y | Y | — | — | — | — |
| GetSnapshot | Y | Y | — | — | — | — |
| GetAnalysis | Y | Y | — | — | — | — |
| Clear / SetMode | Y | Y | — | — | — | — |
| Concurrent-safe connector | asyncio | mutex | — | — | — | — |

#### 7.6 C# implementation checklist (Go oracle)

Checkable delivery items for the C# program (flip when landed + tested):

- [ ] Types / `Result` + animated result (`types.go`)
- [ ] Context / binding interfaces (`context.go`)
- [ ] Dispatcher routing + exact error/usage strings (`dispatcher.go`)
- [ ] Full `help` / `help <cmd>` text (`help.go`)
- [ ] `clear`, `exit`/`quit`, `env` (context semantics)
- [ ] `sessions` / `sessions kill`
- [ ] `kv` list/get/set/delete
- [ ] `fetch` (http/https only, size/time limits)
- [ ] `storage` list/get
- [ ] `render` (+ animation frames)
- [ ] `cast` (+ fps/loop)
- [ ] `py` stub matching Go strings
- [ ] HTTP helpers + frame builders (`http.go`, `frame.go`)
- [ ] **UshellConnector** lifecycle (input, poll, control, snapshot, analysis, flow, animation)
- [ ] Wire `connector_type=ushell` on C# server/session path
- [ ] Unit tests ported from Go shell tests
- [ ] Layer B scenario(s): help, kv, fetch fixture, connector echo/dispatch

#### 7.7 Future languages (Rust / TS / Bun)

Not in this C# program’s critical path. If added later:

| Target | Suggested approach |
|--------|-------------------|
| **Rust** | New crate mirroring Go `shell/` API; `py` stub; connector trait |
| **TypeScript** | Shared package (e.g. `packages/provide-uterm-ushell-ts`); Node 20+ |
| **Bun** | Consume same TS package; Bun-specific only if fetch/fs differ — prefer one implementation |

Do not invent language-specific command sets; black-box scenarios stay shared.

**`py` command policy (match Go, not Python implementation identity):**

- Python: restricted eval/exec sandbox (`commands/py.py` + `_sandbox.py`).
- Go: **stub** — empty arg → `usage: py <expr>`; otherwise `py: unavailable in the Go build (Python sandbox not ported)` (`cmd_py.go`).
- C# / Rust / TS / Bun: **same stub policy** (wording may say “this build” instead of “Go build” if strings are generalized in a future cross-lang polish — until then C# copies Go strings).
- Do **not** embed CPython / Deno Python / etc. Capability: `ushell.py: python` vs `ushell.py: stub`.

**Oracle:** Go for portable commands and connector I/O; Python for codec/ANSI Layer A and for `py` *when* testing the sandbox. Parity scenarios: help text, kv round-trip, fetch against httptest fixture, cast replay event sequence, connector echo/dispatch, flow-pause backpressure, snapshot shape.

**Completion (C#):** checklist §7.6 green; connector black-box in Layer B; residual policy only for true network arms of fetch/cast if needed.

## Security Requirements

### Threat model (summary)

| Boundary | Threat | Mitigation |
|---|---|---|
| SSH client | MitM / wrong host key | Fail closed without known_hosts; real key match; insecure opt-in auditable |
| SSH gateway | Credential misuse | No credential logging; ephemeral fixtures |
| WS endpoints | Oversized frames / origin abuse | Message-size limits; origin/auth checks |
| RFB peer | Alloc bombs | Max w/h + checked arithmetic before decode |
| GUI input | Confused deputy | Active hijack lease required |
| Ushell fetch/cast | SSRF / unbounded download | http(s) only; size/time limits aligned with Go |
| Ushell storage | Path traversal | Bound root; reject `..` escapes |
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

1. **Harness + contracts + CI scaffold** — Layer B schema, sample scenario, ubuntu csharp-quality, contract appendix (this doc), residual policy documented. *(landed)*
2. **SSH host-key + security fixes** — real known_hosts; proxy not insecure by default. *(landed)*
3. **GUI REST (memory attach)** — hub `GraphicalSession`, Go-compatible routes. *(landed; RFB attach also partial)*
4. **PTY/process + socket foundation** — Unix native opt-in landed; ConPTY still open; limit table scenarios.
5. **Telnet + WebSocket parity** — WS fragment/max partial; full telnet IAC open.
6. **SSH gateway parity** — FxSsh listen → drive landed; residual/hardening open.
7. **RFB client + GUI against real RFB fixture** — client + attach partial; fixture suite open.
8. **Ushell connector + commands** — port Go shell package to C# (Workstream 7); `py` stub like Go.
9. **External MCP 6a then 6b; expand OS matrix**

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
| Ushell oracle | Go portable model | C# must match Go commands/connector; not Python layout |
| Ushell `py` | Stub like Go | No embedded Python sandbox in C# |
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
| Embed Python in C# for `py` | Sidecar / runtime bloat | Match Go stub; capability-tag vs Python |

## Risks

| Risk | Mitigation |
|---|---|
| Multi-OS flake | Capability tags; OS-specialized jobs; quarantine policy |
| Three-language drift | Shared scenarios + single oracle per surface |
| ConPTY semantic gaps | Explicit Windows scenarios + tags |
| Ushell command string drift | Port Go help/error strings; golden tests |
| Coverlet vs live code | Separate harness gate |
| Secret leaks in artifacts | Hash-by-default; redaction CI check |

## Open Questions

1. Should C# ever implement litevirt gRPC attach, or remain raw-RFB + memory only?
2. Preferred Stryker perimeter file list for phase-2 pure logic?
3. When Windows/macOS become **required** merge gates vs nightly?
4. Should ushell land before or after ConPTY (independent — can parallelize)?

## Non-Goals

- implementing MCP in C#;
- hosting or managing local VMs;
- embedding QEMU, litevirt, Python, or Go in the C# distribution;
- claiming parity from coverage percentage or unit tests alone;
- accepting permanent silent skips for platform-specific live behavior;
- C# litevirt dual-stream unless a follow-on design is accepted;
- embedding a Python eval/exec sandbox in C# (ushell `py` is a Go-compatible stub).

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

### PR4 — PTY foundation (Unix) *(partially landed — opt-in native)*
### PR5 — ConPTY + socket limits
### PR6 — Telnet full negotiation + WS foundation
### PR7 — SSH gateway pump *(partially landed — FxSsh)*
### PR8 — RFB client Raw + deterministic fixture *(partially landed)*
### PR9 — Ushell linebuffer/output already partial; **dispatcher + commands**
- Files: expand `Shell/` from Go `shell/` (types, dispatcher, help, kv, fetch, storage, render, cast, py stub, http, frame)
- Deps: existing render/ANSI surfaces
- Exit: Go-aligned unit tests for each command; exact help/error strings

### PR10 — UshellConnector lifecycle
- Files: connector input/snapshot/analysis/flow/modes/welcome frames
- Deps: PR9
- Exit: connector black-box scenarios; wire as session connector type where server already lists shell sessions

### PR11 — MCP 6a (terminal) Linux
### PR12 — MCP 6b (GUI) + three-OS expansion

---

*Revised after multi-agent design review (architecture, parity gap, security/CI). Review notes: `docs/superpowers/specs/2026-07-11-csharp-live-parity-design.REVIEW.md`.*

*Updated 2026-07-12: Workstream 7 ushell — multi-language feature matrix (Py/Go/C#/Rust/TS/Bun); checkable C# delivery list; Rust/TS/Bun absent in monorepo.*
