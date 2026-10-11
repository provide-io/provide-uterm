//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using Provide.Uterm.Client;
using Provide.Uterm.ControlPlane;
using Provide.Uterm.Gui;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.Vnc;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>GUI attach, screenshot and graphical-target registry/route error arms.</summary>
public sealed class Coverage100GuiTargetsTests
{
    private const string Tenant = "acme";

    private sealed class NoopWorker : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) => Task.CompletedTask;
    }

    /// <summary>A target registry that returns one canned answer or throws.</summary>
    private sealed class StubTargets : IGraphicalTargetRegistry
    {
        public Func<string, GraphicalTargetDefinition?> Get { get; set; } = _ => null;

        public Task<GraphicalTargetDefinition?> GetAsync(GraphicalTargetScope scope, string targetId, CancellationToken ct = default) =>
            Task.FromResult(Get(targetId));

        public Task<IReadOnlyList<GraphicalTargetDefinition>> ListAsync(GraphicalTargetScope scope, CancellationToken ct = default) =>
            throw new GraphicalTargetException(GraphicalTargetErrorCode.Closed, "closed");

        public Task<GraphicalTargetDefinition> CreateAsync(GraphicalTargetScope scope, GraphicalTargetDefinition target, CancellationToken ct = default) =>
            throw new NotSupportedException();

        public Task<GraphicalTargetDefinition> UpdateAsync(GraphicalTargetScope scope, GraphicalTargetDefinition target, CancellationToken ct = default) =>
            throw new NotSupportedException();

        public Task DeleteAsync(GraphicalTargetScope scope, string targetId, CancellationToken ct = default) =>
            throw new NotSupportedException();

        public Task AddStaticAsync(GraphicalTargetDefinition target, CancellationToken ct = default) =>
            throw new NotSupportedException();
    }

    private sealed class ThrowingDisposeSession : IGraphicalSession, IAsyncDisposable
    {
        public bool Disposed { get; private set; }
        public RgbaImage Screenshot() => new(1, 1);
        public void InjectPointer(int x, int y, byte buttonMask) { }
        public void InjectKey(uint keySym, bool down) { }
        public ValueTask DisposeAsync()
        {
            Disposed = true;
            return ValueTask.CompletedTask;
        }
    }

    private static GraphicalTargetDefinition Def(string protocol, string? endpoint = null, int w = 8, int h = 8) => new()
    {
        TargetId = "gt-1",
        TenantId = Tenant,
        DisplayName = "t",
        Protocol = protocol,
        Endpoint = endpoint,
        Width = w,
        Height = h,
    };

    private static Task<C100Server> StartAsync(StubTargets targets) =>
        C100Server.StartAsync(new C100Server.Options
        {
            Configure = c => c.Security.BlockPrivateConnectorTargets = false,
            Deps = (cfg, hub) =>
            {
                var d = C100Server.Default(cfg, hub);
                hub.Conn.RegisterWorker(C100Server.Session, new NoopWorker());
                return new ServerDeps
                {
                    Hub = d.Hub, Auth = d.Auth, Authz = d.Authz, Config = d.Config, Registry = d.Registry,
                    ApiKeys = d.ApiKeys, Profiles = d.Profiles, Version = d.Version, GraphicalTargets = targets,
                };
            },
        });

    private static Task<HttpResponseMessage> AttachAsync(HttpClient http) =>
        http.PostAsync($"/worker/{C100Server.Session}/gui/attach", C100Server.Json(new { target_id = "gt-1" }));

    [Fact]
    public async Task Attach_refusals_map_each_failure_to_its_status()
    {
        var targets = new StubTargets();
        await using var s = await StartAsync(targets);
        using var admin = s.Http(s.TokenFor("admin", Tenant, "admin"));

        // Hijack capability without the graphical attach capability.
        using var narrow = s.Http(s.TokenWith("admin", Tenant, new[] { "admin" }, "session.control.hijack session.read"));
        Assert.Equal(HttpStatusCode.Forbidden, (await AttachAsync(narrow)).StatusCode);

        targets.Get = _ => Def("spice");
        Assert.Equal(HttpStatusCode.NotImplemented, (await AttachAsync(admin)).StatusCode);

        targets.Get = _ => Def("memory", w: 1_000_000);
        Assert.Equal(HttpStatusCode.UnprocessableEntity, (await AttachAsync(admin)).StatusCode);

        targets.Get = _ => throw new ArgumentException("bad target");
        Assert.Equal(HttpStatusCode.UnprocessableEntity, (await AttachAsync(admin)).StatusCode);

        targets.Get = _ => Def("rfb", endpoint: "no-port-here");
        Assert.Equal(HttpStatusCode.UnprocessableEntity, (await AttachAsync(admin)).StatusCode);
    }

    [Fact]
    public async Task Rfb_attach_replaces_and_disposes_the_previous_session()
    {
        await using var rfb = new FakeRfbServer();
        var targets = new StubTargets { Get = _ => Def("rfb", endpoint: $"127.0.0.1:{rfb.Port}") };
        await using var s = await StartAsync(targets);
        using var admin = s.Http(s.TokenFor("admin", Tenant, "admin"));

        var previous = new ThrowingDisposeSession();
        s.Hub.Registry.Get(C100Server.Session)!.GraphicalSession = previous;
        Assert.Equal(HttpStatusCode.OK, (await AttachAsync(admin)).StatusCode);
        Assert.True(previous.Disposed);
        var attached = Assert.IsType<RfbClient>(s.Hub.Registry.Get(C100Server.Session)!.GraphicalSession);
        Assert.Equal(4, attached.Width);
        await attached.DisposeAsync();
    }

    [Fact]
    public async Task Screenshot_without_a_graphical_session_is_not_found()
    {
        await using var s = await StartAsync(new StubTargets());
        var token = s.TokenFor("admin", Tenant, "admin");
        using var client = HijackClient.WithBearer(s.BaseUrl, token);
        var acq = await client.AcquireAsync(C100Server.Session, owner: "operator", leaseS: 60);
        var hid = acq["hijack_id"]!.ToString();
        using var admin = s.Http(token);
        var shot = await admin.GetAsync($"/worker/{C100Server.Session}/hijack/{hid}/gui/screenshot");
        Assert.Equal(HttpStatusCode.NotFound, shot.StatusCode);
    }

    [Theory]
    [InlineData(GraphicalTargetErrorCode.AlreadyExists, HttpStatusCode.Conflict)]
    [InlineData(GraphicalTargetErrorCode.Conflict, HttpStatusCode.Conflict)]
    [InlineData(GraphicalTargetErrorCode.Closed, HttpStatusCode.ServiceUnavailable)]
    [InlineData(GraphicalTargetErrorCode.Backend, HttpStatusCode.ServiceUnavailable)]
    public async Task Graphical_target_route_errors_map_to_statuses(GraphicalTargetErrorCode code, HttpStatusCode expected)
    {
        var targets = new StubTargets { Get = _ => throw new GraphicalTargetException(code, "x") };
        await using var s = await StartAsync(targets);
        using var admin = s.Http(s.TokenFor("admin", Tenant, "admin"));
        Assert.Equal(expected, (await admin.GetAsync("/api/graphical-targets/gt-1")).StatusCode);
        // The stub's list always reports a closed store.
        Assert.Equal(HttpStatusCode.ServiceUnavailable, (await admin.GetAsync("/api/graphical-targets")).StatusCode);
    }

    [Fact]
    public async Task Registries_refuse_cross_tenant_updates_and_invalid_static_targets()
    {
        var engine = await Bootstrap.OpenAsync("memory", null);
        try
        {
            var reg = new ControlPlaneGraphicalTargetRegistry(engine);
            Assert.True(GraphicalTargetScope.TryForTenant("a", out var scopeA));
            await reg.CreateAsync(GraphicalTargetScope.System(), new GraphicalTargetDefinition
            {
                TargetId = "gt-b", TenantId = "b", DisplayName = "b", Protocol = "memory", Width = 8, Height = 8,
            });

            var wrongTenant = await Assert.ThrowsAsync<GraphicalTargetException>(() => reg.UpdateAsync(scopeA, new GraphicalTargetDefinition
            {
                TargetId = "gt-b", TenantId = "b", DisplayName = "b", Protocol = "memory", Width = 8, Height = 8,
            }));
            Assert.Equal(GraphicalTargetErrorCode.Forbidden, wrongTenant.Code);

            var hijackedRow = await Assert.ThrowsAsync<GraphicalTargetException>(() => reg.UpdateAsync(scopeA, new GraphicalTargetDefinition
            {
                TargetId = "gt-b", TenantId = "a", DisplayName = "a", Protocol = "memory", Width = 8, Height = 8,
            }));
            Assert.Equal(GraphicalTargetErrorCode.Forbidden, hijackedRow.Code);
        }
        finally
        {
            await engine.CloseAsync();
        }

        var invalid = await Assert.ThrowsAsync<GraphicalTargetException>(() =>
            new InMemoryGraphicalTargetRegistry().AddStaticAsync(new GraphicalTargetDefinition { TargetId = "bad id!", Protocol = "memory" }));
        Assert.Equal(GraphicalTargetErrorCode.Invalid, invalid.Code);
    }
}
