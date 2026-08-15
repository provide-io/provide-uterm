//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;
using System.Text;
using Provide.Uterm.ControlChannel;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

/// <summary>
/// A browser socket is rate limited, per connection, on two separate budgets.
///
/// This port validated <c>browser_rate_limit_per_sec</c> and then never consumed
/// it: the value reached <c>TermHubConfig</c> and stopped, so a browser could
/// send input as fast as it liked. The keystroke path into somebody's terminal
/// had no ceiling at all.
///
/// The reference (bridge/routes/websockets_browser.py, dispatch_browser_event)
/// holds two buckets, both created inside the WebSocket handler and therefore
/// per connection rather than per worker: <c>browser_rate_limit_per_sec</c> for
/// <c>input</c> frames and <c>browser_control_rate_limit_per_sec</c> for every
/// other control frame. Over budget it counts, logs, sends an
/// <c>error</c>/<c>rate_limited</c> frame back, and **drops the message** — it
/// does not close the socket, because one impatient client should not lose its
/// session.
/// </summary>
public sealed class BrowserWsRateLimitTests
{
    private static int FreePort()
    {
        var listener = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        listener.Start();
        var port = ((System.Net.IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    private static async Task<(UtermServer Server, string Base, string Token)> BootAsync(
        double inputRate,
        double controlRate)
    {
        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "demo",
            DisplayName = "Demo",
            ConnectorType = "shell",
            Visibility = "public",
            Owner = "admin",
        });
        var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
        {
            TokenPath = Path.Combine(Path.GetTempPath(), "bwsrl-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = new[] { "admin" },
        });
        var clock = new RealClock();
        var hub = new TermHub(new TermHubConfig
        {
            Clock = clock,
            BrowserRateLimitPerSec = inputRate,
            BrowserControlRateLimitPerSec = controlRate,
            OnMetric = (name, value) => Provide.Telemetry.Metrics.Counter(name).Add(value),
        });
        hub.Registry.Put("demo", new WorkerTermState());
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Clock = clock,
            Version = "bwsrl",
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        return (server, $"http://127.0.0.1:{port}", token);
    }

    private static async Task<ClientWebSocket> ConnectAsync(string baseUrl, string token)
    {
        var ws = new ClientWebSocket();
        ws.Options.SetRequestHeader("Authorization", "Bearer " + token);
        await ws.ConnectAsync(new Uri($"ws://127.0.0.1:{new Uri(baseUrl).Port}/ws/browser/demo/term"), CancellationToken.None);
        // Drain the server's opening hello so later reads see only our answers.
        var buffer = new byte[16384];
        await ws.ReceiveAsync(buffer, CancellationToken.None);
        return ws;
    }

    private static async Task SendAsync(ClientWebSocket ws, Dictionary<string, object?> frame)
    {
        var text = ControlChannelCodec.EncodeControlFrame(frame);
        await ws.SendAsync(Encoding.UTF8.GetBytes(text), WebSocketMessageType.Text, true, CancellationToken.None);
    }

    /// <summary>Read frames until one reports rate_limited, or the budget of reads runs out.</summary>
    private static async Task<bool> SawRateLimitedAsync(ClientWebSocket ws, int reads = 40)
    {
        var buffer = new byte[16384];
        for (var attempt = 0; attempt < reads; attempt++)
        {
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(2));
            WebSocketReceiveResult result;
            try
            {
                result = await ws.ReceiveAsync(buffer, timeout.Token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return false;
            }

            var text = Encoding.UTF8.GetString(buffer, 0, result.Count);
            if (text.Contains("rate_limited", StringComparison.Ordinal))
            {
                return true;
            }
        }

        return false;
    }

    [Fact]
    public async Task AnInputFloodIsRefusedAndTold()
    {
        var (server, baseUrl, token) = await BootAsync(inputRate: 2, controlRate: 1000);
        await using (server)
        {
            using var ws = await ConnectAsync(baseUrl, token);

            for (var i = 0; i < 30; i++)
            {
                await SendAsync(ws, new Dictionary<string, object?> { ["type"] = "input", ["data"] = "x" });
            }

            Assert.True(await SawRateLimitedAsync(ws), "a browser input flood must be told it was refused");
        }
    }

    [Fact]
    public async Task AControlFloodIsRefusedOnItsOwnBudget()
    {
        // Separate budgets, so a resize storm cannot spend the keystroke
        // allowance and a keystroke storm cannot silence resizes.
        var (server, baseUrl, token) = await BootAsync(inputRate: 1000, controlRate: 2);
        await using (server)
        {
            using var ws = await ConnectAsync(baseUrl, token);

            for (var i = 0; i < 30; i++)
            {
                await SendAsync(ws, new Dictionary<string, object?> { ["type"] = "resize", ["cols"] = 80 + i });
            }

            Assert.True(await SawRateLimitedAsync(ws), "a browser control flood must be told it was refused");
        }
    }

    [Fact]
    public async Task TheSocketStaysOpenAfterARefusal()
    {
        // The message is dropped, not the connection. Closing on a burst would
        // cost an operator their session for typing quickly.
        var (server, baseUrl, token) = await BootAsync(inputRate: 2, controlRate: 1000);
        await using (server)
        {
            using var ws = await ConnectAsync(baseUrl, token);

            for (var i = 0; i < 30; i++)
            {
                await SendAsync(ws, new Dictionary<string, object?> { ["type"] = "input", ["data"] = "x" });
            }

            Assert.True(await SawRateLimitedAsync(ws));
            Assert.Equal(WebSocketState.Open, ws.State);
        }
    }

    [Fact]
    public async Task AGenerousBudgetRefusesNothing()
    {
        // The guard against a limiter that refuses regardless: with a budget
        // nobody could exceed, no refusal may appear.
        var (server, baseUrl, token) = await BootAsync(inputRate: 10_000, controlRate: 10_000);
        await using (server)
        {
            using var ws = await ConnectAsync(baseUrl, token);

            for (var i = 0; i < 20; i++)
            {
                await SendAsync(ws, new Dictionary<string, object?> { ["type"] = "input", ["data"] = "x" });
            }

            Assert.False(await SawRateLimitedAsync(ws, reads: 3), "a generous budget must refuse nothing");
        }
    }

    [Fact]
    public async Task EachConnectionGetsItsOwnBudget()
    {
        // The reference builds both buckets inside the WebSocket handler, so they
        // are per connection. Sharing them per worker would let one browser
        // starve every other viewer of the same session.
        var (server, baseUrl, token) = await BootAsync(inputRate: 2, controlRate: 1000);
        await using (server)
        {
            using var greedy = await ConnectAsync(baseUrl, token);
            for (var i = 0; i < 30; i++)
            {
                await SendAsync(greedy, new Dictionary<string, object?> { ["type"] = "input", ["data"] = "x" });
            }

            Assert.True(await SawRateLimitedAsync(greedy));

            using var fresh = await ConnectAsync(baseUrl, token);
            await SendAsync(fresh, new Dictionary<string, object?> { ["type"] = "input", ["data"] = "y" });

            Assert.False(
                await SawRateLimitedAsync(fresh, reads: 3),
                "a second browser must not inherit the first one's spent budget");
        }
    }

    [Fact]
    public async Task TheConfiguredCeilingReachesTheLimiterThroughTheHostedFactory()
    {
        // The tests above build the hub directly, which proves the limiter works
        // but not that a deployment gets it. This drives the path that was
        // actually broken: `browser_rate_limit_per_sec` in a config file, through
        // `ServerFactory.CreateFromConfig`, to a bucket that refuses. The value
        // used to reach `TermHubConfig` and stop there.
        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        // 1.0 is the floor the config schema permits — the tightest ceiling an
        // operator can actually write, and enough to be outrun by a loop.
        cfg.BrowserRateLimitPerSec = 1.0;
        cfg.Sessions.Add(new SessionDefinition
        {
            SessionId = "demo",
            DisplayName = "Demo",
            ConnectorType = "shell",
            Visibility = "public",
            Owner = "admin",
        });

        var (server, _) = ServerFactory.CreateFromConfig(cfg, "bwsrl-hosted");
        await using (server)
        {
            server.Build(new[] { $"http://127.0.0.1:{port}" });
            await server.StartAsync();
            var token = DevIdp.Setup(cfg.Auth, new DevIdp.Options
            {
                TokenPath = Path.Combine(Path.GetTempPath(), "bwsrl-h-" + Guid.NewGuid().ToString("N")),
                Subject = "admin",
                Roles = new[] { "admin" },
            });
            server.HubForTests.Registry.Put("demo", new WorkerTermState());

            using var ws = await ConnectAsync($"http://127.0.0.1:{port}", token);
            for (var i = 0; i < 30; i++)
            {
                await SendAsync(ws, new Dictionary<string, object?> { ["type"] = "input", ["data"] = "x" });
            }

            Assert.True(await SawRateLimitedAsync(ws), "a configured ceiling must reach the limiter");
        }
    }
}
