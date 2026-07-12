# Standalone C# Live Transport and GUI Parity Design

## Goal

Make `provide-uterm-csharp` a fully standalone implementation of the live terminal and remote-GUI platform on Linux, macOS, and Windows. C# must match or improve upon the observable Python and Go behavior for PTY/process sessions, SSH, telnet, WebSocket and raw-socket transport, gateway operation, and remote VNC/RFB control.

MCP is not part of the C# deliverable. The C# backend must implement the common REST, WebSocket, authentication, session, hijack, and GUI contracts well enough that the existing Go and Python MCP adapters can control it without C#-specific behavior.

## Product Boundary

The C# implementation is self-contained and requires only its .NET runtime and explicitly declared native/runtime dependencies. It does not shell out to Python or Go and does not run either language as a sidecar.

C# connects to and controls remote graphical sessions. It does not launch or host QEMU, libvirt, or litevirt VMs. QEMU may be used only as a real RFB test fixture. VM lifecycle and hosting remain the responsibility of litevirt or another remote hypervisor.

## Parity Contract

Parity is defined by externally observable behavior, not by matching source layout or class names. The same black-box scenarios run against Python, Go, and C# and compare:

- bytes and control frames sent and received;
- connection, authentication, negotiation, and session state transitions;
- timeout, cancellation, half-close, reconnect, and error behavior;
- backpressure and bounded-buffer behavior;
- cleanup of processes, sockets, tasks, and leases;
- screenshots, framebuffer state, keyboard/pointer events, and GUI lease behavior;
- REST and WebSocket response shapes consumed by MCP clients.

A C# feature is complete only when all required shared scenarios pass on every supported operating system. A platform-specific exception requires a documented OS limitation and an alternative test proving the closest equivalent observable behavior.

## Shared Black-Box Harness

A repository-level harness owns protocol fixtures and scenario definitions. Each scenario describes fixture setup, client/server actions, expected event sequence, expected byte/frame output, timeout budget, and cleanup assertions. Thin language drivers launch the Python, Go, or C# implementation and translate harness commands without changing semantics.

Scenario results use a canonical JSON format with ordered events and base64-encoded byte payloads. The harness compares exact results when behavior is deterministic and explicit capability-tagged results where an operating system exposes a genuinely different primitive.

The harness provides deterministic in-process or child-process fixtures for every CI platform. Optional real-service fixtures add confidence but never replace deterministic required scenarios.

## Workstreams

### 1. PTY and process lifecycle

C# gains production implementations for spawn, stdin/stdout/stderr routing, terminal resize, environment and working-directory handling, echo and terminal-mode management, cancellation, graceful termination, forced termination, and descendant cleanup.

Linux and macOS use their native PTY facilities. Windows uses ConPTY. Shared scenarios cover interactive echo, binary data, resize, Unicode, child exit, cancellation, process-tree termination, repeated open/close, and descriptor/handle cleanup.

### 2. Socket and WebSocket foundation

All live transports share bounded asynchronous read/write loops with explicit ownership and cancellation. Tests cover partial reads/writes, text and binary WebSocket frames, fragmentation, ping/pong, orderly close, abrupt close, TCP half-close, stalled readers/writers, backpressure, timeout, reconnect, and repeated disposal.

Buffers and queues have configurable hard limits. Overflow produces the same structured error and cleanup behavior across languages.

### 3. Telnet

C# implements the Python/Go telnet negotiation and stream semantics: IAC escaping, WILL/WONT/DO/DONT negotiation, NAWS resize, terminal type, CR/LF normalization, subnegotiation fragmentation, binary-safe payload handling, reconnect, and gateway lifecycle.

The current telnet gateway becomes a complete live implementation rather than a coverage-excluded residual path.

### 4. SSH gateway and client

C# implements a production SSH client transport and SSH gateway with the same authentication and host-key policies as the common contract. Required behavior includes password and key authentication, known-host validation, explicit insecure mode, PTY allocation and resize, environment forwarding, bidirectional streaming, cancellation, disconnect, and bounded cleanup.

