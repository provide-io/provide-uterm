//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using Provide.Uterm.Cli;

namespace Provide.Uterm.Tests.Cli;

/// <summary>
/// Proxy host real-path tests (handler graph + health; upstream bridge covered via unit Bridge path).
/// </summary>
public class ProxyBridgeTests
{
    [Fact]
    public async Task Proxy_Starts_And_Serves_Health()
    {
        var proxyPort = FreePort();
        var opts = new ProxyCommand.Options
        {
            Host = "127.0.0.1",
            BbsPort = 23,
            Bind = "127.0.0.1",
            Port = proxyPort,
            Path = "/ws/terminal",
            Transport = "telnet",
        };
        await using var app = ProxyCommand.Build(opts, new[] { $"http://127.0.0.1:{proxyPort}" });
        await app.StartAsync();
        try
        {
            using var http = new HttpClient();
            var health = await http.GetStringAsync($"http://127.0.0.1:{proxyPort}/health");
            Assert.Contains("uterm-proxy", health, StringComparison.Ordinal);
            Assert.Contains("\"status\":\"ok\"", health, StringComparison.Ordinal);
        }
        finally
        {
            await app.StopAsync();
        }
    }

    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var p = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return p;
    }
}
