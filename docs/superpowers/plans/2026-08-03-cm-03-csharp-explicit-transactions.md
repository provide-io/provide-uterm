# CM-03: Explicit C# Control-Plane Transactions

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every C# control-plane store operation take the transaction it
belongs to, so unrelated concurrent requests cannot join one another's
transaction and `MemoryTx` stops being a no-op that claims to be a transaction.

**Architecture:** `ITx` exists but is threaded nowhere. `MemoryTx.CommitAsync`
sets a bool and returns; nothing is isolated and nothing is rolled back.
`SqliteEngine` keeps an ambient `_current` slot that `Command()` binds to every
statement, so whichever request happens to be inside `BeginAsync` at the time
captures statements from every other. Add `ITx` as the first parameter of all 18
store methods, give the memory engine real snapshot isolation with
revision-checked optimistic commit, and delete the ambient slot.

**This is the approved breaking change.** uterm is pre-1.0 and the design says
so explicitly: "correctness is preferred to retaining an ambient or
non-transactional API." Migration errors must be compile-time errors.

**Tech Stack:** C# .NET 10, `Microsoft.Data.Sqlite`, xUnit.

## Global Constraints

- Target framework .NET 10. Nullable enabled.
- SPDX headers on new files, in the form used elsewhere in the package.
- **No compatibility overload retains ambient behavior.** An overload without
  `ITx` would let a call site silently keep the old semantics, which is the
  entire defect. Every call site migrates or the build fails.
- `make quality-gate` in `packages/provide-uterm-csharp` must pass, including
  the coverage floor and the Stryker mutation gate.
- Wire format and database schema do not change. The `cp_*` columns are shared
  with the Python and Go control planes and a database written by any of the
  three stays readable by the others.
- CM-10 (warning-free Release build) should land **before** this plan, so the
  API break arrives into a build that treats warnings as errors rather than
  adding warnings nothing catches.

## Context

`packages/provide-uterm-csharp/src/Provide.Uterm/ControlPlane/Engine.cs:15`:

```csharp
public interface ITx
{
    Task CommitAsync(CancellationToken cancellationToken = default);
    Task RollbackAsync(CancellationToken cancellationToken = default);
}
```

Measured 2026-08-03: `grep -rn "ITx tx" packages/provide-uterm-csharp/src --include="*.cs" | wc -l` returns **0**.
The type is returned by `BeginAsync` and accepted by nothing.

`MemoryTx` (`Engine.cs:197`) sets `_done = true` on commit and on rollback. It
does not snapshot, does not isolate, and rolling back publishes exactly as much
as committing does — everything, because the stores write straight through to
the shared dictionaries.

`SqliteEngine.cs:87`, `:213`, `:225` keep an ambient `_current`, and `Command()`
binds `cmd.Transaction = _current.Inner`. Two concurrent requests, one of which
is between `BeginAsync` and `CommitAsync`, means the other's statements join the
first's transaction. Its writes commit when the unrelated caller commits, or
vanish when that caller rolls back.

18 store methods across 5 interfaces; 6 files call them.

Measured 2026-08-03; see
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`,
finding CM-03.

## File Structure

- `ControlPlane/Engine.cs` — `ITx` gains ownership and completion state; all 5
  store interfaces gain `ITx tx` as the first parameter; `MemoryEngine` and
  `MemoryTx` are rewritten for real isolation.
- `ControlPlane/SqliteEngine.cs` — `SqliteTx` carries its own connection and
  transaction; `_current` is deleted; `Command()` takes the tx.
- `tests/Provide.Uterm.Tests/ControlPlaneTransactionTests.cs` — new.
- `spec/control_plane_tx_scenarios.json` and
  `tests/conformance/test_control_plane_tx_parity.py` — new.
- 6 call-site files, enumerated in Task 5.

---

### Task 1: ITx carries ownership and completion state

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/ControlPlane/Engine.cs:15-19`
- Create: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/ControlPlaneTransactionTests.cs`

**Interfaces:**
- Produces:
  ```csharp
  public interface ITx
  {
      IEngine Owner { get; }
      bool IsCompleted { get; }
      Task CommitAsync(CancellationToken cancellationToken = default);
      Task RollbackAsync(CancellationToken cancellationToken = default);
  }
  ```
  Tasks 2, 3 and 4 consume `Owner` and `IsCompleted` to reject foreign and spent
  transactions.

- [ ] **Step 1: Write the failing test**

Create `tests/Provide.Uterm.Tests/ControlPlaneTransactionTests.cs`:

```csharp
//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ControlPlane;

namespace Provide.Uterm.Tests;

/// <summary>
/// Transaction ownership and lifetime. A transaction that can be used twice, or
/// used against an engine that did not create it, is not a transaction.
/// </summary>
public class ControlPlaneTransactionTests
{
    private static async Task<MemoryEngine> OpenMemoryAsync()
    {
        var engine = new MemoryEngine();
        await engine.OpenAsync();
        return engine;
    }

