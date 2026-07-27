//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ControlPlane;
using Provide.Uterm.Server;
using Def = Provide.Uterm.Server.GraphicalTargetDefinition;

namespace Provide.Uterm.Tests;

/// <summary>
/// Control-plane-backed registry tests. Every behavioural case runs against
/// BOTH backends so the two cannot drift apart behind the registry.
/// </summary>
public sealed class ControlPlaneGraphicalTargetRegistryTests
{
    private static readonly DateTimeOffset Fixed = DateTimeOffset.FromUnixTimeSeconds(1_700_000_000);

    public static TheoryData<string> Backends => new() { "memory", "sqlite" };

    private static async Task<(ControlPlaneGraphicalTargetRegistry Registry, IEngine Engine, string? Path)>
        NewAsync(string backend)
    {
        string? path = null;
        IEngine engine;
        if (backend == "memory")
        {
            engine = await Bootstrap.OpenAsync("memory", null);
        }
        else
        {
            path = Path.Combine(Path.GetTempPath(), $"reg-{Guid.NewGuid():N}.db");
            engine = await Bootstrap.OpenAsync("sqlite", path);
        }

        var registry = new ControlPlaneGraphicalTargetRegistry(engine) { Now = () => Fixed };
        return (registry, engine, path);
    }

    private static async Task CleanupAsync(IEngine engine, string? path)
    {
        await engine.CloseAsync();
        if (path is null)
        {
            return;
        }

        foreach (var suffix in new[] { "", "-wal", "-shm" })
        {
            if (File.Exists(path + suffix))
            {
                File.Delete(path + suffix);
            }
        }
    }

    private static Def Target(string targetId, string tenant) => new()
    {
        TargetId = targetId,
        TenantId = tenant,
        DisplayName = "console",
        Protocol = GraphicalTargetConstants.ProtocolMemory,
        Width = 640,
        Height = 480,
    };

    private static GraphicalTargetScope Scope(string tenant)
    {
        Assert.True(GraphicalTargetScope.TryForTenant(tenant, out var scope));
        return scope;
    }

    [Theory]
    [MemberData(nameof(Backends))]
    public async Task CreateThenGet(string backend)
    {
        var (reg, engine, path) = await NewAsync(backend);
        var scope = Scope("acme");

        await reg.CreateAsync(scope, Target("gt-1", "acme"));
        var got = await reg.GetAsync(scope, "gt-1");

        Assert.NotNull(got);
        Assert.Equal("gt-1", got!.TargetId);
        Assert.Equal(Fixed, got.CreatedAt);
        await CleanupAsync(engine, path);
    }

    /// <summary>
    /// The security-critical case: a tenant must never see or mutate another
    /// tenant's target, through any verb.
    /// </summary>
    [Theory]
    [MemberData(nameof(Backends))]
    public async Task TenantIsolation(string backend)
    {
        var (reg, engine, path) = await NewAsync(backend);
        var acme = Scope("acme");
        var other = Scope("other");
        await reg.CreateAsync(acme, Target("gt-1", "acme"));

        Assert.Null(await reg.GetAsync(other, "gt-1"));
        Assert.Empty(await reg.ListAsync(other));
        await Assert.ThrowsAsync<GraphicalTargetException>(() => reg.DeleteAsync(other, "gt-1"));

        // The victim's row survives the failed delete.
        Assert.NotNull(await reg.GetAsync(acme, "gt-1"));
        await CleanupAsync(engine, path);
    }

    [Theory]
    [MemberData(nameof(Backends))]
    public async Task CreateRejectsForeignTenantAndDuplicates(string backend)
    {
        var (reg, engine, path) = await NewAsync(backend);
        var scope = Scope("acme");

        var foreignErr = await Assert.ThrowsAsync<GraphicalTargetException>(
            () => reg.CreateAsync(scope, Target("gt-1", "other")));
        Assert.Equal(GraphicalTargetErrorCode.Forbidden, foreignErr.Code);

        await reg.CreateAsync(scope, Target("gt-1", "acme"));
        var dupErr = await Assert.ThrowsAsync<GraphicalTargetException>(
            () => reg.CreateAsync(scope, Target("gt-1", "acme")));
        Assert.Equal(GraphicalTargetErrorCode.AlreadyExists, dupErr.Code);

        await CleanupAsync(engine, path);
    }

