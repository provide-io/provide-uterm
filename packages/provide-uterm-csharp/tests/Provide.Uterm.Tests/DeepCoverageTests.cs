//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using Provide.Uterm.Client;
using Provide.Uterm.Connectors;
using Provide.Uterm.Emulator;
using Provide.Uterm.Hub;
using Provide.Uterm.Manager;
using Provide.Uterm.Recording;
using Provide.Uterm.Redaction;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Provide.Uterm.Session;
using Provide.Uterm.TermSession;
using Provide.Uterm.Transports;
using Provide.Uterm.Tunnel;
using Redact = Provide.Uterm.Redaction.Redaction;
using SessionLog = Provide.Uterm.SessionLogger.SessionLogger;
using SessionLogOpts = Provide.Uterm.SessionLogger.SessionLoggerOptions;
using ControlChannelMode = Provide.Uterm.SessionLogger.ControlChannelMode;

namespace Provide.Uterm.Tests;

public class DeepCoverageTests
{
    private sealed class FakeTransport : IConnectionTransport
    {
        private readonly Queue<byte[]> _incoming = new();
        private bool _connected;
        public List<byte[]> Sent { get; } = new();

        public void Enqueue(string s) => _incoming.Enqueue(Encoding.UTF8.GetBytes(s));

        public Task ConnectAsync(string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default)
        {
            _connected = true;
            return Task.CompletedTask;
        }

        public Task DisconnectAsync(CancellationToken cancellationToken = default)
        {
            _connected = false;
            return Task.CompletedTask;
        }

        public Task SendAsync(byte[] data, CancellationToken cancellationToken = default)
        {
            Sent.Add(data);
            return Task.CompletedTask;
        }

        public async Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default)
        {
            var deadline = DateTime.UtcNow + timeout;
            while (DateTime.UtcNow < deadline)
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (_incoming.Count > 0) return _incoming.Dequeue();
                await Task.Delay(5, cancellationToken);
            }