    [Fact]
    public async Task Transaction_KnowsWhichEngineCreatedIt()
    {
        var engine = await OpenMemoryAsync();
        var tx = await engine.BeginAsync();

        Assert.Same(engine, tx.Owner);
    }

    [Fact]
    public async Task Transaction_IsNotCompletedBeforeCommit()
    {
        var engine = await OpenMemoryAsync();
        var tx = await engine.BeginAsync();

        Assert.False(tx.IsCompleted);
    }

    [Fact]
    public async Task Commit_MarksTheTransactionCompleted()
    {
        var engine = await OpenMemoryAsync();
        var tx = await engine.BeginAsync();

        await tx.CommitAsync();

        Assert.True(tx.IsCompleted);
    }

    [Fact]
    public async Task Commit_Twice_Throws()
    {
        // Committing a spent transaction is a caller bug, and one that silently
        // succeeding would hide: the second commit's writes went nowhere.
        var engine = await OpenMemoryAsync();
        var tx = await engine.BeginAsync();
        await tx.CommitAsync();

        await Assert.ThrowsAsync<InvalidOperationException>(() => tx.CommitAsync());
    }

    [Fact]
    public async Task Rollback_AfterCommit_Throws()
    {
        var engine = await OpenMemoryAsync();
        var tx = await engine.BeginAsync();
        await tx.CommitAsync();

        await Assert.ThrowsAsync<InvalidOperationException>(() => tx.RollbackAsync());
    }
}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet test tests/Provide.Uterm.Tests --filter FullyQualifiedName~ControlPlaneTransactionTests
```

Expected: FAIL at build — `ITx` has no `Owner` and no `IsCompleted`.

- [ ] **Step 3: Extend ITx and MemoryTx**

In `Engine.cs`, replace the `ITx` interface with:

```csharp
/// <summary>
/// A unit of control-plane work. Every store operation takes one, so there is
/// no ambient slot for an unrelated request to join.
/// </summary>
public interface ITx
{
    /// <summary>The engine that created this transaction.</summary>
    IEngine Owner { get; }

    /// <summary>True once committed or rolled back. A completed transaction is spent.</summary>
    bool IsCompleted { get; }

    Task CommitAsync(CancellationToken cancellationToken = default);
    Task RollbackAsync(CancellationToken cancellationToken = default);
}
```

Replace `MemoryTx` with a version that reports both and refuses reuse. The
working-set fields are added in Task 2; for now:

```csharp
public sealed class MemoryTx : ITx
{
    private readonly MemoryEngine _engine;

    internal MemoryTx(MemoryEngine engine) => _engine = engine;

    public IEngine Owner => _engine;
    public bool IsCompleted { get; private set; }

    public Task CommitAsync(CancellationToken cancellationToken = default)
    {
        RequirePending();
        IsCompleted = true;
        return Task.CompletedTask;
    }

    public Task RollbackAsync(CancellationToken cancellationToken = default)
    {
        RequirePending();
        IsCompleted = true;
        return Task.CompletedTask;
    }

    private void RequirePending()
    {
        if (IsCompleted)
        {
            throw new InvalidOperationException("control-plane transaction is already completed");
        }
    }
}
```

Update `MemoryEngine.BeginAsync`:

```csharp
    public Task<ITx> BeginAsync(CancellationToken ct = default) =>
        Task.FromResult<ITx>(new MemoryTx(this));
```

`SqliteTx` (`SqliteEngine.cs:22`) needs the same two members to keep compiling.
Add `public IEngine Owner => engine;` and an `IsCompleted` backed by the same
flag it already uses, or add one if it has none.

- [ ] **Step 4: Run the tests to verify they pass**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet test tests/Provide.Uterm.Tests --filter FullyQualifiedName~ControlPlaneTransactionTests
```

Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add packages/provide-uterm-csharp/src/Provide.Uterm/ControlPlane/Engine.cs \
        packages/provide-uterm-csharp/src/Provide.Uterm/ControlPlane/SqliteEngine.cs \
        packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/ControlPlaneTransactionTests.cs
git commit -m "feat(csharp): give control-plane transactions ownership and lifetime

ITx could not say which engine made it or whether it had been used, so
nothing could reject a foreign or spent transaction. Both are needed
before store operations can take one.

Committing twice now throws rather than silently succeeding — a second
commit that reports success has sent its writes nowhere."
```

---

### Task 2: The memory engine isolates and rolls back

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/ControlPlane/Engine.cs`
- Modify: `packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/ControlPlaneTransactionTests.cs`

**Interfaces:**
- Consumes: `ITx.Owner`, `ITx.IsCompleted` from Task 1.
- Produces: `MemoryEngine` gains an internal revision counter and per-transaction
  working state. `MemoryTx` gains `internal MemoryState Working { get; }` and
  `internal long BeganAtRevision { get; }`, both consumed by the store
  implementations in Task 3.

