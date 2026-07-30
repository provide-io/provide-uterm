//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Collections.Concurrent;
using System.Net;
using System.Net.WebSockets;
using System.Text;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;
using Xunit;

namespace Provide.Uterm.Tests.Hub;

/// <summary>
/// What the hub says out loud when it refuses a worker.
///
/// The port had no logging surface on the hub at all — no logger, no sink, no
/// <c>Log</c> — so the two decisions the reference logs had to become metric
/// counters instead (<c>worker_hello_invalid_mode_total</c>,
/// <c>worker_hello_mode_blocked_total</c>). A counter is the actionable half: an
/// operator can see that refusals are happening. It cannot say <em>which</em>
/// worker was refused, which is the one fact somebody debugging a session stuck
/// in <c>hijack</c> actually needs — a fleet of a hundred workers reconnecting
/// produces one number and no way to find the culprit.
///
/// The reference logs both at <c>warning</c>:
/// <c>worker_hello_invalid_mode worker_id=… input_mode=…</c> from
/// <c>bridge/routes/websockets_worker.py</c>, and
/// <c>worker_hello_mode_blocked worker_id=…</c> from
/// <c>bridge/hub/connection.py:set_worker_hello</c>. Each is emitted here at the
/// same place in the same code path, so the two ports can be read side by side.
///
/// The sink is a callback on <see cref="TermHubConfig"/>, injected exactly the
/// way <c>OnMetric</c> and <c>OnHijackChanged</c> already are, rather than a
/// logging-framework dependency this port does not otherwise carry. Unset it is
/// a no-op, so an embedder that wants nothing gets what it had before.
/// </summary>
public sealed class HubLogSinkTests
{
    private const string Worker = "w-log";

    private static (TermHub Hub, List<(string Level, string Message)> Log) HubWithSink()
    {
        var log = new List<(string, string)>();
        var hub = new TermHub(new TermHubConfig { OnLog = (level, message) => log.Add((level, message)) });
        hub.Registry.Put(Worker, new WorkerTermState());
        return (hub, log);
    }

    // -- The surface itself -------------------------------------------------

    /// <summary>The sink receives what the hub writes, level and message.</summary>
    [Fact]
    public void The_Sink_Receives_What_The_Hub_Logs()
    {
        var (hub, log) = HubWithSink();

        hub.Log("warning", "something happened");

        Assert.Equal(("warning", "something happened"), Assert.Single(log));
    }

    /// <summary>
    /// And with no sink injected the hub logs into nothing rather than throwing —
    /// which is what keeps every existing embedder, and every test that builds a
    /// bare <see cref="TermHubConfig"/>, working unchanged.
    /// </summary>
    [Fact]
    public void Logging_Without_A_Sink_Is_A_No_Op()
    {
        var hub = new TermHub(new TermHubConfig());
        hub.Log("warning", "into the void");
    }

    // -- The refused hello --------------------------------------------------

    /// <summary>
    /// The refusal a counter could not explain. A hello that would lower a
    /// decided mode is refused, and the log names the worker that sent it — the
    /// difference between "refusals are happening" and "this session is stuck
    /// because that worker keeps announcing open".
    /// </summary>
    [Fact]
    public void A_Refused_Hello_Names_The_Worker_It_Refused()
    {
        var (hub, log) = HubWithSink();
        var (ok, _) = hub.Router.SetInputMode(Worker, InputModes.Hijack);
        Assert.True(ok);

        Assert.False(hub.Conn.SetWorkerHello(Worker, InputModes.Open));

        var (level, message) = Assert.Single(log);
        Assert.Equal("warning", level);
        Assert.Contains("worker_hello_mode_blocked", message, StringComparison.Ordinal);
        Assert.Contains(Worker, message, StringComparison.Ordinal);
    }

    /// <summary>
    /// An applied hello is not an incident and says nothing. A log that fires on
    /// the common path is a log an operator learns to ignore.
    /// </summary>
    [Fact]
    public void An_Applied_Hello_Says_Nothing()
    {
        var (hub, log) = HubWithSink();

        Assert.True(hub.Conn.SetWorkerHello(Worker, InputModes.Open));

        Assert.Empty(log);
    }

    /// <summary>
    /// A hello for a worker the hub never registered is refused for a different
    /// reason — there is no session to have a mode — so it is not reported as a
    /// blocked mode change. Conflating the two would have an operator hunting a
    /// decision nobody made.
    /// </summary>
    [Fact]
    public void A_Hello_For_An_Unknown_Worker_Is_Not_Reported_As_A_Blocked_Mode()
    {
        var log = new List<(string, string)>();
        var hub = new TermHub(new TermHubConfig { OnLog = (level, message) => log.Add((level, message)) });

        Assert.False(hub.Conn.SetWorkerHello("nobody", InputModes.Open));

        Assert.Empty(log);
    }

    // -- The invalid mode, over a live worker socket -------------------------

    /// <summary>
    /// The other logged decision, reached the way it is reached in production: a
    /// worker socket sending a <c>worker_hello</c> whose <c>input_mode</c> is
    /// neither <c>hijack</c> nor <c>open</c>. It is counted and ignored rather
    /// than refused — a worker announcing nonsense is not a reason to drop a
    /// working session — and the log carries both the worker and the value it
    /// sent, so the operator can go and fix the worker.
    /// </summary>
    [Fact]
    public async Task A_Worker_Announcing_Nonsense_Is_Named_Along_With_What_It_Said()
    {
        var log = new ConcurrentQueue<string>();
        var metrics = new ConcurrentQueue<string>();
        var cfg = UtermServerConfig.Default();
        var port = FreePort();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "hub-log-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = ["admin"],
        });
        var server = new UtermServer(new ServerDeps
        {
            Hub = new TermHub(new TermHubConfig
            {
                OnLog = (_, message) => log.Enqueue(message),
                OnMetric = (name, _) => metrics.Enqueue(name),
            }),
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Version = "hub-log",
        });
        server.Build([$"http://127.0.0.1:{port}"]);
        await server.StartAsync();
        await using (server)
        {
            using var ws = new ClientWebSocket();
            await ws.ConnectAsync(new Uri($"ws://127.0.0.1:{port}/ws/worker/provide-shell/term"), CancellationToken.None);
            var frame = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
            {
                ["type"] = "worker_hello",
                ["input_mode"] = "sideways",
            });
            await ws.SendAsync(
                Encoding.UTF8.GetBytes(frame), WebSocketMessageType.Text, true, CancellationToken.None);

            var line = await WaitFor(log, "worker_hello_invalid_mode");
            Assert.Contains("provide-shell", line, StringComparison.Ordinal);
            Assert.Contains("sideways", line, StringComparison.Ordinal);
            Assert.Contains("worker_hello_invalid_mode_total", metrics);
        }
    }

    private static async Task<string> WaitFor(ConcurrentQueue<string> log, string needle)
    {
        var deadline = DateTime.UtcNow + TimeSpan.FromSeconds(10);
        while (DateTime.UtcNow < deadline)
        {
            foreach (var line in log)
            {
                if (line.Contains(needle, StringComparison.Ordinal)) return line;
            }

            await Task.Delay(20);
        }

        Assert.Fail($"no log line containing '{needle}' arrived; saw: {string.Join(" | ", log)}");
        return "";
    }

    private static int FreePort()
    {
        var listener = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }
}
