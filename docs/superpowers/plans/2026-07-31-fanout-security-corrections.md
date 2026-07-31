# Fan-out Security Corrections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` to execute this plan task-by-task.
> Use `superpowers:test-driven-development` for each production change,
> `superpowers:systematic-debugging` for unexpected failures, and
> `superpowers:verification-before-completion` before any completion claim.

**Goal:** Correct the independently reviewed fan-out implementation so every
served operation is global-admin-only, every dispatch rechecks current member
authorization in the controller, policy and output results are truthful, shared
state is safe, C# respects one operation deadline, and conformance evidence is
semantic and executable.

**Architecture:** Transport gates provide early 401/403 refusal, while each
controller owns the authoritative security invariant through a mandatory
principal-aware authorizer. Only the controller may construct a private dispatch
snapshot. Output capture is prepared before send. Mutable group records are
cloned at Go/C# store boundaries and grants mutate atomically. One shared JSON
scenario fixture drives native adapter suites and a top-level result comparator.

**Contract authority:** `docs/ard-multi-session-fanout.md` is authoritative:
create, list, delete, send, and grant are global-admin-only. Any older operator
wording is corrected, not implemented.

**Tech stack:** Python/FastAPI/Pydantic/pytest, Go/net/http/testing/race detector,
C#/ASP.NET/xUnit, TypeScript/Vitest, JSON/Python conformance runner.

**Completion rule:** Keep `FANOUT-001` through `FANOUT-005` at `[~]` throughout
implementation and local verification. Only the parent agent may close them,
after independent spec-compliance and code-quality reviewers approve the full
diff with no open findings.

---

## Task 1: Python controller-owned authorization and transport gates

**Files:**
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/bridge/fanout/_controller.py`
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/bridge/fanout/_routes.py`
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/bridge/routes/websockets_browser.py`
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/app/factory_impl.py`
- Test: `packages/provide-uterm-server/tests/bridge/test_fanout_authz.py`
- Test: `packages/provide-uterm-server/tests/bridge/test_fanout_approval.py`
- Test: `packages/provide-uterm-server/tests/bridge/test_routes_coverage.py`
- Test: `packages/provide-uterm-server/tests/bridge/test_browser_handlers_coverage.py`

- [ ] **Step 1: Witness REST and WebSocket auth failures**

Add table-driven REST tests for all five paths. Unauthenticated calls must return
401, and authenticated viewer/operator/session-admin calls must return 403,
before JSON parsing, group lookup, policy invocation, audit emission, observer
notification, or worker input. Add equivalent browser-WS `fanout_send` cases.

```bash
uv run pytest -q \
  packages/provide-uterm-server/tests/bridge/test_routes_coverage.py \
  packages/provide-uterm-server/tests/bridge/test_browser_handlers_coverage.py \
  -k fanout
```

Expected RED: one or more non-admin calls reach a handler or disclose a group.

- [ ] **Step 2: Witness controller fail-closed behavior**

Add direct-controller tests proving missing principal, string-only principal,
missing resolve/can-read/admin dependencies, non-admin role, revoked access, and
group grant without session access all return a typed error and produce zero
input/observer notifications. Add approval-release tests proving the persisted
full principal is rechecked after admin or session access is revoked.

```bash
uv run pytest -q \
  packages/provide-uterm-server/tests/bridge/test_fanout_authz.py \
  packages/provide-uterm-server/tests/bridge/test_fanout_approval.py
```

Expected RED: optional dependencies and caller-supplied/string identity still
bypass one or more checks.

- [ ] **Step 3: Implement the Python security boundary**

Require a real `Principal` and mandatory authorizer dependencies in public send
and release paths. Centralize `is_global_admin`, group-member resolution, and
fresh `can_read_session` checks in one helper. Persist the complete principal in
approval state. Make raw subset execution private and reachable only with the
controller-produced immutable snapshot. Add route and WS defense-in-depth gates
that return 401/403 before parsing or lookup.

- [ ] **Step 4: Use the actual policy role and serialize policy outcomes**

Derive the strongest normalized caller role (`admin`, then `operator`, then
`viewer`) from the real principal. Never hardcode `admin`. Ensure REST and WS
responses always include `error`, `approval_required`, and `approval_id`, with
explicit `null`/`false` normal values.

- [ ] **Step 5: Verify and commit the Python security slice**

