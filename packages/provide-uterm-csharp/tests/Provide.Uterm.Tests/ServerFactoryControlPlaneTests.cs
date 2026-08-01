//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Server;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

/// <summary>
/// Wiring tests for the control-plane-backed server factory: the point where
/// config selects durability.
/// </summary>
public sealed class ServerFactoryControlPlaneTests
{
    private static UtermServerConfig Config(string backend, string? dbPath)
    {
        var cfg = new UtermServerConfig();
        cfg.ControlPlane.Backend = backend;
        cfg.ControlPlane.DatabaseUrl = dbPath;
        cfg.GraphicalTargets =
        [
            new Provide.Uterm.ServerConfig.GraphicalTargetDefinition
            {
                TargetId = "gt-static", TenantId = "acme", Protocol = "memory", Enabled = true,
            },
        ];
        return cfg;
    }

    private static GraphicalTargetScope Scope(string tenant)
    {
        Assert.True(GraphicalTargetScope.TryForTenant(tenant, out var scope));
        return scope;
    }

    private static Provide.Uterm.Server.GraphicalTargetDefinition Runtime() => new()
    {
        TargetId = "gt-runtime",
        TenantId = "acme",
        DisplayName = "console",
        Protocol = GraphicalTargetConstants.ProtocolMemory,
        Width = 640,
        Height = 480,
    };

    private static void Cleanup(string path) => SqliteTestDb.Delete(path);

    /// <summary>
    /// The end-to-end point of the wiring: a runtime target created against a
    /// sqlite-backed server is still there after a restart, while the
    /// config-seeded static target is re-seeded rather than persisted.
    /// </summary>
    [Fact]
    public async Task SqliteBackend_RuntimeTargetsSurviveRestart()
    {
        var path = Path.Combine(Path.GetTempPath(), $"factory-{Guid.NewGuid():N}.db");
        var cfg = Config("sqlite", path);
        var scope = Scope("acme");

        var (server, _, engine) = await ServerFactory.CreateFromConfigAsync(cfg);
        var registry = server.GraphicalTargets;
        await registry.CreateAsync(scope, Runtime());
        await server.DisposeAsync();
        await engine.CloseAsync();

        var (server2, _, engine2) = await ServerFactory.CreateFromConfigAsync(cfg);
        var registry2 = server2.GraphicalTargets;

        var runtime = await registry2.GetAsync(scope, "gt-runtime");
        Assert.NotNull(runtime);
        Assert.False(runtime!.IsStatic);

        // The static target is present because config re-seeded it, and is still
        // immutable.
        var seeded = await registry2.GetAsync(scope, "gt-static");
        Assert.NotNull(seeded);
        Assert.True(seeded!.IsStatic || seeded.IsSystem);

        Assert.Equal(2, (await registry2.ListAsync(scope)).Count);

        await server2.DisposeAsync();
        await engine2.CloseAsync();
        Cleanup(path);
    }

    /// <summary>A memory backend keeps the previous behaviour, so the backend
    /// setting is the only thing that changes durability.</summary>
    [Fact]
    public async Task MemoryBackend_RuntimeTargetsAreNotDurable()
    {
        var cfg = Config("memory", null);
        var scope = Scope("acme");

        var (server, _, engine) = await ServerFactory.CreateFromConfigAsync(cfg);
        await server.GraphicalTargets.CreateAsync(scope, Runtime());
        await server.DisposeAsync();
        await engine.CloseAsync();

        var (server2, _, engine2) = await ServerFactory.CreateFromConfigAsync(cfg);
        var registry2 = server2.GraphicalTargets;
        Assert.Null(await registry2.GetAsync(scope, "gt-runtime"));
        // Static still re-seeds.
        Assert.NotNull(await registry2.GetAsync(scope, "gt-static"));

        await server2.DisposeAsync();
        await engine2.CloseAsync();
    }

    /// <summary>An unusable backend must fail construction rather than yield a
    /// server with a half-built registry — and must not leak the engine.</summary>
    [Fact]
    public async Task UnknownBackend_FailsConstruction()
    {
        var cfg = Config("postgres", null);
        await Assert.ThrowsAsync<ControlPlane.ControlPlaneConfigurationException>(
            () => ServerFactory.CreateFromConfigAsync(cfg));
    }

    /// <summary>A bad config target fails construction too.</summary>
    [Fact]
    public async Task BadConfigTarget_FailsConstruction()
    {
        var cfg = Config("memory", null);
        cfg.GraphicalTargets =
        [
            new Provide.Uterm.ServerConfig.GraphicalTargetDefinition
            {
                TargetId = "gt-x", Protocol = "vnc", TargetAddress = "vm:5900", Enabled = true,
            },
        ];
        await Assert.ThrowsAsync<ArgumentException>(() => ServerFactory.CreateFromConfigAsync(cfg));
    }

    /// <summary>The synchronous factory keeps its previous behaviour: a
    /// non-durable registry seeded from config.</summary>
    [Fact]
    public async Task SyncFactory_StillUsesAnInMemoryRegistry()
    {
        var cfg = Config("memory", null);
        var (server, _) = ServerFactory.CreateFromConfig(cfg);
        var registry = server.GraphicalTargets;
        Assert.IsType<InMemoryGraphicalTargetRegistry>(registry);
        Assert.NotNull(await registry.GetAsync(Scope("acme"), "gt-static"));
        await server.DisposeAsync();
    }
}
