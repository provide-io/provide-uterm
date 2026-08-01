# uterm Comprehensive Code Review and Architecture Analysis

Date: 2026-07-31

Tracker: [`docs/roadmap/uterm-code-review-remediation.md`](../roadmap/uterm-code-review-remediation.md)

## Executive summary

This review covered the complete maintained uterm workspace: the Python reference
implementation and supporting packages, the Go and C# ports, the TypeScript
library and browser applications, the Cloudflare edge implementation, and the C
host-integration modules. It examined architecture, public protocol boundaries,
authorization and lease invariants, concurrency, failure behavior, capability
claims, build health, tests, and documentation.

The architecture is fundamentally sound. Its strongest property is a shared set
of explicit wire and behavior contracts around terminal data, control frames,
session ownership, and conformance. The principal risk was not the high-level
design; it was semantic drift between implementations at the failure boundaries:
partial WebSocket messages, concurrent admission, stale ownership, policy
unavailability, fan-out authorization, mutable in-memory records, and failed
downstream delivery.

The review found high-impact defects in those boundaries and converted them into
tracked, executable regressions. The remediation makes rejection the default for
unknown fan-out members, unavailable governance, unauthorized session members,
non-owner control actions, and failed delivery. Permissive behavior remains
configurable only where it is an intentional deployment choice. No known
high-severity finding is waived or unresolved; every tracker item is closed with
current verification evidence in the linked tracker.

## Scope and method

The review treated an implementation claim as supported only when it could be
traced through the public route or transport, the state-owning component, and an
observable result. It combined:

- static review of production and test code in every maintained package;
- public-route integration tests for served backends;
- native per-language tests and build/type/lint gates;
- live client/server capability intersection tests;
- shared semantic fixtures executed by native adapters; and
- independent specification, implementation-quality, and release-candidate
  review passes.

Generated assets, vendored dependencies, caches, and build outputs were not
reviewed as authored source. Terraform, CI YAML, shell scripts, configuration,
and documentation were reviewed where they define deployment, security, build,
or capability behavior.

### Maintained package and surface inventory

