# C# Pause-Fence FIFO and Resume Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep replacement registration FIFO across acquisition pause/repair and fully reconcile workers whose release, expiry, or force resume send fails.

**Architecture:** Replacements reserve or enqueue their exact existing lifecycle node before awaiting `PendingPauseCompletion`, making the lifecycle coordinator the single FIFO for replacement/input interaction. `CompleteResumeAsync` adopts the bounded captured-task send pattern and routes every unsuccessful result through identity-gated `ReconcileFailedWorkerSendAsync`, leaving transition completion idempotent.

**Tech Stack:** C# 14, .NET 10, xUnit, System.Net.WebSockets

---

### Task 1: Replacement FIFO across successful pause/repair

**Files:**
- Modify: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Connection.cs`

- [ ] **Step 1: Add the deterministic FIFO regression test**

Hold the successful acquisition pause/repair sequence at its completion fence. Start R1, then R2, and assert R1 owns the active lifecycle node while R2 owns the one queued successor. Start a later browser input and assert it remains pending.

Release pause/repair and record replacement task completion order. Assert R1 completes before R2, the final registered worker is R2, R1 receives no later input, and R2 alone receives the input.

- [ ] **Step 2: Run the focused test and verify RED**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~ReplacementsWaitingForPauseRepairKeepFifoPriorityOverLaterInput' --logger 'console;verbosity=minimal'
```

Expected: no active/queued lifecycle nodes exist while both registrations wait naked on `PendingPauseCompletion`, or completion/input ordering is nondeterministic.

- [ ] **Step 3: Reserve the exact lifecycle node before waiting**

In `RegisterWorkerAsync`, when the current call reaches an incomplete `PendingPauseCompletion` without a foreign active transition, reserve its active lifecycle node before awaiting the pause task. Preserve the existing reservation string, completion source, `preserveOnWorkerClear`, self-transition skip, and exact-node cancellation cleanup.

The next registration must take the existing foreign-transition branch and enqueue normally. Do not add a second queue or signal any lifecycle node from the pause sequence itself.

- [ ] **Step 4: Re-run the focused test and adjacent FIFO/cancellation tests**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~ReplacementsWaitingForPauseRepairKeepFifoPriorityOverLaterInput|FullyQualifiedName~QueuedReplacementsKeepFifoPriorityOverLaterOpenModeInput|FullyQualifiedName~CancelledRebasedQueuedReplacementDoesNotStrandItsFifoSuccessor' --logger 'console;verbosity=minimal'
```

Expected: all pass.

### Task 2: Release/expiry/force resume failure matrix

**Files:**
- Modify: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs`

- [ ] **Step 1: Add local and WebSocket-like failure fixtures**

Use a real `LocalWorkerLink` path for local-worker rows and an abortable WebSocket-like worker for WebSocket rows. Both fixtures support resume throw and cancellation-ignoring hang, expose the send attempt, and allow a late fault/completion/finalizer replay after replacement.

- [ ] **Step 2: Add all twelve theory rows**

Cover operation `release`, `expiry`, and `force`, transport `local` and `ws`, and failure `throw` and `hang`. In each row:

- establish REST ownership;
- begin the selected ownership-ending operation;
- wait until resume send is attempted;
- enqueue a replacement;
- complete or time out the failed send;
- assert the operation and replacement finish within bounds;
- assert the captured worker is no longer authoritative and the server registry is offline;
- assert exactly one `worker_disconnected` and one current-state publication for teardown;
- apply the route's replacement online mark, replay late completion/finalizer effects, and assert replacement identity/online status and publication counts remain unchanged.

- [ ] **Step 3: Run the matrix and verify RED**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~FailedOwnershipEndingResumeReconcilesWorkerExactlyOnce' --logger 'console;verbosity=minimal'
```

Expected: local workers are merely nulled and remain registered online; WebSocket cleanup/publication depends on an external receive-loop finalizer; hanging/faulted send tasks are not centrally observed.

### Task 3: Centralize bounded resume failure teardown

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Lease.cs`

- [ ] **Step 1: Capture and bound the resume send task**

Track the exact `Task? sendTask`. Create a linked cancellation source, apply `ResumeSendTimeout`, call `WaitAsync` with the timeout and caller token, and verify an abortable worker remains active after the await before treating the send as successful.

- [ ] **Step 2: Observe abandoned faults**

In the failure catch, call the existing `ObserveEventualSendFault(sendTask)` so a cancellation-ignoring send that faults after timeout is observed.

- [ ] **Step 3: Reconcile every unsuccessful captured worker**

In `finally`, when send was unsuccessful and a worker was captured, await `ReconcileFailedWorkerSendAsync(workerId, worker)`. Remove the transport-specific partial abort/direct-null branch. Then locate and complete the exact transition if it still exists; centralized reconciliation may already have cleared and advanced it. Always signal the completion source.

- [ ] **Step 4: Run the matrix and lifecycle integration suite**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~FailedOwnershipEndingResumeReconcilesWorkerExactlyOnce|FullyQualifiedName~ResumeLifecycleIntegrationTests' --logger 'console;verbosity=minimal'
```

Expected: all pass, with exact once-only publication and successor progress.

### Task 4: Full verification and implementation commit

**Files:**
- Verify all modified C# production/test files and committed design/plan documents

- [ ] **Step 1: Audit ordering and teardown call sites**

```bash
rg -n 'PendingPauseCompletion|CompleteResumeAsync|ReconcileFailedWorkerSendAsync|ObserveEventualSendFault' packages/provide-uterm-csharp/src/Provide.Uterm/Hub
git diff --check
```

- [ ] **Step 2: Run the complete C# suite**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --logger 'console;verbosity=minimal'
```

Expected: all tests pass with zero failures.

- [ ] **Step 3: Commit implementation**

```bash
git add packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Connection.cs packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Lease.cs packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs
git commit -m "fix(csharp): reconcile resume failure lifecycle"
```