- [ ] **Step 1: Write the failing tests**

Add to `ControlPlaneTransactionTests.cs`:

```csharp
    private static SessionRecord Session(string id) =>
        new() { SessionId = id, DisplayName = id, ConnectorType = "pty", CreatedAt = 1, UpdatedAt = 1 };

    [Fact]
    public async Task Reads_ObserveTransactionLocalWrites()
    {
        var engine = await OpenMemoryAsync();
        var tx = await engine.BeginAsync();

        await engine.Sessions().UpsertAsync(tx, Session("s1"));
        var seen = await engine.Sessions().GetAsync(tx, "s1");

        Assert.NotNull(seen);
        Assert.Equal("s1", seen!.SessionId);
    }

    [Fact]
    public async Task Rollback_PublishesNothing()
    {
        var engine = await OpenMemoryAsync();
        var tx = await engine.BeginAsync();
        await engine.Sessions().UpsertAsync(tx, Session("s1"));

        await tx.RollbackAsync();

        var after = await engine.BeginAsync();
        Assert.Null(await engine.Sessions().GetAsync(after, "s1"));
    }

    [Fact]
    public async Task UncommittedWrites_AreInvisibleToAnotherTransaction()
    {
        var engine = await OpenMemoryAsync();
        var writer = await engine.BeginAsync();
        var reader = await engine.BeginAsync();

        await engine.Sessions().UpsertAsync(writer, Session("s1"));

        Assert.Null(await engine.Sessions().GetAsync(reader, "s1"));
    }

    [Fact]
    public async Task Commit_AfterAConflictingCommit_Fails()
    {
        // Two transactions that began at the same revision. The first to commit
        // wins; the second must not silently overwrite it.
        var engine = await OpenMemoryAsync();
        var first = await engine.BeginAsync();
        var second = await engine.BeginAsync();

        await engine.Sessions().UpsertAsync(first, Session("s1"));
        await engine.Sessions().UpsertAsync(second, Session("s2"));

        await first.CommitAsync();

        await Assert.ThrowsAsync<ControlPlaneConflictException>(() => second.CommitAsync());
    }

    [Fact]
    public async Task Commit_AfterAConflict_PublishesNothing()
    {
        var engine = await OpenMemoryAsync();
        var first = await engine.BeginAsync();
        var second = await engine.BeginAsync();
        await engine.Sessions().UpsertAsync(first, Session("s1"));
        await engine.Sessions().UpsertAsync(second, Session("s2"));
        await first.CommitAsync();

        await Assert.ThrowsAsync<ControlPlaneConflictException>(() => second.CommitAsync());

        var check = await engine.BeginAsync();
        Assert.NotNull(await engine.Sessions().GetAsync(check, "s1"));
        Assert.Null(await engine.Sessions().GetAsync(check, "s2"));
    }

    [Fact]
    public async Task Store_RejectsATransactionFromAnotherEngine()
    {
        var a = await OpenMemoryAsync();
        var b = await OpenMemoryAsync();
        var foreign = await b.BeginAsync();

        await Assert.ThrowsAsync<InvalidOperationException>(
            () => a.Sessions().UpsertAsync(foreign, Session("s1")));
    }

    [Fact]
    public async Task Store_RejectsACompletedTransaction()
    {
        var engine = await OpenMemoryAsync();
        var tx = await engine.BeginAsync();
        await tx.CommitAsync();

        await Assert.ThrowsAsync<InvalidOperationException>(
            () => engine.Sessions().UpsertAsync(tx, Session("s1")));
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet test tests/Provide.Uterm.Tests --filter FullyQualifiedName~ControlPlaneTransactionTests
```

Expected: FAIL at build — the store methods do not take an `ITx`, and
`ControlPlaneConflictException` does not exist.

- [ ] **Step 3: Add the conflict exception and the snapshot state**

In `Engine.cs`, add:

