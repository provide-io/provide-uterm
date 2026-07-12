# Architecture and Security Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve every actionable finding in `ARCHITECTURE_CODE_REVIEW.md` by securing graphical targets and input, making graphical/embed lifecycle ownership deterministic, bounding RFB, expanding quality gates, and adding operational/parity safeguards.

**Architecture:** The work is split into independently testable increments. Python and Go control-plane stores gain graphical target records; server-side merged registries combine immutable static entries with runtime rows. Go owns the currently active graphical runtime through a manager and a bounded RFB core. Python embed sessions become an actor. Machine-readable behavioral contracts and centralized quality scopes prevent future drift.

**Tech Stack:** Python 3.11+, asyncio, Pydantic v2, aiosqlite, pytest; Go, gRPC, coder/websocket, x509/TLS, Go fuzzing; C#/.NET contract checks; Bash/Python CI tooling.

---

## File map

New focused units:

- `packages/provide-uterm/src/provide/uterm/control/plane/graphical_target/`: Python durable record/store contract.
- `packages/provide-uterm-server/src/provide/uterm/server/config_schema_graphical.py`: Python static target schema.
- `packages/provide-uterm-server/src/provide/uterm/server/secrets.py`: environment/file secret references.
- `packages/provide-uterm-server/src/provide/uterm/server/graphical/targets.py`: Python static/runtime registry.
- `packages/provide-uterm-go/controlplane/graphical_target.go`: Go durable target contract.
- `packages/provide-uterm-go/serverconfig/graphical.go`: Go static graphical configuration.
- `packages/provide-uterm-go/server/graphical_targets.go`: Go merged target registry and API.
- `packages/provide-uterm-go/server/graphical_dial.go`: validated, DNS-bound TLS/mTLS dialing.
- `packages/provide-uterm-go/gui/manager.go`: graphical session ownership.
- `packages/provide-uterm-go/vnc/protocol.go`, `pixel.go`, `limits.go`: bounded RFB state machine.
- `packages/provide-uterm-go/vnc/capability.go`, `relay.go`: principal-bound input and joined relay pumps.
- `spec/behavioral-contract.yaml`: cross-port policy contract.
- `scripts/quality_scope.py`, `scripts/check_mypy_baseline.py`: centralized Python quality scope and ratchet.
- `scripts/generate_mutation_perimeter.py`: generated mutation documentation.
- `scripts/check_recording_artifacts.py`: recording tracking guard.

Existing lifecycle/config/route files are modified only to delegate into these units.

## Phase A — durable target registry and configuration

### Task 1: Python graphical target store contract and memory backend

**Files:**
- Create: `packages/provide-uterm/src/provide/uterm/control/plane/graphical_target/{__init__.py,types.py,store.py}`
- Create: `packages/provide-uterm/src/provide/uterm/control/plane/memory/graphical_target_store.py`
- Modify: `packages/provide-uterm/src/provide/uterm/control/plane/{bootstrap.py,memory/engine.py,memory/transaction.py}`
- Test: `packages/provide-uterm/tests/control/plane/test_memory_graphical_target_store.py`

- [ ] Write tests defining a frozen `GraphicalTargetRecord` and async `put/get/list/delete` behavior, deterministic listing, replacement, transaction rollback, and conflict detection.
- [ ] Run `uv run pytest packages/provide-uterm/tests/control/plane/test_memory_graphical_target_store.py -q` and verify failure because the contract/store is absent.
- [ ] Implement the record with ID, endpoint, TLS/secret references, VM patterns, tenant/minimum role, timeout/limit/CIDR fields, audit labels, and timestamps. Add the table to memory snapshot/copy/conflict/merge logic and expose `graphical_target_store(tx)`.
- [ ] Run the targeted test and the existing memory transaction suite; require green.
- [ ] Commit only Task 1 files with `feat(control-plane): add graphical target memory store`.

### Task 2: Python SQLite target persistence

