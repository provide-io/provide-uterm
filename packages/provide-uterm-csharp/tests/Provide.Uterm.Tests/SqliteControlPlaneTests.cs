//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Microsoft.Data.Sqlite;
using Provide.Uterm.ControlPlane;

namespace Provide.Uterm.Tests;

/// <summary>
/// SQLite control-plane engine tests. Every store is exercised against a real
/// database file so the SQL, the column order and the null handling are all
/// covered by behaviour rather than by inspection.
/// </summary>
public sealed class SqliteControlPlaneTests
{
    private static async Task<(SqliteEngine Engine, string Path)> OpenAsync()
    {
        var path = Path.Combine(Path.GetTempPath(), $"cp-{Guid.NewGuid():N}.db");
        var engine = new SqliteEngine(path) { Now = () => 1000.0 };
        await engine.OpenAsync();
        await engine.MigrateAsync();
        return (engine, path);
    }

    private static void Cleanup(string path) => SqliteTestDb.Delete(path);

    [Fact]
    public void Capabilities_ReportDurableSqlite()
    {
        var caps = new SqliteEngine(":memory:").Capabilities();
        Assert.True(caps.Durable);
        Assert.True(caps.Sqlite);
        Assert.True(caps.AuditChain);
    }

    [Theory]
    [InlineData(":memory:", ":memory:")]
    [InlineData("file::memory:", ":memory:")]
    [InlineData("sqlite:///tmp/cp.db", "/tmp/cp.db")]
    [InlineData("sqlite+aiosqlite:///tmp/cp.db", "/tmp/cp.db")]
    [InlineData("/var/lib/cp.db", "/var/lib/cp.db")]
    public void ResolveDatabasePath_NormalizesUrls(string input, string expected) =>
        Assert.Equal(expected, SqliteEngine.ResolveDatabasePath(input));

    [Fact]
    public async Task Migrate_CreatesEveryTableAndRecordsVersions()
    {
        var (engine, path) = await OpenAsync();
        await engine.CloseAsync();

        var tables = new List<string>();
        var versions = new List<long>();

        // Scoped so the connection is closed before Cleanup: a method-scoped
        // `await using` would still be holding the file when the delete runs.
        await using (var raw = SqliteTestDb.Connect(path))
        {
            await raw.OpenAsync();

            await using (var cmd = raw.CreateCommand())
            {
                cmd.CommandText = "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name";
                await using var r = await cmd.ExecuteReaderAsync();
                while (await r.ReadAsync())
                {
                    tables.Add(r.GetString(0));
                }
            }

            await using (var cmd = raw.CreateCommand())
            {
                cmd.CommandText = "SELECT version FROM cp_schema_version ORDER BY version";
                await using var r = await cmd.ExecuteReaderAsync();
                while (await r.ReadAsync())
                {
                    versions.Add(r.GetInt64(0));
                }
            }
        }

        foreach (var want in new[]
                 {
                     "cp_schema_version", "cp_sessions", "cp_session_tokens", "cp_resume_tokens",
                     "cp_approvals", "cp_leases", "cp_audit_head", "cp_graphical_targets",
                 })
        {
            Assert.Contains(want, tables);
        }

        Assert.Equal([1L, 2L, 3L], versions);
        Cleanup(path);
    }

    [Fact]
    public async Task Migrate_IsIdempotent()
    {
        var (engine, path) = await OpenAsync();
        await engine.MigrateAsync();
        await engine.MigrateAsync();
        var head = await engine.GetAuditHeadAsync();
        Assert.Null(head);
        await engine.CloseAsync();
        Cleanup(path);
    }

    [Fact]
    public async Task Migrate_RejectsInvalidTableName()
    {
        // Exercised through the internal runner: a non-identifier name must be
        // refused before it can reach string interpolation into SQL.
        Assert.False(Migration.IsIdentifier("no-hyphens"));
        Assert.False(Migration.IsIdentifier(""));
        Assert.False(Migration.IsIdentifier("9leading"));
        Assert.True(Migration.IsIdentifier("cp_schema_version"));
        await Task.CompletedTask;
    }

