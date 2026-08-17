//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.WebSockets;
using Provide.Uterm.Hub;
using Provide.Uterm.Server;
using Provide.Uterm.ServerAuth;
using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

/// <summary>
/// A browser socket with no credentials is refused before the upgrade.
///
/// This had no coverage at all, and the hole it left was not small: the handler
/// authenticated, ignored the result, and asked only whether the principal
/// could READ the session. CanReadSession returns true for visibility "public",
/// which is the shipped default (ServerConfig DefaultSessionVisibility), so an
/// anonymous socket was admitted to any public session and could send input
/// frames into the terminal. The hostile-client burst probe measured it: 200 of
/// 200 unauthenticated connects accepted against C#, 0 of 200 against Python
/// and Go.
///
/// Python refuses every anonymous websocket at the gate
/// (app/factory_impl.py → WS 1008 "authentication required") before authorization
/// is consulted, and the worker socket in this port already refused with its
/// bearer. These pin the browser socket to the same rule, including for a
/// session that does not exist — an unauthenticated caller must not be able to
/// tell 403 from 404 and enumerate sessions.
/// </summary>
public sealed class BrowserWsAnonymousRefusalTests
{
    private static int FreePort()
    {
        var listener = new System.Net.Sockets.TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    private static async Task<(UtermServer Server, int Port, string Token)> BootAsync()
    {
        var port = FreePort();
        var cfg = UtermServerConfig.Default();
        cfg.Server.Host = "127.0.0.1";
        cfg.Server.Port = port;
        cfg.Server.PublicBaseUrl = $"http://127.0.0.1:{port}";
        cfg.Auth.Mode = "dev_token";
        // Public on purpose: this is the default posture, and it is exactly the
        // one that used to let an anonymous socket through.
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
            TokenPath = Path.Combine(Path.GetTempPath(), "bwsanon-" + Guid.NewGuid().ToString("N")),
            Subject = "admin",
            Roles = new[] { "admin" },
        });
        var clock = new RealClock();
        var hub = new TermHub(new TermHubConfig { Clock = clock });
        hub.Registry.Put("demo", new WorkerTermState());
        var server = new UtermServer(new ServerDeps
        {
            Hub = hub,
            Auth = new LocalIdentityProvider(cfg.Auth, new ApiKeyStore()),
            Authz = new AuthorizationService(),
            Config = cfg,
            Registry = new InMemorySessionRegistry(cfg.Sessions),
            Clock = clock,
            Version = "bwsanon",
        });
        server.Build(new[] { $"http://127.0.0.1:{port}" });
        await server.StartAsync();
        return (server, port, token);
    }

    private static async Task<WebSocketException> RefusedAsync(int port, string workerId)
    {
        var ws = new ClientWebSocket();
        return await Assert.ThrowsAsync<WebSocketException>(() =>
            ws.ConnectAsync(new Uri($"ws://127.0.0.1:{port}/ws/browser/{workerId}/term"), CancellationToken.None));
    }

    [Fact]
    public async Task An_unauthenticated_browser_socket_is_refused_on_a_public_session()
    {
        var (server, port, _) = await BootAsync();
        try
        {
            var refused = await RefusedAsync(port, "demo");

            // 401 rather than a completed upgrade. "public" governs who may READ
            // a session, never whether the caller must say who they are.
            Assert.Contains("401", refused.Message, StringComparison.Ordinal);
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task An_unauthenticated_socket_cannot_tell_a_missing_session_from_a_real_one()
    {
        var (server, port, _) = await BootAsync();
        try
        {
            var real = await RefusedAsync(port, "demo");
            var missing = await RefusedAsync(port, "no-such-session");

            // Both 401. Refusing after the registry lookup would answer 403 for
            // one and 404 for the other, which enumerates sessions for anyone
            // who can reach the port.
            Assert.Contains("401", real.Message, StringComparison.Ordinal);
            Assert.Contains("401", missing.Message, StringComparison.Ordinal);
        }
        finally
        {
            await server.StopAsync();
        }
    }

    [Fact]
    public async Task An_authenticated_browser_socket_still_connects()
    {
        // The negative control. A refusal test passes just as well against a
        // server that refuses everyone, which would be its own outage.
        var (server, port, token) = await BootAsync();
        try
        {
            var ws = new ClientWebSocket();
            ws.Options.SetRequestHeader("Authorization", "Bearer " + token);
            await ws.ConnectAsync(new Uri($"ws://127.0.0.1:{port}/ws/browser/demo/term"), CancellationToken.None);

            Assert.Equal(WebSocketState.Open, ws.State);
            ws.Dispose();
        }
        finally
        {
            await server.StopAsync();
        }
    }
}
