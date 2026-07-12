# provide-uterm — Deep Code Review and Architectural Analysis

Review date: 2026-07-12  
Review target: `main` at `54fce59a`, including the staged and unstaged working tree present during review

## Executive summary

`provide-uterm` has unusually strong engineering controls for a project of its breadth: protocol schemas are generated from a single source, the Python hub has been decomposed into explicit services, security-sensitive code is partly mutation tested, CI spans four Python versions plus Go, C#, TypeScript, race detection, dependency scanning, and cross-language interoperability, and the core packages enforce very high coverage.

The architecture is nevertheless carrying two different maturity levels. The established terminal/control-channel stack is disciplined and heavily gated; the newer graphical-session and embed runtimes bypass several of those strengths. The current checkout should not ship the graphical path as a production security boundary until the critical and high-severity items below are addressed.

The most important findings are:

1. **Critical — unrestricted insecure gRPC dial/SSRF in GUI attachment.** A request-supplied `target_address` is passed directly to `grpc.NewClient` with plaintext credentials.
2. **Critical — embed-session serialization can be bypassed across tasks.** A session-wide `_pipeline_depth` is treated as if it were task-local lock ownership, allowing an unrelated coroutine to enter the pipeline while another coroutine owns the lock.
3. **High — VNC input authorization is not principal-bound and fails open.** The relay asks only whether *some* lease exists for a session; it cannot prove the sending browser owns that lease, and a nil manager allows all input.
4. **High — the headless RFB client is incomplete and protocol-fragile.** It selects security type `None` without checking the advertised list, omits pixel-format/encoding/update-request messages, assumes four-byte raw pixels, and aborts on ordinary server messages.
5. **High — graphical connection lifecycle and error handling leak resources and report false success.** gRPC connections are not closed, stale sessions remain registered after loop failure, replacement is unmanaged, and injection/PNG errors are ignored.
6. **Medium — CI quality guarantees are materially uneven across packages.** Ruff, Bandit, Xenon, and Vulture gate only the core Python package; strict mypy gates only core and Cloudflare; the new live VNC glue is deliberately removed from Go coverage.

## Scope and method

The review covered repository structure, package metadata, CI and quality scripts, the bridge/hub architecture, authentication and authorization boundaries, control-frame design, the active embed and graphical-session work, tests, cross-language parity, and operational/release concerns. This is a risk-oriented architectural review, not a claim that every one of the roughly 2,700 tracked files received line-by-line inspection.

Evidence was gathered from source inspection, working-tree diffs, root Python tests, and targeted Go race tests. Verification results appear near the end of this report.

## Architectural assessment

### System shape

The repository is a polyglot monorepo with six Python workspace packages, two npm workspaces, and standalone Go and C# ports. The central data path combines raw terminal bytes and DLE/STX-framed JSON control messages over WebSockets. Server-side session coordination is centered on `TermHub`; worker integration is split between `HijackableMixin` and `TermBridge`. The newer graphical path adds RFB/VNC over a litevirt bidirectional gRPC stream, while the embed layer provides an in-process multi-client proxy abstraction.

This is a sensible high-level decomposition:

- Core terminal parsing, wire contracts, server orchestration, clients, and platform-specific process/PTY code are separate packages.
- The server hub uses composition rather than a deep mixin hierarchy.
- Wire frames have a canonical Pydantic source and generated TypeScript consumers.
- Go and C# are treated as ports with conformance gates rather than being hidden behind Python FFI.

The principal architectural liability is **parallel implementation drift**. Python, Go, C#, Cloudflare/Pyodide, and two frontend surfaces each reproduce portions of policy, framing, auth, and lifecycle behavior. Conformance vectors reduce byte-level drift, but they do not automatically prove semantic equivalence for authorization, failure handling, cancellation, or resource ownership.

### Trust boundaries

The effective trust boundaries are:

1. Browser/client to HTTP/WebSocket server.
2. Server to worker/PTY/SSH/telnet/tunnel targets.
3. Server to external identity providers and webhooks.
4. Server to litevirt gRPC/VNC targets.
5. MCP/AI tool invocation to session-control operations.
6. Cloudflare edge/runtime to Durable Object and origin-facing services.

The established terminal bridge generally makes authorization and framing explicit. The graphical path currently collapses boundaries 1, 4, and 5: an authorized API caller can select an arbitrary network target, the resulting connection is plaintext, and input ownership is represented only by a session-level boolean lease query.

