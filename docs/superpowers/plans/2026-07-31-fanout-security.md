# Fan-out Security Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enforce strict-by-default member admission, current session authorization, and configured governance policy on every fan-out command in every implementation.

**Architecture:** Route adapters resolve authenticated principals and session definitions through a small authorization dependency passed into each fan-out controller. Group creation may allow dormant members only when `fanout_allow_unknown_members` is explicitly enabled; execution always re-resolves and authorizes members. Python wires the existing `WebhookFanOutPolicyGate`; ports that cannot honor configured governance refuse the operation rather than bypassing it.

**Tech Stack:** Python/FastAPI/Pydantic/pytest, Go/net/http/testing, C#/ASP.NET/xUnit, TypeScript/Vitest.

---

## Task 1: Python strict admission and send-time authorization

**Files:**
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/config_schema.py`
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/app/factory_impl.py`
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/bridge/fanout/_controller.py`
- Modify: `packages/provide-uterm-server/src/provide/uterm/server/bridge/fanout/_routes.py`
- Test: `packages/provide-uterm-server/tests/bridge/test_fanout_authz.py`
- Test: `packages/provide-uterm-server/tests/bridge/test_fanout_approval.py`
- Test: `packages/provide-uterm-server/tests/server/test_app_governance.py`

- [ ] **Step 1: Add failing route tests for strict and permissive creation**

Add tests that create a group containing `future-worker`; assert the default response is 400 and a config/hub with `fanout_allow_unknown_members=True` accepts it. Run:

```bash
uv run pytest -q packages/provide-uterm-server/tests/bridge/test_fanout_authz.py
```

Expected: the strict test fails because the route currently accepts unknown IDs.

- [ ] **Step 2: Add failing send and approval-release revocation tests**

Create an authorized group, revoke `can_read_session`, then call both normal send and `release_approved_command`; assert no worker input is emitted and the result identifies the member as failed. Expected: both tests fail because authorization is only checked at creation.

- [ ] **Step 3: Implement the Python contract**

Add this server configuration field:

```python
fanout_allow_unknown_members: bool = False
```

Pass it to `FanOutController`. Give the controller async dependencies that resolve a definition and authorize a principal, then implement one helper used by normal dispatch and approval release:

```python
async def _authorized_members(self, group: FanOutGroup, principal: str) -> tuple[list[str], list[str]]:
    allowed: list[str] = []
    refused: list[str] = []
    for worker_id in group.worker_ids:
        definition = await self._resolve_session(worker_id)
        if definition is None or not await self._can_read_session(principal, definition):
            refused.append(worker_id)
        else:
            allowed.append(worker_id)
    return allowed, refused
