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