**Files:**
- Create: `packages/provide-uterm/src/provide/uterm/control/plane/sqlite/{graphical_target_store.py,schema/v0003_graphical_targets.py}`
- Modify: `packages/provide-uterm/src/provide/uterm/control/plane/sqlite/{engine.py,migration.py}`
- Test: `packages/provide-uterm/tests/control/plane/test_sqlite_graphical_target_store.py`
- Test: existing SQLite migration/cross-compatibility suites under `packages/provide-uterm/tests/control/plane/`

- [ ] Write migration and CRUD/reopen tests expecting schema version 3, deterministic JSON round trips, deletion, and no resolved secret material.
- [ ] Run the targeted tests and observe the missing migration/store failure.
- [ ] Add `cp_graphical_targets`, register migration 3, implement the transaction-bound store, and expose it on `SqliteControlPlane`.
- [ ] Run all Python control-plane tests and verify green.
- [ ] Commit with `feat(control-plane): persist graphical targets in sqlite`.

### Task 3: Go target contract and memory/SQLite parity

**Files:**
- Create: `packages/provide-uterm-go/controlplane/graphical_target.go`
- Create: `packages/provide-uterm-go/controlplane/{memory/graphical_target_store.go,sqlite/graphical_target_store.go}`
- Modify: `packages/provide-uterm-go/controlplane/{engine.go,records.go,memory/engine.go,memory/state.go,memory/transaction.go,sqlite/engine.go,sqlite/migrations.go}`
- Test: `packages/provide-uterm-go/controlplane/{memory/graphical_target_store_test.go,sqlite/graphical_target_store_test.go}`

- [ ] Write backend contract tests for put/get/list/delete, static JSON fields, isolation, conflict, reopen, and migration version parity.
- [ ] Run `go test ./controlplane/...` and verify the new tests fail to compile due to the absent interface.
- [ ] Implement the Go record/store, both backends, migration, transaction copying/conflict/merge, and engine factory.
- [ ] Run `go test -race ./controlplane/...` and require green.
- [ ] Commit with `feat(controlplane): add graphical target stores`.

### Task 4: Static schema, secret references, and merged registries

**Files:**
- Create: `packages/provide-uterm-server/src/provide/uterm/server/{config_schema_graphical.py,secrets.py,graphical/__init__.py,graphical/targets.py}`
- Modify: Python `config_schema.py`, `models.py`, `config.py`, app control-plane/factory files, example TOML
- Create/modify equivalent Go files: `serverconfig/graphical.go`, `server/graphical_targets.go`, server construction
- Test: Python `test_graphical_target_config.py`, `test_secret_references.py`, `test_graphical_target_registry.py`
- Test: Go `serverconfig/graphical_test.go`, `server/graphical_targets_test.go`

- [ ] Write table tests for target IDs, endpoints, TLS combinations, VM patterns, roles, tenants, positive limits, CIDRs, duplicate static IDs, and production rejection of dynamic targets.
- [ ] Write secret tests for `env:` and `file:` references: missing/unreadable/directory/oversized/symlink/unsafe relative paths and redacted errors. Persist references only.
- [ ] Write merged-registry tests for static precedence, runtime shadow rejection, immutable static mutation, merged listing, memory/SQLite parity, and tenant filtering.
- [ ] Run the focused Python and Go tests and observe missing-schema/registry failures.
- [ ] Implement the schemas, a bounded secret resolver, and merged registries; publish Python registry in app state and Go registry in server dependencies.
- [ ] Run focused suites plus configuration/factory tests; require green.
- [ ] Commit with `feat(graphical): add configurable target registry`.

### Task 5: Tenant identity, graphical capabilities, and runtime target APIs

**Files:**
- Modify Python identity/auth/authorization modules and tests.
- Modify Go `serverauth` principal/authorization modules and tests.
- Create Python/Go graphical-target CRUD routes and route tests.

