# C# WebSocket and Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the C# server correctly assemble WebSocket messages, enforce admission limits, isolate slow browsers, and provide bounded truthful resume behavior.

**Architecture:** A shared bounded message receiver owns WebSocket fragmentation. ConnectionManager owns pending/active browser admission and quotas. Resume state is stored in a bounded expiring component rather than an unbounded dictionary in `UtermServer`.

**Tech Stack:** C#/.NET WebSockets, ASP.NET Core, xUnit.

---

### Task 1: Shared bounded WebSocket message receiver

**Files:**
- Create: `packages/provide-uterm-csharp/src/Provide.Uterm/Server/WebSocketMessageReader.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Server/UtermServer.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Server/UtermServer.Tunnel.cs`
- Test: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/ServerWebSocketFragmentationTests.cs`

- [ ] **Step 1: Write failing fragmentation tests**

Use a scripted `WebSocket` that returns two or more receive fragments. Assert browser JSON/control text, worker snapshot frames, and tunnel binary frames are handled once only after `EndOfMessage`. Add an over-limit case and a mixed-message-type case.

```bash
dotnet test -c Release --filter "FullyQualifiedName~ServerWebSocketFragmentationTests" -- xUnit.ParallelizeAssembly=false xUnit.MaxParallelThreads=1
```

- [ ] **Step 2: Implement the bounded reader**

Create a result record containing `WebSocketMessageType` and `byte[] Payload`. The read loop appends `Count` bytes, rejects a changed message type, checks the cumulative size before growth, and returns only on `EndOfMessage`. A close frame returns a close result without payload.

- [ ] **Step 3: Replace all three server receive loops**

Browser and worker limits must be at least the control-channel protocol maximum; tunnel uses its protocol maximum. Reuse one worker `ControlFrameDecoder` for the socket lifetime.

- [ ] **Step 4: Verify and commit**

```bash
dotnet test -c Release --filter "FullyQualifiedName~ServerWebSocketFragmentationTests|FullyQualifiedName~ServerIntegration" -- xUnit.ParallelizeAssembly=false xUnit.MaxParallelThreads=1
git add packages/provide-uterm-csharp
git commit -m "fix(csharp): assemble fragmented websocket messages"
```

### Task 2: Worker and browser admission

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Connection.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Server/UtermServer.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Server/UtermServer.Tunnel.cs`
- Test: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/ServerConnectionAdmissionTests.cs`

- [ ] **Step 1: Write failing admission tests**

Assert rejected worker/tunnel sockets close with policy violation and remain offline. Connect two browsers for one subject at a limit of one and assert the second is rejected. Assert setup failure rolls the counter back.

- [ ] **Step 2: Implement principal quota accounting**

Store the subject ID alongside pending/active browser state. Admission increments atomically under `SharedLock`; cleanup decrements exactly once. Anonymous/service principals remain exempt only where the Python contract exempts them.

- [ ] **Step 3: Honor worker registration results**

Check `RegisterWorker`; on false, close with `PolicyViolation`, skip registry state changes and broadcasts, and return.

- [ ] **Step 4: Verify and commit**

```bash
dotnet test -c Release --filter "FullyQualifiedName~ServerConnectionAdmissionTests|FullyQualifiedName~Hub" -- xUnit.ParallelizeAssembly=false xUnit.MaxParallelThreads=1
git add packages/provide-uterm-csharp
git commit -m "fix(csharp): enforce websocket admission limits"
```

### Task 3: Deferred activation and bounded broadcast

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Connection.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Server/UtermServer.cs`
- Test: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/HubBroadcastIsolationTests.cs`

- [ ] **Step 1: Write failing ordering and timeout tests**

Register with `deferBroadcast:true`, broadcast, and assert the pending socket receives nothing. Activate it and assert delivery. Add one never-completing browser and one healthy browser; assert the healthy browser receives promptly and the stalled socket is pruned.

- [ ] **Step 2: Implement pending activation**

Track pending browsers separately or mark each entry inactive. Add `ActivateBrowser(workerId, ws)`. Production sends `hello`, hijack state, and presence sync before activation; failure cleans up without exposing the socket.

- [ ] **Step 3: Implement isolated broadcasts**

Run sends with a per-socket linked cancellation token and a five-second timeout, collect them with `Task.WhenAll`, and remove failed/timed-out sockets after the snapshot iteration.

- [ ] **Step 4: Verify and commit**

```bash
dotnet test -c Release --filter "FullyQualifiedName~HubBroadcastIsolationTests|FullyQualifiedName~ServerIntegration" -- xUnit.ParallelizeAssembly=false xUnit.MaxParallelThreads=1
git add packages/provide-uterm-csharp
git commit -m "fix(csharp): isolate browser broadcasts"
```

### Task 4: Bounded truthful resume storage

**Files:**
- Create: `packages/provide-uterm-csharp/src/Provide.Uterm/Server/ResumeTokenStore.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Server/UtermServer.cs`
- Test: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/ResumeTokenStoreTests.cs`
- Test: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/ServerResumeOwnershipTests.cs`

- [ ] **Step 1: Write failing storage tests**

Use a fake clock to assert single-use consume, expiry sweep on mint/consume, and hard-cap eviction. Add a server test showing a resumed owner either regains its lease or the hello advertises `resume_supported:false`.

- [ ] **Step 2: Implement `ResumeTokenStore`**

Store token, worker ID, role, browser identity/ownership claim, creation, and expiry. Cap entries using oldest-expiry eviction. `Consume` removes before validating to preserve single use.

- [ ] **Step 3: Implement truthful server semantics**

Rebind ownership only if the consumed state still owns the worker lease and no newer owner replaced it. Otherwise return a non-resumed hello. Never claim success merely because the token parsed.

- [ ] **Step 4: Verify and commit**

```bash
dotnet test -c Release --filter "FullyQualifiedName~ResumeTokenStoreTests|FullyQualifiedName~ServerResumeOwnershipTests" -- xUnit.ParallelizeAssembly=false xUnit.MaxParallelThreads=1
git add packages/provide-uterm-csharp
git commit -m "fix(csharp): bound and restore resume state"
```

### Task 5: Constant-time worker credentials and final C# gate

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Server/UtermServer.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Server/UtermServer.Tunnel.cs`
- Test: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/ServerWorkerAuthTests.cs`
- Modify: `docs/roadmap/uterm-code-review-remediation.md`

- [ ] **Step 1: Add a focused credential helper test**

Test exact match, wrong token, prefix/suffix, invalid bearer grammar, and non-ASCII input against a helper that hashes/compares UTF-8 bytes with `CryptographicOperations.FixedTimeEquals`.

- [ ] **Step 2: Replace ordinary string comparisons**

Use the helper in both worker and tunnel endpoints without changing refusal shapes.

- [ ] **Step 3: Run the supported C# gate and update tracker**

```bash
make test
dotnet build -c Release
```

Record counts and check `CSHARP-*` entries only when the commands exit zero, then commit.

### Task 6: Serialize explicit and forced release resumes

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Models.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Lease.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Connection.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/TermHub.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Server/UtermServer.cs`
- Test: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs`

- [ ] **Step 1: Add deterministic stale-resume races**

Delay the worker's next resume for dashboard release, REST release, and forced
release. Start a successor acquisition while the resume is blocked and assert no
successor owner is published. Release the send and assert a later acquisition can
own the worker without receiving a stale resume.

- [ ] **Step 2: Observe the three tests fail**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --filter "FullyQualifiedName~ReleaseResumeBlocksSuccessor" --no-restore
```