    [Theory]
    [MemberData(nameof(Backends))]
    public async Task UpdatePreservesCreationStamps(string backend)
    {
        var (reg, engine, path) = await NewAsync(backend);
        var scope = Scope("acme");

        var original = Target("gt-1", "acme");
        original.CreatedBy = "alice";
        await reg.CreateAsync(scope, original);

        var next = Target("gt-1", "acme");
        next.DisplayName = "renamed";
        var updated = await reg.UpdateAsync(scope, next);

        Assert.Equal("renamed", updated.DisplayName);
        Assert.Equal("alice", updated.CreatedBy);
        Assert.NotNull(updated.UpdatedAt);

        var absent = await Assert.ThrowsAsync<GraphicalTargetException>(
            () => reg.UpdateAsync(scope, Target("missing", "acme")));
        Assert.Equal(GraphicalTargetErrorCode.NotFound, absent.Code);

        await CleanupAsync(engine, path);
    }

    [Theory]
    [MemberData(nameof(Backends))]
    public async Task Delete(string backend)
    {
        var (reg, engine, path) = await NewAsync(backend);
        var scope = Scope("acme");
        await reg.CreateAsync(scope, Target("gt-1", "acme"));

        await reg.DeleteAsync(scope, "gt-1");
        Assert.Null(await reg.GetAsync(scope, "gt-1"));

        var second = await Assert.ThrowsAsync<GraphicalTargetException>(
            () => reg.DeleteAsync(scope, "gt-1"));
        Assert.Equal(GraphicalTargetErrorCode.NotFound, second.Code);

        await CleanupAsync(engine, path);
    }

    /// <summary>Mirrors the in-memory registry: a seeded static target shadows a
    /// runtime id and cannot be mutated.</summary>
    [Theory]
    [MemberData(nameof(Backends))]
    public async Task StaticIsImmutableAndWins(string backend)
    {
        var (reg, engine, path) = await NewAsync(backend);
        var scope = Scope("acme");

        var seeded = Target("gt-static", "acme");
        seeded.DisplayName = "seeded";
        await reg.AddStaticAsync(seeded);

        Assert.Equal(
            GraphicalTargetErrorCode.Immutable,
            (await Assert.ThrowsAsync<GraphicalTargetException>(
                () => reg.DeleteAsync(scope, "gt-static"))).Code);
        Assert.Equal(
            GraphicalTargetErrorCode.Immutable,
            (await Assert.ThrowsAsync<GraphicalTargetException>(
                () => reg.UpdateAsync(scope, Target("gt-static", "acme")))).Code);
        Assert.Equal(
            GraphicalTargetErrorCode.AlreadyExists,
            (await Assert.ThrowsAsync<GraphicalTargetException>(
                () => reg.CreateAsync(scope, Target("gt-static", "acme")))).Code);
        Assert.Equal(
            GraphicalTargetErrorCode.Conflict,
            (await Assert.ThrowsAsync<GraphicalTargetException>(
                () => reg.AddStaticAsync(Target("gt-static", "acme")))).Code);

        var got = await reg.GetAsync(scope, "gt-static");
        Assert.Equal("seeded", got!.DisplayName);
        Assert.True(got.IsSystem);

        await CleanupAsync(engine, path);
    }

    [Theory]
    [MemberData(nameof(Backends))]
    public async Task ListMergesAndSorts(string backend)
    {
        var (reg, engine, path) = await NewAsync(backend);
        var scope = Scope("acme");
        await reg.CreateAsync(scope, Target("gt-c", "acme"));
        await reg.CreateAsync(scope, Target("gt-a", "acme"));
        await reg.AddStaticAsync(Target("gt-b", "acme"));

        var rows = await reg.ListAsync(scope);
        Assert.Equal(["gt-a", "gt-b", "gt-c"], rows.Select(r => r.TargetId));

        await CleanupAsync(engine, path);
    }

    [Theory]
    [MemberData(nameof(Backends))]
    public async Task ClosedAndInvalidScopeRejected(string backend)
    {
        var (reg, engine, path) = await NewAsync(backend);
        var scope = Scope("acme");

        // Neither tenant nor system — invalid.
        var zero = default(GraphicalTargetScope);
        Assert.Equal(
            GraphicalTargetErrorCode.Forbidden,
            (await Assert.ThrowsAsync<GraphicalTargetException>(
                () => reg.GetAsync(zero, "gt-1"))).Code);

        reg.Close();
        Assert.Equal(
            GraphicalTargetErrorCode.Closed,
            (await Assert.ThrowsAsync<GraphicalTargetException>(
                () => reg.GetAsync(scope, "gt-1"))).Code);
        Assert.Equal(
            GraphicalTargetErrorCode.Closed,
            (await Assert.ThrowsAsync<GraphicalTargetException>(
                () => reg.ListAsync(scope))).Code);

        await CleanupAsync(engine, path);
    }

