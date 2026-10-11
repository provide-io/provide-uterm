//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Logging;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>A loopback Kestrel WebSocket endpoint at <c>/ws</c> whose session the test scripts.</summary>
internal sealed class LoopbackWsServer : IAsyncDisposable
{
    private readonly WebApplication _app;

    private LoopbackWsServer(WebApplication app, int port)
    {
        _app = app;
        Port = port;
    }

    public int Port { get; }
    public string Url => $"ws://127.0.0.1:{Port}/ws";

    public static async Task<LoopbackWsServer> StartAsync(Func<WebSocket, HttpContext, Task> handler)
    {
        var port = C100Server.FreePort();
        var builder = WebApplication.CreateBuilder(new WebApplicationOptions { Args = Array.Empty<string>() });
        builder.Logging.ClearProviders();
        builder.WebHost.UseKestrel().UseUrls($"http://127.0.0.1:{port}");
        var app = builder.Build();
        app.UseWebSockets();
        app.Map("/ws", async ctx =>
        {
            using var ws = await ctx.WebSockets.AcceptWebSocketAsync();
            await handler(ws, ctx);
        });
        await app.StartAsync();
        return new LoopbackWsServer(app, port);
    }

    public async ValueTask DisposeAsync()
    {
        await _app.StopAsync();
        await _app.DisposeAsync();
    }
}