- [ ] **Step 3: Add the resume transition**

Rename the disconnect-only completion fields to generic pending-resume fields.
Each async release reserves `HijackPending`, captures `WorkerWs`, clears the old
owner, sends resume to that captured transport, and clears only its own reservation
in `finally`. Update server callers to await release completion.

- [ ] **Step 4: Run the focused tests and commit with the remaining lifecycle work**

### Task 7: Fence worker replacement and stale frames

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Connection.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Server/UtermServer.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Server/LocalWorkerLink.cs`
- Test: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs`

- [ ] **Step 1: Add a deterministic replacement test**

Acquire against an old abortable worker, delay its reconciliation resume, register
a replacement, and assert the old identity is fenced, stale state updates are
rejected, ownership is cleared, and new acquisition is blocked until old resume
and abort complete.

- [ ] **Step 2: Observe the test fail**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --filter "FullyQualifiedName~WorkerReplacement" --no-restore
```

- [ ] **Step 3: Implement async replacement registration**

Add `RegisterWorkerAsync`. Under `SharedLock`, replace the identity, invalidate
ownership, and install a replacement reservation. Outside the lock, send a bounded
resume to the captured old worker and abort it. Clear only the matching reservation.
Use the async API in WebSocket and local-worker production paths. Check
`IsActiveWorker` before accepting each decoded worker frame.

- [ ] **Step 4: Run the focused tests**

### Task 8: Actively settle lease expiry

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/Lease.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Hub/TermHub.cs`
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Server/UtermServer.cs`
- Test: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/ResumeLifecycleIntegrationTests.cs`