```bash
uv run pytest -q \
  packages/provide-uterm-server/tests/bridge/test_fanout_authz.py \
  packages/provide-uterm-server/tests/bridge/test_fanout_approval.py \
  packages/provide-uterm-server/tests/bridge/test_routes_coverage.py \
  packages/provide-uterm-server/tests/bridge/test_browser_handlers_coverage.py
uv run ruff check packages/provide-uterm-server/src packages/provide-uterm-server/tests
git add packages/provide-uterm-server
git commit -m "fix(server): make fanout authorization controller-owned"
```

## Task 2: Python pre-send output capture

**Files:**
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/bridge/fanout/_collector.py`
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/bridge/fanout/_controller.py`
- Test: `packages/provide-uterm-server/tests/bridge/test_fanout_collector.py`
- Test: `packages/provide-uterm-server/tests/bridge/test_fanout_parallel.py`
- Test: `packages/provide-uterm-server/tests/bridge/test_fanout_sequential.py`

- [ ] **Step 1: Witness immediate-output loss and cleanup**

Create a deterministic hub that emits worker output synchronously inside send.
Assert parallel mode captures all members, sequential mode captures each member,
capture-open failure prevents that member's input, and send/policy/cancellation
failure closes every prepared capture exactly once. Do not wait for subscriber
counts or sleep.

```bash
uv run pytest -q \
  packages/provide-uterm-server/tests/bridge/test_fanout_collector.py \
  packages/provide-uterm-server/tests/bridge/test_fanout_parallel.py \
  packages/provide-uterm-server/tests/bridge/test_fanout_sequential.py
```

Expected RED: synchronous output is emitted before the current subscription.

- [ ] **Step 2: Add an explicit capture lifecycle**

Refactor the collector into open/collect/close phases. Parallel execution opens
all captures before any observer notification or worker input. Sequential
execution opens one immediately before that member's notification/input. Ensure
every exit path closes captures once and elapsed time starts at accepted worker
dispatch.

- [ ] **Step 3: Verify and commit Python capture**

```bash
uv run pytest -q packages/provide-uterm-server/tests/bridge/test_fanout_collector.py packages/provide-uterm-server/tests/bridge/test_fanout_parallel.py packages/provide-uterm-server/tests/bridge/test_fanout_sequential.py
git add packages/provide-uterm-server
git commit -m "fix(server): subscribe before fanout dispatch"
```

## Task 3: Go controller-owned authorization and admin-only surfaces

**Files:**
- Modify: `packages/provide-uterm-go/fanout/controller.go`
- Modify: `packages/provide-uterm-go/server/server.go`
- Modify: `packages/provide-uterm-go/server/routes_fanout.go`
- Modify: `packages/provide-uterm-go/server/ws_browser_fanout.go`
- Test: `packages/provide-uterm-go/fanout/controller_test.go`
- Test: `packages/provide-uterm-go/server/routes_fanout_test.go`
- Test: `packages/provide-uterm-go/server/ws_browser_fanout_test.go`

- [ ] **Step 1: Witness all Go refusal cases**

Table-test all five REST operations for 401/403 before body parsing or lookup.
Test browser-WS non-admin refusal with no input. Directly invoke the controller
with nil authorizer, missing/invalid principal, non-admin, revoked visibility,
and a caller-supplied extra subset ID; assert fail closed and zero side effects.

```bash
cd packages/provide-uterm-go
GOWORK=off go test ./fanout ./server -run 'Fanout|FanOut'
```

Expected RED: authenticated viewers can reach at least one route; WS and raw
controller APIs can bypass the intended invariant.

- [ ] **Step 2: Implement controller-owned authorization**

Define the smallest principal/authorizer contract over the existing registry and
authz services. The public send accepts a principal, verifies global admin, loads
the stored group, resolves current definitions, and checks current read access.
Remove or privatize public APIs accepting an authorized subset. Route and WS
gates independently reject non-admins.

- [ ] **Step 3: Verify and commit the Go security slice**

```bash
cd packages/provide-uterm-go
gofmt -w fanout/controller.go server/server.go server/routes_fanout.go server/ws_browser_fanout.go fanout/controller_test.go server/routes_fanout_test.go server/ws_browser_fanout_test.go
GOWORK=off go test ./fanout ./server -run 'Fanout|FanOut'
GOWORK=off go vet ./fanout ./server
git add packages/provide-uterm-go
git commit -m "fix(go): enforce admin fanout in the controller"
```

## Task 4: Go capture lifecycle and immutable atomic store

**Files:**
- Modify: `packages/provide-uterm-go/fanout/collector.go`
- Modify: `packages/provide-uterm-go/fanout/controller.go`
- Modify: `packages/provide-uterm-go/fanout/store.go`
- Test: `packages/provide-uterm-go/fanout/collector_test.go`
- Test: `packages/provide-uterm-go/fanout/controller_test.go`
- Test: `packages/provide-uterm-go/fanout/store_test.go`

