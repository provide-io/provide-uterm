//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using Microsoft.AspNetCore.Http;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerConfig;
using Provide.Uterm.Tunnel;
using Provide.Uterm.TunnelClient;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>Worker/tunnel frame routing, framework refusals, static UI and webhook wiring arms.</summary>
public sealed class Coverage100ServerMiscTests
{
    private sealed class RecordingWorker : IWorkerWs
    {
        public Func<string, Task>? OnSend { get; set; }
        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default) =>
            OnSend?.Invoke(payload) ?? Task.CompletedTask;
    }

    private static byte[] Json(object o) => JsonSerializer.SerializeToUtf8Bytes(o);

    [Fact]
    public async Task Tunnel_and_worker_frames_fall_back_to_frame_acceptance()
    {
        await using var s = await C100Server.StartAsync();
        var worker = new RecordingWorker();
        Assert.True(s.Hub.Conn.RegisterWorker("tw", worker));
        var stranger = new RecordingWorker();

        TunnelFrame Frame(byte channel, byte[] payload) => new() { Channel = channel, Payload = payload };
        Assert.True(await s.Server.ProcessTunnelFrameAsync("tw", worker, Frame(TunnelProtocol.ChannelHttp, "{nope"u8.ToArray())));
        Assert.False(await s.Server.ProcessTunnelFrameAsync("tw", stranger, Frame(TunnelProtocol.ChannelHttp, Json(new { type = "request" }))));
        Assert.True(await s.Server.ProcessTunnelFrameAsync("tw", worker, Frame(TunnelProtocol.ChannelData, Array.Empty<byte>())));
        Assert.True(await s.Server.ProcessTunnelFrameAsync("tw", worker, Frame(TunnelProtocol.ChannelControl, "{nope"u8.ToArray())));
        Assert.True(await s.Server.ProcessTunnelFrameAsync("tw", worker, Frame(TunnelProtocol.ChannelControl, Json(new { type = "other" }))));

        Assert.True(await s.Server.ProcessWorkerChunkAsync("tw", worker, new ControlChunk(new Dictionary<string, object?> { ["type"] = "other" })));
        Assert.True(await s.Server.ProcessWorkerChunkAsync("tw", worker, new DataChunk("")));
        Assert.True(await s.Server.ProcessWorkerChunkAsync("tw", worker, new ControlChunk(new Dictionary<string, object?>
        {
            ["type"] = "worker_hello",
            ["input_mode"] = "hijack",
            ["protocol_version"] = "2",
        })));
        Assert.Equal("hijack", s.Hub.Registry.Get("tw")!.InputMode);
    }

    [Fact]
    public async Task A_disconnected_worker_socket_is_dropped_on_its_next_frame()
    {
        await using var s = await C100Server.StartAsync();
        var uri = new Uri(s.BaseUrl.Replace("http", "ws", StringComparison.Ordinal) + "/ws/worker/w-rep/term");
        using var ws = new ClientWebSocket();
        ws.Options.SetRequestHeader("Authorization", "Bearer " + s.Config.Auth.WorkerBearerToken);
        await ws.ConnectAsync(uri, CancellationToken.None);
        await WaitForAsync(() => s.Hub.Registry.Get("w-rep")?.WorkerWs is not null);

        // The admin detaches the worker; the socket stays open until it next
        // speaks, and that frame is refused and ends the session.
        using var admin = s.Http(s.AdminToken);
        Assert.Equal(HttpStatusCode.OK, (await admin.PostAsync("/worker/w-rep/disconnect_worker", null)).StatusCode);
        await ws.SendAsync(Encoding.UTF8.GetBytes(ControlChannelCodec.EncodeTerminalData("stale")), WebSocketMessageType.Text, true, CancellationToken.None);
        var buf = new byte[256];
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        WebSocketReceiveResult r;
        do
        {
            r = await ws.ReceiveAsync(buf, cts.Token);
        }
        while (r.MessageType != WebSocketMessageType.Close);
        Assert.Equal(WebSocketCloseStatus.NormalClosure, r.CloseStatus);
    }

    private static async Task WaitForAsync(Func<bool> cond)
    {
        var deadline = DateTime.UtcNow.AddSeconds(10);
        while (!cond())
        {
            Assert.True(DateTime.UtcNow < deadline, "condition not reached");
            await Task.Delay(5);
        }
    }

    [Fact]
    public async Task Close_handler_waits_for_the_peer_acknowledgement()
    {
        var ws = new ScriptedWebSocket();
        ws.EnqueueClose();
        await WebSocketCloseHandler.CloseAndTerminateAsync(ws, WebSocketCloseStatus.NormalClosure, "bye");
        Assert.Equal(WebSocketState.Closed, ws.State);
    }

    [Fact]
    public async Task Framework_refusals_get_a_body_only_for_statuses_with_a_phrase()
    {
        var unknown = new DefaultHttpContext();
        unknown.Response.Body = new MemoryStream();
        unknown.Response.StatusCode = 599;
        await UtermServer.WriteFrameworkRefusalBodyAsync(unknown);
        Assert.Equal(0, unknown.Response.Body.Length);

        // (A status with a phrase gets its body through the live pipeline; the
        // framework 404/405 tests cover that half.)
        Assert.Null(SecurityHeaders.ReadOverride(new SecurityConfig(), "not_a_field"));
    }

    [Fact]
    public async Task In_process_handler_forwards_request_content()
    {
        await using var s = await C100Server.StartAsync(new C100Server.Options { Start = false });
        using var http = new HttpClient(s.Server.CreateHandler()) { BaseAddress = new Uri(s.BaseUrl) };
        http.DefaultRequestHeaders.Add("Authorization", "Bearer " + s.AdminToken);
        var resp = await http.PostAsync("/api/connect", C100Server.Json(new
        {
            connector_type = "shell",
            connector_config = new { inner = new { a = 1 }, list = new[] { 1 }, nothing = (string?)null },
        }));
        Assert.Equal(HttpStatusCode.OK, resp.StatusCode);
        var cfg = (await C100Server.ReadJsonAsync(resp)).GetProperty("connector_config");
        // Non-scalar nested values are kept as their JSON text.
        Assert.Equal("{\"a\":1}", cfg.GetProperty("inner").GetString());
        Assert.Equal("[1]", cfg.GetProperty("list").GetString());
    }

    [Fact]
    public async Task Static_ui_serves_terminal_html_then_the_built_in_shell()
    {
        // Two frontend dirs rather than deleting terminal.html between requests:
        // Kestrel can still hold the file it just streamed, and Windows refuses
        // to delete an open file.
        var withTerminal = Directory.CreateTempSubdirectory("fe-");
        var empty = Directory.CreateTempSubdirectory("fe-");
        try
        {
            await File.WriteAllTextAsync(Path.Combine(withTerminal.FullName, "terminal.html"), "<p>terminal</p>");
            await using (var s = await StartWithFrontend(withTerminal.FullName))
            {
                using var admin = s.Http(s.AdminToken);
                Assert.Contains("terminal", await admin.GetStringAsync("/app/x"), StringComparison.Ordinal);

                var prom = await admin.GetStringAsync("/api/metrics/prometheus");
                Assert.Contains("# TYPE c100_counter counter\nc100_counter 3\n", prom, StringComparison.Ordinal);
            }

            await using (var s = await StartWithFrontend(empty.FullName))
            {
                using var admin = s.Http(s.AdminToken);
                Assert.Contains("<html", await admin.GetStringAsync("/app/x"), StringComparison.OrdinalIgnoreCase);
            }
        }
        finally
        {
            withTerminal.Delete(recursive: true);
            empty.Delete(recursive: true);
        }
    }

    private static Task<C100Server> StartWithFrontend(string frontendDir) =>
        C100Server.StartAsync(new C100Server.Options
        {
            Deps = (cfg, hub) =>
            {
                var d = C100Server.Default(cfg, hub);
                d.Metrics.Increment("c100_counter", 3);
                return new ServerDeps
                {
                    Hub = d.Hub, Auth = d.Auth, Authz = d.Authz, Config = d.Config, Registry = d.Registry,
                    ApiKeys = d.ApiKeys, Profiles = d.Profiles, Version = d.Version, FrontendDir = frontendDir,
                    Metrics = d.Metrics,
                };
            },
        });

    [Fact]
    public async Task Lazy_webhooks_stamp_deliveries_and_refuse_loopback_while_a_share_is_live()
    {
        await using var target = new TinyHttpServer();
        var store = new MemoryTunnelStore();
        await using var s = await C100Server.StartAsync(new C100Server.Options
        {
            Deps = (cfg, hub) =>
            {
                var d = C100Server.Default(cfg, hub);
                return new ServerDeps
                {
                    Hub = d.Hub, Auth = d.Auth, Authz = d.Authz, Config = d.Config, Registry = d.Registry,
                    ApiKeys = d.ApiKeys, Profiles = d.Profiles, Version = d.Version, TunnelStore = store,
                };
            },
        });
        var cfg = s.Server.WebhooksForTests.Register(C100Server.Session, target.Url(), null, null, null);
        s.Hub.AppendEventData(C100Server.Session, "term", new Dictionary<string, object?> { ["data"] = "x" });
        var delivered = await target.Requests.Reader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10));
        Assert.Contains("\"timestamp\":", delivered, StringComparison.Ordinal);

        store.PutToken(C100Server.Session, new TokenRecord { ExpiresAt = DateTimeOffset.UtcNow.ToUnixTimeSeconds() + 600 });
        Assert.False(s.Server.WebhooksForTests.IsDeliveryAllowed(cfg));
    }

    [Fact]
    public async Task Egress_refuses_names_that_resolve_to_nothing()
    {
        var saved = EgressGuard.ResolveHostAsync;
        EgressGuard.ResolveHostAsync = (_, _) => Task.FromResult(Array.Empty<IPAddress>());
        try
        {
            var ex = await Assert.ThrowsAsync<InvalidOperationException>(() => EgressGuard.AssertConnectorTargetAllowedAsync("empty.test"));
            Assert.Contains("could not resolve connector host", ex.Message, StringComparison.Ordinal);
        }
        finally
        {
            EgressGuard.ResolveHostAsync = saved;
        }
    }

    [Fact]
    public async Task Factory_logs_through_the_telemetry_level_parser()
    {
        await using var target = new TinyHttpServer(n => n == 1 ? 500 : 200);
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = C100Server.FreePort();
        cfg.Auth.Mode = "jwt";
        cfg.Auth.JwtPublicKeyPem = "factory-secret-factory-secret-factory-secret";
        cfg.Sessions.Add(new SessionDefinition { SessionId = "fs", ConnectorType = "shell" });
        var (server, _) = ServerFactory.CreateFromConfig(cfg);
        await using (server)
        {
            server.HubForTests.Log("warning", "c100 hub log");
            server.WebhooksForTests.Register("fs", target.Url(), null, null, null);
            server.HubForTests.AppendEventData("fs", "term", new Dictionary<string, object?> { ["data"] = "x" });
            // The second request is the retry, sent only after the failed first
            // attempt was logged through the factory's OnLog.
            await target.Requests.Reader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10));
            await target.Requests.Reader.ReadAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10));
        }

        Assert.Equal(Provide.Telemetry.Levels.Parse("warn"), Provide.Telemetry.Levels.Parse("warning"));
    }

}