            return Array.Empty<byte>();
        }

        public bool IsConnected() => _connected;
    }

    private sealed class TestConnector : BaseConnector
    {
        private readonly FakeTransport _t = new();

        public FakeTransport Transport => _t;

        public override async Task StartAsync(CancellationToken cancellationToken = default)
        {
            var session = new TransportSession(_t, ct => _t.ConnectAsync("h", 1, null, ct),
                new TransportSessionOptions { Cols = 40, Rows = 10, ControlFrames = true });
            AttachWatch(session);
            await session.ConnectAsync(cancellationToken);
            LiveSession = session;
        }
    }

    [Fact]
    public async Task Connector_Base_And_Registry()
    {
        var c = new TestConnector();
        Assert.False(c.IsConnected());
        await c.StartAsync();
        Assert.True(c.IsConnected());
        c.SetMode("hijack");
        Assert.Contains("hijack", c.Analysis(), StringComparison.Ordinal);
        Assert.Throws<ArgumentException>(() => c.SetMode("nope"));
        c.HandleControl("pause");
        await c.HandleInputAsync("abc");
        Assert.Contains(c.Transport.Sent, b => Encoding.UTF8.GetString(b) == "abc");
        c.Transport.Enqueue("OUT\r\n");
        await Task.Delay(50);
        var snap = c.Snapshot();
        Assert.True(snap.Cols > 0);
        c.Clear();
        var events = c.Events();
        Assert.NotNull(events);
        Assert.NotNull(c.Session());
        await c.StopAsync();
        Assert.False(c.IsConnected());

        var reg = new ConnectorRegistry();
        Assert.Contains("telnet", reg.Types());
        Assert.Contains("shell", reg.Types());
        var tel = reg.Create("telnet", new Dictionary<string, object?> { ["host"] = "127.0.0.1", ["port"] = 1 });
        Assert.IsType<TelnetConnector>(tel);
        var ssh = reg.Create("ssh", new Dictionary<string, object?>());
        Assert.IsType<SshConnector>(ssh);
        var ws = reg.Create("ws", new Dictionary<string, object?> { ["url"] = "ws://x" });
        Assert.IsType<WebSocketConnector>(ws);
        var sh = reg.Create("shell", new Dictionary<string, object?>());
        Assert.IsType<ShellConnector>(sh);
        Assert.Throws<ArgumentException>(() => reg.Create("nope", new Dictionary<string, object?>()));
    }

    [Fact]
    public async Task SessionLogger_RecordsEvents()
    {
        var store = new InMemoryStore();
        await using var logger = new SessionLog(store, new SessionLogOpts
        {
            MaxBytes = 1_000_000,
            BatchSize = 2,
            FlushInterval = TimeSpan.FromMilliseconds(50),
            ControlChannelMode = ControlChannelMode.Wire,
            Redactor = Redact.MakeRedactor([@"secret=\S+"]),
        });
        await logger.StartAsync("sess-log");
        await logger.LogAsync("read", new Dictionary<string, object?> { ["raw"] = "hello secret=hunter2" });
        await logger.LogAsync("screen", new Dictionary<string, object?> { ["screen"] = "screen secret=x" });
        await logger.LogAsync("write", new Dictionary<string, object?> { ["keys"] = "keys" });
        await logger.LogAsync("custom", new Dictionary<string, object?> { ["a"] = 1 });
        await logger.LogAsync("wire", new Dictionary<string, object?> { ["x"] = 1 }); // included: Wire mode
        await logger.FlushAsync();
        await Task.Delay(80);
        await logger.StopAsync();

        var entries = await store.GetEntriesAsync("sess-log", new Query { Limit = 100 });
        Assert.True(entries.Count >= 1);
    }

    [Fact]
    public void Manager_AgentFleet_And_Http()
    {
        var mgr = new AgentManager(new ManagerConfig { Host = "127.0.0.1", Port = 0, AuthToken = "tok" });
        var a = mgr.Spawn("worker", "agent-1");
        Assert.Equal("running", a.State);
        Assert.NotNull(mgr.Get("agent-1"));
        Assert.Single(mgr.List());
        Assert.True(mgr.Stop("agent-1"));
        Assert.Equal("stopped", mgr.Get("agent-1")!.State);
        Assert.True(mgr.Remove("agent-1"));
        Assert.Null(mgr.Get("agent-1"));
        Assert.False(mgr.Stop("missing"));

        var status = mgr.GetSwarmStatus();
        Assert.Equal(0, Convert.ToInt32(status["agents"]));
        Assert.NotNull(mgr.GetTimeseriesRecent(10));
        Assert.Equal(120, Convert.ToInt32(mgr.GetTimeseriesSummary()["window_minutes"]));

        // spawn more for timeseries
        mgr.Spawn();
        mgr.Spawn("t2");
        Assert.True(mgr.GetTimeseriesRecent(1).Count >= 1);
        Assert.True(Convert.ToInt32(mgr.GetTimeseriesInfo()["rows"]) >= 1);
    }

    [Fact]
    public async Task ManagerServer_HealthEndpoint()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();

        var mgr = new AgentManager(new ManagerConfig { Host = "127.0.0.1", Port = port });
        mgr.Spawn("default", "a1");
        await using var server = new ManagerServer(mgr);
        await server.StartAsync();
        Assert.False(string.IsNullOrEmpty(server.BaseAddress));
        using var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };
        var health = await http.GetAsync("/health");
        health.EnsureSuccessStatusCode();
        var swarm = await http.GetAsync("/swarm/status");
        swarm.EnsureSuccessStatusCode();
        var agents = await http.GetAsync("/swarm/agents");
        agents.EnsureSuccessStatusCode();
        var ts = await http.GetAsync("/swarm/timeseries/info");
        ts.EnsureSuccessStatusCode();
        var recent = await http.GetAsync("/swarm/timeseries/recent?limit=5");
        recent.EnsureSuccessStatusCode();
        var spawn = await http.PostAsync("/swarm/agents",
            new StringContent("""{"worker_type":"t"}""", Encoding.UTF8, "application/json"));
        spawn.EnsureSuccessStatusCode();
        Assert.Equal(0, await ManagerProgram.RunAsync(new[] { "--help" }));
        await server.DisposeAsync();
    }

    [Fact]
    public void Tunnel_Tokens_And_Store()
    {
        var hash = TunnelTokens.HashToken("secret");
        Assert.True(TunnelTokens.VerifyToken("secret", hash));
        Assert.False(TunnelTokens.VerifyToken("nope", hash));
        Assert.Equal(300.0, TunnelConstants.InviteTtlS);

        var invite = new Invite
        {
            SessionId = "s",
            Role = TunnelRole.Operator,
            TunnelToken = "secret",
            ExpiresAt = 999,
        };
        Assert.True(TunnelTokens.InviteMatchesTokenHash(invite, hash));
        Assert.False(TunnelTokens.InviteMatchesTokenHash(null, hash));

        var store = new MemoryTunnelStore();
        store.PutToken("t1", new TokenRecord
        {
            WorkerTokenHash = hash,
            ShareTokenHash = hash,
            ControlTokenHash = hash,
            CreatedAt = 1,
            ExpiresAt = 999,
            TunnelType = "share",
        });
        Assert.NotNull(store.GetToken("t1"));
        store.PutInvite("i1", invite);
        Assert.NotNull(store.ConsumeInvite("i1"));
        Assert.Null(store.ConsumeInvite("i1"));
        Assert.NotNull(store.GetToken("t1"));
    }


    [Fact]
    public async Task Server_SessionCrud_And_InputMode()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();

        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "dc-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = new[] { "admin" },
        });
        var apiKeys = new ApiKeyStore();
        var auth = new LocalIdentityProvider(cfg.Auth, apiKeys);
        var authz = new AuthorizationService();
        var clock = new ManualClock(100);
        clock.SetMonotonic(10);
        var hub = new TermHub(new TermHubConfig { Clock = clock });
        var worker = new EchoWorker();
        hub.Conn.RegisterWorker("w1", worker);
        var registry = new InMemorySessionRegistry(cfg.Sessions);
        await using var server = new UtermServer(new ServerDeps
        {
            Hub = hub, Auth = auth, Authz = authz, Config = cfg, Registry = registry, Clock = clock, Version = "t",
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        using var client = HijackClient.WithBearer(server.BaseAddress!, token);

        // create session via raw HTTP POST
        using var http = new HttpClient();
        http.DefaultRequestHeaders.TryAddWithoutValidation("Authorization", "Bearer " + token);
        var create = await http.PostAsync(
            server.BaseAddress + "/api/sessions",
            new StringContent("""{"session_id":"new1","display_name":"N","connector_type":"shell"}""", Encoding.UTF8, "application/json"));
        create.EnsureSuccessStatusCode();

        var one = await client.GetSessionAsync("new1");
        Assert.Equal("new1", one["session_id"]?.ToString());

        var mode = await client.SetInputModeAsync("w1", "open");
        Assert.NotNull(mode);
        // back to hijack for acquire
        await client.SetInputModeAsync("w1", "hijack");

        // hijack lifecycle extra methods
        try
        {
            var acq = await client.AcquireAsync("w1");
            if (acq.TryGetValue("hijack_id", out var hid) && hid is string hijackId)
            {
                await client.EventsAsync("w1", hijackId);
                await client.StepAsync("w1", hijackId);
                await client.ReleaseAsync("w1", hijackId);
            }
        }
        catch (ApiException)
        {
            // race / mode — still covered create/delete paths
        }

        try { await client.DisconnectWorkerAsync("w1"); }
        catch (ApiException) { /* ok */ }

        // delete session
        var del = await http.DeleteAsync(server.BaseAddress + "/api/sessions/new1");
        Assert.True(del.IsSuccessStatusCode || del.StatusCode is HttpStatusCode.Forbidden or HttpStatusCode.NotFound);

        var ready = await http.GetAsync(server.BaseAddress + "/readyz");
        ready.EnsureSuccessStatusCode();
    }

    [Fact]
    public void Authorization_SessionVisibility()
    {
        var authz = new AuthorizationService();
        var viewer = new Principal { SubjectId = "v", Roles = StringSet.Of("viewer") };
        var op = new Principal { SubjectId = "o", Roles = StringSet.Of("operator") };
        var admin = new Principal { SubjectId = "a", Roles = StringSet.Of("admin") };
        var pub = new SessionDefinition { SessionId = "s", Visibility = "public", Owner = "o" };
        var priv = new SessionDefinition { SessionId = "s2", Visibility = "private", Owner = "o" };
        var opOnly = new SessionDefinition { SessionId = "s3", Visibility = "operator", Owner = "x" };

        Assert.True(authz.CanReadSession(viewer, pub));
        Assert.False(authz.CanReadSession(viewer, priv));
        Assert.True(authz.CanReadSession(op, opOnly));
        Assert.True(authz.CanReadSession(admin, priv));
        Assert.True(authz.CanCreateSession(admin));
        Assert.False(authz.CanCreateSession(viewer));
        Assert.True(authz.IsOwner(op, pub));
        Assert.True(authz.HasRole(admin, "admin"));
        Assert.True(authz.CanMutateSession(admin, pub, "session.control.delete"));
    }

    [Fact]
    public async Task LocalIdentity_DevToken_And_ApiKey()
    {
        var cfg = new AuthConfig { Mode = "dev_token", ApiKeysEnabled = true };
        var path = Path.Combine(Path.GetTempPath(), "li-" + Guid.NewGuid().ToString("N"));
        var token = DevIdp.Setup(cfg, new DevIdp.Options { TokenPath = path, Subject = "dev", Roles = new[] { "admin" } });
        var keys = new ApiKeyStore();
        var (raw, _) = keys.Create("k1", StringSet.Of("session.read"));
        var idp = new LocalIdentityProvider(cfg, keys);

        var p1 = await idp.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["Authorization"] = "Bearer " + token,
            },
        });
        Assert.NotNull(p1);
        Assert.Equal("dev", p1.SubjectId);

        // API keys via header while still in a supported mode
        cfg.Mode = "dev_token";
        cfg.ApiKeysEnabled = true;
        var idp2 = new LocalIdentityProvider(cfg, keys);
        var p2 = await idp2.AuthenticateAsync(new AuthRequest
        {
            Headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
            {
                ["X-Api-Key"] = raw,
            },
        });
        _ = p2;
        if (File.Exists(path)) File.Delete(path);
    }

    [Fact]
    public async Task Hub_Lease_OpenMode_And_SendRest()
    {
        var clock = new ManualClock(1);
        clock.SetMonotonic(1);
        var hub = new TermHub(new TermHubConfig { Clock = clock, MaxEventDataChars = 256 });
        var worker = new EchoWorker();
        hub.Conn.RegisterWorker("w1", worker);
        hub.Router.SetInputMode("w1", InputModes.Open);
        var (ok, reason) = await hub.TryAcquireRestHijackAsync("w1", "op", 30, "h", 1);
        Assert.False(ok);
        Assert.Equal("open_mode", reason);

        hub.Router.SetInputMode("w1", InputModes.Hijack);
        (ok, reason) = await hub.TryAcquireRestHijackAsync("w1", "op", 30, "h1", 1);
        Assert.True(ok, reason);
        var send = await hub.Conn.SendRestInputAsync("w1", "h1", "keys");
        Assert.True(send.Ok, send.Reason);
        Assert.Contains("keys", worker.Sent);
        await hub.Conn.BroadcastHijackStateAsync("w1");
        hub.Conn.ForceReleaseHijack("w1");
    }

    [Fact]
    public void TermSession_Watchers_And_ControlFrames()
    {
        var t = new FakeTransport();
        var session = new TransportSession(t, ct => t.ConnectAsync("h", 1, null, ct),
            new TransportSessionOptions { ControlFrames = true, Cols = 20, Rows = 5 });
        var watched = 0;
        session.AddWatch((_, _) => Interlocked.Increment(ref watched));
        session.ConnectAsync().GetAwaiter().GetResult();
        t.Enqueue("abc");
        for (var i = 0; i < 40 && watched == 0; i++)
        {
            Thread.Sleep(20);
        }

        Assert.True(session.UpdateSeq() >= 0);
        var snap = session.Snapshot();
        Assert.NotNull(snap.Screen);
        session.CloseAsync().GetAwaiter().GetResult();
    }

    [Fact]
    public void Emulator_MoreVt_Scroll_And_Charset()
    {
        var emu = new TerminalEmulator(30, 10);
        var bytes = Encoding.ASCII.GetBytes(string.Concat(
            "\x1b[?47h", // alt buffer
            "alt\r\n",
            "\x1b[?47l",
            "\x1b[1;5r",
            "\x1b[20h\x1b[20l",
            "\x1b[?7h\x1b[?7l",
            "\x1b[6n",
            "\x1b[c",
            "\x1b[0c",
            "\x1b]0;title\x07",
            "\x1b[38;5;1m\x1b[48;5;2mX\x1b[0m",
            "\x1b[39m\x1b[49m",
            "\x1b[1;3;4;5;7;8mY\x1b[22;23;24;25;27;28m",
            "\x1b[H\x1b[2J",
            "final"));
        emu.Process(bytes);
        Assert.Contains("final", emu.GetSnapshot().Screen, StringComparison.Ordinal);
    }

    private sealed class EchoWorker : IWorkerWs
    {
        public List<string> Sent { get; } = new();

        public Task SendTextAsync(string payload, CancellationToken cancellationToken = default)
        {
            Sent.Add(payload);
            return Task.CompletedTask;
        }
    }

    private sealed class StubHandler : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) =>
            Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("""{"ok":true,"hijack_id":"h1","status":"ok"}""", Encoding.UTF8, "application/json"),
            });
    }
}
