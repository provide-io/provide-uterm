# C# Dashboard Disconnect Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish dashboard owner labels and disconnect release transitions in an exact, stale-proof sequence without invalidating resume generations.

**Architecture:** `DashboardHeld` centrally fixes the public callback owner to `"dashboard"` while retaining the browser object as validation identity. A typed browser-cleanup core captures `Released` at an immediate owner clear and publishes before returning the restorable generation. Deferred disconnect-resume captures the same token at its later clear and publishes before signaling the lifecycle fence.

**Tech Stack:** C# 14, .NET 10, xUnit, System.Net.WebSockets

---

## Task 1: Exact production callback contract

**Files:**
- Modify: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs`

- [ ] **Step 1: Strengthen acquire/release callback payload test**

Change the production callback collector to `(bool Enabled, string? Owner)` and assert the real WebSocket request/release flow emits exactly:

```csharp
[(true, "dashboard"), (false, (string?)null)]
```

- [ ] **Step 2: Strengthen disconnect/resume sequence test**

For the real WebSocket acquire, disconnect, and single-use resume flow, assert exactly:

```csharp
[
    (true, "dashboard"),
    (false, (string?)null),
    (true, "dashboard"),
]
```

Continue asserting that the restored `hijack_state` frame reports owner `"me"`.

- [ ] **Step 3: Run the production tests and verify RED**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~ProductionDashboardAcquireReleasePublishesExactlyOnce|FullyQualifiedName~ProductionDashboardResumePublishesRestoredOwnershipExactlyOnce' --logger 'console;verbosity=minimal'
```

Expected: current callbacks use null owner, and resume lacks the disconnect `false` transition.

## Task 2: Dashboard owner payload

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Store.cs`

- [ ] **Step 1: Centralize dashboard callback owner**

Make `OwnershipPublicationToken.DashboardHeld` always set `PublishedOwner` to `"dashboard"`:

```csharp
public static OwnershipPublicationToken DashboardHeld(
    string workerId,
    long ownershipVersion,
    object owner) => new(
        workerId,
        ownershipVersion,
        OwnershipPublicationExpectation.DashboardHeld,
        true,
        "dashboard",
        null,
        null,
        owner);
```

Remove the legacy optional public-owner argument and update current-token capture to call the fixed factory. Keep `DashboardOwner` as the exact object identity checked by `MatchesExpectedOwnership`.

- [ ] **Step 2: Run acquire/release test**

Run the Task 1 command. Expected: acquire/release owner payload passes; resume still fails because disconnect false is missing.

## Task 3: Immediate and deferred disconnect release

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Connection.cs`
- Modify: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs`

- [ ] **Step 1: Add failing deferred-clear timing test**

Acquire dashboard ownership, reserve a delayed browser input, call `CleanupBrowser`, and start `ResumeWorkerIfOwnershipUnchangedAsync`. Assert only the held callback exists before releasing input. Release input, await resume, and assert the release callback appears exactly once at the later owner clear.

- [ ] **Step 2: Run deferred and production resume tests and verify RED**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~DeferredDashboardDisconnectPublishesAtExactOwnerClear|FullyQualifiedName~ProductionDashboardResumePublishesRestoredOwnershipExactlyOnce' --logger 'console;verbosity=minimal'
```

Expected: both lack disconnect release callbacks.

- [ ] **Step 3: Add typed immediate-cleanup core**

Introduce a private `BrowserCleanupResult` carrying the existing nullable ownership version plus `OwnershipPublicationToken?`. Move current cleanup mutation logic into a core. When the active owner is cleared immediately, capture:

```csharp
OwnershipPublicationToken.Released(workerId, st.HijackOwnershipVersion)
```

Do not increment `HijackOwnershipVersion`. The existing `CleanupBrowser` wrapper publishes after leaving `SharedLock` and before returning the version.

- [ ] **Step 4: Publish deferred clear before fence completion**

When `ResumeWorkerIfOwnershipUnchangedAsync` clears `transition.DisconnectOwner`, capture the same `Released` token and submit it before sending resume and before completing the lifecycle transition. If cancellation reaches `CompleteLifecycleTransition` before the normal clear, have that fallback clear capture and publish the token before signaling `Activated` or `Completion`. Guard fence completion with `finally` so a callback exception cannot strand successors.

- [ ] **Step 5: Run Task 2 and Task 3 filters and verify GREEN**

Expected: exact production and deferred sequences pass.

## Task 4: Same-generation stale disconnect suppression

**Files:**
- Modify: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs`

- [ ] **Step 1: Add disconnect case to delayed dashboard theory**

Capture `Released(workerId, ownershipVersion)` for an immediate cleanup, restore a different registered browser with the same version, clear recorded callbacks, replay the delayed release through `StateStore.NotifyHijackChanged`, and assert false with no new callback. This proves state and WebSocket identity distinguish transitions even when the restorable generation is intentionally unchanged.

- [ ] **Step 2: Run stale and broader lifecycle tests**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~DelayedDashboardPublicationCannotOverwriteSuccessor|FullyQualifiedName~ResumeLifecycleIntegrationTests|FullyQualifiedName~HubServicesTests|FullyQualifiedName~ResumeOwnershipTests' --logger 'console;verbosity=minimal'
```

Expected: all pass after updating exact owner/sequence assertions affected by the documented dashboard payload.

## Task 5: Full verification and commit

**Files:**
- Verify all modified production, tests, spec, and plan files

- [ ] **Step 1: Audit typed token and cleanup call sites**

```bash
rg -n 'DashboardHeld\(|CleanupBrowser\(|NotifyHijackChanged\(' packages/provide-uterm-csharp/src/Provide.Uterm
git diff --check
```

Confirm every dashboard-held factory emits `"dashboard"`, immediate cleanup publishes before return, and deferred cleanup publishes before transition signals.

- [ ] **Step 2: Run the complete C# suite**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --logger 'console;verbosity=minimal'
```

Expected: all existing and new tests pass with zero failures.

- [ ] **Step 3: Commit implementation**

```bash
git add packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Connection.cs packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Store.cs packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Hub/HubServicesTests.cs packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Hub/ResumeOwnershipTests.cs packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs
git commit -m "fix(csharp): publish dashboard disconnect ownership"
```