    [Fact]
    public async Task Migrate_NonIdentifierTable_ThrowsBeforeTouchingSql()
    {
        await using var conn = new SqliteConnection("Data Source=:memory:");
        await conn.OpenAsync();
        var ex = await Assert.ThrowsAsync<SqliteMigrationException>(
            () => Migration.ApplyAsync(conn, "cp-schema-version", 1000.0));
        Assert.Contains("invalid migration table name", ex.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Migrate_SqlFailure_RollsBackAndWraps()
    {
        // query_only turns the first DDL statement into a SqliteException, which
        // ApplyAsync must translate into SqliteMigrationException after a
        // best-effort ROLLBACK (itself a no-op here — no transaction is open).
        await using var conn = new SqliteConnection("Data Source=:memory:");
        await conn.OpenAsync();
        await using (var pragma = conn.CreateCommand())
        {
            pragma.CommandText = "PRAGMA query_only = 1";
            await pragma.ExecuteNonQueryAsync();
        }

        var ex = await Assert.ThrowsAsync<SqliteMigrationException>(
            () => Migration.ApplyAsync(conn, "cp_schema_version", 1000.0));
        Assert.Contains("failed to apply control-plane migration", ex.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Migrate_SqlFailureInsideTransaction_RollsBackForReal()
    {
        // Same wrap, but with a transaction actually open, so the ROLLBACK in the
        // handler succeeds instead of raising "no transaction is active". A
        // pre-existing cp_schema_version without a `version` column makes the
        // CREATE TABLE IF NOT EXISTS a no-op and the version probe fail.
        await using var conn = new SqliteConnection("Data Source=:memory:");
        await conn.OpenAsync();
        foreach (var sql in new[] { "CREATE TABLE cp_schema_version (foo INTEGER)", "BEGIN" })
        {
            await using var cmd = conn.CreateCommand();
            cmd.CommandText = sql;
            await cmd.ExecuteNonQueryAsync();
        }

        var ex = await Assert.ThrowsAsync<SqliteMigrationException>(
            () => Migration.ApplyAsync(conn, "cp_schema_version", 1000.0));
        Assert.Contains("failed to apply control-plane migration", ex.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task SessionStore_RoundTripsAndSoftDeletes()
    {
        var (engine, path) = await OpenAsync();
        var sessions = engine.Sessions();

        await sessions.UpsertAsync(new SessionRecord
        {
            SessionId = "s1",
            DisplayName = "demo",
            ConnectorType = "pty",
            Owner = "alice",
            Visibility = "private",
            LifecycleState = "running",
            CreatedAt = 1,
            UpdatedAt = 2,
        });

        var got = await sessions.GetAsync("s1");
        Assert.NotNull(got);
        Assert.Equal("demo", got!.DisplayName);
        Assert.Equal("pty", got.ConnectorType);
        Assert.Equal("alice", got.Owner);
        Assert.Equal("running", got.LifecycleState);
        Assert.Null(got.DeletedAt);
        Assert.Null(await sessions.GetAsync("missing"));

        // Upsert replaces rather than duplicating, and a null owner round-trips.
        await sessions.UpsertAsync(new SessionRecord
        {
            SessionId = "s1", DisplayName = "renamed", ConnectorType = "ssh",
            Owner = null, Visibility = "public", LifecycleState = "stopped",
            CreatedAt = 1, UpdatedAt = 3,
        });
        var updated = await sessions.GetAsync("s1");
        Assert.Equal("renamed", updated!.DisplayName);
        Assert.Null(updated.Owner);

        await sessions.MarkDeletedAsync("s1", 10);
        var deleted = await sessions.GetAsync("s1");
        Assert.Equal("deleted", deleted!.LifecycleState);
        Assert.Equal(10, deleted.DeletedAt);

        // Reap drops rows past the retention cutoff, and only those.
        Assert.Equal(1, await engine.ReapAsync(now: 1000, retentionS: 10));
        Assert.Null(await sessions.GetAsync("s1"));

        await engine.CloseAsync();
        Cleanup(path);
    }

    [Fact]
    public async Task TokenStore_SessionTokensAndResumeTokens()
    {
        var (engine, path) = await OpenAsync();
        var tokens = engine.Tokens();

        await tokens.PutSessionTokenAsync(new SessionTokenRecord
        {
            SessionId = "s1", TokenKind = "player", TokenValue = "tv",
            CreatedAt = 1, ExpiresAt = 99,
        });
        var st = await tokens.GetSessionTokenAsync("s1", "player");
        Assert.Equal("tv", st!.TokenValue);
        Assert.Equal(99, st.ExpiresAt);
        Assert.Null(st.RevokedAt);
        Assert.Null(await tokens.GetSessionTokenAsync("s1", "absent"));

        // A null expiry round-trips as null rather than 0.
        await tokens.PutSessionTokenAsync(new SessionTokenRecord
        {
            SessionId = "s2", TokenKind = "player", TokenValue = "tv2", CreatedAt = 1,
        });
        Assert.Null((await tokens.GetSessionTokenAsync("s2", "player"))!.ExpiresAt);

        await tokens.CreateResumeTokenAsync(new ResumeTokenRecord
        {
            TokenValue = "rt", SessionId = "s1", Role = "player",
            CreatedAt = 1, ExpiresAt = 50, WasHijackOwner = true,
        });
        var rt = await tokens.GetResumeTokenAsync("rt");
        Assert.True(rt!.WasHijackOwner);
        Assert.Equal("player", rt.Role);
        Assert.Null(await tokens.GetResumeTokenAsync("absent"));

        await engine.CloseAsync();
        Cleanup(path);
    }

    [Fact]
    public async Task ResumeToken_ConsumeIsSingleUse()
    {
        var (engine, path) = await OpenAsync();
        var tokens = engine.Tokens();
        await tokens.CreateResumeTokenAsync(new ResumeTokenRecord
        {
            TokenValue = "rt", SessionId = "s1", Role = "player", CreatedAt = 1, ExpiresAt = 50,
        });

        var first = await tokens.ConsumeResumeTokenAsync("rt", 51);
        Assert.NotNull(first);
        // Second consume returns null: the UPDATE ... WHERE revoked_at IS NULL
        // is what makes it single-use.
        Assert.Null(await tokens.ConsumeResumeTokenAsync("rt", 52));
        // And a consumed token no longer reads back.
        Assert.Null(await tokens.GetResumeTokenAsync("rt"));

        await tokens.CreateResumeTokenAsync(new ResumeTokenRecord
        {
            TokenValue = "rt2", SessionId = "s1", Role = "player", CreatedAt = 1, ExpiresAt = 50,
        });
        await tokens.RevokeResumeTokenAsync("rt2", 52);
        Assert.Null(await tokens.GetResumeTokenAsync("rt2"));
        Assert.Null(await tokens.ConsumeResumeTokenAsync("rt2", 53));

        await engine.CloseAsync();
        Cleanup(path);
    }

    [Fact]
    public async Task ApprovalStore_PendingOrderedByCreatedThenId()
    {
        var (engine, path) = await OpenAsync();
        var approvals = engine.Approvals();

        await approvals.PutApprovalAsync(new ApprovalRecord
        {
            ApprovalId = "b", SessionId = "s1", Command = "ls", State = "pending", CreatedAt = 1,
        });
        await approvals.PutApprovalAsync(new ApprovalRecord
        {
            ApprovalId = "a", SessionId = "s1", Command = "ls", State = "pending", CreatedAt = 1,
        });
        await approvals.PutApprovalAsync(new ApprovalRecord
        {
            ApprovalId = "c", SessionId = "s1", Command = "rm", State = "approved",
            CreatedAt = 0, ResolvedAt = 5, ResolvedBy = "ops", RequestedBy = "alice",
        });

        var pending = await approvals.ListPendingAsync();
        Assert.Equal(["a", "b"], pending.Select(p => p.ApprovalId));

        var resolved = await approvals.GetApprovalAsync("c");
        Assert.Equal("approved", resolved!.State);
        Assert.Equal(5, resolved.ResolvedAt);
        Assert.Equal("ops", resolved.ResolvedBy);
        Assert.Equal("alice", resolved.RequestedBy);
        Assert.Null(await approvals.GetApprovalAsync("absent"));

        await engine.CloseAsync();
        Cleanup(path);
    }

    [Fact]
    public async Task LeaseStore_ClearIsASoftDelete()
    {
        var (engine, path) = await OpenAsync();
        var leases = engine.Leases();

        await leases.PutLeaseAsync(new LeaseRecord
        {
            SessionId = "s1", HijackId = "h1", Owner = "alice",
            LeaseExpiresAt = 9, CreatedAt = 1,
        });
        var got = await leases.GetLeaseAsync("s1");
        Assert.Equal("h1", got!.HijackId);
        Assert.Equal("alice", got.Owner);
        Assert.Null(await leases.GetLeaseAsync("absent"));

        await leases.ClearLeaseAsync("s1");
        // Cleared reads as absent even though the row is still there.
        Assert.Null(await leases.GetLeaseAsync("s1"));

        await engine.CloseAsync();
        Cleanup(path);
    }

    [Fact]
    public async Task AuditHead_IsMonotonic()
    {
        var (engine, path) = await OpenAsync();
        Assert.Null(await engine.GetAuditHeadAsync());

        await engine.SetAuditHeadAsync(5, "hash5");
        Assert.Equal(5, (await engine.GetAuditHeadAsync())!.Seq);

        // Lower-or-equal seq is ignored — the anti-rollback guard.
        await engine.SetAuditHeadAsync(5, "ignored");
        await engine.SetAuditHeadAsync(1, "ignored");
        var head = await engine.GetAuditHeadAsync();
        Assert.Equal(5, head!.Seq);
        Assert.Equal("hash5", head.RecordHash);

        await engine.SetAuditHeadAsync(6, "hash6");
        Assert.Equal("hash6", (await engine.GetAuditHeadAsync())!.RecordHash);

        await engine.CloseAsync();
        Cleanup(path);
    }

    [Fact]
    public async Task Transaction_CommitPersistsAndRollbackDiscards()
    {
        var (engine, path) = await OpenAsync();

        var tx = await engine.BeginAsync();
        await engine.Sessions().UpsertAsync(new SessionRecord
        {
            SessionId = "keep", DisplayName = "d", ConnectorType = "pty", CreatedAt = 1, UpdatedAt = 1,
        });
        await tx.CommitAsync();
        Assert.True(((SqliteTx)tx).IsDone);
        // Commit and rollback are both idempotent once done.
        await tx.CommitAsync();
        await tx.RollbackAsync();

        var tx2 = await engine.BeginAsync();
        await engine.Sessions().UpsertAsync(new SessionRecord
        {
            SessionId = "drop", DisplayName = "d", ConnectorType = "pty", CreatedAt = 1, UpdatedAt = 1,
        });
        await tx2.RollbackAsync();

        Assert.NotNull(await engine.Sessions().GetAsync("keep"));
        Assert.Null(await engine.Sessions().GetAsync("drop"));

        await engine.CloseAsync();
        Cleanup(path);
    }

    [Fact]
    public async Task Operations_BeforeOpenFail()
    {
        var engine = new SqliteEngine(":memory:");
        await Assert.ThrowsAsync<SqliteConnectionException>(() => engine.MigrateAsync());
        await Assert.ThrowsAsync<SqliteConnectionException>(async () => await engine.BeginAsync());
    }

    [Fact]
    public async Task OpenAndCloseAreIdempotent()
    {
        var (engine, path) = await OpenAsync();
        await engine.OpenAsync();
        await engine.CloseAsync();
        await engine.CloseAsync();
        await engine.DisposeAsync();
        Cleanup(path);
    }

    [Fact]
    public async Task Data_SurvivesReopen()
    {
        var (engine, path) = await OpenAsync();
        await engine.Sessions().UpsertAsync(new SessionRecord
        {
            SessionId = "s1", DisplayName = "durable", ConnectorType = "pty",
            CreatedAt = 1, UpdatedAt = 1,
        });
        await engine.CloseAsync();

        var reopened = new SqliteEngine(path);
        await reopened.OpenAsync();
        await reopened.MigrateAsync();
        var got = await reopened.Sessions().GetAsync("s1");
        Assert.Equal("durable", got!.DisplayName);
        await reopened.CloseAsync();
        Cleanup(path);
    }
}