## Findings

### CRITICAL-1: Request-controlled plaintext gRPC dial enables SSRF and target impersonation

**Evidence:** `packages/provide-uterm-go/server/bridge_rest.go:265-294`

`handleGUIAttach` accepts `target_address` from the request body and passes it directly to:

```go
grpc.NewClient(target, grpc.WithTransportCredentials(insecure.NewCredentials()))
```

There is no scheme/host/port validation, egress policy, DNS/IP classification, allowlist, TLS, server identity verification, or binding between the worker and the selected litevirt endpoint. Even if `authorizeHubRoute(..., hubMode)` restricts this to a privileged role, privilege to manage a terminal worker should not automatically grant arbitrary network reachability from the server process.

**Impact:** A caller can probe or connect to internal services reachable from the server, including loopback, link-local, private, and potentially cloud metadata/control-plane addresses. A network attacker on the path can impersonate litevirt or observe/inject VNC traffic. A malicious endpoint can also send protocol fields that drive memory allocation and parser behavior.

**Required remediation:**

- Replace raw `target_address` with a server-side target identifier resolved from trusted configuration or worker metadata.
- If dynamic targets are essential, reuse a single centralized egress validator that resolves DNS and rejects loopback, link-local, multicast, unspecified, and disallowed private ranges both before and after resolution; defend against DNS rebinding.
- Require TLS/mTLS and verify the expected litevirt identity.
- Add connect/handshake deadlines and an explicit maximum receive/message size.
- Audit-log target selection without leaking credentials.
- Add hostile tests for IPv4, IPv6, alternate numeric encodings, DNS rebinding, Unix targets, and redirect/resolver edge cases.

### CRITICAL-2: `_pipeline_depth` allows cross-task lock bypass and data-order corruption

**Evidence:** `packages/provide-uterm/src/provide/uterm/embed/session.py:220-227,290-308`

`send_to_upstream` bypasses `_lock` whenever `_pipeline_depth > 0`, on the assumption that the call is re-entrant from the interceptor. `_pipeline_depth` is session-wide, not task-local. While the reader task holds `_lock` and awaits an interceptor, any unrelated coroutine sees a positive depth and calls `_process_client` without acquiring the lock.

This violates the class contract at line 130 (“Ordered ... via single lock”) and permits concurrent interceptor calls, upstream sends, callback execution, deferred-list mutation, and depth mutation. The GIL does not preserve async ordering or prevent logical races.

**Impact:** Terminal input can be reordered, interceptors can be entered concurrently despite expecting serialization, deferred actions can interleave, and a malicious/slow interceptor can widen the race window. This is especially dangerous if interceptors enforce policy or redact/audit data.

**Required remediation:**

- Do not model lock ownership with a shared integer.
- Prefer an explicit serialized command queue/actor loop for all session operations. This naturally supports re-entrant requests by enqueuing follow-up work and avoids awaiting user callbacks while holding a state lock.
- If retaining the lock design, use a task-local `contextvars.ContextVar` re-entry token tied to the owning task, with a bounded recursion/injection depth. Document ordering semantics and test two independent tasks with a barrier inside the interceptor.
- Add tests proving no concurrent interceptor entry and deterministic ordering under `asyncio.gather`.

### HIGH-1: VNC input authorization is not bound to the sending principal and fails open

**Evidence:** `packages/provide-uterm-go/vnc/litevirt_human.go:18-21,112-172`

The authorization interface is only:

```go
HasLease(sessionID string) bool
```

The relay has no caller/principal/client identifier and therefore cannot establish that the WebSocket sending a key, pointer, or clipboard message owns the lease. If any actor holds a lease for the session, all connected relays satisfy the same boolean. In addition, `leaseMgr == nil` permits input.

**Impact:** A viewer can inject input while another operator holds a lease. A wiring/configuration failure disables enforcement instead of denying input. Clipboard injection is affected as well as keyboard and pointer events.

**Required remediation:**

- Authenticate the WebSocket before upgrade and carry an immutable principal/client ID into the relay.
- Change the decision to something like `CanInject(sessionID, leaseID, principalID) error`, validated on every mutating message or against an unforgeable, expiring capability bound to the connection.
- Make a missing authorization dependency fail closed.
- Ensure lease revocation closes or immediately disables existing input streams.
- Add multi-client tests: owner, viewer, expired owner, revoked owner, and nil/unavailable lease store.