The gateway accepts SSH sessions and bridges each session to the configured remote terminal WebSocket using the shared control-channel protocol. The current CLI rejection for `listen --protocol ssh` is removed only after the shared SSH scenarios pass.

### 5. RFB/VNC and GUI control

C# gains a real RFB client with protocol-version negotiation, security negotiation for supported remote endpoints, server-init parsing, pixel-format handling, framebuffer update decoding, bounded framebuffer allocation, and incremental screenshot state.

Required encodings begin with Raw and CopyRect because they provide a deterministic interoperability baseline. Additional encodings may be added after the required suite is green. Unsupported encodings fail explicitly without corrupting stream state.

GUI control exposes screenshot, key down/up, named key, pointer, click, drag, and text-entry operations. It integrates with the same hijack/lease authorization and human-relay rules as Go/Python. Tests verify lease ownership, release, expiry, concurrent human/AI access, and input rejection without a valid lease.

The deterministic RFB fixture runs on Linux, macOS, and Windows. An optional QEMU fixture validates a real RFB server where QEMU is available; QEMU remains test-only.

### 6. External MCP compatibility

No C# MCP server or MCP binary is created. Instead, black-box tests start the standalone C# backend and point the existing Go and Python MCP servers at it. The same tool scenarios verify session inspection, hijack lifecycle, screenshot, click, typing, key, drag, authorization denial, and error shapes.

This makes MCP a consumer-level conformance proof rather than a duplicated C# implementation.

## Security Requirements

- SSH host-key verification is secure by default; insecure bypass requires explicit configuration.
- Credentials and private keys never appear in logs, exception messages, trace attributes, or conformance artifacts.
- WebSocket and socket endpoints enforce scheme, host, origin/authentication, message-size, and buffer limits.
- RFB dimensions, pixel formats, rectangle counts, and allocation arithmetic are validated before allocation or decoding.
- GUI input requires an authorized active lease and is auditable.
- Cancellation and disconnect paths are bounded so hostile peers cannot retain tasks, handles, or leases indefinitely.
- Test fixtures use ephemeral ports and isolated temporary credentials.

## CI Matrix

Required CI runs on current supported Linux, macOS, and Windows runners. Jobs are partitioned by protocol so failures are attributable and runtime remains bounded:

1. PTY/process and raw sockets;
2. telnet and WebSocket;
3. SSH client and gateway;
4. deterministic RFB/GUI;
5. cross-language parity and external MCP compatibility.

Each job uploads the canonical scenario result and diagnostic logs with secrets redacted. QEMU/RFB runs as an additional job on platforms where the dependency is available, but deterministic RFB remains the required cross-platform gate.

## Testing Strategy

Development follows test-first slices. For each scenario, the shared harness expectation is added and observed failing against C# before the minimum implementation is written. Unit tests remain for codecs and state machines, while live black-box tests prove OS and network integration.

Completion requires:

- every required shared scenario passing for Python, Go, and C#;
- all three operating-system matrices green for C#;
- the existing C# coverage floor remaining at or above 97%, then ratcheting upward;
- mutation testing covering the new C# logic with no unexplained surviving mutants;
- no skipped live-path tests in required jobs;
- external Go and Python MCP adapters passing against the C# backend.

## Delivery Sequence

This program is split into independently reviewable implementation plans:

1. shared harness and capability matrix;
2. PTY/process and socket foundation;
3. telnet and WebSocket parity;
4. SSH client and gateway parity;
5. RFB/VNC and GUI parity;
6. external MCP compatibility and final three-OS enforcement.

Each phase leaves a working, testable system and adds its scenarios to required CI before the next phase starts.

## Non-Goals

- implementing MCP in C#;
- hosting or managing local VMs;
- embedding QEMU, litevirt, Python, or Go in the C# distribution;
- claiming parity from coverage percentage or unit tests alone;
- accepting permanent silent skips for platform-specific live behavior.