- [ ] **Step 1: Witness capture and aliasing defects**

Add deterministic synchronous-output tests and capture-cleanup tests. Add store
tests that mutate the saved object, returned get object, and returned list object;
none may change stored state. Add concurrent distinct-grant and enumerate-while-
granting tests with barriers rather than timing sleeps.

```bash
cd packages/provide-uterm-go
GOWORK=off go test ./fanout -run 'Capture|Immediate|Store|Grant'
GOWORK=off go test -race ./fanout -run 'Store|Grant'
```

Expected RED: immediate output is lost, returned slices alias store state, or a
grant update is lost/racy.

- [ ] **Step 2: Implement prepare-before-send capture**

Add open/collect/close capture handles. Prepare all parallel handles before any
send and one sequential handle before each send. A preparation failure is a
member failure and blocks its input. Close every handle exactly once.

- [ ] **Step 3: Clone records and make grant atomic**

Deep-clone groups and nested worker/grant slices on save, get, and list. Move
creator validation, duplicate detection, mutation, and persistence into one
store method under one lock; the controller must not perform get-mutate-save.

- [ ] **Step 4: Verify and commit Go capture/store**

```bash
cd packages/provide-uterm-go
gofmt -w fanout/collector.go fanout/controller.go fanout/store.go fanout/collector_test.go fanout/controller_test.go fanout/store_test.go
GOWORK=off go test ./fanout
GOWORK=off go test -race ./fanout
GOWORK=off go vet ./fanout
git add packages/provide-uterm-go
git commit -m "fix(go): harden fanout capture and group storage"
```

## Task 5: C# controller-owned authorization and admin-only REST

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Fanout/Controller.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Server/UtermServer.Fanout.cs`
- Test: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/FanoutExecutionTests.cs`
- Test: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/ServerFanoutTests.cs`

- [ ] **Step 1: Witness C# 401/403 and direct-controller bypasses**

Table-test create/list/delete/send/grant. Missing auth returns 401 and non-admin
roles return 403 before parsing/lookup/input. Add direct controller tests for
missing authorizer/principal, non-admin, revoked session access, group-grant
non-bypass, and caller-supplied arbitrary worker IDs.

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~ServerFanoutTests|FullyQualifiedName~FanoutExecutionTests' -- xUnit.ParallelizeAssembly=false xUnit.MaxParallelThreads=1
```

Expected RED: handlers authenticate without requiring admin, and public raw
dispatch trusts its member subset.

- [ ] **Step 2: Implement the C# security boundary**

Require an authenticated global-admin principal in all handlers. Give the
controller a mandatory authorizer over the registry/authz services. Resolve and
authorize stored members at send time. Make raw snapshot dispatch private and
remove the public authorized-subset escape hatch.

- [ ] **Step 3: Verify and commit the C# security slice**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~ServerFanoutTests|FullyQualifiedName~FanoutExecutionTests' -- xUnit.ParallelizeAssembly=false xUnit.MaxParallelThreads=1
dotnet build packages/provide-uterm-csharp/Provide.Uterm.sln --no-restore
git add packages/provide-uterm-csharp
git commit -m "fix(csharp): enforce admin fanout in the controller"
```

## Task 6: C# immutable atomic store and one operation deadline

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Fanout/Controller.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Fanout/OutputCollector.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Fanout/Models.cs`
- Test: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/FanoutExecutionTests.cs`

- [ ] **Step 1: Witness store isolation and atomicity**

Test mutation after save/get/list, concurrent distinct grants without lost
updates, and safe enumeration while grants change. Use coordination primitives,
not sleeps.

- [ ] **Step 2: Witness the total deadline**

Create deterministic observer, broadcast, and worker-send tasks that never
complete despite cancellation. Assert one `maxResponseMs` bounds notification,
all dispatch, and collection; sequential members do not each receive a fresh
budget. Assert unfinished members are failures, cancellation is requested, caller
cancellation propagates, and a late task fault is observed.

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~FanoutExecutionTests' -- xUnit.ParallelizeAssembly=false xUnit.MaxParallelThreads=1
```

Expected RED: live records alias the store, grants race, or a hanging send blocks
beyond the configured total bound.

- [ ] **Step 3: Implement store cloning and atomic grant**

Deep-clone group records and collections across every store boundary. Add one
locked grant operation performing validation, duplicate detection, and mutation.

