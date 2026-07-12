# Architecture and Security Remediation Design

**Date:** 2026-07-12
**Status:** Approved design
**Source review:** `ARCHITECTURE_CODE_REVIEW.md`

## Objective

Resolve every actionable issue identified by the 2026-07-12 architectural review without turning the work into a wholesale session-platform rewrite. The program hardens graphical attachment and VNC authorization, makes graphical resource ownership explicit, corrects RFB behavior, serializes embed sessions safely, closes CI coverage gaps, improves cross-port behavioral parity, and addresses operational recording and documentation drift.

## Chosen approach

Use a unified hardening program built on existing abstractions. Urgent security fixes and lifecycle ownership come first. Terminal, embed, and graphical sessions will share behavioral contracts where useful, but they will not be forced behind one new universal runtime API during this program.

Rejected alternatives:

- A minimal patch would leave the same duplicated ownership, protocol, and policy structures in place.
- An immediate full session-runtime unification would make security remediation dependent on a risky platform rewrite.

## Graphical target registry

### Sources and precedence

The server exposes a single logical graphical-target registry with two sources:

1. Static bootstrap entries from TOML/environment-backed configuration.
2. Runtime-managed entries persisted through the existing control-plane store abstraction, including its memory and SQLite implementations.

Static entries are immutable through runtime APIs and take precedence over runtime entries with the same ID. Runtime creation or update must reject an ID that shadows a static entry.

### Target definition

Each target has:

- stable logical ID;
- gRPC endpoint;
- TLS mode and references to CA, client certificate, and client key material;
- expected server name or certificate identity;
- allowed VM-name patterns;
- tenant and minimum-role policy;
- connect, handshake, read, write, and shutdown timeouts;
- maximum gRPC message size, framebuffer dimensions, rectangle count, clipboard size, and pixel allocation;
- allowed resolved CIDRs where dynamic resolution is applicable;
- non-secret audit labels.

Secret values are referenced from the existing environment/file secret mechanism. Ordinary runtime registry rows must not contain private keys or bearer secrets.

### Dynamic development targets

Production requests contain `target_id`; raw network addresses are rejected. An explicit development-only `allow_dynamic_targets` setting may accept raw endpoints when the server is in a development mode. Dynamic endpoints still pass centralized egress validation and are limited to configured CIDRs.

Egress validation resolves all addresses and rejects disallowed loopback, link-local, unspecified, multicast, and private addresses. The validated address set is bound to dialing so DNS rebinding cannot replace it after authorization. IPv4, IPv6, alternate numeric forms, Unix targets, and resolver failures are covered by hostile tests.

## Graphical session ownership

A `GraphicalSessionManager` is the sole owner of graphical connections and registry publication. It provides attach, readiness, replacement, lookup, detach, and close operations.

Attach flow:

1. Authenticate the principal and authorize use of the worker and target.
2. Resolve the logical target and validate the requested VM name.
3. Open a deadline-bound TLS/mTLS gRPC connection.
4. Complete RFB negotiation and receive/request the first usable framebuffer.
5. Atomically publish the ready session.
6. Emit an audit/state event and return success.

Replacement is transactional: the currently published healthy session remains active until its replacement is ready. After atomic publication, the previous connection is cancelled and closed. A failed replacement leaves the previous session untouched.

The manager removes and closes a session on explicit detach, worker removal, RFB loop failure, server shutdown, or replacement. Closing is idempotent. No handler writes the registry's graphical-session field directly.

If asynchronous attachment is introduced later, it must return an explicit operation resource with observable state. The current design does not return success before readiness.

## Authorization and human relay

VNC input authorization is principal-bound and fails closed. The authorization decision contains:

- principal ID;
- worker/session ID;
- exact hijack lease ID;
- action (`key`, `pointer`, or `clipboard`);
- expiry;
- revocation generation.

Human WebSocket relays authenticate before upgrade and bind an immutable capability to the connection. Every mutating RFB message validates that capability. Missing policy dependencies, ownership mismatch, expiry, or revocation deny input. Lease revocation immediately disables or closes established input streams.

Viewer operations such as screenshots use the existing read authorization boundary. Mutating GUI REST operations use the same principal/lease capability contract as WebSocket input.

The relay uses a shared lifecycle context. Either pump's exit cancels the other, closes the gRPC send side and WebSocket, waits for both pumps, and releases connection resources. Origin policy, compression choice, size limits, and deadlines are explicit. Internal errors are logged with correlation data but mapped to stable public error codes.

## RFB client and parser

RFB protocol logic is separated from concrete gRPC and WebSocket adapters as a bounded state machine.

The client:

- validates and negotiates supported protocol versions;
- reads server failure reasons within a strict size limit;
- selects only an advertised and locally allowed security type;
- never silently assumes `None` security;
- parses and honors the server pixel format;
- sends `SetPixelFormat`, `SetEncodings`, and framebuffer update requests;
- supports raw encoding first, with explicit rejection of unconfigured encodings;
- handles legal bell, cut-text, color-map, and supported extension messages without corrupting stream state;
- validates framebuffer bounds, rectangle counts, coordinates, dimensions, arithmetic overflow, and allocation size before reading pixel data;
- converts negotiated pixels into the internal RGBA framebuffer;
- propagates tracker and transport errors;
- uses context-aware cancellation and deadlines.

The parser is driven by recorded fixtures, fuzz tests, and a real local litevirt/QEMU-compatible integration fixture. Protocol parsing and policy code are included in Go coverage; only irreducible live adapter branches may remain excluded with documented justification.

## GUI HTTP behavior

