//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Microsoft.Data.Sqlite;
using Provide.Uterm.ControlPlane;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>Control-plane engine failure arms: open, begin and close races.</summary>
public sealed class Coverage100ControlPlaneTests
{
    [Fact]
    public async Task Open_of_an_unreachable_sqlite_path_closes_the_engine_and_rethrows()
    {
        // The "directory" is a regular file, so creating it fails on every OS.
        var blocker = Path.GetTempFileName();
        try
        {
            var path = Path.Combine(blocker, "cp.db");
            var ex = await Assert.ThrowsAsync<SqliteConnectionException>(() => Bootstrap.OpenAsync("sqlite", path));
            Assert.Contains("failed to initialize sqlite control-plane connection", ex.Message, StringComparison.Ordinal);
        }
        finally
        {
            File.Delete(blocker);
        }
    }

    [Fact]
    public async Task Begin_that_sqlite_refuses_releases_the_transaction_gate()
    {
        var engine = new SqliteEngine(":memory:");
        await engine.OpenAsync();
        try
        {
            // A transaction the engine did not start: SQLite refuses a nested BEGIN.
            await using (var begin = engine.Command())
            {
                begin.CommandText = "BEGIN";
                await begin.ExecuteNonQueryAsync();
            }

            await Assert.ThrowsAnyAsync<Exception>(() => engine.BeginAsync());

            await using (var commit = engine.Command())
            {
                commit.CommandText = "COMMIT";
                await commit.ExecuteNonQueryAsync();
            }

            // The gate was released on failure, so this neither waits nor fails.
            var tx = await engine.BeginAsync().WaitAsync(TimeSpan.FromSeconds(5));
            await tx.CommitAsync();
            Assert.True(((SqliteTx)tx).IsDone);
        }
        finally
        {
            await engine.CloseAsync();
        }
    }

    [Fact]
    public async Task Finishing_a_transaction_the_engine_no_longer_tracks_leaves_the_engine_alone()
    {
        var engine = new SqliteEngine(":memory:");
        await engine.OpenAsync();
        var tx = await engine.BeginAsync();
        // Close forgets the pending transaction; finishing it afterwards must
        // not touch the engine's (now absent) current transaction.
        await engine.CloseAsync();
        try
        {
            await tx.RollbackAsync();
        }
        catch (Exception ex) when (ex is InvalidOperationException or ObjectDisposedException or SqliteException)
        {
            // The connection is gone; what matters is that the tx is finished.
        }

        Assert.True(((SqliteTx)tx).IsDone);
    }

    [Fact]
    public async Task Memory_engine_rollback_consume_twice_and_pending_ordering()
    {
        var tx = new MemoryTx();
        Assert.False(tx.IsDone);
        await tx.RollbackAsync();
        Assert.True(tx.IsDone);

        var engine = new MemoryEngine();
        var tokens = engine.Tokens();
        await tokens.CreateResumeTokenAsync(new ResumeTokenRecord { TokenValue = "t1", SessionId = "s", Role = "viewer" });
        Assert.NotNull(await tokens.ConsumeResumeTokenAsync("t1", 1.0));
        Assert.Null(await tokens.ConsumeResumeTokenAsync("t1", 2.0));
        Assert.Null(await tokens.ConsumeResumeTokenAsync("missing", 2.0));

        var approvals = engine.Approvals();
        await approvals.PutApprovalAsync(new ApprovalRecord { ApprovalId = "b", CreatedAt = 5 });
        await approvals.PutApprovalAsync(new ApprovalRecord { ApprovalId = "a", CreatedAt = 5 });
        await approvals.PutApprovalAsync(new ApprovalRecord { ApprovalId = "c", CreatedAt = 1 });
        await approvals.PutApprovalAsync(new ApprovalRecord { ApprovalId = "d", CreatedAt = 0, State = "approved" });
        var pending = await approvals.ListPendingAsync();
        Assert.Equal(new[] { "c", "a", "b" }, pending.Select(p => p.ApprovalId));
    }
}