- [ ] Write tests for `tenant_id` population in JWT, header, webhook, tunnel/share, worker, and anonymous principals. Define absent tenant as global/unscoped only where configuration explicitly allows it.
- [ ] Write role/capability tests for `graphical.target.read`, `graphical.target.manage`, and `graphical.session.attach`; global admin cross-tenant access is denied unless an explicit cross-tenant capability exists.
- [ ] Write CRUD API tests for authorization, tenant isolation, static immutability, conflicts, malformed payloads, and response/audit redaction.
- [ ] Verify failures, then implement canonical tenant fields/configuration, capabilities, and runtime APIs against the merged registries.
- [ ] Run all affected auth, hypothesis, webhook, route, and backend-parity tests.
- [ ] Commit with `feat(auth): bind graphical targets to tenants and roles`.

## Phase B — secure graphical runtime

### Task 6: Endpoint validation and TLS/mTLS dialing

**Files:**
- Create: `packages/provide-uterm-go/server/graphical_dial.go`
- Modify: `packages/provide-uterm-go/server/server_egress.go` only to extract reusable classification where necessary
- Test: `packages/provide-uterm-go/server/graphical_dial_test.go`

- [ ] Write hostile tests for IPv4/IPv6 loopback, link-local, private, multicast, unspecified, alternate numeric hosts, Unix targets, multiple DNS answers, resolver errors, and rebinding. Assert the dialer connects only to a previously validated resolved address.
- [ ] Write local TLS/mTLS gRPC tests for valid identity, wrong server name, missing CA, missing client pair, connect/handshake deadline, and receive/send size limits.
- [ ] Run the focused tests and verify failure because the graphical dialer does not exist.
- [ ] Implement endpoint parsing, classification reuse, DNS-result binding via a custom context dialer, and TLS credential construction from resolved secret references. Dynamic mode additionally enforces configured CIDRs.
- [ ] Run focused tests, `go test -race ./server`, and `go vet ./server`.
- [ ] Commit with `feat(graphical): secure target dialing`.

### Task 7: Bounded RFB protocol state machine

**Files:**
- Create: `packages/provide-uterm-go/vnc/{protocol.go,pixel.go,limits.go,protocol_test.go,pixel_test.go}`
- Modify: `vnc/rfb.go`, `tracker.go`, `tracker_test.go`
- Add fuzz corpus/tests under `vnc/testdata/fuzz/` and `vnc/*_fuzz_test.go`

- [ ] Write fixture tests for RFB version negotiation, bounded failure reasons, advertised security selection, ServerInit parsing, pixel-format conversion, SetPixelFormat/SetEncodings, full/incremental requests, raw rectangles, bell/cut-text/color-map handling, and unsupported encodings.
- [ ] Write boundary tests for framebuffer dimensions, names, rectangle counts/coordinates, checked multiplication, allocation caps, clipboard size, zero dimensions, tracker bounds, and error propagation.
- [ ] Run the focused tests and verify missing parser/state-machine failure.
- [ ] Implement a pure context-aware state machine with explicit `Limits`; it must never choose an unadvertised security type or allocate before checked bounds.
- [ ] Add fuzz targets for handshake, server messages, and pixel conversion; seed them with fixtures and run a bounded fuzz smoke.
- [ ] Run `go test -race ./vnc` and require green.
- [ ] Commit with `feat(vnc): implement bounded RFB state machine`.

### Task 8: Graphical session manager and litevirt adapter

**Files:**
- Modify: `packages/provide-uterm-go/gui/session.go`
- Create: `packages/provide-uterm-go/gui/manager.go`, `manager_test.go`
- Rewrite: `packages/provide-uterm-go/vnc/litevirt_ai.go`
- Modify: `packages/provide-uterm-go/server/{server.go,bridge_rest.go}` and worker removal/shutdown hooks