- [ ] **Step 4: Implement one monotonic operation deadline**

Create one linked cancellation source before observer notification and dispatch.
Pass only remaining time to every stage. Race non-cooperative tasks against the
deadline, stop awaiting them at expiry, retain them, and attach fault observation.
Do not translate caller cancellation into timeout results.

- [ ] **Step 5: Verify and commit C# state/deadline**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~FanoutExecutionTests|FullyQualifiedName~ServerFanoutTests' -- xUnit.ParallelizeAssembly=false xUnit.MaxParallelThreads=1
dotnet build packages/provide-uterm-csharp/Provide.Uterm.sln --no-restore
git add packages/provide-uterm-csharp
git commit -m "fix(csharp): bound fanout and isolate group state"
```

## Task 7: TypeScript component security, result shape, and capture

**Files:**
- Modify: `packages/provide-uterm-ts/src/fanout/controller.ts`
- Modify: `packages/provide-uterm-ts/src/fanout/collector.ts`
- Modify: `packages/provide-uterm-ts/src/fanout/routes.ts`
- Test: `packages/provide-uterm-ts/src/fanout/controller-send.test.ts`
- Test: `packages/provide-uterm-ts/src/fanout/collector.test.ts`
- Test: `packages/provide-uterm-ts/src/fanout/routes.test.ts`

- [ ] **Step 1: Witness component auth and policy-result defects**

Use the existing full `ServerPrincipal`/`AuthorizablePrincipal` model. Test all
five component routes for unauthenticated and non-admin refusal before parsing or
lookup. Directly test missing authorizer/principal, group-grant non-bypass,
revocation, actual policy role, deny, hold, and release recheck. Assert route
results always expose `error`, `approval_required`, and `approval_id`.

- [ ] **Step 2: Witness immediate-output loss**

Use a synchronous-output fake and assert capture precedes dispatch in both modes,
capture-open failure blocks input, and all handles close exactly once.

```bash
cd packages/provide-uterm-ts
npx vitest run src/fanout/routes.test.ts src/fanout/controller-send.test.ts src/fanout/collector.test.ts
```

Expected RED: read permission substitutes for admin, the controller accepts
string/optional dependencies, policy role is hardcoded, response fields disappear,
or synchronous output is lost.

- [ ] **Step 3: Implement the TypeScript component contract**

Require global admin at routes and controller, retain the full principal for
release, centralize current member authorization, privatize raw subset execution,
derive actual strongest role, normalize result fields, and add explicit capture
handles prepared before send. Keep the server surface unmounted/unadvertised.

- [ ] **Step 4: Verify and commit TypeScript**

```bash
cd packages/provide-uterm-ts
npx vitest run src/fanout
npm run typecheck
npm run lint
git add packages/provide-uterm-ts
git commit -m "fix(ts): secure fanout component execution"
```

## Task 8: Shared executable semantic scenarios

**Files:**
- Delete: `spec/fanout_security_coverage.json`
- Delete: `scripts/validate_fanout_security_coverage.py`
- Create: `spec/fanout_security_scenarios.json`
- Create: `scripts/run_fanout_security_scenarios.py`
- Create: `packages/provide-uterm-server/tests/bridge/test_fanout_security_scenarios.py`
- Create: `packages/provide-uterm-go/fanout/security_scenarios_test.go`
- Create: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/FanoutSecurityScenarioTests.cs`
- Create: `packages/provide-uterm-ts/src/fanout/security-scenarios.test.ts`
- Modify: `tests/conformance/test_fanout_security_coverage.py`

- [ ] **Step 1: Define data, not test names**

Create shared semantic cases for: unauthenticated refusal; viewer refusal on a
public session; strict/permissive dormant admission; revocation; group grant
non-bypass; partial failure; policy deny and hold/release; missing direct-
controller dependencies; immediate output; Go/C# store isolation and atomicity;
and the C# total deadline. Correct the prior Python/TypeScript ID swap.

Each backend/surface declares exactly one status: `execute`,
`unsupported_fail_closed`, `component_execute`, or `unserved`. Expectations must
include status/result fields, delivered workers, observer notifications, failed
members, and output.

- [ ] **Step 2: Witness the old validator's insufficiency**

Update the repository conformance test to require scenario semantics, exact
backend status coverage, and a real runner. Run it before adapters exist.

```bash
uv run pytest -q tests/conformance/test_fanout_security_coverage.py
```

Expected RED: the declaration-only manifest/regex validator cannot execute or
compare outcomes.

- [ ] **Step 3: Add one native adapter per implementation**