- [ ] **Step 1: Add gated-clock REST and dashboard expiry tests**

Arm a one-second owner, wait for the timer sleep, advance past expiration, and
release the gate. Assert the expired owner is cleared and the captured worker sees
exactly `pause,resume` with no indefinite pending reservation.

- [ ] **Step 2: Observe both tests fail**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --filter "FullyQualifiedName~ExpiredLeaseResumesWorker" --no-restore
```

- [ ] **Step 3: Arm and settle expiry**

Arm a fire-and-forget check after acquire, touch, and extend. The check sleeps via
`IClock`, revalidates current expiration and ownership, then invokes the same
captured-worker resume reservation. Make explicit cleanup async and await it from
the REST acquire route.

- [ ] **Step 4: Run expiry and release tests**

### Task 9: Fail closed for unknown browser sessions

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/Server/UtermServer.cs`
- Test: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Server/WorkerAdmissionIntegrationTests.cs`

- [ ] **Step 1: Add an HTTP-upgrade admission test**

Send a valid authenticated browser WebSocket upgrade for an unknown safe ID and
assert HTTP 404 with no hub browser state created.

- [ ] **Step 2: Observe the test fail with an upgrade/200 path**

- [ ] **Step 3: Require `TryGetDefinition` outside explicit test mode**

Return 404 before `AcceptWebSocketAsync`; for known sessions retain current read
authorization and role resolution.

- [ ] **Step 4: Run browser and worker admission integration tests**

### Task 10: Restore process-wide test mode and verify

**Files:**
- Modify: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/SessionReferenceParityTests.cs`
- Modify: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/ServerIntegrationHostRestTests.cs`

- [ ] **Step 1: Add an isolated contamination assertion**

Run the test-mode fixtures followed by viewer/unknown-session authorization and
assert the environment has its original value and viewer access remains denied.

- [ ] **Step 2: Remove unnecessary test-mode mutation and scope the required one**

The reference-parity helper does not use the browser bypass, so remove its write.
For the SSE heartbeat test, capture the previous value, set `1` only while the
handler selects its interval, and restore in `finally`.

- [ ] **Step 3: Run isolated, impacted, and full batches**

```bash
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --filter "FullyQualifiedName~ResumeLifecycleIntegrationTests|FullyQualifiedName~WorkerAdmissionIntegrationTests|FullyQualifiedName~SessionReferenceParityTests|FullyQualifiedName~ServerIntegrationHostRestTests" --no-restore
dotnet test packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/Provide.Uterm.Tests.csproj --no-restore
```

- [ ] **Step 4: Commit**

```bash
git add docs/superpowers packages/provide-uterm-csharp
git commit -m "fix(csharp): serialize lease lifecycle transitions"
```