```csharp
/// <summary>
/// A transaction committed against a revision that had already moved. Nothing
/// was published. The caller may retry from a fresh transaction.
/// </summary>
public sealed class ControlPlaneConflictException : Exception
{
    public ControlPlaneConflictException()
        : base("control-plane transaction conflicted with a concurrent commit") { }
}

/// <summary>
/// One coherent set of control-plane rows. A transaction gets a deep copy at
/// begin and installs it wholesale at commit.
/// </summary>
internal sealed class MemoryState
{
    internal Dictionary<string, SessionRecord> Sessions { get; init; } = new();
    internal Dictionary<string, SessionTokenRecord> SessionTokens { get; init; } = new();
    internal Dictionary<string, ResumeTokenRecord> ResumeTokens { get; init; } = new();
    internal Dictionary<string, ApprovalRecord> Approvals { get; init; } = new();
    internal Dictionary<string, LeaseRecord> Leases { get; init; } = new();
    internal Dictionary<string, GraphicalTargetRecord> GraphicalTargets { get; init; } = new();
    internal AuditHead? AuditHead { get; set; }

    /// <summary>
    /// Deep, because the record types are mutable reference types: a shallow
    /// copy would let a transaction mutate a record the committed state still
    /// points at, which is a rollback that does not roll back.
    /// </summary>
    internal MemoryState Clone() => new()
    {
        Sessions = CloneMap(Sessions, CloneSession),
        SessionTokens = CloneMap(SessionTokens, CloneSessionToken),
        ResumeTokens = CloneMap(ResumeTokens, CloneResumeToken),
        Approvals = CloneMap(Approvals, CloneApproval),
        Leases = CloneMap(Leases, CloneLease),
        GraphicalTargets = CloneMap(GraphicalTargets, CloneGraphicalTarget),
        AuditHead = AuditHead is null ? null : new AuditHead { Seq = AuditHead.Seq, RecordHash = AuditHead.RecordHash },
    };

    private static Dictionary<string, T> CloneMap<T>(Dictionary<string, T> source, Func<T, T> clone)
    {
        var copy = new Dictionary<string, T>(source.Count);
        foreach (var (key, value) in source)
        {
            copy[key] = clone(value);
        }
        return copy;
    }
}
```

Write one `Clone*` static per record type, copying every property. There are six.
Do not use reflection or a serializer — a field added later must produce a
compile error here, not a silently uncloned property.

- [ ] **Step 4: Rewrite MemoryEngine and MemoryTx**

`MemoryEngine` holds:

```csharp
    private readonly object _lock = new();
    private MemoryState _committed = new();
    private long _revision;
```

`BeginAsync` takes the lock, deep-clones `_committed`, and records `_revision`:

```csharp
    public Task<ITx> BeginAsync(CancellationToken ct = default)
    {
        lock (_lock)
        {
            return Task.FromResult<ITx>(new MemoryTx(this, _committed.Clone(), _revision));
        }
    }
```

`MemoryTx` gains `internal MemoryState Working { get; }` and
`internal long BeganAtRevision { get; }`, and commit becomes:

```csharp
    public Task CommitAsync(CancellationToken cancellationToken = default)
    {
        RequirePending();
        _engine.Commit(this);
        IsCompleted = true;
        return Task.CompletedTask;
    }
```

with, on `MemoryEngine`:

```csharp
    /// <summary>
    /// Optimistic serializable commit: if the revision moved since this
    /// transaction began, someone else published in the meantime and installing
    /// this working set would drop their write. Fail instead, publishing
    /// nothing. This avoids holding a global lock across arbitrary caller work.
    /// </summary>
    internal void Commit(MemoryTx tx)
    {
        lock (_lock)
        {
            if (tx.BeganAtRevision != _revision)
            {
                throw new ControlPlaneConflictException();
            }

            _committed = tx.Working;
            _revision++;
        }
    }
```

Note the ordering in `MemoryTx.CommitAsync`: `IsCompleted` is set **after**
`_engine.Commit` returns, so a conflicting commit leaves the transaction
pending rather than spent. `Commit_AfterAConflictingCommit_Fails` covers the
throw; if you set the flag first, the transaction is silently unusable
afterwards.

- [ ] **Step 5: Run the tests**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet test tests/Provide.Uterm.Tests --filter FullyQualifiedName~ControlPlaneTransactionTests
```

Expected: still FAIL at build until Task 3 changes the store signatures. That is
expected — Tasks 2 and 3 are one compile unit and are split only because the
snapshot machinery and the 18 signature changes are separately reviewable. Do
not commit between them.

---

### Task 3: All 18 store methods take the transaction

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/ControlPlane/Engine.cs:130-178` (interfaces)
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/ControlPlane/Engine.cs` (memory implementations)

**Interfaces:**
- Consumes: `MemoryTx.Working`, `MemoryTx.BeganAtRevision` from Task 2.
- Produces: every method on `ISessionStore`, `ITokenStore`, `IApprovalStore`,
  `ILeaseStore` and `IGraphicalTargetStore` gains `ITx tx` as its first
  parameter. Task 4 implements the same signatures for SQLite; Task 5 migrates
  the call sites.

- [ ] **Step 1: Change the interface signatures**

Every method gains `ITx tx` first. The full list:

```csharp
public interface ISessionStore
{
    Task UpsertAsync(ITx tx, SessionRecord rec, CancellationToken ct = default);
    Task<SessionRecord?> GetAsync(ITx tx, string sessionId, CancellationToken ct = default);
    Task MarkDeletedAsync(ITx tx, string sessionId, double deletedAt, CancellationToken ct = default);
}