- [ ] Write connector-fake tests proving readiness before publication, failed attach leaves no session, transactional replacement preserves the old session, success closes the old session, loop failure evicts, close is idempotent, concurrent attach/detach is safe, worker removal and shutdown close resources.
- [ ] Run `go test ./gui ./server` and verify missing-manager failures.
- [ ] Implement `GraphicalSessionManager` as sole owner; extend session behavior with readiness and close; adapt litevirt to the RFB state machine and make the manager own `grpc.ClientConn`.
- [ ] Remove direct `WorkerTermState.GraphicalSession` mutation and route lifecycle hooks through the manager without holding hub locks during dialing/handshake.
- [ ] Run `go test -race ./gui ./vnc ./hub ./server` and require green.
- [ ] Commit with `feat(graphical): own session lifecycle in manager`.

### Task 9: Principal-bound input capabilities and relay cleanup

**Files:**
- Create: `packages/provide-uterm-go/vnc/{capability.go,relay.go}`
- Rewrite: `vnc/litevirt_human.go`
- Modify: Go hub lease acquisition/release/expiry models and tests
- Modify: Go authenticated WebSocket route registration and origin configuration

- [ ] Write multi-principal tests for owner, viewer, wrong lease, expired lease, revoked generation, missing policy, and action mismatch. Missing policy must deny.
- [ ] Write relay tests proving either pump cancels the other, `CloseSend` runs, both goroutines join, size/deadline/origin policy is explicit, revocation closes established input immediately, and internal errors map to stable public codes.
- [ ] Verify failures, then add immutable `InputCapability` and an atomic lease generation/notification mechanism. Move `AcquiredBy` into atomic lease creation.
- [ ] Refactor the relay into a pure policy/parser core and thin WebSocket/gRPC adapters; authenticate before upgrade and bind principal/lease capability to the connection.
- [ ] Run hub/VNC/server race suites and hostile-client tests.
- [ ] Commit with `fix(vnc): bind input to principal lease capability`.

### Task 10: Replace GUI REST behavior and propagate failures

**Files:**
- Modify: `packages/provide-uterm-go/server/bridge_rest.go`
- Test: new `packages/provide-uterm-go/server/bridge_gui_test.go`
- Modify client/MCP tests if response schemas change.

- [ ] Write tests showing attach accepts `target_id`, raw targets fail in production, dynamic targets require explicit development configuration, attach waits for readiness, and stable errors redact endpoint/TLS/resolver details.
- [ ] Write tests for exact-principal/exact-lease REST mutation, unknown button/key 422, pointer/key/drag error propagation, PNG failure, and typing's first failed rune index/partial status.
- [ ] Verify the old handlers fail these tests.
- [ ] Rewrite handlers to delegate registry, manager, and capability services; inject PNG encoding for deterministic failure testing; never return `ok` after ignored failure.
- [ ] Run server, client GUI, and MCP GUI suites plus Go race tests.
- [ ] Commit with `fix(server): harden graphical REST operations`.

## Phase C — embed runtime

### Task 11: Actor serialization and bounded re-entry

**Files:**
- Rewrite: `packages/provide-uterm/src/provide/uterm/embed/session.py`
- Modify: `embed/types.py`
- Create: `packages/provide-uterm/tests/test_embed_actor.py`
- Modify: existing `test_embed_session.py`

- [ ] Write a barrier-based concurrency test asserting `max_active_interceptors == 1` and FIFO ordering across unrelated tasks.
- [ ] Write tests proving interceptor follow-up is enqueued, never recursively entered, and injection depth beyond the configured limit emits a diagnostic while the actor stays alive.
- [ ] Run focused tests and observe the current shared-depth race or ordering failure.
- [ ] Implement a single actor command queue and actor task; all state mutation/interceptor/forward/defer work flows through commands. Remove `_pipeline_depth` lock bypass.
- [ ] Run all embed tests and require green.
- [ ] Commit with `fix(embed): serialize session work through actor`.

### Task 12: Embed lifecycle, clients, callbacks, and transactional upstreams

**Files:**
- Modify: Python `embed/{session.py,types.py,hub.py}`
- Test: `test_embed_actor.py`, `test_embed_session.py`