| Implementation / package | Role and reviewed boundary | Release evidence required |
|---|---|---|
| `provide-uterm` (Python) | terminal/protocol primitives, coordinator, ANSI, replay, shell, VNC | package tests with 100% branch gate; static and wire conformance |
| `provide-uterm-server` (Python) | reference HTTP/WS server, TermHub, policy, approvals, fan-out, tunnels, UI host | full server coverage; fan-out and lifecycle native adapters |
| `provide-uterm-client` (Python) | HTTP/WS transports, SDK, AI/MCP integration | full client package coverage and live client/server matrix |
| `provide-uterm-platform` (Python + C) | PTY/manager integration, capture interposer, PAM module | platform coverage plus capture and PAM native self-tests |
| `provide-uterm-annotation` (Python) | streaming pattern detection, annotation, redaction | full annotation package coverage |
| `provide-uterm-go` (Go) | native server, hub, protocols, clients, tunnels, fan-out, CLI | full tests, race detector, vet, builds, native adapters |
| `provide-uterm-csharp` (C#) | native server/TermHub, clients, connectors, control plane, tunnels, CLI | serial full suite, Release build, mutation/coverage gates, native adapters |
| `provide-uterm-ts` (TypeScript) | cross-runtime protocol and component library; no advertised server | build, ESM smoke, typecheck, lint, strict coverage, component adapter |
| `provide-uterm-frontend` (TypeScript/HTML/CSS) | terminal, DeckMux, hijack, VNC browser widgets | build, typecheck, lint, browser unit tests |
| `provide-uterm-app` (TSX/HTML/CSS) | React application shell and operational views | build, typecheck, lint, browser unit tests |
| `provide-uterm-cloudflare` (Python Worker) | edge HTTP/WS server and Durable Object state owner | full coverage plus authenticated local edge-runtime adapter |
| Conformance and operations | JSON/protobuf contracts, Python/shell drivers, CI, Docker, Terraform | schema validation, semantic runners, live matrix, docs and deployment checks |

The verification ledger is filled from the final tree rather than inferred from
package presence. Every maintained package named here has fresh prescribed-gate
evidence.

## System architecture

uterm is a terminal control plane organized around four layers:

1. **Terminal and protocol primitives.** ANSI parsing, screen state, rendering,
   control-channel framing, replay, recording, redaction, and PTY abstractions.
2. **Session data plane.** Workers own PTYs or remote transports. Browsers and
   API clients observe or submit input through a hub. Terminal data and framed
   control messages share an ordered stream.
3. **Control plane.** Authentication, authorization, session registry, exclusive
   hijack leases, lifecycle operations, approvals, governance, audit, fan-out,
   tunnel management, and graphical control.
4. **Deployment and clients.** Self-hosted Python, Go, and C# servers; a
   Cloudflare Durable Object edge backend; browser applications; SDK/MCP clients;
   and native host capture/PAM integration.

The central runtime relationship is:

```text
browser / SDK / MCP client
          |
          | authenticated HTTP or WebSocket
          v
server route -> authorization -> TermHub / session state -> worker transport
                                      |                         |
                                      +---- event/audit --------+
                                      +---- recording ----------+
                                      +---- observers ----------+
```

The hub is the security and concurrency boundary. A route-level check alone is
not sufficient: ownership and session authorization must still be current when
the state mutation or delivery occurs. The remediations therefore moved critical
invariants into controllers and stores, made state transitions atomic where
needed, and verified behavior through real public surfaces.

### Protocol boundaries

- Terminal/control streams use DLE/STX framing with escaped DLE data and bounded
  length-prefixed JSON control frames.
- Browser, worker, and tunnel WebSocket messages may be fragmented by the
  transport. Implementations must accumulate through `EndOfMessage`, enforce a
  bound before allocation/dispatch, and perform no pre-final action.
- Tunnel channels multiplex terminal, TCP, control, and inspection traffic.
- Capability manifests are part of protocol negotiation. A client/server matrix
  may exercise only the intersection of truthfully advertised capabilities.
- Shared conformance fixtures are normative evidence only when a native adapter
  executes the served surface and validates observable results.

### State ownership and lifecycle

The session hub owns worker registration, browser membership, connection quotas,
input mode, REST and WebSocket hijack ownership, event history, and delivery.
Lifecycle correctness depends on ordering:

```text
admit -> authenticate/authorize -> reserve quota -> initialize -> publish ready
  |                                                       |
  +---------------- rollback on every failure ------------+

acquire -> pause worker -> publish ownership -> heartbeat/release -> resume
  |              |                 |                         |
  +------ fenced identity and serialized transitions -------+
```

Resume tokens are capabilities, not hints. The required invariant is that they
are bounded, expire, are single use, and cannot restore stale authority after a
competing ownership generation wins. Cross-language public-route scenarios are
the acceptance evidence for that invariant.

## Implementation map and assessment

### Python

Python is the reference architecture and is split by responsibility rather than
kept in one package:

- `provide-uterm` contains terminal, ANSI, control-channel, DeckMux, detection,
  rendering, replay, shell, and VNC primitives.
- `provide-uterm-server` contains FastAPI routes, TermHub services, connectors,
  fan-out, policy/governance integration, tunnels, and the hosted UI.
- `provide-uterm-client` contains the HTTP/WebSocket client, transports, and AI/MCP
  integration.
- `provide-uterm-platform` contains PTY/manager integration and owns the native C
  modules discussed below.
- `provide-uterm-cloudflare` contains the Python-authored Worker/Durable Object
  edge runtime.
- `provide-uterm-annotation` contains boundary-aware streaming detection and
  redaction.

Strengths include clear package boundaries, extensive typed models, a decomposed
hub, broad negative-path tests, and the most complete served feature surface.
Findings included malformed-input handling, parser exception normalization,
thread cleanup, fan-out authorization/policy ordering, mutable store aliases,
non-atomic grants, and a completed-command governance path that did not reject
deny/unavailable decisions. A later approval audit found that already-expired
requests remained claimable until the periodic sweep, timeout callbacks could be
overwritten and strand browser input, and fan-out approval release could report
success without a delivered command. The final store transition checks expiry
atomically, queues immutable revision-bound timeout snapshots for composed
listeners, settles exact browser state, and maps fan-out delivery and partial
failure truthfully. Final core-package verification also exposed a stale resume
test and a real ordering mismatch: the server promised not to consume authority
until all gates passed, but consumed a single-use token before detecting a
competing owner. The closure prepares replacement authority, preserves rejected
tokens, and generation-safely compensates any provisional pause/ownership when a
concurrent consume loses. Each finding has a focused regression and is part of
the relevant shared semantic gate.

Architectural caution: the reference server is intentionally feature-rich. New
security invariants should remain in state-owning services/controllers instead of
being duplicated across routes.

### Go

The Go tree is a broad native port spanning the server, hub, clients, protocols,
terminal/emulator stack, policy, fan-out, recording, tunnels, MCP, VNC, and CLI.
Its package decomposition is strong and makes low-level behavior easy to test
with the race detector.

Review findings centered on fan-out admission and send-time authorization,
governance state/evidence, capture ordering, and non-owner WebSocket step input.
An initial lifecycle fix passed its tests but independent review still found
release/resume and replacement races, an unbounded writer-lock wait, approval-ID
ABA and mutable snapshots, false partial-delivery status, tunnel-step false
success, and a reconnect bookkeeping race. Two fix-forward reviews then closed
failed-acquire compensation, unsupported tunnel controls, revision-bound approval
CAS, atomic input holds, lifecycle-fenced mode transitions, same-socket token
continuity, and immediate reconnect/dead-browser detach ordering. The final
implementation preserves Go's explicit error style while moving these operations
behind generation-aware reservations; deterministic barriers, the module suite,
the full hub/server race suite, vet, and an independent acceptance review pass.
The Go implementation is a served backend, not merely a codec port, and is
consequently held to public-route and race-detector evidence.

Architectural caution: test adapters must not infer success from handler return
alone. They now record actual worker delivery, observer notification, and policy
state.

### C#

The C# implementation is also a broad port. It contains terminal and protocol
primitives, a server and TermHub, control plane, fan-out, clients, connectors,
tunnels, recording/replay, graphical control, CLI, and conformance drivers.

The deepest findings were in asynchronous lifecycle state: fragmented receive,
admission rollback, handshake publication, bounded broadcast, resume-token
storage, stale ownership, pause/resume compensation, concurrent input and lease
transitions, displaced worker frames, and disconnect reconciliation. These were
resolved by explicit fenced state transitions and deterministic concurrency
tests. Later review found three additional issues: a non-owner browser could send
`hijack_step`, lazy fan-out initialization could create split stores under
concurrent first use, and the REST step route returned success after failed worker
delivery without the expected event, metric, or lease expiry. All three now have
real-route regressions.

The server remains a partial class with a large surface. Decomposition is a
maintainability concern, but rewriting the lifecycle state machine merely to make
files smaller would increase risk. Extraction should be limited to cohesive,
behavior-preserving helpers with concurrency tests retained at the public surface.

The final package-defined quality gate also showed that the new lifecycle and
fan-out branches outgrew the prior coverage evidence: all 1,366 initially selected
serial batch tests passed, but merged coverage was 95.34% against the configured
97.4% floor. The root cause included Makefile filters that omitted 172 discovered
tests. Corrected discovery plus targeted lifecycle/fan-out regressions now execute
1,665 serial tests and clear the retained floor at 97.44% before validating both
release binaries.

### TypeScript and TSX

There are three distinct TypeScript roles:

- `provide-uterm-ts` is the cross-runtime protocol/component library and includes
  clients, terminal modules, fan-out, policy, Cloudflare helpers, and conformance
  support.
- `provide-uterm-frontend` is the browser terminal/DeckMux frontend.
- `provide-uterm-app` is the React application shell and operational UI.

The TypeScript library had a build-target mismatch and strict-coverage gaps, and
its fan-out component initially trusted mutable store objects and detached
read/modify/write grants. The library build now emits against the intended ES2023
library, its strict coverage is restored, the emitted package is smoke-tested
under native ESM, unknown fan-out members reject by default, per-send
authorization is enforced, store reads are isolated, and grants are atomic within
the in-memory store.

The browser packages exposed a separate quality-gate defect. The frontend's
original 523 tests passed while its configured thresholds failed because large
DeckMux and browser-runtime paths were unexercised. The React app's original 330
tests had no thresholds and left most operational views unexecuted. Lint also
exited zero while reporting 116 frontend and 8 app warnings. These baselines were
retained in the verification ledger rather than mistaken for release evidence.

The browser fix-forward added direct DeckMux and operational-view behavior tests,
warning-fatal lint, and CI coverage gates. Frontend now clears 90/85/90/90 with
552 tests; the app clears its new 90/80/90/90 floor with 369 tests. The work also
fixed stale DeckMux ownership flags and clean embedded-VNC disconnect state.

Independent re-review accepted the follow-up owner reconciliation: one authority
now updates presence, internal users, edge indicators, and cursor overlays for
owner change, transfer, snapshot, and clear. It also accepted the real embedded
VNC teardown assertion and the narrow coverage/lint configuration. The final
frontend suite has 556 passing tests and the app suite has 369.

The TypeScript package does **not** advertise a standalone uterm server. Its
component-level fan-out and protocol scenarios are executed as components; live
server matrix cells remain explicitly unserved. This distinction prevents a
passing library test from becoming a false server-capability claim.

### Cloudflare edge implementation

The Cloudflare package maps session isolation to Durable Objects, with state,
authentication, API routing, WebSocket bridging, and hibernation/resume behavior
adapted to the edge runtime. It is intentionally not assumed to have every
self-hosted server feature.

The review hardened invalid JSON handling and found additional lifecycle gaps:
authorization and worker delivery could be split by Durable Object re-entry;
browser ownership could not be bootstrapped through the advertised public
surface; stale-owner resume could report success after a competing acquisition;
and quota/governance support status had no observable public refusal. A first
fix-forward passed a real workerd adapter, but independent review then found that
worker replacement and stale frames were not identity-fenced, ownership was not
recoverable after hibernation, CI did not provision the pinned edge runtime,
REST heartbeat conflated display owner with authenticated identity, invalid
prompt regexes were validated after input delivery, and disabled resume was
still advertised. The final fix-forward persists generation/incarnation state,
proves a real cold activation while retaining the original edge-held sockets,
fences rebound and stale worker disconnects by generation, separates heartbeat
identity from display ownership, and validates expectation patterns before any
worker frame. Its conservative grammar rejects sequential variable quantifiers,
omitted-lower counted forms, backreferences, alternation, and lookaround.

This pass also exposed a false-green package gate: the declared 100% Cloudflare
coverage command initially measured only 97.06%, with 105 lines and 51 branches
unbound across lifecycle, route, tunnel, and entry modules. Behavioral tests now
bind those paths. Independent review also proved that time-based lease expiry can
cross an awaited successful pause even under the delivery guard; resumed ownership
therefore requires a post-await live-session revalidation. The prescribed gate
reaches 100% without lowering the threshold. Lifecycle conformance requires a real Cloudflare adapter for every
served claim; a missing runtime, skipped adapter, synthetic unsupported result,
or absent observation cannot count as a pass.

Architectural caution: Durable Object restarts and hibernation make persisted
ownership identity and replay ordering more important than process-local state.
Those behaviors belong in edge-native tests, not Python-server simulations.

### C

The maintained C source is in `provide-uterm-platform/native` and covers capture
interposition and PAM integration. This code sits directly on application I/O and
authentication boundaries, so blocking, allocation, and serialization bugs have
larger blast radius than its line count suggests.

Capture delivery is now nonblocking and bounded; short writes and `EINTR` are
handled; concurrent writers cannot interleave frame boundaries; disconnects do
not corrupt a future connection; and payload lengths are checked before frame
allocation. Deterministic writer tests, sanitizer coverage, symbol checks, and
real injection smoke tests protect the boundary. PAM input handling in the Python
listener rejects non-object JSON without terminating the handler.

### Operational and declarative code

Shell scripts, CI YAML, TOML/JSON configuration, Terraform, HTML/CSS, and generated
schema boundaries were reviewed where they affect runtime claims. The important
findings were CI false-greens: documentation validation did not cover all tracked
plans, a declaration-only fan-out manifest could pass without executing behavior,
and lifecycle claims could be marked served without a native adapter. The gates
now fail when evidence is missing, stale, unsupported, or semantically different
from the contract.

## Findings and dispositions

### High severity

1. **Fan-out admitted or retained unauthorized members.** Resolved by strict
   default admission, configurable dormant-member support, controller-owned
   per-send authorization, and release-time reauthorization.
2. **Configured governance could be bypassed or fail open.** Resolved by concrete
   policy adapters and fail-closed deny/unavailable behavior. Policy hold/release
   is reauthorized before dispatch.
3. **Group ACL and store state could be mutated through aliases or lost updates.**
   Resolved by copied persistence boundaries and atomic grant operations.
4. **C# lifecycle races could publish stale ownership or mis-handle pause/resume.**
   Resolved by fenced ownership generations, serialized transition handoffs,
   bounded sends/closes, and teardown reconciliation.
5. **Non-owner browser step input could reach a worker in Go and C#.** Resolved by
   atomic owner gating and real WebSocket route tests.
6. **Failed C# REST step delivery returned HTTP 200.** Resolved: failed delivery
   returns 409 and produces no success event/metric; successful delivery reports
   the current lease expiry and records both effects.
7. **Ownership could change between authorization and delivery.** Python, Go,
   and Cloudflare browser input, REST input/step, release/expiry/replacement, and
   worker-delivery paths now require one state-owner reservation. Deterministic
   race tests, bounded external sends, and native semantic adapters close the
   authorize-to-deliver gap.
8. **Approval release could dispatch stale authority or report false success.**
   Resolved by retaining the origin browser and ownership generation,
   revision-bound claim/finalize, post-governance reauthorization, serialized
   command/replay, and delivery-aware refusal/partial results. Fan-out pending
   payload cleanup is also revision-qualified, so a delayed old timeout cannot
   delete state for a pruned and reused request ID.
9. **Expired approvals remained claimable and timeout callbacks could strand
   browser input.** Resolved by atomic expiry transitions, immutable queued
   notifications, composed listeners, and exact timeout state settlement.
10. **Cloudflare expectation regexes permitted practical ReDoS before worker
    delivery.** Resolved with a conservative fail-closed grammar, including
    omitted-lower counted quantifiers and lookaround, plus public zero-frame tests.
11. **Python rejected resume could burn legitimate single-use authority.** The
    final prepare/commit flow defers token consumption until reclaim succeeds and
    generation-safely compensates a concurrent losing attempt.
12. **TypeScript held fan-out approvals were vulnerable to reused-ID ABA.**
    Resolved with store-assigned monotonic revisions and exact claim, resolve,
    expiry, and controller release. Public approval-input compatibility is
    preserved, live duplicate IDs reject, stale cleanup cannot consume a newer
    revision, and counter exhaustion fails before insertion.
13. **A wedged native adapter could hang the central fan-out release gate.**
    Resolved with a 120-second per-backend subprocess bound, explicit
    `TimeoutExpired` failure with zero accepted observations, and a 600-second
    outer conformance-test bound.

### Medium severity

1. **Fragmented WebSocket messages could be parsed or acted on early.** Resolved
   with bounded reassembly for browser, worker, and tunnel paths and shared
   pre-final/no-action/exactly-once/oversize scenarios.
2. **Connection quotas and setup rollback were incomplete.** Resolved with atomic
   admission, unaffected incumbent connections, and rollback on failed setup and
   disconnect.
3. **Resume-token and ownership semantics were overstated or unbounded.**
   Resolved across C#, Python, Go, and Cloudflare with bounded storage,
   current-owner/stale-competitor checks, single-use authority, post-await expiry
   revalidation, compensation, and independently reviewed public-route evidence.
4. **C# fan-out lazy initialization raced.** Resolved with publication-safe
   `Lazy<T>` initialization and concurrent public-route creation/listing evidence.
5. **TypeScript build and maturity claims disagreed with the actual artifact.**
   Resolved by the ES2023 emit build, ESM smoke test, strict CI gate, and explicit
   served/unserved documentation.
6. **Native capture could block application I/O or corrupt frames.** Resolved with
   bounded nonblocking delivery and complete serialized writes.
7. **Semantic conformance could pass on declarations rather than behavior.**
   Resolved with native adapters, normalized observations, and CI enforcement.
8. **Browser quality gates did not represent the shipped surfaces.** Resolved
   with direct DeckMux and operational-view behavior tests, explicit frontend and
   application thresholds, warning-fatal lint, and an independently accepted
   ownership/VNC teardown fix-forward.
9. **The Cloudflare package's declared coverage gate was false-green in prior
   evidence.** Resolved by exercising the 105 missed lines and 51 partial branches
   behaviorally and retaining the 100% statement/branch threshold.

### Low severity

Malformed PAM JSON, malformed graphical endpoint input, Cloudflare JSON decoding,
test-thread cleanup, and fixed-time worker credential comparison were corrected
and covered by focused regressions.

## Cross-cutting conclusions

### Security posture

The intended posture is now consistent: reject by default, authorize at the point
of use, fail closed when configured policy cannot decide, and report downstream
failure truthfully. Configuration may opt into dormant fan-out members, but it
does not bypass send-time authorization. Group access never implies access to the
underlying sessions.

### Concurrency posture

The most consequential defects were check/use and publication races. The reliable
patterns across languages are:

- keep mutable records private to their store;
- make compound state transitions atomic at the owner;
- attach monotonic identity to replaceable ownership;
- do not publish readiness before setup completes;
- bound external awaits and isolate failed peers; and
- verify delivery and state effects, not just returned status objects.

### Conformance posture

Conformance has three levels:

1. codec/schema fixtures for deterministic wire compatibility;
2. native semantic adapters for security and state behavior; and
3. live public-route client/server matrices for served capability intersections.

No one level substitutes for the others. A component-only TypeScript result is
not a server result, and a simulated Cloudflare result is not edge-runtime
evidence.

### Maintainability

The repository has substantial intentional duplication because Go, C#, and
TypeScript are real ports rather than foreign-function wrappers. Shared JSON
fixtures, generated schemas, and semantic runners are therefore the correct
deduplication layer. Attempting to unify runtime state machines across languages
would sacrifice native testability and clarity.

The largest ongoing maintenance risk is adding a capability in the reference
implementation without simultaneously declaring whether each other backend
serves, component-serves, explicitly rejects, or does not support it. Capability
and semantic-contract updates should be part of the feature's definition of done.

## Residual risks and recommendations

- In-memory fan-out and session stores are process-local by design. Deployments
  requiring restart durability must select a durable backend and test its atomic
  operations; copying in-memory records does not create durability.
- C# lifecycle code is concurrency-sensitive. Preserve the public-route and
  deterministic scheduler tests during any decomposition.
- Cloudflare hibernation behavior depends on the platform runtime. Keep the
  adapter mandatory in CI for every edge capability claim.
- Full test counts are useful release evidence but do not replace the focused
  semantic gates. Keep both.
- Treat new outbound control operations as ownership-sensitive by default and add
  a non-owner negative scenario before exposing them.

## Release assessment

The reviewed architecture is suitable for release. Every item in the linked
tracker is closed with fresh verification, no known high-severity defect remains,
served and unserved surfaces are distinguished truthfully, and the cross-language
fan-out and lifecycle contracts execute in CI. Final command results and
independent-review decisions are recorded in the tracker's verification ledger
rather than duplicated here, so this analysis remains architectural and the
tracker remains the operational source of truth.
