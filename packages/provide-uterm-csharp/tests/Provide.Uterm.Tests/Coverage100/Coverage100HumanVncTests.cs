//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.WebSockets;
using Provide.Uterm.Client;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.Vnc;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>
/// The human VNC relay: both pumps' cancel/fault exits (with a short drain
/// timeout), the route's refusals and upstream teardown, and the role fallback.
/// </summary>
public sealed class Coverage100HumanVncTests
{
    private const string Tenant = "acme";

    private sealed class FaultingStream(Exception error) : Stream
    {
        public bool Disposed { get; private set; }
        public override bool CanRead => true;
        public override bool CanSeek => false;
        public override bool CanWrite => true;
        public override long Length => throw new NotSupportedException();
        public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
        public override void Flush() { }
        public override int Read(byte[] buffer, int offset, int count) => throw error;
        public override ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default) =>
            ValueTask.FromException<int>(error);
        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) { }

        protected override void Dispose(bool disposing)
        {
            Disposed = true;
            throw new IOException("dispose boom");
        }
    }

    private sealed class ThrowingLifetime : IAsyncDisposable
    {
        public bool Disposed { get; private set; }
        public ValueTask DisposeAsync()
        {
            Disposed = true;
            throw new IOException("lifetime boom");
        }
    }

    private sealed class NoopWorker : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) => Task.CompletedTask;
    }

    private static async Task WithShortDrainAsync(Func<Task> body)
    {
        var saved = HumanRelay.DrainTimeout;
        HumanRelay.DrainTimeout = TimeSpan.FromMilliseconds(50);
        try
        {
            await body();
        }
        finally
        {
            HumanRelay.DrainTimeout = saved;
        }
    }

    [Fact]
    public async Task Upstream_io_failure_ends_the_relay_and_the_parked_client_read_is_cancelled() =>
        await WithShortDrainAsync(async () =>
        {
            var client = new ScriptedDuplexStream(); // never sends: parked until cancelled
            var announced = 0;
            await HumanRelay.RelayAsync(
                client, new MemoryStream(), new FaultingStream(new IOException("upstream closed")), new MemoryStream(),
                canInject: null, "s", "l", "p", "operator",
                onFirstCompletion: () =>
                {
                    announced++;
                    return Task.CompletedTask;
                }).WaitAsync(TimeSpan.FromSeconds(10));
            Assert.Equal(1, announced);
        });

    [Fact]
    public async Task Client_eof_with_a_failing_close_announcement_cancels_the_parked_upstream_read() =>
        await WithShortDrainAsync(async () =>
        {
            var client = new ScriptedDuplexStream();
            client.EnqueueEof();
            var upstream = new ScriptedDuplexStream(); // parked until cancelled
            await HumanRelay.RelayAsync(
                client, new MemoryStream(), upstream, new MemoryStream(),
                canInject: null, "s", "l", "p", "operator",
                onFirstCompletion: () => throw new WebSocketException("peer gone"))
                .WaitAsync(TimeSpan.FromSeconds(10));
        });

    [Fact]
    public async Task An_unexpected_upstream_fault_is_rethrown_once_both_pumps_finish()
    {
        var client = new ScriptedDuplexStream();
        client.EnqueueEof();
        var ex = await Assert.ThrowsAsync<InvalidOperationException>(() => HumanRelay.RelayAsync(
            client, new MemoryStream(), new FaultingStream(new InvalidOperationException("bad frame")), new MemoryStream(),
            canInject: null, "s", "l", "p", "operator"));
        Assert.Equal("bad frame", ex.Message);
    }

    [Fact]
    public async Task Relay_closes_the_socket_and_ignores_a_close_that_fails() =>
        await WithShortDrainAsync(async () =>
        {
            var ws = new ScriptedWebSocket { CloseError = new WebSocketException("gone") };
            var upstream = new ScriptedDuplexStream();
            upstream.EnqueueEof();
            await UtermServer.RunHumanVncRelayAsync(ws, upstream, "s", "l", "p", "operator", CancellationToken.None)
                .WaitAsync(TimeSpan.FromSeconds(10));
            // Once when the upstream ended (announcement), once in the final close.
            Assert.Equal(2, ws.CloseCalls);
        });

    [Fact]
    public async Task A_browser_close_is_answered_once_the_relay_ends() =>
        await WithShortDrainAsync(async () =>
        {
            var ws = new ScriptedWebSocket();
            ws.EnqueueClose(); // the browser hangs up first
            var upstream = new ScriptedDuplexStream(); // parked until the relay cancels it
            await UtermServer.RunHumanVncRelayAsync(ws, upstream, "s", "l", "p", "operator", CancellationToken.None)
                .WaitAsync(TimeSpan.FromSeconds(10));
            // No close announcement (the socket was no longer open), one answering close.
            Assert.Equal(1, ws.CloseCalls);
            Assert.Equal(WebSocketState.Closed, ws.State);
            Assert.Equal(WebSocketCloseStatus.NormalClosure, ws.CloseStatus);
        });

    [Fact]
    public async Task Role_for_a_worker_without_a_definition_follows_the_principals_roles()
    {
        await using var s = await C100Server.StartAsync();
        Assert.Equal("admin", s.Server.ResolvePrincipalRole(new Principal { SubjectId = "a", Roles = StringSet.Of("admin") }, "nodef"));
        Assert.Equal("operator", s.Server.ResolvePrincipalRole(new Principal { SubjectId = "o", Roles = StringSet.Of("operator") }, "nodef"));
        Assert.Equal("viewer", s.Server.ResolvePrincipalRole(new Principal { SubjectId = "v", Roles = StringSet.Of("viewer") }, "nodef"));
    }

    private static GraphicalTargetDefinition Def(string protocol, string? endpoint) => new()
    {
        TargetId = "gt-1", TenantId = Tenant, DisplayName = "t", Protocol = protocol, Endpoint = endpoint, Width = 8, Height = 8,
    };

    private static async Task<(C100Server Server, HttpClient Http, string Path)> LeasedAsync(StubTargets targets)
    {
        var s = await C100Server.StartAsync(new C100Server.Options
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
        var token = s.TokenFor("admin", Tenant, "admin");
        using var client = HijackClient.WithBearer(s.BaseUrl, token);
        var acq = await client.AcquireAsync(C100Server.Session, owner: "operator", leaseS: 60);
        var path = $"/worker/{C100Server.Session}/hijack/{acq["hijack_id"]}/gui/vnc?target_id=gt-1";
        return (s, s.Http(token), path);
    }

    [Fact]
    public async Task Vnc_route_refuses_unsupported_protocols_and_bad_endpoints()
    {
        var targets = new StubTargets();
        var (s, http, path) = await LeasedAsync(targets);
        await using (s)
        using (http)
        {
            targets.Get = _ => Def("spice", null);
            Assert.Equal(HttpStatusCode.NotImplemented, (await http.GetAsync(path)).StatusCode);
            targets.Get = _ => Def("rfb", "no-port");
            Assert.Equal(HttpStatusCode.UnprocessableEntity, (await http.GetAsync(path)).StatusCode);
        }
    }

    [Fact]
    public async Task Vnc_route_tears_down_each_upstream_kind_after_a_non_websocket_request()
    {
        await using var rfb = new FakeRfbServer();
        var targets = new StubTargets { Get = _ => Def("rfb", $"127.0.0.1:{rfb.Port}") };
        var (s, http, path) = await LeasedAsync(targets);
        await using (s)
        using (http)
        {
            // Dialled TCP upstream: the dial succeeds, the plain GET is refused.
            Assert.Equal(HttpStatusCode.BadRequest, (await http.GetAsync(path)).StatusCode);
            await rfb.Accepted.Task.WaitAsync(TimeSpan.FromSeconds(10));

            // Factory upstream with a lifetime whose dispose throws.
            var life = new ThrowingLifetime();
            s.Server.HumanVncUpstreamFactory = _ => Task.FromResult<(Stream, IAsyncDisposable?)>((new MemoryStream(), life));
            Assert.Equal(HttpStatusCode.BadRequest, (await http.GetAsync(path)).StatusCode);
            Assert.True(life.Disposed);

            // Factory upstream without a lifetime: the stream itself is disposed (and throws).
            var stream = new FaultingStream(new IOException("unused"));
            s.Server.HumanVncUpstreamFactory = _ => Task.FromResult<(Stream, IAsyncDisposable?)>((stream, null));
            Assert.Equal(HttpStatusCode.BadRequest, (await http.GetAsync(path)).StatusCode);
            Assert.True(stream.Disposed);
        }
    }
}