Each native suite loads the same fixture, executes every applicable ID through
real controller/route boundaries, asserts the exact ID set, and emits normalized
JSON. Explicit unsupported cases must execute refusal and prove zero input;
TypeScript uses `component_execute` and never advertises a server.

- [ ] **Step 4: Add the top-level result comparator**

The runner invokes all four native semantic suites, parses their normalized
results, compares each observation to the shared expectation, and fails on
missing/extra IDs, skips, false capability claims, unsupported mismatches, or
command failures.

- [ ] **Step 5: Run RED-to-GREEN conformance and commit**

```bash
uv run pytest -q tests/conformance/test_fanout_security_coverage.py
uv run python scripts/run_fanout_security_scenarios.py
git add spec scripts tests/conformance packages/provide-uterm-server/tests/bridge/test_fanout_security_scenarios.py packages/provide-uterm-go/fanout/security_scenarios_test.go packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/FanoutSecurityScenarioTests.cs packages/provide-uterm-ts/src/fanout/security-scenarios.test.ts
git commit -m "test(conformance): execute fanout security scenarios"
```

## Task 9: Live capability intersection and documentation truth

**Files:**
- Modify: `conformance/live/harness/matrix.py`
- Modify: `tests/conformance/live/test_matrix.py`
- Modify: `docs/ard-multi-session-fanout.md`
- Modify: `docs/security-language-parity.md`
- Modify: `docs/protocol-matrix.md`
- Modify: `docs/roadmap/uterm-code-review-remediation.md`

- [ ] **Step 1: Witness client-only capability selection**

Add matrix tests proving a scenario runs only when both the selected client and
the announced server support it. A manually selected TypeScript server fan-out
cell must remain unsupported/unserved.

```bash
uv run pytest -q tests/conformance/live/test_matrix.py -k 'capab or fanout'
```

Expected RED: current selection consults only the client capability.

- [ ] **Step 2: Intersect client and server capabilities**

Require both capability sets before starting a live cell. Preserve an explicit
unsupported reason instead of silently skipping or claiming support.

- [ ] **Step 3: Correct docs without closing findings**

Document global-admin-only fan-out everywhere; remove operator wording. Record
served/unserved/unsupported policy surfaces accurately. Add verification ledger
entries, but keep every `FANOUT-*` item `[~]` pending independent approval.

- [ ] **Step 4: Verify and commit live/docs**

```bash
uv run pytest -q tests/conformance/live/test_matrix.py tests/conformance/test_fanout_security_coverage.py
git add conformance tests/conformance docs
git commit -m "docs: align fanout security and live capabilities"
```

## Task 10: Full verification and independent approval gate

**Files:**
- Modify only if needed: `docs/roadmap/uterm-code-review-remediation.md`

- [ ] **Step 1: Run focused and language-wide gates from a clean tree**

```bash
git diff --check
uv run pytest -q packages/provide-uterm-server/tests/bridge -k fanout
uv run ruff check packages/provide-uterm-server/src packages/provide-uterm-server/tests scripts tests/conformance
cd packages/provide-uterm-go && GOWORK=off go test ./fanout ./server ./serverconfig && GOWORK=off go test -race ./fanout && GOWORK=off go vet ./fanout ./server ./serverconfig
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~Fanout' -- xUnit.ParallelizeAssembly=false xUnit.MaxParallelThreads=1
dotnet build packages/provide-uterm-csharp/Provide.Uterm.sln --no-restore
cd packages/provide-uterm-ts && npx vitest run src/fanout && npm run typecheck && npm run lint
uv run python scripts/run_fanout_security_scenarios.py
```

- [ ] **Step 2: Run repository/full language gates required by the tracker**

Run the repository's documented Python, Go, C#, TypeScript, static-analysis, and
live-matrix commands. Record exact commands, counts, durations, and any unrelated
baseline failures in the tracker ledger. Do not relabel a failure as passing.

- [ ] **Step 3: Dispatch independent reviews**

Send the complete correction diff and design to two fresh reviewers:

1. spec-compliance reviewer: every approved requirement and scenario;
2. code-quality/security reviewer: bypasses, races, cleanup, deadlines, and
   evidence validity.

Address each finding with another witnessed red-green cycle and rerun all affected
gates. Reviewers must re-review the corrected diff.

- [ ] **Step 4: Hand off without self-closing**

Report verification evidence and both approvals to the parent agent. Leave
`FANOUT-001` through `FANOUT-005` at `[~]`; the parent performs the final tracker
closure only after confirming independent approvals and clean full gates.
