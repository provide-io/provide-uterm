# C# Lease Publication Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bound every acquisition/repair worker send and prevent stale ownership callbacks from overwriting a successor generation.

**Architecture:** `HijackLeaseManager` will use one bounded send result for pause/resume and identity-gated worker reconciliation on uncertain failure. Ownership mutations will create typed generation/state/identity tokens; `StateStore` will serialize and revalidate those tokens before invoking host callbacks.

**Tech Stack:** C# 13, .NET 10, xUnit, existing `TermHub` lifecycle coordinator and control-channel codec.

---

## Task 1: Reproduce unbounded pause and compensation

**Files:**
- Modify: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs`

- [ ] Add `AcquisitionFaultWorker`, which can hang or throw on `pause` or compensating `resume`, exposes attempted/release signals, ignores cancellation in hang mode, and records whether abort happened while authoritative.
- [ ] Add REST/dashboard pause hang/throw theory rows. Start acquisition, wait for the pause attempt, queue `RegisterWorkerAsync`, release throw when applicable, and assert acquisition fails within the configured timeout, the old worker aborts/reconciles, `HijackPending` clears, and the replacement becomes current.
- [ ] Add REST/dashboard compensating-resume hang/throw rows. Let pause succeed, invalidate its reservation through forced release, wait for compensating resume, queue the replacement, and assert bounded failure plus successor progress.
- [ ] Run the eight rows and verify timeout/hang failures before production changes:

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~FailedAcquireWorkerSendReconcilesAndReleasesReplacement'
```

Expected: FAIL because cancellation-ignoring sends do not complete within the test deadline.

## Task 2: Bound lease worker sends and reconcile failures

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Lease.cs`

- [ ] Replace static `SendPauseAsync` with an instance helper that starts `SendTextAsync` with a linked timeout token and awaits it using both `WaitAsync(_hub.ResumeSendTimeout, ct)` and `CancelAfter(_hub.ResumeSendTimeout)`.
- [ ] Reuse `ObserveEventualSendFault(Task?)` so late faults from cancellation-ignoring transports are consumed.
- [ ] Add a shared failure helper:

```csharp
private async Task ReconcileFailedWorkerSendAsync(string workerId, IWorkerWs worker)
{
    if (worker is IAbortableBrowserWs abortable)
    {
        try { abortable.Abort(); } catch { }
    }
    await _hub.ReconcileWorkerDisconnectAsync(workerId, worker).ConfigureAwait(false);
}
```

- [ ] On pause failure, perform bounded compensation when delivery is uncertain, then reconcile the captured worker before returning/throwing.
- [ ] In `ResolvePauseObligationAsync`, bound compensating resume; on failure observe the eventual task, abort/reconcile the captured worker, and always clear only the matching repair reservation.
- [ ] Run the eight acquisition rows and the complete `ResumeLifecycleIntegrationTests`; expect all green.

## Task 3: Reproduce stale ordinary ownership notifications

**Files:**
- Modify: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs`

- [ ] Add delayed callback gates and real REST acquire, REST release, and forced-release successor races.
- [ ] For acquire, block true callback while a successor release mutates state; assert callback order cannot finish with stale true.
- [ ] For release and force, block the outgoing callback while a successor acquire mutates state; assert callback order cannot finish with stale false.
- [ ] Add a never-hijacked worker reconciliation test asserting `worker_disconnected` is emitted but `OnHijackChanged(false)` is not.
- [ ] Run the four notification tests and verify at least one fails against unconditional handler/connection notification.

## Task 4: Introduce typed publication tokens and validate every callback

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Store.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Lease.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Connection.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/TermHub.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Server/UtermServer.cs`

- [ ] Define an internal ownership publication token containing worker ID, version, expected held/released state, expected REST hijack ID, and expected owner.
- [ ] Replace the boolean-only conditional API with `StateStore.NotifyHijackChanged(token)`. Under the per-worker notification gate, revalidate version and exact expected lease identity under the hub state lock, release the state lock, then invoke the callback while retaining publication order.
- [ ] Increment `HijackOwnershipVersion` whenever REST/dashboard ownership is acquired, released, expired, force-cleared, or disconnected after having been held.
- [ ] Capture held tokens at REST acquisition commit and released tokens at release/force/expiry/disconnect mutation.
- [ ] Publish tokens only after lifecycle transition completion. Remove unconditional REST handler and connection notifications; keep browser broadcasts as current-state snapshots.
- [ ] For disconnect, create/publish a released token only when `WasHijacked` is true.
- [ ] Run notification tests, prior delayed-disconnect coverage, and all lifecycle tests; expect green.

## Task 5: Verify and commit

**Files:**
- Verify all modified files.

- [ ] Run formatting/diff checks:

```bash
git diff --check
```

- [ ] Run focused lifecycle tests:

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore --filter 'FullyQualifiedName~ResumeLifecycleIntegrationTests|FullyQualifiedName~HubServicesTests'
```

- [ ] Run the full C# suite:

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore
```

- [ ] Commit production and test changes:

```bash
git add packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Store.cs packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Lease.cs packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Connection.cs packages/provide-uterm-csharp/src/Provide.Uterm/Hub/TermHub.cs packages/provide-uterm-csharp/src/Provide.Uterm/Server/UtermServer.cs packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs
git commit -m 'fix(csharp): harden lease lifecycle publication'
```