- [ ] Write tests separating durable `SessionPhase` from events; client attach must not replace `CONNECTED`.
- [ ] Write tests for idempotent `ClientHandle.detach`, backpressure/session-close EOF and reason, waking blocked receives, and callback exceptions producing diagnostics without killing subsequent reads.
- [ ] Write tests for failed initial connect, slow connect outside state commit, failed replacement preserving the old upstream, successful atomic swap, and `EmbedHub.remove_session` closing owned resources.
- [ ] Verify failures, implement lifecycle/event separation, queue sentinel/result types, detach, callback isolation, transactional connect/replace, and async hub removal.
- [ ] Run core package coverage tests and mypy for the embed package.
- [ ] Commit with `fix(embed): make lifecycle and cleanup deterministic`.

## Phase D — parity, CI, and operations

### Task 13: Versioned behavioral contract and generated port metadata

**Files:**
- Create: `spec/behavioral-contract.yaml`, schema/check/generator scripts and Python tests
- Generate: Python, Go, C#, and TypeScript/JSON capability artifacts where consumed
- Modify: Python/Go/C# authorization policy tables to consume or check generated data
- Modify: conformance adapters and CI/Makefiles

- [ ] Write schema tests requiring unique operations and known roles/errors/events plus capability, states, identity/lease, idempotency, limits, audit event, and explicit port support.
- [ ] Write drift tests that deliberately alter a generated artifact and require `--check` failure.
- [ ] Write per-port tests that role tables/capability metadata equal the contract and unsupported operations are explicit.
- [ ] Verify failures, implement the contract/generator, generate artifacts, and extend representative role/state/lease/error/idempotency/event conformance scenarios.
- [ ] Add contract checks to root, Go, and C# gates; run all conformance suites.
- [ ] Commit with `feat(spec): add behavioral capability contract`.

### Task 14: Expand Python quality scope and add type ratchet

**Files:**
- Create: `scripts/quality_scope.py`, `scripts/check_mypy_baseline.py`, `.ci/mypy-baseline.json`
- Modify: `ci/quality_checks.sh`, `ci/typecheck.sh`, relevant Ruff/Bandit exclusions
- Test: `tests/scripts/test_quality_scope.py`, `test_mypy_baseline.py`

- [ ] Write tests asserting every first-party Python `src` root appears exactly once, generated/vendor roots are excluded deliberately, and CI invokes the shared scope.
- [ ] Write baseline tests: equal passes, a new normalized diagnostic fails, a removed diagnostic requires ratcheting, and absolute/temp paths normalize stably.
- [ ] Verify failures, implement shared root output and make Ruff/Bandit/complexity/dead-code consume it; replace soft mypy exit-zero behavior with the ratchet.
- [ ] Generate the initial baseline from current diagnostics, ensuring it contains no secrets or machine-specific paths.
- [ ] Run script tests and the entire static quality gate.
- [ ] Commit with `ci: gate every Python production package`.

### Task 15: Recording defaults, quota cleanup, and tracking guard

**Files:**
- Modify: Python and Go recording/server configuration and cleanup code
- Create: shared per-language default-data-directory and one-shot cleanup helpers/tests
- Create: `scripts/check_recording_artifacts.py`, `tests/scripts/test_recording_artifacts.py`
- Modify: docs and quality gate

- [ ] Write deterministic OS tests for XDG, macOS Library, and Windows data paths outside the repository/CWD; distinguish explicit relative user paths from the default.
- [ ] Write cleanup tests for age deletion, oldest-first aggregate quota, deterministic ties, active files, non-recordings, symlinks, absent directories, errors, and structured stats.
- [ ] Write permission integration tests for 0700 directories and 0600 files.
- [ ] Write git-index guard tests rejecting recording artifacts except an explicit canonical-demo allowlist.
- [ ] Verify failures, implement nonzero secure retention/aggregate quota defaults, one-shot cleanup helpers, summary metrics, OS data defaults, and the tracking guard.
- [ ] Update recording handling docs with credentials/PII, deletion, redaction, encryption-at-rest, retention, and quota.
- [ ] Run Python/Go recording suites and the quality gate.
- [ ] Commit with `fix(recording): secure storage defaults and retention`.

