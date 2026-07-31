# C# Pause-to-Replacement Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the input-admission gap between acquisition pause completion and an already-reserved replacement continuation.

**Architecture:** `CompletePauseSequenceIfIdle` atomically hands `HijackPending` from the cleared pause reservation to the exact active lifecycle reservation before signaling `PendingPauseCompletion`. A null-default internal post-pause-wait callback freezes replacement continuation only in tests and does not alter normal production behavior.

**Tech Stack:** C# 14, .NET 10, xUnit

---

### Task 1: Deterministic handoff regression

**Files:**
- Modify: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Connection.cs`

- [ ] **Step 1: Write the regression test against the planned seam**

Add `InputWaitsWhilePauseFenceHandsOffToReplacement`. Register an open-mode original worker, dashboard owner, and admin input browser. Delay the dashboard pause, configure `ConnectionManager.AfterReplacementPauseFenceWait` with a gate, begin R1 registration, and release the successful pause. Once the gate confirms R1 resumed from `PendingPauseCompletion` but has not re-entered its registration loop, start admin input and assert:

```csharp
Assert.False(laterInput.IsCompleted);
Assert.Empty(original.Inputs);
```

Release R1 and assert registration and input complete, the replacement is current, the original remains input-free, and R1 receives exactly the later input.

- [ ] **Step 2: Run the test and verify the seam is absent**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~InputWaitsWhilePauseFenceHandsOffToReplacement' --logger 'console;verbosity=minimal'
```

Expected: compile failure because `AfterReplacementPauseFenceWait` is not yet defined.

- [ ] **Step 3: Add only the null-default continuation seam**

Add this internal property to `ConnectionManager`:

```csharp
internal Func<string, Task>? AfterReplacementPauseFenceWait { get; set; }
```

Track whether the current registration loop iteration selected `PendingPauseCompletion`. After its `WaitAsync(ct)` succeeds, invoke the callback with `workerId` before the loop can reacquire `SharedLock`:

```csharp
if (waitedForPause && AfterReplacementPauseFenceWait is { } afterPauseWait)
{
    await afterPauseWait(workerId).ConfigureAwait(false);
}
```

- [ ] **Step 4: Run the test and verify behavioral RED**

Run the Step 2 command again.

Expected: `laterInput` completes immediately and/or `original.Inputs` contains the input because `HijackPending` is null during the frozen handoff.

### Task 2: Atomic active-reservation handoff

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Lease.cs`

- [ ] **Step 1: Mirror the active lifecycle reservation under lock**

In `CompletePauseSequenceIfIdle`, after confirming both `PendingPauseReservation` and `PendingPauseObligation` are null, and before clearing `PendingPauseCompletion`, add:

```csharp
if (st.ActiveLifecycleTransition is { } activeTransition)
{
    st.HijackPending = activeTransition.Reservation;
}
```

Do not inspect or mirror queued transitions. Do not move `TrySetResult` inside the lock.

- [ ] **Step 2: Run the focused test and verify GREEN**

Run the Task 1 Step 2 command.

Expected: one passing test; input remains fenced at the callback and reaches only R1 after release.

- [ ] **Step 3: Run adjacent pause/FIFO tests**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~InputWaitsWhilePauseFenceHandsOffToReplacement|FullyQualifiedName~ReplacementsWaitingForPauseRepairKeepFifoPriorityOverLaterInput|FullyQualifiedName~CancelledRebasedQueuedReplacementDoesNotStrandItsFifoSuccessor|FullyQualifiedName~FailedAcquireWorkerSendReconcilesAndReleasesReplacement' --logger 'console;verbosity=minimal'
```

Expected: all selected tests pass.

### Task 3: Completion-path and full verification

**Files:**
- Verify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Connection.cs`
- Verify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Lease.cs`
- Verify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Models.cs`
- Verify: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs`

- [ ] **Step 1: Audit every pause completion/clear path**

```bash
rg -n 'CompletePauseSequenceIfIdle|PendingPauseCompletion = null|pendingPauseCompletion.*TrySetResult|LifecycleTransitionCoordinator.Clear' packages/provide-uterm-csharp/src/Provide.Uterm/Hub
```

Confirm acquisition `finally` blocks converge on the centralized handoff and deregistration activates preserved lifecycle nodes before signaling its captured pause completion.

- [ ] **Step 2: Run lifecycle integration tests**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~ResumeLifecycleIntegrationTests' --logger 'console;verbosity=minimal'
```

Expected: all lifecycle integration tests pass.

- [ ] **Step 3: Run the complete C# suite and diff check**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --logger 'console;verbosity=minimal'
git diff --check
```

Expected: all tests pass with zero failures and no whitespace errors.

- [ ] **Step 4: Commit implementation**

```bash
git add packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Connection.cs packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Lease.cs packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs
git commit -m "fix(csharp): close pause replacement handoff"
```
