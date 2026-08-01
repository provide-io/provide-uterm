//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Microsoft.Data.Sqlite;
using Provide.Uterm.ControlPlane;

namespace Provide.Uterm.Tests;

/// <summary>
/// Graphical-target store tests. Runs against both backends through the same
/// assertions so the memory engine cannot silently drift from the SQLite one —
/// the two are meant to be interchangeable behind the registry.
/// </summary>
public sealed class SqliteGraphicalTargetStoreTests
{
    private static GraphicalTargetRecord Record(string targetId = "gt-1") => new()
    {
        TargetId = targetId,
        TenantId = "acme",
        DisplayName = "console",
        Protocol = "rfb",
        Width = 640,
        Height = 480,
        Config = """{"vm_name":"vm-1"}""",
        CreatedAt = 100,
    };

    public static TheoryData<string> Backends => new() { "memory", "sqlite" };

    private static async Task<(IEngine Engine, string? Path)> OpenAsync(string backend)
    {
        if (backend == "memory")
        {
            var mem = new MemoryEngine();
            await mem.OpenAsync();
            return (mem, null);
        }

        var path = Path.Combine(Path.GetTempPath(), $"gt-{Guid.NewGuid():N}.db");
        var engine = new SqliteEngine(path) { Now = () => 1000.0 };
        await engine.OpenAsync();
        await engine.MigrateAsync();
        return (engine, path);
    }

    private static async Task CloseAsync(IEngine engine, string? path)
    {
        await engine.CloseAsync();
        if (path is null)
        {
            return;
        }

        SqliteTestDb.Delete(path);
    }

    [Theory]
    [MemberData(nameof(Backends))]
    public async Task PutThenGet_RoundTrips(string backend)
    {
        var (engine, path) = await OpenAsync(backend);
        var store = engine.GraphicalTargets();

        await store.PutAsync(Record());
        var got = await store.GetAsync("gt-1");

        Assert.NotNull(got);
        Assert.Equal("gt-1", got!.TargetId);
        Assert.Equal("acme", got.TenantId);
        Assert.Equal(640, got.Width);
        Assert.Equal("""{"vm_name":"vm-1"}""", got.Config);
        Assert.Null(await store.GetAsync("missing"));

        await CloseAsync(engine, path);
    }

    [Theory]
    [MemberData(nameof(Backends))]
    public async Task Put_IsUpsert(string backend)
    {
        var (engine, path) = await OpenAsync(backend);
        var store = engine.GraphicalTargets();

        await store.PutAsync(Record());
        var second = Record();
        second.DisplayName = "renamed";
        second.UpdatedAt = 200;
        second.UpdatedBy = "ops";
        await store.PutAsync(second);

        var got = await store.GetAsync("gt-1");
        Assert.Equal("renamed", got!.DisplayName);
        Assert.Equal(200, got.UpdatedAt);
        Assert.Equal("ops", got.UpdatedBy);
        // Upsert, not insert — exactly one row survives.
        Assert.Single(await store.ListAsync());

        await CloseAsync(engine, path);
    }

    [Theory]
    [MemberData(nameof(Backends))]
    public async Task List_IsOrderedByTargetId(string backend)
    {
        var (engine, path) = await OpenAsync(backend);
        var store = engine.GraphicalTargets();

        foreach (var id in new[] { "gt-c", "gt-a", "gt-b" })
        {
            await store.PutAsync(Record(id));
        }

        var rows = await store.ListAsync();
        Assert.Equal(["gt-a", "gt-b", "gt-c"], rows.Select(t => t.TargetId));

        await CloseAsync(engine, path);
    }

    [Theory]
    [MemberData(nameof(Backends))]
    public async Task Delete_ReportsWhetherARowWent(string backend)
    {
        var (engine, path) = await OpenAsync(backend);
        var store = engine.GraphicalTargets();
        await store.PutAsync(Record());

        Assert.True(await store.DeleteAsync("gt-1"));
        Assert.False(await store.DeleteAsync("gt-1"));
        Assert.Empty(await store.ListAsync());

        await CloseAsync(engine, path);
    }