### Task 16: Generated mutation-perimeter documentation and VNC quality inclusion

**Files:**
- Create: `scripts/generate_mutation_perimeter.py`, tests
- Modify: `docs/TESTING.md`, `ci/quality_checks.sh`, Go Makefile and VNC test/fuzz scope

- [ ] Write tests parsing `[tool.mutmut]` and deterministically generating a marked Markdown section; `--check` must fail on drift, nonexistent/non-production paths, or duplicate hand-maintained status lists.
- [ ] Verify failure, implement the generator, label historical survivor documentation as archived, and add the check to CI.
- [ ] Remove `litevirt_ai.go`/`litevirt_human.go` blanket coverage exclusion after Tasks 7–10; permit only specific irreducible adapter exclusions with documented evidence.
- [ ] Add bounded fuzz smoke to the Go gate and verify coverage remains above threshold.
- [ ] Run mutation config tests, Go quality gate, and root quality gate.
- [ ] Commit with `ci: enforce generated mutation and VNC quality scope`.

## Phase E — integration and closure

### Task 17: Migration docs, observability, and final reconciliation

**Files:**
- Modify: operator/configuration/security documentation and example TOML
- Modify: structured metrics/audit event definitions and tests
- Modify: `ARCHITECTURE_CODE_REVIEW.md` with a resolution appendix linking commits/tests

- [ ] Write tests for attach transitions, resolution decisions, handshake/malformed-message failures, input denial/revocation, backpressure disconnect, cleanup, and redaction in logs/audit events.
- [ ] Implement missing structured metrics/events without logging endpoint secrets, certificate material, VM credentials, terminal contents, or resolved secret values.
- [ ] Document target registry CRUD, static precedence, secret references, TLS/mTLS, dynamic development mode, migration, rollback, recording changes, capability matrix, and shutdown behavior.
- [ ] Re-read every finding in `ARCHITECTURE_CODE_REVIEW.md`; add a resolution table mapping each finding to code, regression tests, and verification commands. Any unmapped item keeps this task open.
- [ ] Run `git diff --check`, full Python workspace tests, Go quality/vulnerability/fuzz gates, C# quality gate, npm typecheck/lint/tests, conformance, hostile-client tests, root quality gate, and changed mutation gates.
- [ ] Commit with `docs: close architecture remediation program` only after every required verification is green.

## Phase F — runtime-neutral REST convergence

### Task 18: RouteDef foundation and eligible-route inventory

**Files:**
- Modify: root/package Python dependencies and lockfile to add `routedef>=0.1.1,<0.2.0`
- Create: shared route metadata keys, typed application contexts, and route inventory/check scripts
- Test: `tests/conformance/test_routedef_inventory.py` and package-local adapter contract tests

- [ ] Write an inventory test that compares FastAPI and Cloudflare HTTP/JSON routes, classifies portable candidates, and requires every exclusion to use a known reason (`websocket`, `stream`, `download`, `static`, `lifecycle`, or runtime-only capability).
- [ ] Write dependency/adapter smoke tests proving `RouteDef`, `RouteTable`, FastAPI routing, and Cloudflare dispatch work in the normal and vendored Pyodide environments.
- [ ] Run tests and observe failure because dependency, inventory, and shared metadata are absent.
- [ ] Add the pinned dependency, vendor/update Cloudflare modules through the repository's supported process, define typed contexts and metadata constants aligned with `behavioral-contract.yaml`, and implement deterministic inventory checking.
- [ ] Run dependency, vendor-tree, adapter, and inventory tests.
- [ ] Commit with `feat(routes): establish shared routedef foundation`.

### Task 19: Characterize and migrate portable route families

