# Code Review Remediation Design

## Purpose

Resolve every actionable finding from the 2026-07-31 repository-wide uterm
architecture and implementation review while preserving protocol compatibility,
making security behavior explicit, and leaving a durable progress record.

The work is split into five independently verifiable tracks. A single umbrella
tracker records status and evidence, while each track receives its own detailed
implementation plan so changes remain reviewable and bisectable.

## Tracking model

`docs/roadmap/uterm-code-review-remediation.md` is the source of truth for
progress. Each item has a stable identifier, severity, affected implementations,
acceptance criteria, dependencies, and a verification ledger. An item is complete
only after its focused regression tests and relevant package quality gate pass.

The implementation tracks are:

1. Cross-language fan-out authorization and governance.
2. C# WebSocket and connection lifecycle correctness.
3. TypeScript build, coverage, runtime maturity, and CI.
4. Native capture backpressure and frame integrity.
5. Python malformed-input robustness and test hygiene.

## Fan-out security contract

All served fan-out implementations must enforce the same contract:

- Group creation rejects unknown worker IDs by default.
- A consistently named configuration option can explicitly permit dormant,
  currently unknown worker IDs for deployments that need pre-provisioning.
- Permissive creation never grants future command authority. Every send, including
  a send released from approval, resolves every current session and checks the
  caller's current session authorization before delivering input.
- A member that is unknown or unauthorized at send time receives a per-session
  failure and no input.
- Authorization revocation takes effect without recreating the group.
- Group grants do not override session authorization.
- When governance policy is configured, fan-out uses that policy. Failure to
  construct or invoke the policy adapter is fail-closed rather than allow-all.
- Go and C# either implement deny/hold/approval behavior or expose the unsupported
  capability honestly and refuse governed fan-out; they must not silently bypass
  configured governance.

Wire-format fields remain unchanged. The new configuration defaults to the safer
strict behavior and is documented as a migration switch.

## C# server design

Introduce a shared bounded WebSocket message receiver that accumulates fragments
until `EndOfMessage`, preserves message type, rejects mixed-type fragments, and
closes or refuses messages exceeding the protocol cap. Browser, worker, and tunnel
server loops use this helper instead of treating each receive result as complete.

Connection admission becomes explicit:

- A rejected worker registration closes the accepted socket and never marks the
  registry online.
- Browser admission enforces `MaxConnectionsPerPrincipal` with rollback on failed
  setup.
- Browsers remain pending until `hello`, hijack state, and presence synchronization
  have been sent; only then may normal broadcasts reach them.
- Broadcasts run independently with a bounded timeout and remove failed sockets so
  one slow viewer cannot block others.

Resume tokens use bounded, expiring storage. A token identifies the resumable
browser state needed to rebind ownership, is single-use, and is swept on mint and
consume. If full ownership restoration cannot be guaranteed, the server advertises
resume as unsupported rather than sending a misleading successful response.

### C# lease-transition follow-up

Pause and resume I/O is serialized per worker through the existing reservation
state. A release that must resume first clears the departing logical owner and
installs a unique resume reservation against the captured worker transport. The
reservation remains until the resume send completes or fails, so neither REST nor
dashboard acquisition can publish a successor that a stale resume would undo.
Dashboard disconnect resumes, explicit dashboard and REST release, forced release,
and expiry settlement share this ordering contract.

Lease expiry is active rather than field-only. Successful acquire, heartbeat, and
lease extension arm an expiry check. When the current expiration is observed, the
manager clears only the expired owner and uses the same bounded resume transition.
An obsolete timer is harmless because it rechecks the current owner and expiration.
If a newer pause reservation already exists, the expired owner's landed-pause
obligation transfers to that reservation instead of racing it with a resume.

Worker replacement is an identity fence. Registration publishes a unique
replacement reservation, clears and invalidates inherited lease ownership, resumes
the captured predecessor on a bounded best-effort path, aborts an abortable
predecessor, and only then clears the fence. The worker receive loop verifies the
captured transport identity before accepting every decoded frame, so a displaced
socket cannot mutate snapshots, hello state, events, or browser output. The
replacement starts with truthful unowned, unpaused state.

Browser WebSocket admission requires a configured `SessionDefinition` outside the
explicit test-mode escape hatch. Missing definitions receive HTTP 404 before the
upgrade, matching REST's unknown-session default. Tests that enable
`UTERM_TEST_MODE` own a scoped previous value and restore it in `finally`; no helper
may leave process-wide authentication state behind.

C# fan-out must either implement the accepted parallel/sequential, output
collection, timing, stop-on-error, and divergence semantics or explicitly return
an unsupported response. Because its API already advertises these fields, the
preferred design is to complete the semantics using the existing event bus and Go
implementation as a behavioral reference.

## TypeScript readiness design

Restore emit builds by retaining `ES2023` in the build configuration, then add the
emit command to CI. Close the strict coverage gaps with meaningful boundary tests,
not exclusions. The README and roadmap must describe the runtime as partial until
all declared server surfaces are integrated and exercised. Capability declarations
remain authoritative: unserved routes are not advertised.

The existing narrow live matrix remains useful but is not evidence of full runtime
parity. TypeScript joins multi-backend browser testing only after it serves the
required WebSocket and lifecycle contracts.

## Native capture design

Application `read` and `write` hooks must not perform an unbounded blocking send to
the capture consumer. Frames are serialized through a bounded delivery mechanism.
When the capture path cannot keep up, behavior is explicit and availability-first:
drop capture data and account for the drop rather than blocking the observed
process.

The writer handles short writes and interruption without corrupting frame
boundaries. Concurrent producers cannot interleave bytes. Allocation and maximum
payload limits are checked before converting `size_t` to the 32-bit wire length.
macOS and Linux retain the same frame format.

## Python robustness design

- PAM notification parsing treats every non-object or malformed JSON value as an
  invalid event and keeps the listener alive.
- Graphical endpoint parsing converts malformed bracketed IPv6 and related parser
  exceptions into `GraphicalTargetError` responses.
- Cloudflare request-body decoding returns its documented invalid-body result for
  JSON decode failures.
- The VNC relay test helper closes only closeable values and never leaves an
  unhandled background-thread exception.
- The server and TypeScript strict coverage gates return to green through focused
  regression tests for the currently uncovered normalization branches.

## Testing and delegation

Every behavior change follows red-green-refactor: add a focused regression test,
run it and observe the expected failure, make the minimum production change, then
run focused and package-level verification. Independent file sets may be delegated
to parallel agents; overlapping edits are serialized. Each completed track receives
spec-compliance and code-quality review before its tracker entries are checked.

Final verification includes Python package suites, Go test/vet/format checks, the
repository-prescribed batched C# suite, TypeScript build/typecheck/lint/coverage,
both browser workspaces, native builds/self-tests, static conformance, and the full
live client/server matrix.

## Compatibility and rollout

No control-channel or tunnel wire format changes are planned. Security tightening
is intentional: strict unknown-member rejection is the default, authorization is
rechecked at execution time, and governance failures are fail-closed. Operators
requiring pre-provisioned fan-out groups must explicitly enable dormant members and
still receive send-time authorization enforcement.
