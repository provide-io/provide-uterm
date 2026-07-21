//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Net.WebSockets;
using System.Text;
using Provide.Uterm.Client;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests.Vnc;

/// <summary>
/// HTTP/WS gates for <c>/worker/{id}/hijack/{hid}/gui/vnc</c> (Go human-relay parity).
/// </summary>
public class HumanVncRouteTests
{
    private const string TestTenant = "acme";
    private const string WorkerId = "demo";

    [Fact]
    public async Task Unauthenticated_Returns_401()
    {
        var (server, baseUrl, _, _, _) = await StartServerAsync();
        await using (server)
        {
            using var http = new HttpClient { BaseAddress = new Uri(baseUrl) };
            var resp = await http.GetAsync(VncPath("00000000-0000-4000-8000-000000000001", "gt-x"));
            Assert.Equal(HttpStatusCode.Unauthorized, resp.StatusCode);
        }
    }

    [Fact]
    public async Task Viewer_Returns_403()
    {
        var (server, baseUrl, token, graphicalTargets, _) = await StartServerAsync(roles: new[] { "viewer" });
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            // Viewer cannot acquire either — seed hijack via hub isn't needed for capability 403.
            // Use a ghost id; AuthorizeHub fails first for viewer on mutate.
            var targetId = CreateGraphicalTarget(graphicalTargets);
            using var http = Authed(baseUrl, token);
            var resp = await http.GetAsync(VncPath("00000000-0000-4000-8000-000000000099", targetId));
            Assert.Equal(HttpStatusCode.Forbidden, resp.StatusCode);
        }
    }

    [Fact]
    public async Task Missing_Hijack_Returns_404()
    {
        var (server, baseUrl, token, graphicalTargets, _) = await StartServerAsync();
        await using (server)
        {
            var targetId = CreateGraphicalTarget(graphicalTargets);
            using var http = Authed(baseUrl, token);
            var resp = await http.GetAsync(VncPath("00000000-0000-4000-8000-000000000099", targetId));
            Assert.Equal(HttpStatusCode.NotFound, resp.StatusCode);
        }
    }

    [Fact]
    public async Task Non_Owner_Returns_403()
    {
        var (server, baseUrl, token, graphicalTargets, hub) = await StartServerAsync();
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var acq = await client.AcquireAsync(WorkerId, owner: "operator", leaseS: 60);
            var hid = acq["hijack_id"]!.ToString()!;
            var st = hub.Registry.Get(WorkerId)!;
            st.HijackSession!.AcquiredBy = "someone-else";

            var targetId = CreateGraphicalTarget(graphicalTargets);
            using var http = Authed(baseUrl, token);
            var resp = await http.GetAsync(VncPath(hid, targetId));
            Assert.Equal(HttpStatusCode.Forbidden, resp.StatusCode);
            var body = await resp.Content.ReadAsStringAsync();
            Assert.Contains("not owned", body, StringComparison.OrdinalIgnoreCase);
        }
    }

    [Fact]
    public async Task Missing_TargetId_Returns_422()
    {
        var (server, baseUrl, token, _, _) = await StartServerAsync();
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var acq = await client.AcquireAsync(WorkerId, owner: "operator", leaseS: 60);
            var hid = acq["hijack_id"]!.ToString()!;
            using var http = Authed(baseUrl, token);
            var resp = await http.GetAsync($"/worker/{WorkerId}/hijack/{hid}/gui/vnc");
            Assert.Equal(HttpStatusCode.UnprocessableEntity, resp.StatusCode);
        }
    }

    [Fact]
    public async Task Memory_Target_Returns_501()
    {
        var (server, baseUrl, token, graphicalTargets, _) = await StartServerAsync();
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var acq = await client.AcquireAsync(WorkerId, owner: "operator", leaseS: 60);
            var hid = acq["hijack_id"]!.ToString()!;
            var targetId = CreateGraphicalTarget(graphicalTargets, protocol: "memory");
            using var http = Authed(baseUrl, token);
            var resp = await http.GetAsync(VncPath(hid, targetId));
            Assert.Equal(HttpStatusCode.NotImplemented, resp.StatusCode);
        }
    }

    [Fact]
    public async Task Litevirt_Target_Returns_501()
    {
        var (server, baseUrl, token, graphicalTargets, _) = await StartServerAsync();
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var acq = await client.AcquireAsync(WorkerId, owner: "operator", leaseS: 60);
            var hid = acq["hijack_id"]!.ToString()!;
            var targetId = CreateGraphicalTarget(
                graphicalTargets, protocol: "litevirt", endpoint: "10.0.0.5:7443");
            using var http = Authed(baseUrl, token);
            var resp = await http.GetAsync(VncPath(hid, targetId));
            Assert.Equal(HttpStatusCode.NotImplemented, resp.StatusCode);
        }
    }

    [Fact]
    public async Task Metadata_Endpoint_Returns_403()
    {
        var (server, baseUrl, token, graphicalTargets, _) = await StartServerAsync(blockPrivate: true);
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var acq = await client.AcquireAsync(WorkerId, owner: "operator", leaseS: 60);
            var hid = acq["hijack_id"]!.ToString()!;
            var targetId = CreateGraphicalTarget(
                graphicalTargets, protocol: "rfb", endpoint: "169.254.169.254:5900");
            using var http = Authed(baseUrl, token);
            var resp = await http.GetAsync(VncPath(hid, targetId));
            Assert.Equal(HttpStatusCode.Forbidden, resp.StatusCode);
        }
    }

    [Fact]
    public async Task Rfb_Connect_Failure_Returns_502()
    {
        var (server, baseUrl, token, graphicalTargets, _) = await StartServerAsync(blockPrivate: false);
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var acq = await client.AcquireAsync(WorkerId, owner: "operator", leaseS: 60);
            var hid = acq["hijack_id"]!.ToString()!;
            // High port with nothing listening on loopback.
            var targetId = CreateGraphicalTarget(
                graphicalTargets, protocol: "rfb", endpoint: "127.0.0.1:1");
            using var http = Authed(baseUrl, token);
            var resp = await http.GetAsync(VncPath(hid, targetId));
            Assert.Equal(HttpStatusCode.BadGateway, resp.StatusCode);
        }
    }

    [Fact]
    public async Task Ws_Relay_With_Factory_Forwards_Key_When_Owner()
    {
        var (server, baseUrl, token, _, _) = await StartServerAsync(blockPrivate: false);
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var acq = await client.AcquireAsync(WorkerId, owner: "operator", leaseS: 60);
            var hid = acq["hijack_id"]!.ToString()!;

            var video = Encoding.ASCII.GetBytes("RFB-SERVER-VIDEO");
            var gotInput = new MemoryStream();
            var inputDone = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);

            // Local TCP peer stands in for an RFB server (duplex NetworkStream).
            var listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            var peerPort = ((IPEndPoint)listener.LocalEndpoint).Port;
            var peerTask = Task.Run(async () =>
            {
                using var peer = await listener.AcceptTcpClientAsync().ConfigureAwait(false);
                await using var peerStream = peer.GetStream();
                await peerStream.WriteAsync(video).ConfigureAwait(false);
                await peerStream.FlushAsync().ConfigureAwait(false);
                var buf = new byte[4096];
                try
                {
                    while (true)
                    {
                        var n = await peerStream.ReadAsync(buf).ConfigureAwait(false);
                        if (n <= 0) break;
                        gotInput.Write(buf, 0, n);
                        if (gotInput.Length >= 14 + 8)
                        {
                            inputDone.TrySetResult();
                            break;
                        }
                    }
                }
                catch
                {
                    // closed
                }
                finally
                {
                    inputDone.TrySetResult();
                }
            });

            var upstreamTcp = new TcpClient();
            await upstreamTcp.ConnectAsync(IPAddress.Loopback, peerPort);
            server.HumanVncUpstreamFactory = _ =>
            {
                Stream s = upstreamTcp.GetStream();
                return Task.FromResult<(Stream, IAsyncDisposable?)>((s, new TcpLifetime(upstreamTcp)));
            };

            using var ws = new ClientWebSocket();
            ws.Options.SetRequestHeader("Authorization", "Bearer " + token);
            var uri = new Uri(baseUrl.Replace("http://", "ws://", StringComparison.Ordinal)
                               + $"/worker/{WorkerId}/hijack/{hid}/gui/vnc?target_id=unused-with-factory");
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(8));
            await ws.ConnectAsync(uri, cts.Token);

            // Handshake + KeyEvent (type 4).
            var handshake = new byte[14];
            Encoding.ASCII.GetBytes("RFB 003.008\n").CopyTo(handshake, 0);
            handshake[12] = 1;
            handshake[13] = 1;
            var key = new byte[8];
            key[0] = 4;
            await ws.SendAsync(handshake.AsMemory(), WebSocketMessageType.Binary, true, cts.Token);
            await ws.SendAsync(key.AsMemory(), WebSocketMessageType.Binary, true, cts.Token);

            // Receive video from upstream.
            var recv = new byte[64];
            var result = await ws.ReceiveAsync(recv, cts.Token);
            Assert.True(result.Count >= video.Length);
            Assert.Equal(video, recv.AsSpan(0, video.Length).ToArray());

            await inputDone.Task.WaitAsync(TimeSpan.FromSeconds(5), cts.Token);
            Assert.True(gotInput.Length >= 14 + 8, $"expected handshake+key on upstream, got {gotInput.Length}");

            try { await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "done", CancellationToken.None); }
            catch { /* best-effort */ }

            listener.Stop();
            try { await peerTask.WaitAsync(TimeSpan.FromSeconds(2)); }
            catch { /* best-effort */ }
        }
    }

    [Fact]
    public async Task Non_WebSocket_Get_Returns_400_After_Authz()
    {
        var (server, baseUrl, token, _, _) = await StartServerAsync();
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var acq = await client.AcquireAsync(WorkerId, owner: "operator", leaseS: 60);
            var hid = acq["hijack_id"]!.ToString()!;

            // Factory supplies a throwaway stream so dial is skipped.
            server.HumanVncUpstreamFactory = _ =>
                Task.FromResult<(Stream, IAsyncDisposable?)>((new MemoryStream(), null));

            using var http = Authed(baseUrl, token);
            // Plain GET (no Upgrade) → 400 after authz + factory.
            var resp = await http.GetAsync($"/worker/{WorkerId}/hijack/{hid}/gui/vnc?target_id=x");
            Assert.Equal(HttpStatusCode.BadRequest, resp.StatusCode);
        }
    }

    [Fact]
    public async Task Invalid_WorkerId_Returns_422()
    {
        var (server, baseUrl, token, _, _) = await StartServerAsync();
        await using (server)
        {
            using var http = Authed(baseUrl, token);
            var resp = await http.GetAsync(
                "/worker/bad%20id/hijack/00000000-0000-4000-8000-000000000001/gui/vnc?target_id=x");
            Assert.Equal(HttpStatusCode.UnprocessableEntity, resp.StatusCode);
        }
    }

    [Fact]
    public async Task Target_Not_Found_Returns_404()
    {
        var (server, baseUrl, token, _, _) = await StartServerAsync();
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var acq = await client.AcquireAsync(WorkerId, owner: "operator", leaseS: 60);
            var hid = acq["hijack_id"]!.ToString()!;
            using var http = Authed(baseUrl, token);
            var resp = await http.GetAsync(VncPath(hid, "no-such-target"));
            Assert.Equal(HttpStatusCode.NotFound, resp.StatusCode);
        }
    }

    [Fact]
    public async Task No_Tenant_Scope_Returns_403()
    {
        var (server, baseUrl, token, graphicalTargets, _) = await StartServerAsync(tenant: null);
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var acq = await client.AcquireAsync(WorkerId, owner: "operator", leaseS: 60);
            var hid = acq["hijack_id"]!.ToString()!;
            // Target exists under acme but principal has no tenant claim.
            var targetId = CreateGraphicalTarget(graphicalTargets, protocol: "rfb", endpoint: "127.0.0.1:5900");
            using var http = Authed(baseUrl, token);
            var resp = await http.GetAsync(VncPath(hid, targetId));
            Assert.Equal(HttpStatusCode.Forbidden, resp.StatusCode);
        }
    }

    [Fact]
    public async Task Unbound_Lease_Ws_Drops_Inject()
    {
        // AcquiredBy null → leaseId "" → KeyEvent not forwarded (fail closed).
        var (server, baseUrl, token, _, hub) = await StartServerAsync(blockPrivate: false);
        await using (server)
        {
            using var client = HijackClient.WithBearer(baseUrl, token);
            var acq = await client.AcquireAsync(WorkerId, owner: "operator", leaseS: 60);
            var hid = acq["hijack_id"]!.ToString()!;
            hub.Registry.Get(WorkerId)!.HijackSession!.AcquiredBy = null;

            var gotInput = new MemoryStream();
            var inputDone = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            var listener = new TcpListener(IPAddress.Loopback, 0);
            listener.Start();
            var peerPort = ((IPEndPoint)listener.LocalEndpoint).Port;
            var peerTask = Task.Run(async () =>
            {
                using var peer = await listener.AcceptTcpClientAsync().ConfigureAwait(false);
                await using var peerStream = peer.GetStream();
                await peerStream.WriteAsync(new byte[] { 1, 2, 3 }).ConfigureAwait(false);
                var buf = new byte[4096];
                try
                {
                    while (true)
                    {
                        var n = await peerStream.ReadAsync(buf).ConfigureAwait(false);
                        if (n <= 0) break;
                        gotInput.Write(buf, 0, n);
                        if (gotInput.Length >= 14)
                        {
                            // Wait briefly for a key that must NOT arrive.
                            await Task.Delay(200).ConfigureAwait(false);
                            inputDone.TrySetResult();
                            break;
                        }
                    }
                }
                catch { /* closed */ }
                finally { inputDone.TrySetResult(); }
            });

            var upstreamTcp = new TcpClient();
            await upstreamTcp.ConnectAsync(IPAddress.Loopback, peerPort);
            server.HumanVncUpstreamFactory = _ =>
                Task.FromResult<(Stream, IAsyncDisposable?)>(
                    (upstreamTcp.GetStream(), new TcpLifetime(upstreamTcp)));

            using var ws = new ClientWebSocket();
            ws.Options.SetRequestHeader("Authorization", "Bearer " + token);
            var uri = new Uri(baseUrl.Replace("http://", "ws://", StringComparison.Ordinal)
                               + $"/worker/{WorkerId}/hijack/{hid}/gui/vnc?target_id=x");
            using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(8));
            await ws.ConnectAsync(uri, cts.Token);

            var handshake = new byte[14];
            Encoding.ASCII.GetBytes("RFB 003.008\n").CopyTo(handshake, 0);
            handshake[12] = 1;
            handshake[13] = 1;
            var key = new byte[8];
            key[0] = 4;
            await ws.SendAsync(handshake.AsMemory(), WebSocketMessageType.Binary, true, cts.Token);
            await ws.SendAsync(key.AsMemory(), WebSocketMessageType.Binary, true, cts.Token);
            await inputDone.Task.WaitAsync(TimeSpan.FromSeconds(5), cts.Token);

            // Handshake only (14); key dropped without owned lease.
            Assert.Equal(14, gotInput.Length);

            try { await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, "done", CancellationToken.None); }
            catch { /* best-effort */ }
            listener.Stop();
            try { await peerTask.WaitAsync(TimeSpan.FromSeconds(2)); }
            catch { /* best-effort */ }
        }
    }

    private static string VncPath(string hijackId, string targetId) =>
        $"/worker/{WorkerId}/hijack/{hijackId}/gui/vnc?target_id={Uri.EscapeDataString(targetId)}";

    private static HttpClient Authed(string baseUrl, string token)
    {
        var http = new HttpClient { BaseAddress = new Uri(baseUrl) };
        http.DefaultRequestHeaders.Add("Authorization", "Bearer " + token);
        return http;
    }

    private static string CreateGraphicalTarget(
        InMemoryGraphicalTargetRegistry graphicalTargets,
        string protocol = "memory",
        string? endpoint = null)
    {
        var targetId = "gt-" + Guid.NewGuid().ToString("N")[..12];
        var target = new Provide.Uterm.Server.GraphicalTargetDefinition
        {
            TargetId = targetId,
            TenantId = TestTenant,
            DisplayName = targetId,
            Protocol = protocol,
            Endpoint = endpoint,
            Width = 32,
            Height = 24,
            IsSystem = false,
            CreatedBy = "test",
            UpdatedBy = "test",
        };
        Assert.True(GraphicalTargetScope.TryForTenant(TestTenant, out var scope));
        graphicalTargets.Create(scope, target);
        return targetId;
    }

    private static async Task<(UtermServer Server, string BaseUrl, string Token, InMemoryGraphicalTargetRegistry GraphicalTargets, TermHub Hub)> StartServerAsync(
        string[]? roles = null,
        bool blockPrivate = true,
        string? tenant = TestTenant)
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();

        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        cfg.Security.BlockPrivateConnectorTargets = blockPrivate;
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = WorkerId,
            DisplayName = "Demo",
            ConnectorType = "shell",
            Visibility = "public",
            Owner = "dev-user",
        });

        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "uterm-vnc-token-" + Guid.NewGuid().ToString("N")),
            Subject = "dev-user",
            Roles = roles ?? new[] { "admin" },
            Tenant = tenant,
        });

        var auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore());
        var hub = new TermHub(new TermHubConfig
        {
            Clock = new RealClock(),
            WorkerToken = cfg.Auth.WorkerBearerToken,
        });
        hub.Conn.RegisterWorker(WorkerId, new NoopWorker());

        var graphicalTargets = new InMemoryGraphicalTargetRegistry();
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = auth,
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            GraphicalTargets = graphicalTargets,
            Version = "test",
            Clock = new RealClock(),
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        return (server, $"http://127.0.0.1:{port}", token, graphicalTargets, hub);
    }

    private sealed class NoopWorker : IWorkerWs
    {
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            Task.CompletedTask;
    }

    private sealed class TcpLifetime : IAsyncDisposable
    {
        private readonly TcpClient _tcp;
        public TcpLifetime(TcpClient tcp) => _tcp = tcp;
        public ValueTask DisposeAsync()
        {
            try { _tcp.Dispose(); }
            catch { /* best-effort */ }
            return ValueTask.CompletedTask;
        }
    }
}