public interface ITokenStore
{
    Task PutSessionTokenAsync(ITx tx, SessionTokenRecord rec, CancellationToken ct = default);
    Task<SessionTokenRecord?> GetSessionTokenAsync(ITx tx, string sessionId, string tokenKind, CancellationToken ct = default);
    Task CreateResumeTokenAsync(ITx tx, ResumeTokenRecord rec, CancellationToken ct = default);
    Task<ResumeTokenRecord?> GetResumeTokenAsync(ITx tx, string tokenValue, CancellationToken ct = default);
    Task RevokeResumeTokenAsync(ITx tx, string tokenValue, double revokedAt, CancellationToken ct = default);
    Task<ResumeTokenRecord?> ConsumeResumeTokenAsync(ITx tx, string tokenValue, double revokedAt, CancellationToken ct = default);
}

public interface IApprovalStore
{
    Task PutApprovalAsync(ITx tx, ApprovalRecord rec, CancellationToken ct = default);
    Task<ApprovalRecord?> GetApprovalAsync(ITx tx, string approvalId, CancellationToken ct = default);
    Task<IReadOnlyList<ApprovalRecord>> ListPendingAsync(ITx tx, CancellationToken ct = default);
}

public interface ILeaseStore
{
    Task PutLeaseAsync(ITx tx, LeaseRecord rec, CancellationToken ct = default);
    Task<LeaseRecord?> GetLeaseAsync(ITx tx, string sessionId, CancellationToken ct = default);
    Task ClearLeaseAsync(ITx tx, string sessionId, CancellationToken ct = default);
}

public interface IGraphicalTargetStore
{
    Task PutAsync(ITx tx, GraphicalTargetRecord rec, CancellationToken ct = default);
    Task<GraphicalTargetRecord?> GetAsync(ITx tx, string targetId, CancellationToken ct = default);
    Task<IReadOnlyList<GraphicalTargetRecord>> ListAsync(ITx tx, CancellationToken ct = default);
    Task<bool> DeleteAsync(ITx tx, string targetId, CancellationToken ct = default);
}
```

Keep the existing XML doc comments on `IGraphicalTargetStore` — including the
tenant-isolation note, which is still accurate and explains a deliberate
omission.

- [ ] **Step 2: Add the shared guard to the memory stores**

Add one internal helper on `MemoryEngine` and call it first in all 18 memory
implementations:

```csharp
    /// <summary>
    /// Reject a transaction this engine did not create, and one that has been
    /// spent. Both are caller bugs that would otherwise write into a working
    /// set nobody will ever install.
    /// </summary>
    internal MemoryTx Working(ITx tx)
    {
        if (tx is not MemoryTx mine || !ReferenceEquals(mine.Owner, this))
        {
            throw new InvalidOperationException("transaction belongs to a different engine");
        }
        if (mine.IsCompleted)
        {
            throw new InvalidOperationException("control-plane transaction is already completed");
        }
        return mine;
    }
```

Each store method then reads and writes `Working(tx).Working` rather than the
engine's dictionaries. For example:

```csharp
    public Task UpsertAsync(ITx tx, SessionRecord rec, CancellationToken ct = default)
    {
        _engine.Working(tx).Working.Sessions[rec.SessionId] = CloneSession(rec);
        return Task.CompletedTask;
    }

    public Task<SessionRecord?> GetAsync(ITx tx, string sessionId, CancellationToken ct = default)
    {
        var state = _engine.Working(tx).Working;
        return Task.FromResult(state.Sessions.TryGetValue(sessionId, out var rec) ? CloneSession(rec) : null);
    }
```

Clone on the way in **and** on the way out. The record types are mutable
reference types, so handing a caller the stored instance lets them mutate
committed state without a transaction — which is the defect wearing a different
hat.

The engine's `_lock` is no longer taken by store methods. Only `BeginAsync`,
`Commit` and `ReapAsync` touch it, because those are the only operations that
read or write `_committed`.

- [ ] **Step 3: Run the transaction tests**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet test tests/Provide.Uterm.Tests --filter FullyQualifiedName~ControlPlaneTransactionTests
```

Expected: PASS, 12 tests. The rest of the suite still fails to build — Task 5
migrates those call sites.

- [ ] **Step 4: Commit**

```bash
git add packages/provide-uterm-csharp/src/Provide.Uterm/ControlPlane/Engine.cs \
        packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/ControlPlaneTransactionTests.cs
git commit -m "feat(csharp)!: control-plane stores take their transaction

BREAKING CHANGE: every store method now takes ITx as its first
parameter. There is deliberately no compatibility overload — one would
let a call site keep the ambient behavior silently, which is the whole
defect.

MemoryTx was a no-op: commit and rollback both set a bool, and the
stores wrote straight through to shared dictionaries, so rolling back
published exactly as much as committing. It now takes a deep snapshot at
begin, serves reads from it, and installs it wholesale at commit only if
the engine revision has not moved. A transaction that lost the race
fails with a conflict and publishes nothing.

Deep clones, because the record types are mutable reference types and a
shallow copy is a rollback that does not roll back."
```

---

