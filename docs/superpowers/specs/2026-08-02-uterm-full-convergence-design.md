# uterm Full Convergence Design

## Status and scope

This design closes every actionable finding from the 2026-08-02 deep review of
the uterm monorepo. It supplements the completed 2026-07-31 remediation wave; it
does not reopen evidence that remains valid from that wave.

The target is one coherent product implemented by Python, Go, C#, TypeScript,
TSX, native C, and shell tooling. Completion means that portable behavior is
either implemented and executable in the relevant runtime or explicitly
classified as platform-specific. An unfinished adapter is not an acceptable
reason for a capability to be absent.

The work is intentionally staged:

1. Make behavioral contracts executable across languages.
2. Repair transaction, lifetime, parser, I/O, and native-boundary safety.
3. Complete the TypeScript Node server against the same contracts.
4. Make quality gates, CI, documentation, and capability reporting truthful.
5. Run the complete static, native, race, coverage, and live interoperability
   matrix and reconcile every result.

The approved breaking change is that C# control-plane stores become explicitly
transaction-scoped. uterm is pre-1.0, and correctness is preferred to retaining
an ambient or non-transactional API.

## Design package

This umbrella document owns sequencing and completion. Detailed contracts live
in three companion designs:

- `2026-08-02-uterm-semantic-safety-convergence-design.md` defines shared
  semantic evidence and the C#, Go, and native safety changes.
- `2026-08-02-uterm-typescript-server-parity-design.md` defines complete Node
  server parity, integration boundaries, and runtime failure behavior.
- `2026-08-02-uterm-quality-evidence-design.md` defines type, warning, CI,
  documentation, capability-manifest, and final-proof requirements.

Each companion design receives a separate TDD implementation plan and bounded
commits. The full-goal tracker records finding IDs, dependencies, red/green
evidence, and the final verification ledger.

## Architectural decisions

### Python is the behavioral oracle, not a privileged implementation

The mature Python server supplies the initial route, authorization, lifecycle,
error-shape, and redaction contract. Shared fixtures then become the durable
authority. If a fixture exposes a defect in Python, the contract is corrected
and all served implementations converge; parity never means reproducing a known
bug.

### Contracts must execute

Static symbol checks remain useful for packaging drift, but they cannot prove
semantics. Shared scenario fixtures invoke native adapters and compare observable
results such as status, error code, state transition, emitted event, delivered
bytes, expiry behavior, and redaction. Capability declarations are generated
from the same operation inventory used by the runners.

### State ownership is explicit

Every mutable subsystem has one visible owner:

- a C# `ITx` owns control-plane reads and writes until commit or rollback;
- a WebSocket receiver owns its bounded message accumulation;
- a streaming detector owns only the unmatched suffix needed for the next feed;
- a native socket-address formatter owns a length-bounded byte view;
- the TypeScript Node composition root owns all long-lived registries, stores,
  timers, hubs, and shutdown.

Ambient transactions, process-global mutable request state, unbounded receives,
and path rechecks after opening are excluded by design.

### Platform differences are data

Portable and platform-specific operations are classified in a generated
capability manifest. Platform-only facilities may be unsupported where they do
not exist, but the manifest must state why and the server must fail explicitly.
The Node server is not required to install native PAM modules; it is required to
serve the portable PAM-event ingestion surface when configured.

## Delivery sequence

### Phase 1: executable contract foundation

Extend the existing conformance architecture without replacing working fan-out
and lifecycle runners. Add focused scenario sets for control-plane transaction
isolation and reaping, annotation streaming, WebSocket limits, secure append,
native address formatting, and server route/capability parity. Adapters must
produce normalized observations and fail closed on timeout, crash, malformed
output, or missing evidence.

### Phase 2: safety convergence

Use those tests to implement the explicit C# transaction API and complete its
reaper, align the C# detector with canonical annotation behavior, bound Go
WebSocket messages, make secure append descriptor-based, and make native capture
address handling length-aware. Each behavior change follows red-green-refactor.

### Phase 3: TypeScript Node server completion

Build a production Node composition root around existing TypeScript domain
modules. Bind the canonical REST operations, non-registry operational endpoints,
browser and worker WebSockets, resume, hijack, fan-out, approvals, API keys,
profiles, webhooks, graphical targets, tunnels, health/readiness/security,
metrics, and built frontend assets. Validate handler completeness at startup and
exercise the server in the live matrix.

### Phase 4: evidence and maintainability

Make `ty` diagnostic-clean and enforce its result, clean C# and shell warnings,
make the Go live driver freshness-safe, stabilize application tests on Node
22/24/26, update architecture and package documentation, and generate truthful
capability artifacts.

### Phase 5: full proof

Run focused tests first, then package gates, then cross-language semantic and live
matrices. Final proof includes Python strict coverage, TypeScript 100% coverage,
browser coverage floors, Go vet/race, C# build/test/coverage without warnings,
native tests and sanitizers where supported, shellcheck, dependency and license
audits, workflow validation, and a clean worktree.

## Error and compatibility policy

Wire formats remain compatible unless a failing executable contract proves that
an existing representation is unsafe. The C# store interface is the sole planned
source-breaking API change. Migration errors should be compile-time errors: every
store operation must receive the transaction it belongs to.

Security-sensitive failures are closed and normalized. Limit excess, stale
transaction, symlink substitution, malformed stream data, unavailable policy,
missing handler, and adapter timeout cannot degrade to success. Error bodies must
not reveal credentials, tokens, raw approval payloads, or filesystem internals.

## Completion criteria

The goal is complete only when all of the following are true:

- every review finding has a regression test and tracker evidence;
- all portable TypeScript server operations are served and advertised;
- the live matrix has no unexplained TypeScript `unsupported` cells;
- static conformance and semantic runners agree with generated capabilities;
- every prescribed build, lint, type, test, coverage, race, native, audit, and
  live gate passes from a clean checkout-compatible state;
- architecture and package documentation describe the implementation that
  actually ships; and
- the worktree contains no uncommitted implementation or generated-file drift.