    /// <summary>
    /// The INTEGER 0/1 columns and the nullable TEXT columns together — the two
    /// places a column-order or type slip would show up.
    /// </summary>
    [Theory]
    [MemberData(nameof(Backends))]
    public async Task BooleansAndNulls_RoundTrip(string backend)
    {
        var (engine, path) = await OpenAsync(backend);
        var store = engine.GraphicalTargets();

        var rec = Record();
        rec.IsSystem = true;
        rec.IsStatic = false;
        rec.Endpoint = "host:5900";
        rec.Secret = null;
        rec.CaSecretRef = "env:CA"; // pragma: allowlist secret
        rec.ClientCertSecretRef = "file:/tmp/cert.pem"; // pragma: allowlist secret
        rec.ClientKeySecretRef = null;
        rec.CreatedBy = "alice";
        await store.PutAsync(rec);

        var got = await store.GetAsync("gt-1");
        Assert.True(got!.IsSystem);
        Assert.False(got.IsStatic);
        Assert.Equal("host:5900", got.Endpoint);
        Assert.Null(got.Secret);
        Assert.Equal("env:CA", got.CaSecretRef); // pragma: allowlist secret
        Assert.Equal("file:/tmp/cert.pem", got.ClientCertSecretRef); // pragma: allowlist secret
        Assert.Null(got.ClientKeySecretRef);
        Assert.Equal("alice", got.CreatedBy);
        Assert.Null(got.UpdatedAt);

        await CloseAsync(engine, path);
    }

    /// <summary>
    /// Guards the NOT NULL config column: a zero-value record must still write
    /// valid JSON rather than an empty string.
    /// </summary>
    [Fact]
    public async Task EmptyConfig_SatisfiesNotNull()
    {
        var (engine, path) = await OpenAsync("sqlite");
        var store = engine.GraphicalTargets();

        var rec = Record();
        rec.Config = "";
        await store.PutAsync(rec);

        Assert.Equal("{}", (await store.GetAsync("gt-1"))!.Config);
        await CloseAsync(engine, path);
    }

    [Fact]
    public async Task Rows_SurviveReopen()
    {
        var path = Path.Combine(Path.GetTempPath(), $"gt-{Guid.NewGuid():N}.db");
        var engine = new SqliteEngine(path);
        await engine.OpenAsync();
        await engine.MigrateAsync();
        await engine.GraphicalTargets().PutAsync(Record());
        await engine.CloseAsync();

        var reopened = new SqliteEngine(path);
        await reopened.OpenAsync();
        await reopened.MigrateAsync();
        var got = await reopened.GraphicalTargets().GetAsync("gt-1");
        Assert.NotNull(got);
        Assert.Equal("""{"vm_name":"vm-1"}""", got!.Config);
        await CloseAsync(reopened, path);
    }

    [Fact]
    public async Task Rollback_DiscardsTheWrite()
    {
        var path = Path.Combine(Path.GetTempPath(), $"gt-{Guid.NewGuid():N}.db");
        var engine = new SqliteEngine(path);
        await engine.OpenAsync();
        await engine.MigrateAsync();

        var tx = await engine.BeginAsync();
        await engine.GraphicalTargets().PutAsync(Record());
        await tx.RollbackAsync();

        Assert.Null(await engine.GraphicalTargets().GetAsync("gt-1"));
        await CloseAsync(engine, path);
    }

    /// <summary>
    /// Dropping the table is the cheapest way to make the driver fail for real
    /// rather than mocking ADO.NET, so the error paths are exercised instead of
    /// being dead weight.
    /// </summary>
    [Fact]
    public async Task SqlErrors_Surface()
    {
        var path = Path.Combine(Path.GetTempPath(), $"gt-{Guid.NewGuid():N}.db");
        var engine = new SqliteEngine(path);
        await engine.OpenAsync();
        await engine.MigrateAsync();
        await engine.GraphicalTargets().PutAsync(Record());

        await using (var raw = SqliteTestDb.Connect(path))
        {
            await raw.OpenAsync();
            await using var drop = raw.CreateCommand();
            drop.CommandText = "DROP TABLE cp_graphical_targets";
            await drop.ExecuteNonQueryAsync();
        }

        var store = engine.GraphicalTargets();
        await Assert.ThrowsAsync<SqliteException>(() => store.GetAsync("gt-1"));
        await Assert.ThrowsAsync<SqliteException>(() => store.ListAsync());
        await Assert.ThrowsAsync<SqliteException>(() => store.DeleteAsync("gt-1"));
        await Assert.ThrowsAsync<SqliteException>(() => store.PutAsync(Record("gt-2")));

        await CloseAsync(engine, path);
    }
}