```

Creation returns a client error for unknown members unless the option is true. Dispatch sends only to `allowed` and appends `refused` failures.

- [ ] **Step 4: Wire the real fan-out policy gate**

Extend `GovernanceGates` with `fanout_policy_gate`. When `policy_webhook_url` is set, construct `WebhookFanOutPolicyGate` with the same URL, secret, and timeout and pass it into `FanOutController`. Add a factory test asserting configured fan-out deny and fail-closed exception behavior.

- [ ] **Step 5: Verify Python and commit**

```bash
uv run pytest -q packages/provide-uterm-server/tests/bridge/test_fanout_authz.py packages/provide-uterm-server/tests/bridge/test_fanout_approval.py packages/provide-uterm-server/tests/server/test_app_governance.py
uv run ruff check packages/provide-uterm-server/src packages/provide-uterm-server/tests
git add packages/provide-uterm-server
git commit -m "fix(server): enforce fanout authorization and policy"
```

## Task 2: Go strict admission, current authorization, and fail-closed governance

**Files:**
- Modify: `packages/provide-uterm-go/serverconfig/config.go`
- Modify: `packages/provide-uterm-go/serverconfig/load.go`
- Modify: `packages/provide-uterm-go/server/routes_fanout.go`
- Modify: `packages/provide-uterm-go/server/server.go`
- Modify: `packages/provide-uterm-go/fanout/controller.go`
- Test: `packages/provide-uterm-go/server/routes_fanout_test.go`
- Test: `packages/provide-uterm-go/serverconfig/config_test.go`

- [ ] **Step 1: Write failing Go tests**

Test default rejection, explicit permissive creation, and revocation between creation and send. Also configure governance without a controller gate and assert fan-out returns a policy refusal instead of sending.

```bash
GOWORK=off go test ./server ./serverconfig ./fanout
```

Expected: new tests fail against the current known-only creation check and ungated controller.

- [ ] **Step 2: Implement configuration and authorization dependencies**

Add `FanoutAllowUnknownMembers bool` with JSON/TOML key `fanout_allow_unknown_members`, default false, and load it through the strict config loader. Add a server helper that returns an allowed worker slice plus refused results after calling `Registry.GetDefinition` and `Authz.CanReadSession` for the current principal. Use it for REST and browser WebSocket fan-out.

- [ ] **Step 3: Refuse configured-but-unsupported policy**

Until Go implements approval storage for fan-out, detect configured governance in server construction/send and return a deterministic `fanout governance is not supported by this server` refusal. Never send worker input in this state.

- [ ] **Step 4: Verify Go and commit**

```bash
GOWORK=off gofmt -w serverconfig/config.go serverconfig/load.go server/routes_fanout.go server/server.go fanout/controller.go
GOWORK=off go test ./server ./serverconfig ./fanout
GOWORK=off go vet ./server ./serverconfig ./fanout
git add packages/provide-uterm-go
git commit -m "fix(go): authorize fanout members at send time"
```

## Task 3: C# strict admission and current authorization

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/ServerConfig/Config.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/ServerConfig/Load.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Server/UtermServer.Fanout.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Fanout/Controller.cs`
- Test: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/ServerFanoutTests.cs`

- [ ] **Step 1: Write failing xUnit tests**

Cover strict/default unknown rejection, permissive creation, revoked session authorization, and group-grantee-without-session-access. Run the exact class filter and observe authorization failures.

```bash
dotnet test -c Release --filter "FullyQualifiedName~ServerFanoutTests" -- xUnit.ParallelizeAssembly=false xUnit.MaxParallelThreads=1
```

- [ ] **Step 2: Implement C# configuration and route checks**

Add `FanoutAllowUnknownMembers` default false and parse `fanout_allow_unknown_members`. Reject unknown definitions during create unless enabled. Before `Controller.SendAsync`, resolve every member against `_deps.Registry` and `_deps.Authz`; pass only permitted members to execution and add refused members to `FailedSessions`.

- [ ] **Step 3: Make governance truth explicit**

If C# server configuration enables a policy webhook but no fan-out policy implementation exists, return a deterministic refusal without input. Add an assertion that no fake hub send occurred.

- [ ] **Step 4: Verify C# and commit**

```bash
dotnet test -c Release --filter "FullyQualifiedName~ServerFanoutTests|FullyQualifiedName~ServerConfig" -- xUnit.ParallelizeAssembly=false xUnit.MaxParallelThreads=1
dotnet build -c Release
git add packages/provide-uterm-csharp
git commit -m "fix(csharp): enforce fanout session authorization"
```

## Task 4: Complete C# fan-out execution semantics

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Fanout/Controller.cs`
- Create: `packages/provide-uterm-csharp/src/Provide.Uterm/Fanout/OutputCollector.cs`
- Test: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/FanoutExecutionTests.cs`

- [ ] **Step 1: Write failing parallel and sequential execution tests**

Use a fake `IFanoutHub` with an event stream. Assert parallel mode starts all
workers before collecting; sequential mode completes one before sending the next;
`quiesceMs` and `maxResponseMs` bound collection; `StopOnFirstError` leaves later
members failed; elapsed/output values are populated; divergence is applied before
return.

```bash
dotnet test -c Release --filter "FullyQualifiedName~FanoutExecutionTests" -- xUnit.ParallelizeAssembly=false xUnit.MaxParallelThreads=1
```

Expected: failures because the current controller ignores timing and mode and
returns empty output.

- [ ] **Step 2: Add event subscription to the fan-out hub contract**

Extend `IFanoutHub` with the smallest subscription API needed to observe worker
output and disconnect events. Implement `OutputCollector` with a quiescence timer
and a hard maximum timer, mirroring Go's `fanout.OutputCollector` behavior.

- [ ] **Step 3: Implement advertised group semantics**

Split controller execution into `SendParallelAsync` and `SendSequentialAsync`.
Collect successful output, populate elapsed milliseconds, apply error-pattern stop
only in sequential mode, and call `FlagDivergence` before returning. Preserve failed
members from the authorization layer.

- [ ] **Step 4: Verify and commit**

```bash
dotnet test -c Release --filter "FullyQualifiedName~FanoutExecutionTests|FullyQualifiedName~ServerIntegrationControlPlaneRestTests" -- xUnit.ParallelizeAssembly=false xUnit.MaxParallelThreads=1
git add packages/provide-uterm-csharp
git commit -m "feat(csharp): complete fanout execution semantics"
```

## Task 5: TypeScript route-module parity

**Files:**
- Modify: `packages/provide-uterm-ts/src/fanout/routes.ts`
- Modify: `packages/provide-uterm-ts/src/fanout/controller.ts`
- Test: `packages/provide-uterm-ts/src/fanout/routes.test.ts`
- Test: `packages/provide-uterm-ts/src/fanout/controller-send.test.ts`

- [ ] **Step 1: Add failing Vitest cases**

Add `allowUnknownMembers?: boolean` to route options in the wished-for test API. Assert default unknown rejection, permissive creation, and fresh authorization before `sendToGroup`. Assert denied members are never passed to `ctrl.send`.

```bash
npx vitest run packages/provide-uterm-ts/src/fanout/routes.test.ts packages/provide-uterm-ts/src/fanout/controller-send.test.ts
```

- [ ] **Step 2: Implement the TypeScript contract**

Default `allowUnknownMembers` to false. Resolve every group member during send, authorize the current `ServerPrincipal`, and pass the permitted member set through a controller send option. Preserve failed-member reporting. Repeat the check on approval release.

- [ ] **Step 3: Verify TypeScript and commit**

```bash
npx vitest run packages/provide-uterm-ts/src/fanout
npm run typecheck:ts
npm run lint:ts
git add packages/provide-uterm-ts/src/fanout
git commit -m "fix(ts): enforce fanout member authorization"
```

## Task 6: Cross-language contract and tracker

**Files:**
- Modify: `docs/protocol-matrix.md`
- Modify: `docs/security-language-parity.md`
- Modify: `docs/roadmap/uterm-code-review-remediation.md`
- Test: `spec/behavior.json` or a new focused conformance fixture under `conformance/live/scenarios/`

- [ ] **Step 1: Add executable parity coverage**

Add scenarios for unknown-member default rejection, permissive dormant creation followed by unauthorized registration, authorization revocation, and governance refusal. Only advertise a language capability when the scenario is served.

- [ ] **Step 2: Run the matrix**

```bash
PYTHONPATH=conformance/live GOWORK=off uv run python -m harness --servers python go csharp typescript --clients python go csharp typescript
```

Expected: every applicable cell passes; explicit unsupported cells are documented rather than silently skipped.

- [ ] **Step 3: Update tracker and commit**

Check `FANOUT-001` through `FANOUT-005` only after focused and matrix evidence is recorded in the verification ledger.

```bash
git add docs spec conformance
git commit -m "docs: record fanout security parity"
```
