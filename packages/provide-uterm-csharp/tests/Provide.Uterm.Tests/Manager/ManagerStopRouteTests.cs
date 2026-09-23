//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using Provide.Uterm.Manager;

namespace Provide.Uterm.Tests.Manager;

public class ManagerStopRouteTests
{
    [Fact]
    public async Task Stop_Path_Without_An_Id_Is_404_Not_A_Throw()
    {
        // "/swarm/agents/stop" matches both the "/swarm/agents/" prefix and the
        // "/stop" suffix with no id between them. It used to enter the stop route,
        // throw from the id slice ([14..13]) and echo the exception in a 500.
        var mgr = new AgentManager(new ManagerConfig { Host = "127.0.0.1", Port = FreePort() });
        await using var server = new ManagerServer(mgr);
        await server.StartAsync();
        using var http = new HttpClient { BaseAddress = new Uri(server.BaseAddress!) };

        var noId = await http.PostAsync("/swarm/agents/stop", new StringContent(""));
        Assert.Equal(HttpStatusCode.NotFound, noId.StatusCode);
        Assert.Equal("{\"detail\":\"not found\"}", await noId.Content.ReadAsStringAsync());

        // An unknown id still takes the stop route and answers its own 404.
        var unknown = await http.PostAsync("/swarm/agents/x/stop", new StringContent(""));
        Assert.Equal(HttpStatusCode.NotFound, unknown.StatusCode);
        Assert.Equal("{\"ok\":false}", await unknown.Content.ReadAsStringAsync());
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