### Task 4: SQLite binds the caller's transaction, not an ambient one

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/ControlPlane/SqliteEngine.cs:22`, `:87`, `:204`, `:213`, `:225`

**Interfaces:**
- Consumes: `ITx.Owner`, `ITx.IsCompleted` from Task 1; the store signatures
  from Task 3.
- Produces: `SqliteTx` exposing its `SqliteConnection` and `SqliteTransaction`
  to the engine internally. `_current` is deleted.

- [ ] **Step 1: Write the failing test**

Add to `ControlPlaneTransactionTests.cs`. Follow whatever the existing SQLite
tests in the package use to get a temp database path:

```csharp
    [Fact]
    public async Task Sqlite_ConcurrentTransactionsDoNotJoin()
    {
        // The ambient-slot defect, expressed as a test: a second caller's write
        // must not be captured by, and must not vanish with, the first
        // caller's rollback.
        var path = Path.Combine(Path.GetTempPath(), $"uterm-cp-{Guid.NewGuid():N}.db");
        try
        {
            var engine = new SqliteEngine(path);
            await engine.OpenAsync();
            await engine.MigrateAsync();

            var a = await engine.BeginAsync();
            var b = await engine.BeginAsync();

            await engine.Sessions().UpsertAsync(b, Session("written-by-b"));
            await b.CommitAsync();
            await a.RollbackAsync();

            var check = await engine.BeginAsync();
            Assert.NotNull(await engine.Sessions().GetAsync(check, "written-by-b"));
            await check.RollbackAsync();
            await engine.CloseAsync();
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public async Task Sqlite_RejectsATransactionFromAnotherEngine()
    {
        var memory = await OpenMemoryAsync();
        var foreign = await memory.BeginAsync();

        var path = Path.Combine(Path.GetTempPath(), $"uterm-cp-{Guid.NewGuid():N}.db");
        try
        {
            var engine = new SqliteEngine(path);
            await engine.OpenAsync();
            await engine.MigrateAsync();

            await Assert.ThrowsAsync<InvalidOperationException>(
                () => engine.Sessions().UpsertAsync(foreign, Session("s1")));

            await engine.CloseAsync();
        }
        finally
        {
            File.Delete(path);
        }
    }
```

- [ ] **Step 2: Run to verify it fails**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet test tests/Provide.Uterm.Tests --filter FullyQualifiedName~Sqlite_
```

Expected: FAIL — the SQLite store methods do not yet take `ITx`.

- [ ] **Step 3: Rewrite SqliteTx and Command()**

`SqliteTx` owns one connection and one transaction:

```csharp
public sealed class SqliteTx : ITx
{
    private readonly SqliteEngine _engine;

    internal SqliteTx(SqliteEngine engine, SqliteConnection connection, SqliteTransaction inner)
    {
        _engine = engine;
        Connection = connection;
        Inner = inner;
    }

    internal SqliteConnection Connection { get; }
    internal SqliteTransaction Inner { get; }

    public IEngine Owner => _engine;
    public bool IsCompleted { get; private set; }

    public async Task CommitAsync(CancellationToken cancellationToken = default)
    {
        RequirePending();
        await Inner.CommitAsync(cancellationToken);
        IsCompleted = true;
        Dispose();
    }

    public async Task RollbackAsync(CancellationToken cancellationToken = default)
    {
        RequirePending();
        await Inner.RollbackAsync(cancellationToken);
        IsCompleted = true;
        Dispose();
    }

    private void RequirePending()
    {
        if (IsCompleted)
        {
            throw new InvalidOperationException("control-plane transaction is already completed");
        }
    }

    private void Dispose()
    {
        Inner.Dispose();
        Connection.Dispose();
    }
}
```

`Command()` takes the transaction and binds both its connection and its
transaction:

```csharp
    /// <summary>
    /// Build a command bound to the caller's transaction. The engine used to
    /// keep an ambient `_current` slot and bind that, so any statement issued
    /// while some other request happened to be mid-transaction joined it — and
    /// committed or vanished with that unrelated caller.
    /// </summary>
    internal SqliteCommand Command(ITx tx, string sql)
    {
        if (tx is not SqliteTx mine || !ReferenceEquals(mine.Owner, this))
        {
            throw new InvalidOperationException("transaction belongs to a different engine");
        }
        if (mine.IsCompleted)
        {
            throw new InvalidOperationException("control-plane transaction is already completed");
        }

        var cmd = mine.Connection.CreateCommand();
        cmd.Transaction = mine.Inner;
        cmd.CommandText = sql;
        return cmd;
    }
```

`BeginAsync` opens a fresh connection per transaction rather than sharing one:

```csharp
    public async Task<ITx> BeginAsync(CancellationToken ct = default)
    {
        var connection = new SqliteConnection(_connectionString);
        await connection.OpenAsync(ct);
        var inner = (SqliteTransaction)await connection.BeginTransactionAsync(ct);
        return new SqliteTx(this, connection, inner);
    }
```

Delete the `_current` field and every reference to it.

- [ ] **Step 4: Update the 18 SQLite store implementations**

Each takes `ITx tx` and passes it to `Command(tx, sql)`. No other change.

- [ ] **Step 5: Verify the ambient slot is gone**

Run:
```bash
cd /Volumes/data/pyv/provide-uterm
grep -n "_current" packages/provide-uterm-csharp/src/Provide.Uterm/ControlPlane/SqliteEngine.cs
```

Expected: no output.

- [ ] **Step 6: Run the tests**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet test tests/Provide.Uterm.Tests --filter FullyQualifiedName~ControlPlaneTransactionTests
```

Expected: PASS, 14 tests.

- [ ] **Step 7: Commit**

```bash
git add packages/provide-uterm-csharp/src/Provide.Uterm/ControlPlane/SqliteEngine.cs \
        packages/provide-uterm-csharp/tests/Provide.Uterm.Tests/ControlPlaneTransactionTests.cs
git commit -m "fix(csharp)!: SQLite binds the caller's transaction, not an ambient slot

BREAKING CHANGE: SqliteEngine store methods take ITx.

Command() bound cmd.Transaction to an ambient _current field, so a
statement issued while some unrelated request happened to be between
BeginAsync and CommitAsync joined that request's transaction. Its writes
committed when the other caller committed, or vanished when the other
caller rolled back — with nothing in either call path suggesting the two
were connected.

Each transaction now owns its own connection, and Command() takes the
transaction it is for."
```

---

### Task 5: Migrate every call site

**Files:**
- Modify: the 6 files returned by the grep in Step 1.

**Interfaces:**
- Consumes: the new store signatures from Tasks 3 and 4.
- Produces: nothing new.

- [ ] **Step 1: Enumerate the call sites**

Run:
```bash
cd /Volumes/data/pyv/provide-uterm
grep -rln "\.Sessions()\|\.Tokens()\|\.Approvals()\|\.Leases()\|\.GraphicalTargets()" \
  packages/provide-uterm-csharp --include="*.cs"
```

Expected: 6 files. The compiler is the real enumerator — build and let it list
every error.

- [ ] **Step 2: Build and collect every error**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet build 2>&1 | grep -E "error CS" | sort -u
```

Every error is a call site that must now name its transaction. This is the
migration working as designed: the design says "Migration errors should be
compile-time errors: every store operation must receive the transaction it
belongs to."

- [ ] **Step 3: Migrate each call site**

The shape at each site is begin, use, commit:

```csharp
var tx = await engine.BeginAsync(ct);
try
{
    var session = await engine.Sessions().GetAsync(tx, sessionId, ct);
    // ... further reads and writes on the same tx ...
    await tx.CommitAsync(ct);
}
catch
{
    await tx.RollbackAsync(ct);
    throw;
}
```

Two rules while migrating:

- **Do not open a transaction per store call.** A read and the write that
  depends on it belong in one transaction, or the isolation this plan adds
  buys nothing. Where a call site currently does read-then-write, that is one
  transaction.
- **Do not swallow `ControlPlaneConflictException`.** A conflict means the
  caller's write did not happen. Either retry from a fresh transaction or let
  it propagate; reporting success is the failure mode the design names —
  "success never means merely queued or attempted."

- [ ] **Step 4: Build clean**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet build
```

Expected: no errors.

- [ ] **Step 5: Run the full suite and the gate**

Run:
```bash
cd packages/provide-uterm-csharp
dotnet test tests/Provide.Uterm.Tests
make quality-gate
```

Expected: PASS, including coverage and the Stryker mutation gate.

- [ ] **Step 6: Verify the migration is complete**

Run:
```bash
cd /Volumes/data/pyv/provide-uterm
grep -rc "ITx tx" packages/provide-uterm-csharp/src/Provide.Uterm/ControlPlane/Engine.cs
```

Expected: at least 18. Before this plan the count across all of `src` was 0.

- [ ] **Step 7: Commit**

```bash
git add packages/provide-uterm-csharp/src/
git commit -m "refactor(csharp)!: migrate every control-plane call site to explicit transactions

BREAKING CHANGE: callers pass the transaction they opened.

Read-then-write sequences are one transaction rather than two, which is
the point of the change: isolating each call separately would leave the
same lost-update window the ambient slot had.

ControlPlaneConflictException propagates. A conflict means the write did
not happen, and reporting success would be worse than the defect this
replaces."
```

---

### Task 6: Reaping runs in one transaction, and cross-language scenarios

**Files:**
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/ControlPlane/Engine.cs` (`MemoryEngine.ReapAsync`)
- Modify: `packages/provide-uterm-csharp/src/Provide.Uterm/ControlPlane/SqliteEngine.cs` (`ReapAsync`)
- Create: `spec/control_plane_tx_scenarios.json`
- Create: `tests/conformance/test_control_plane_tx_parity.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `spec/control_plane_tx_scenarios.json`.

The measurement recorded `ReapAsync` as present in both engines but did not
verify its entity scope. This task establishes it by fixture.

- [ ] **Step 1: Write the scenario file**

Read `spec/session_lifecycle_security_scenarios.json` and follow its schema.
The design requires reaping to remove, in one transaction: expired sessions;
expired or revoked session and resume tokens; expired or deleted leases; and
resolved approvals past their retention window.

| Scenario ID | Expected |
|---|---|
| `cptx_001_read_your_writes` | a read inside a transaction sees that transaction's write |
| `cptx_002_rollback_publishes_nothing` | after rollback, a fresh transaction sees nothing |
| `cptx_003_uncommitted_invisible` | a concurrent transaction does not see uncommitted writes |
| `cptx_004_conflicting_commit_fails` | second commit from the same begin-revision fails, publishes nothing |
| `cptx_005_reap_removes_expired_sessions` | past-cutoff deleted sessions gone, in-window ones kept |
| `cptx_006_reap_removes_expired_and_revoked_tokens` | both session and resume tokens, both conditions |
| `cptx_007_reap_removes_expired_and_deleted_leases` | both conditions |
| `cptx_008_reap_removes_resolved_approvals_past_retention` | resolved-and-past only; pending kept regardless of age |
| `cptx_009_reap_is_idempotent` | a second reap at the same instant removes zero |
| `cptx_010_reap_boundary_is_exclusive` | a record exactly at the cutoff is kept |

`cptx_010` fixes the boundary the design says fixtures must pin. Pick one
convention, write it down, and make all backends match.

- [ ] **Step 2: Run against Python and observe**

Run: `uv run pytest tests/conformance/test_control_plane_tx_parity.py -v`

Expected: PASS. Python is the oracle. If a reaping scenario fails against
Python, read its implementation before changing the scenario — the design allows
correcting the contract when a fixture exposes a Python defect, but that is a
decision to make deliberately and record, not a default.

- [ ] **Step 3: Run against C# and fix the gaps**

Run: `uv run pytest tests/conformance/test_control_plane_tx_parity.py -v`

Expected: any C# reaping scenario that fails names an entity the C# reaper
misses. Fix `ReapAsync` in both engines to cover it, running the whole reap in
one transaction.

- [ ] **Step 4: Run against Go and TypeScript**

The measurement marked Go and TypeScript reaping ⚠️ — present but scope
unverified. These scenarios are what verifies it. Any failure is a real finding;
record it and fix it in that port.

- [ ] **Step 5: Full gate**

Run:
```bash
cd packages/provide-uterm-csharp && make quality-gate
cd /Volumes/data/pyv/provide-uterm && uv run pytest tests/conformance/ -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add spec/control_plane_tx_scenarios.json \
        tests/conformance/test_control_plane_tx_parity.py \
        packages/provide-uterm-csharp/src/Provide.Uterm/ControlPlane/
git commit -m "test(conformance): shared control-plane transaction and reaping scenarios

Ten scenarios covering isolation, rollback, conflict, and what reaping
removes. The reaping ones exist because the measurement could confirm
ReapAsync was present in both C# engines but not what it actually
removed — 'a method with the right name' is not evidence.

cptx_010 pins the cutoff boundary as exclusive, which the design asks
fixtures to fix and which nothing previously stated."
```

---

## Definition of done

Per the measurement spec, CM-03 closes when:

- `grep -rn "_current" packages/provide-uterm-csharp/src/Provide.Uterm/ControlPlane/SqliteEngine.cs`
  returns nothing;
- `grep -rc "ITx tx" .../ControlPlane/Engine.cs` returns at least 18, where the
  whole-`src` count was 0;
- `spec/control_plane_tx_scenarios.json` passes on every backend that has a
  control plane;
- the conflict and rollback scenarios were observed failing against the pre-fix
  `MemoryTx`;
- `dotnet test` and `make quality-gate` pass, mutation gate included.

Then update the CM-03 row, the reaping row, and the Status date in
`docs/superpowers/specs/2026-08-03-uterm-convergence-measurement-design.md`.

## Notes for the implementer

- Land CM-10 first. Adding this much code to a build with
  `TreatWarningsAsErrors=false` means new nullable warnings arrive unnoticed.
- The optimistic scheme is whole-state, so any two overlapping transactions
  conflict even when they touch different records. That is deliberate for now:
  it is correct, it is simple, and the control plane is not a hot path. If
  conflicts become a real problem, narrow the check to touched keys — but only
  with a fixture that demonstrates the contention first.
- `Clone*` helpers must be written by hand, one per record type. A reflection or
  serializer-based clone means a property added later is silently not cloned,
  and the failure mode is a rollback that leaves part of its writes behind.
- Task 2 and Task 3 do not compile independently. They are separate tasks
  because they are separately reviewable, not separately committable — Task 2's
  commit step is deliberately absent.