### HIGH-2: Headless RFB negotiation is incomplete and rejects normal protocol behavior

**Evidence:** `packages/provide-uterm-go/vnc/litevirt_ai.go:90-211`

Specific defects:

- The server version is read but never validated or negotiated.
- Security type 1 (`None`) is sent without confirming it appears in `secTypes` (lines 112-120).
- Failure reason strings are not read when negotiation fails.
- The implementation explicitly omits `SetPixelFormat`, `SetEncodings`, and framebuffer update requests (lines 160-162).
- It assumes every raw pixel is four bytes and compatible with `image.RGBA`, ignoring the server pixel-format structure in `ServerInit`.
- It rejects every server message except framebuffer update, although bell, cut-text, color-map, and extension messages are legal.
- It ignores `ApplyRawUpdate` errors (line 208).
- Rectangle validation checks only width/height, not `x + width`, `y + height`, integer/allocation bounds, zero dimensions, or a global framebuffer/memory limit.
- No incremental update request is sent, so many servers will never produce the loop's expected update.

**Impact:** The client will fail or hang against compliant servers, may render incorrect colors, and can allocate attacker-controlled buffers. The code can appear attached while the background loop has already exited.

**Required remediation:** Implement an explicit, tested RFB 3.8 state machine with bounded fields, advertised-security selection, pixel-format conversion, encoding negotiation, update requests, legal-message handling, and context-aware cancellation. Use recorded protocol fixtures plus at least one real litevirt/QEMU integration test.

### HIGH-3: Graphical-session lifecycle leaks connections and converts failures into success

**Evidence:** `packages/provide-uterm-go/server/bridge_rest.go:277-302,324-338,358-401`

- `*grpc.ClientConn` is never closed on constructor error, missing worker, loop exit, replacement, worker removal, or server shutdown.
- Assigning `st.GraphicalSession = client` is unsynchronized at this call site and overwrites any previous session without cancellation/close.
- The request returns `200 {ok:true}` before the handshake succeeds.
- A background-loop failure only logs a warning; the dead client remains registered.
- `png.Encode`, pointer injection, and key injection errors are discarded, yet the HTTP handlers return success.
- An unknown click button becomes mask zero, effectively a release event, instead of validation failure.
- Typing stops reporting neither partial progress nor the first failed key.

**Impact:** resource/goroutine leaks, stale screenshots, misleading automation results, racy replacement, and lost input with false-positive API responses.

**Required remediation:** Introduce an owned `GraphicalSessionManager` with `Attach`, readiness/handshake, `Replace`, `Detach`, and `Close`; make it the sole mutator of the registry field. Return success only after readiness or return an asynchronous operation ID with observable state. Propagate encoding/injection failures and define partial-write semantics.

### HIGH-4: Human relay cancellation and WebSocket hardening are incomplete

**Evidence:** `packages/provide-uterm-go/vnc/litevirt_human.go:193-246`

The handler detaches from the request using `context.Background`, waits for only one goroutine result, and does not explicitly close the gRPC send side or connection. The peer goroutine can remain blocked until deferred cancellation happens; depending on stream implementation and scheduling, shutdown is indirect. `websocket.Accept` receives nil options, so origin policy and compression/read-limit choices are not visible at this boundary. Error strings from downstream components are sent to clients and can disclose internals.

**Required remediation:** Derive a lifecycle context from the authenticated session/server shutdown context, cancel on either pump's exit, close the send side, wait for both pumps, impose WebSocket/gRPC size and time limits, configure origin policy explicitly, and map internal errors to stable public codes.

### MEDIUM-1: Embed client/lifecycle APIs expose stale handles and mixed state/event semantics

**Evidence:** `packages/provide-uterm/src/provide/uterm/embed/session.py:111-119,145-155,208-218,250-265,347-369`; `embed/types.py:31-39`

- `ClientHandle.is_attached` is initialized to true and never changed when backpressure disconnects the client or the session closes.
- A disconnected handle can wait forever on an empty queue because no EOF/sentinel is delivered.
- `SessionLifecycle.CLIENT_ATTACHED` is an event represented as a durable lifecycle state, overwriting `CONNECTED`; later logic checks lifecycle values to decide whether an upstream loss is meaningful.
- There is no public detach operation.
- Backpressure disconnection silently drops the registry entry without lifecycle notification or a reason.
- User callbacks run synchronously within session serialization and exceptions can terminate the reader task without setting a failure state.