- Attach accepts a target ID in production and returns success only after readiness.
- Unknown buttons and keys return validation errors.
- Screenshot encoding errors are returned.
- Pointer and key injection errors are returned.
- Typing reports the first failed character index and does not claim complete success after a partial write.
- Public errors use stable codes and do not expose endpoints, certificate details, resolver output, or downstream error strings.

## Embed runtime

`EmbedSession` becomes a serialized actor/command queue. All state mutations, interceptor invocations, upstream writes, client delivery, and deferred operations enter through this queue. Interceptors may enqueue follow-up operations; they cannot recursively or concurrently mutate session state. Injection recursion has a configured hard bound.

Durable session phase is separate from lifecycle events. Attaching a client emits an event without replacing `CONNECTED` as the session phase.

Client handles support deterministic, idempotent detach. Backpressure disconnection and session shutdown mark handles detached, deliver an EOF/sentinel plus reason, and wake blocked receivers. Callback failures become structured diagnostics and do not silently terminate the reader loop.

Upstream connection and replacement are transactional. Slow connection work occurs outside the state-commit critical section. A successful connection is committed atomically; failure leaves a defined failure state and does not retain a disconnected upstream as current. Replacement either preserves the old connection until commit or completes a documented destructive transition; this program uses preserve-until-commit.

`EmbedHub.remove_session` closes the session or requires an already-closed session; silent removal of live owned resources is forbidden.

## Behavioral parity

A versioned behavioral contract supplements byte-level conformance. Each operation specifies:

- capability name;
- minimum role;
- state preconditions;
- identity/lease requirements;
- idempotency;
- public error codes;
- input and resource limits;
- emitted audit/state event.

Language-specific role/capability tables and tests are generated from or checked against this contract. Deliberately unsupported features appear in executable capability metadata rather than only in prose documentation.

## CI and quality gates

- Ruff format/lint and Bandit gate every first-party Python production `src` tree.
- Complexity and dead-code checks cover applicable production packages with scoped suppressions.
- Existing mypy debt in server, client, annotation, and platform is captured in a checked-in baseline or explicit scoped suppressions; CI fails on new errors and the baseline ratchets downward.
- Graphical protocol and authorization code enters Go unit, race, coverage, and fuzz gates.
- Mutation-perimeter documentation is generated or validated from executable configuration so prose cannot contradict the target list.
- Behavioral contract drift is a CI failure.

## Operational safeguards

Terminal/graphical recordings default to an OS data directory outside the repository. Directories and files use restrictive permissions. Configuration defines retention duration and maximum storage, and cleanup is observable. Documentation explains that recordings can contain credentials and personal data and describes deletion, redaction, and encryption-at-rest options. CI prevents recording artifacts from becoming tracked.

Graphical operations emit structured observability for target resolution decisions, attach transitions, handshake failures, input denial, lease revocation, malformed RFB messages, backpressure disconnection, and cleanup. Sensitive endpoint and certificate material is redacted.

## Error model

Registry validation errors are stable 4xx responses. Network, TLS, and handshake failures are mapped to bounded public errors. Malformed or oversized RFB traffic terminates only its graphical session and emits a diagnostic. Session cleanup remains idempotent after partial initialization.

No missing security dependency permits access. No ignored transport, parser, framebuffer, encoding, or input error is followed by an `ok` response.

## Testing strategy

Every behavior change follows red-green-refactor TDD with the failing test observed before production changes.

Required suites include:

- target validation and static/runtime precedence using memory and SQLite stores;
- egress/DNS rebinding hostile cases for IPv4 and IPv6;
- TLS and mTLS local gRPC integration;
- transactional attach/replacement and cleanup;
- multi-principal owner/viewer/expired/revoked/missing-policy authorization;
- relay cancellation and pump failure;
- RFB state transitions, pixel formats, legal messages, size/overflow bounds, fixtures, and fuzzing;
- GUI HTTP error propagation and partial typing;
- embed concurrent callers, re-entrant interceptor enqueueing, ordering, recursion limits, lifecycle state/events, EOF, backpressure, transactional replacement, and callback failure;
- behavioral-contract checks for each maintained port;
- recording path, permissions, retention, and tracked-artifact guards.

## Rollout

1. Land contracts, registry storage, and tests without routing production attachment through them.
2. Land secure resolution and `GraphicalSessionManager` behind a feature flag.
3. Replace attach/input handlers and reject raw production targets.
4. Land the bounded RFB state machine and integration/fuzz coverage.
5. Land the embed actor runtime and lifecycle corrections.
6. Expand CI gates with ratcheted baselines where necessary.
7. Land parity metadata, recording safeguards, generated perimeter documentation, migration notes, and removal of legacy paths.

GUI attachment remains disabled or explicitly development-only until steps 1 through 4 pass their security and integration suites.

## Success criteria

- Every finding in `ARCHITECTURE_CODE_REVIEW.md` maps to an implemented change, an executable guard, or an explicit verified non-issue.
- Production GUI attachment cannot dial a caller-selected address.
- Every mutating GUI input is bound to the sending principal and exact live lease.
- Graphical sessions have one idempotent lifecycle owner and no known connection/goroutine leaks.
- RFB parsing is bounded, negotiated, fixture-tested, and fuzzed.
- Embed interceptors cannot be entered concurrently by unrelated tasks, and blocked clients terminate deterministically.
- CI gates all first-party Python production sources and includes graphical policy/parser code in Go quality metrics.
- Static and runtime targets coexist through the existing control-plane abstraction with immutable static precedence.
- Full affected Python, Go, C#, frontend, conformance, security, and quality suites pass.