**Files:**
- Create: focused shared route modules grouped by domain (health, sessions, control/lease, tunnels, profiles, approvals, fanout, recordings metadata, and other inventory-confirmed portable JSON families)
- Modify: FastAPI route assembly and Cloudflare dispatcher assembly to mount the same route tables
- Test: direct handler tests, FastAPI adapter tests, Cloudflare adapter tests, and normalized cross-backend parity suites for each family

- [ ] For one route family at a time, write characterization tests pinning successful status/body/headers, malformed input, auth/capability denial, not-found/conflict behavior, side effects, audit event, limits, and idempotency in both current runtimes.
- [ ] Run each family characterization suite before migration and require green; this is the compatibility baseline.
- [ ] Write direct `RouteDef` handler and dual-adapter parity tests, then run them and observe failure because the shared family does not exist.
- [ ] Implement the minimal runtime-neutral handlers against typed context protocols, mount them through both adapters, and remove the two duplicate implementations only after parity is green.
- [ ] Repeat red-green-refactor for every eligible inventory family; never batch uncharacterized families.
- [ ] Run full FastAPI, Cloudflare, conformance, auth, coverage, and Pyodide vendor suites.
- [ ] Commit each family separately using `refactor(routes): share <family> routedef handlers` so regressions are bisectable.

### Task 20: Route metadata/behavioral-contract convergence and legacy removal

**Files:**
- Modify: `spec/behavioral-contract.yaml`, its generator/checker, route metadata checks, docs, and CI gates
- Remove: superseded duplicate portable handlers/adapters after inventory reaches zero unexplained candidates
- Test: contract drift, route uniqueness, eligibility, auth metadata, public errors, limits, audit events, and no-dead-handler tests

- [ ] Write drift tests requiring each portable `RouteDef` to match the behavioral contract for operation ID, capability/role, state/lease requirement, limits, public errors, idempotency, and audit event.
- [ ] Write uniqueness/dead-code tests proving an eligible method/path is registered once per runtime and no superseded handler remains reachable or imported.
- [ ] Verify failures, then make route metadata and the behavioral generator share one canonical operation identifier and add checks to root quality/conformance gates.
- [ ] Delete legacy portable route implementations and update architecture/migration documentation, retaining native exclusions with machine-readable reasons.
- [ ] Run full Python workspace tests, both adapter suites, coverage/mutation gates for changed policy handlers, vendor-tree validation, and package artifact checks.
- [ ] Commit with `refactor(routes): complete routedef convergence`.

## Execution rules

- Execute tasks in order within each phase. Tasks 11–12 may proceed independently from Tasks 1–10 after Task 4's shared schema decisions are stable; Tasks 14–16 may proceed after their affected production changes land.
- Every production change requires a newly written test observed failing for the intended reason before implementation.
- Run `ruff format` and `ruff check` against every Python file immediately after it is created or modified, and again before task handoff. Do not defer formatting/lint cleanup to the end of a multi-file task.
- Run mypy as soon as new public interfaces and record types compile, before expanding their implementation, and rerun it after every signature change. `Any`, `cast`, ignores, and uncovered defensive branches may not be used to conceal an incompatible contract.
- Within each touched subsystem, investigate stability defects exposed during implementation—corrupt persisted state, error-boundary leaks, ignored failures, races, resource leaks, and false-success responses—and add a failing regression test before fixing them. Do not knowingly defer a discovered Critical or Important defect merely because the original task emphasized a happy path.
- Use a fresh implementation subagent per task, then a spec-compliance reviewer, then a code-quality reviewer. Reviewer findings must be fixed and re-reviewed before the task closes.
- Agents must not overwrite unrelated user changes. Each task commits only its declared files.
- GUI attachment stays disabled/default-denied until Tasks 1–10 pass integration/security gates.
- Tasks 18–20 begin after Task 13 establishes the behavioral contract; individual route-family migrations may proceed independently only when their files and state dependencies do not overlap.