**Required remediation:** Separate durable session phase from emitted lifecycle events; add a close/sentinel protocol to handles; provide idempotent detach; notify disconnect reason; isolate callback failures; and never await external/plugin code while holding the state mutex.

### MEDIUM-2: Upstream connect/replace transitions are not transactional

**Evidence:** `packages/provide-uterm/src/provide/uterm/embed/session.py:174-200`

`connect_upstream` awaits external I/O while holding the session lock and leaves lifecycle at `CONNECTING` if `connect()` raises. `replace_upstream` tears down the old upstream before proving the new one can connect; failure leaves `_upstream` pointing to the old disconnected object and no reader, with no `FAILED` state or rollback.

**Required remediation:** Define a transition table and failure state, perform slow connection setup outside the state lock, commit the new upstream atomically after success, and either retain the old upstream until commit or explicitly document destructive replacement. Emit structured failure events.

### MEDIUM-3: CI's “quality gate” does not cover all Python production packages equally

**Evidence:** `ci/quality_checks.sh:41-55`; `ci/typecheck.sh:20-59`

Ruff format/lint, Bandit, Xenon, and Vulture run only over `packages/provide-uterm/src` (and selected core tests/scripts). Server, client, platform, annotation, and Cloudflare production sources are not included in those gating commands. Mypy is strict only for core and Cloudflare; errors in server, client, annotation, and platform are converted to warnings and exit zero. `ty` is informational everywhere.

Package-local Ruff configuration does not create a CI gate by itself. This weakens the strongest controls precisely in network-facing server and process-management code.

**Required remediation:** Expand Ruff and Bandit to every first-party Python `src` tree immediately. Establish a ratcheted mypy baseline for soft packages: fail on new errors even if existing debt is temporarily allowlisted. Run complexity/dead-code analysis where signal is useful, with explicit scoped suppressions rather than directory omission.

### MEDIUM-4: New VNC glue is explicitly excluded from Go coverage

**Evidence:** `packages/provide-uterm-go/Makefile:54-64`

The coverage profile removes `litevirt_ai.go` and `litevirt_human.go`. Those files now contain authentication enforcement, network parsing, concurrency, and lifecycle logic—the exact code that benefits most from branch and hostile-input tests. Race testing compiles/runs packages, but without substantive tests it does not validate these branches.

**Required remediation:** Factor protocol parsing and relay policy away from concrete gRPC/WebSocket types behind small interfaces, then unit/fuzz test them and include them in coverage. Keep only genuinely live transport adapters excluded. Add Go fuzz targets for RFB handshake/message parsers.

### MEDIUM-5: Cross-language parity is stronger at the byte layer than the policy layer

Conformance tests and generated schemas are major strengths, but authorization role maps, lifecycle rules, error semantics, retry behavior, and resource limits are still replicated. The Go graphical features are moving ahead of Python/C# parity documents, while C# explicitly de-scopes MCP.

**Required remediation:** Define a versioned behavioral contract containing operation, minimum role, state preconditions, idempotency, error code, size limit, and audit event. Generate language-specific tables/tests from it where practical. Track intentionally unsupported capabilities explicitly in `/capabilities` or protocol negotiation rather than relying on documentation.

### LOW-1: Development artifacts add operational noise and secret-retention risk

The workspace contains large numbers of local recording, coverage, cache, binary, and build-output artifacts. They are mostly untracked/ignored, but terminal recordings can contain credentials, tokens, commands, and personal data. Their presence also makes broad filesystem inspection and backup behavior riskier.

**Required remediation:** Use an OS cache/data directory outside the repository by default, enforce restrictive permissions, document retention/deletion, offer redaction/encryption, and add a pre-commit/CI guard against recording files becoming tracked.

### LOW-2: Documentation and configuration comments show drift

The root guidance states a fully enabled mutation perimeter, while comments in `pyproject.toml` still describe routes/webhooks/manager targets as deferred. Contradictory operational documentation reduces confidence in the gate and makes future edits error-prone.

**Required remediation:** Make the executable target list authoritative and generate the human-readable perimeter status from it; add the docs accuracy checker to validate these claims.

## Strengths

