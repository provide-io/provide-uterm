# C# Dashboard Publication Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish dashboard acquire, release, and restore ownership callbacks exactly once through typed current-state tokens while preserving existing public APIs and browser broadcasts.

**Architecture:** Private typed-result cores capture `DashboardHeld` or `Released` tokens atomically with each mutation. Existing public tuple/bool methods remain wrappers that publish captured tokens only after pause/resume lifecycle cleanup. Real WebSocket integration tests exercise the unchanged production handler, while focused lease tests prove direct restore and stale token suppression.

**Tech Stack:** C# 14, .NET 10, xUnit, System.Net.WebSockets

---

## Task 1: Production dashboard callback contract

**Files:**
- Modify: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs`

- [ ] **Step 1: Add failing acquire/release integration test**

Add a `BootAsync` callback collector option and a test that connects a real browser WebSocket, drains the handshake, sends `hijack_request`, receives `hijack_state` with `hijacked=true`, sends `hijack_release`, receives `hijack_state` with `hijacked=false`, and asserts callbacks are exactly `[true, false]`.

- [ ] **Step 2: Add failing resume integration test**

Acquire from the original socket, disconnect it, reconnect with the resume token, receive a resumed hello and the restored `hijack_state`, and assert callbacks are exactly `[true, true]`: one original acquisition and one restored acquisition.

- [ ] **Step 3: Run the two tests and verify RED**

Run:

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~ProductionDashboardAcquireReleasePublishesExactlyOnce|FullyQualifiedName~ProductionDashboardResumePublishesRestoredOwnershipExactlyOnce' --logger 'console;verbosity=minimal'
```

Expected: callback assertions fail because dashboard mutations currently produce no host callback.

## Task 2: Typed dashboard acquisition and release cores

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Lease.cs`

- [ ] **Step 1: Add private typed mutation results**

Add immutable private records equivalent to:

```csharp
private sealed record DashboardAcquireResult(
    bool Ok,
    string Reason,
    OwnershipPublicationToken? Publication);

private sealed record DashboardReleaseResult(
    bool Released,
    bool RestActive,
    OwnershipPublicationToken? Publication);
```

- [ ] **Step 2: Split asynchronous acquisition into core and wrapper**

Move the current acquisition state machine into `TryAcquireWsCoreAsync`. At the successful commit, capture:

```csharp
var publication = OwnershipPublicationToken.DashboardHeld(
    workerId,
    st.HijackOwnershipVersion,
    ws);
```

Return it only after the existing `finally` clears the pause fence. Keep `TryAcquireWsAsync` signature unchanged; it awaits the core, calls `_hub.NotifyHijackChanged(result.Publication)` once when non-null, and returns `(result.Ok, result.Reason)`.

- [ ] **Step 3: Split release into core and wrapper**

Move the current release state machine into `TryReleaseWsCoreAsync`. Capture a `Released` token immediately after the dashboard owner is cleared and the generation increments. Preserve suppression when a REST lease remains through token validation. Keep `TryReleaseWsAsync` signature unchanged; publish once after `CompleteResumeAsync`, then return the existing tuple.

- [ ] **Step 4: Run the production tests and verify GREEN**

Run the Task 1 command. Expected: both tests pass, including received browser state frames.

## Task 3: Direct restore and stale ordering

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Lease.cs`
- Modify: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Hub/ResumeOwnershipTests.cs`
- Modify: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs`

- [ ] **Step 1: Add failing direct restore callback test**

Extend `ResumeOwnershipTests` with a configured `OnHijackChanged` collector. Acquire an original dashboard owner, clean it up to retain its ownership version, restore a registered replacement through `TryRestoreWsOwnership`, and assert one restored `enabled=true, owner=null` callback after clearing acquisition changes.

- [ ] **Step 2: Add failing delayed dashboard token theory**

Extend stale publication coverage with dashboard-held and dashboard-released cases. Capture exact tokens from the current version and browser identity, advance state through release or a successor acquire, replay the delayed token through `StateStore.NotifyHijackChanged`, and assert it returns false without appending a callback.

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~ResumeOwnershipTests|FullyQualifiedName~DelayedOwnershipPublicationCannotOverwriteSuccessor' --logger 'console;verbosity=minimal'
```

Expected: direct restore callback test fails before the wrapper publishes; stale token tests document the existing sequencer behavior.

- [ ] **Step 4: Add restore core and compatibility wrapper**

Create a private restore result carrying `Restored` and `Publication`. Capture `DashboardHeld` under the shared lock when restore succeeds, call `ArmExpiry` as today, then let `TryRestoreWsOwnership` publish after leaving the lock and return the existing bool.

- [ ] **Step 5: Cover synchronous compatibility acquisition**

Refactor `TryAcquireWs` through the same typed acquisition result pattern so its successful dashboard mutation also publishes one `DashboardHeld` token without changing its tuple signature.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run the Task 3 command and the 92-test lifecycle/hub filter. Expected: all pass with exact callback sequences.

## Task 4: Full verification and commit

**Files:**
- Verify all modified production, test, spec, and plan files

- [ ] **Step 1: Audit notification and broadcast call sites**

Run:

```bash
rg -n 'NotifyHijackChanged\(|BroadcastHijackStateAsync\(' packages/provide-uterm-csharp/src/Provide.Uterm
git diff --check
```

Confirm dashboard wrappers publish tokens once and server broadcast calls are unchanged.

- [ ] **Step 2: Run the complete C# suite**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --logger 'console;verbosity=minimal'
```

Expected: all 1,308 existing tests plus new tests pass with zero failures.

- [ ] **Step 3: Commit implementation**

```bash
git add packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Lease.cs packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Hub/ResumeOwnershipTests.cs packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs
git commit -m "fix(csharp): publish dashboard ownership transitions"
```