    /// <summary>The whole point of the feature: a target created before a
    /// restart is still there afterwards.</summary>
    [Fact]
    public async Task SurvivesRestart()
    {
        var path = Path.Combine(Path.GetTempPath(), $"reg-{Guid.NewGuid():N}.db");
        var scope = Scope("acme");

        var first = await Bootstrap.OpenAsync("sqlite", path);
        var target = Target("gt-1", "acme");
        target.Config = new Dictionary<string, object?> { ["vm_name"] = "vm-1" };
        await new ControlPlaneGraphicalTargetRegistry(first).CreateAsync(scope, target);
        await first.CloseAsync();

        var second = await Bootstrap.OpenAsync("sqlite", path);
        var got = await new ControlPlaneGraphicalTargetRegistry(second).GetAsync(scope, "gt-1");
        Assert.NotNull(got);
        Assert.False(got!.IsStatic);
        Assert.Contains("vm_name", got.Config.Keys);

        await CleanupAsync(second, path);
    }

    /// <summary>Static targets are re-seeded from config each boot, never
    /// stored — so they must NOT come back from a fresh database.</summary>
    [Fact]
    public async Task StaticIsNotPersisted()
    {
        var path = Path.Combine(Path.GetTempPath(), $"reg-{Guid.NewGuid():N}.db");
        var scope = Scope("acme");

        var first = await Bootstrap.OpenAsync("sqlite", path);
        await new ControlPlaneGraphicalTargetRegistry(first).AddStaticAsync(Target("gt-static", "acme"));
        await first.CloseAsync();

        var second = await Bootstrap.OpenAsync("sqlite", path);
        Assert.Null(await new ControlPlaneGraphicalTargetRegistry(second).GetAsync(scope, "gt-static"));
        await CleanupAsync(second, path);
    }

    /// <summary>A memory backend keeps the previous behaviour, so switching
    /// backends is the only thing that changes durability.</summary>
    [Fact]
    public async Task MemoryBackendIsNotDurable()
    {
        var scope = Scope("acme");
        var first = await Bootstrap.OpenAsync("memory", null);
        await new ControlPlaneGraphicalTargetRegistry(first).CreateAsync(scope, Target("gt-1", "acme"));
        await first.CloseAsync();

        var second = await Bootstrap.OpenAsync("memory", null);
        Assert.Null(await new ControlPlaneGraphicalTargetRegistry(second).GetAsync(scope, "gt-1"));
        await second.CloseAsync();
    }

    [Fact]
    public void BootstrapRejectsUnknownBackend()
    {
        Assert.Throws<ControlPlaneConfigurationException>(() => Bootstrap.New("postgres", null));
        Assert.IsType<MemoryEngine>(Bootstrap.New(null, null));
        Assert.IsType<MemoryEngine>(Bootstrap.New("  ", null));
        Assert.IsType<SqliteEngine>(Bootstrap.New("SQLite", ":memory:"));
    }

    /// <summary>A config blob that is not a JSON object degrades to empty
    /// rather than failing the read.</summary>
    [Fact]
    public async Task NonObjectConfigDegrades()
    {
        var scope = Scope("acme");
        var engine = await Bootstrap.OpenAsync("memory", null);
        foreach (var blob in new[] { "[1,2,3]", "not-json", "\"str\"", "" })
        {
            await engine.GraphicalTargets().PutAsync(new GraphicalTargetRecord
            {
                TargetId = "gt-1", TenantId = "acme", DisplayName = "c",
                Protocol = GraphicalTargetConstants.ProtocolMemory,
                Width = 640, Height = 480, Config = blob, CreatedAt = 100,
            });

            var got = await new ControlPlaneGraphicalTargetRegistry(engine).GetAsync(scope, "gt-1");
            Assert.NotNull(got);
            Assert.Empty(got!.Config);
        }

        await engine.CloseAsync();
    }
}