1. **Single-source frame schemas.** Canonical Pydantic models plus generated TypeScript types are an excellent defense against wire drift.
2. **Composed hub services.** Decomposing `TermHub` into registry, lease, routing, connection, presence, state, polling, approval, and rate-limit services improves ownership and testability.
3. **Strong baseline CI.** Multi-version Python, Go race/lint/vulnerability checks, C# build/coverage, npm typecheck/tests, CodeQL, container scanning, hostile-client workflows, and interoperability tests are far above the norm.
4. **Security-aware defaults in established paths.** Placeholder-secret rejection, trusted-proxy constraints, fail-closed webhook behavior, token hashing, header denylisting, and explicit role gates show mature threat awareness.
5. **High test ambition.** Branch coverage and mutation gates demonstrate concern for test quality rather than test count alone.
6. **Protocol framing discipline.** A dedicated check for bare JSON WebSocket sends protects a subtle mixed-stream invariant.
7. **Dependency and release hygiene.** Frozen locks, pinned GitHub actions, license checks, artifact verification, vulnerability scanning, and dedicated release governance reduce supply-chain risk.
8. **Ports are first-class.** Go and C# have independent quality gates and conformance work rather than being treated as unverified translations.

## Architectural weaknesses and debt

- **Too many semantic copies.** The number of runtimes makes policy drift the dominant long-term risk.
- **Quality labels overstate uniformity.** “100% coverage” and “strict mypy” are true only for selected scopes; comments sometimes obscure exclusions.
- **Lifecycle ownership is diffuse.** Registry fields hold live resources without a consistent owner responsible for close/replacement/readiness.
- **Extension callbacks execute in critical sections.** Plugin/interceptor flexibility currently compromises ordering, latency isolation, and failure containment.
- **New protocol work lacks a state-machine abstraction.** Handwritten sequential reads are difficult to fuzz, limit, and reason about.
- **Security capabilities are sometimes booleans.** A boolean “lease exists” loses subject, object, action, and expiry information needed for authorization.
- **Operational policy is under-specified.** Limits, retention, shutdown behavior, and error/public-observability contracts need the same rigor as wire formats.

## Recommended remediation plan

### Release blockers

1. Disable or feature-gate GUI attachment outside explicitly trusted development environments.
2. Replace request-controlled gRPC targets with trusted target resolution and TLS/mTLS.
3. Bind every VNC input stream to an authenticated principal and exact lease; fail closed.
4. Replace the embed re-entry mechanism with task-safe serialization and add a concurrency regression test.
5. Implement RFB negotiation/update requests correctly and bound every allocation.
6. Add owned graphical-session lifecycle management and propagate all I/O errors.

### Next hardening increment

1. Refactor RFB parsing into a pure state machine with unit, fixture, and fuzz tests.
2. Include graphical code in coverage and mutation/fuzz perimeters.
3. Expand Ruff/Bandit to all Python source packages and ratchet mypy soft-package debt.
4. Separate lifecycle state from events in the embed API; close client handles deterministically.
5. Create a generated behavioral authorization/capability matrix shared across ports.

### Longer-term architecture

1. Establish a single session-runtime contract covering terminal and graphical transports: identity, lease/capability, lifecycle, backpressure, audit, readiness, close, and error semantics.
2. Keep transport adapters thin; move policy and parsing into deterministic, dependency-free cores.
3. Make resource ownership explicit in types (`Close`/`aclose`, cancellation source, manager ownership) and require idempotent cleanup.
4. Add observability for session state transitions, dropped input, backpressure disconnects, handshake failures, and target-dial decisions.
5. Publish a support/parity matrix from executable capability metadata.

## Verification performed

- `git diff --check` and `git diff --cached --check` were run to detect whitespace errors in both unstaged and staged changes.
- `uv run pytest -q` completed successfully: **5,091 passed, 86 skipped, 26 deselected**, with one Starlette/httpx deprecation warning, in 382.67 seconds.
- `go test ./vnc ./server ./embed -race` completed successfully for all three targeted Go packages.
- Source and CI claims were checked against executable files rather than relying solely on README/CLAUDE documentation.

Passing tests would not invalidate the findings above: several concern absent negative tests, excluded coverage, architectural authorization context, or protocol behavior not represented by the present test doubles.

## Overall judgment

The established terminal platform is architecturally credible and supported by strong engineering practice. The monorepo's biggest strategic risk is semantic drift across ports; its immediate tactical risk is that experimental graphical/embed code has entered privileged runtime paths without inheriting the mature stack's security, lifecycle, and test boundaries. Address the release blockers before treating GUI attachment or multi-client embed interception as production-ready.
