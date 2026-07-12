//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;
using System.Text;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Http;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Provide.Uterm.Defaults;
using Provide.Uterm.Transports;

namespace Provide.Uterm.Cli;

/// <summary>
/// `uterm proxy` — browser WebSocket → remote telnet/SSH.
/// Port of packages/provide-uterm-go/cli/proxy.go.
/// </summary>
public static class ProxyCommand
{
    public sealed class Options
    {
        public string Host { get; init; } = "127.0.0.1";
        public int BbsPort { get; init; } = TerminalDefaults.TelnetRemotePort;
        public string Bind { get; init; } = TerminalDefaults.BindAll;
        public int Port { get; init; } = TerminalDefaults.ProxyPort;
        public string Path { get; init; } = TerminalDefaults.ProxyWsPath;
        /// <summary>telnet | ssh | websocket</summary>
        public string Transport { get; init; } = "telnet";
        /// <summary>Upstream WSS/WS URL when Transport is websocket.</summary>
        public string? UpstreamWsUrl { get; init; }
    }

    /// <summary>Build the ASP.NET app for the proxy. Exposed for in-process tests.</summary>
    public static WebApplication Build(Options opts, string[]? urls = null)
    {
        var builder = WebApplication.CreateBuilder(new WebApplicationOptions
        {
            Args = Array.Empty<string>(),
            ApplicationName = typeof(ProxyCommand).Assembly.FullName,
        });
        builder.Logging.ClearProviders();
        builder.WebHost.UseKestrel();
        if (urls is { Length: > 0 })
        {
            builder.WebHost.UseUrls(urls);
        }
        else
        {
            builder.WebHost.UseUrls($"http://{opts.Bind}:{opts.Port}");
        }

        var app = builder.Build();
        app.UseWebSockets();
        app.MapGet("/health", () => Results.Json(new { status = "ok", service = "uterm-proxy" }));
        app.Map(opts.Path, async ctx =>
        {
            if (!ctx.WebSockets.IsWebSocketRequest)
            {
                ctx.Response.StatusCode = StatusCodes.Status400BadRequest;
                await ctx.Response.WriteAsync("expected websocket");
                return;
            }

            using var ws = await ctx.WebSockets.AcceptWebSocketAsync();
            await BridgeAsync(ws, opts, ctx.RequestAborted).ConfigureAwait(false);
        });
        return app;
    }

    public static async Task RunAsync(Options opts, CancellationToken cancellationToken = default)
    {
        await using var app = Build(opts);
        try
        {
            await app.StartAsync(cancellationToken).ConfigureAwait(false);
            await Task.Delay(Timeout.Infinite, cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            // Expected on Ctrl-C / test cancellation.
        }
        finally
        {
            try
            {
                await app.StopAsync(CancellationToken.None).ConfigureAwait(false);
            }
            catch
            {
                // ignore stop races after cancel
            }
        }
    }

    internal static async Task BridgeAsync(WebSocket ws, Options opts, CancellationToken ct)
    {
        IConnectionTransport tr;
        var connectOpts = new ConnectOptions();
        var transport = opts.Transport.ToLowerInvariant();
        if (transport is "websocket" or "ws" or "wss")
        {
            tr = new WebSocketTransport();
            var url = opts.UpstreamWsUrl;
            if (string.IsNullOrEmpty(url))
            {
                url = $"wss://{opts.Host}:{opts.BbsPort}";
            }

            connectOpts.Ws = new WsOptions { Url = url, SendBinary = false };
        }
        else if (transport == "ssh")
        {
            tr = new SshTransport();
            connectOpts.Ssh = new SshOptions { InsecureSkipHostKeyVerify = true };
        }
        else
        {
            tr = new TelnetTransport();
        }

        try
        {
            await tr.ConnectAsync(opts.Host, opts.BbsPort, connectOpts, ct).ConfigureAwait(false);
        }
        catch
        {
            if (ws.State == WebSocketState.Open)
            {
                await ws.CloseAsync(WebSocketCloseStatus.InternalServerError, "upstream connect failed", CancellationToken.None)
                    .ConfigureAwait(false);
            }

            return;
        }

        using var linked = CancellationTokenSource.CreateLinkedTokenSource(ct);
        var token = linked.Token;
        var browserToRemote = Task.Run(async () =>
        {
            var buf = new byte[4096];
            try
            {
                while (!token.IsCancellationRequested && ws.State == WebSocketState.Open)
                {
                    var result = await ws.ReceiveAsync(buf, token).ConfigureAwait(false);
                    if (result.MessageType == WebSocketMessageType.Close)
                    {
                        break;
                    }

                    if (result.Count > 0)
                    {
                        var slice = new byte[result.Count];
                        Buffer.BlockCopy(buf, 0, slice, 0, result.Count);
                        await tr.SendAsync(slice, token).ConfigureAwait(false);
                    }
                }
            }
            catch
            {
            }
            finally
            {
                linked.Cancel();
            }
        }, token);

        var remoteToBrowser = Task.Run(async () =>
        {
            try
            {
                var poll = TimeSpan.FromMilliseconds(TerminalDefaults.ProxyPollMs);
                while (!token.IsCancellationRequested && ws.State == WebSocketState.Open)
                {
                    byte[] data;
                    try
                    {
                        data = await tr.ReceiveAsync(4096, poll, token).ConfigureAwait(false);
                    }
                    catch
                    {
                        break;
                    }

                    if (data.Length == 0)
                    {
                        continue;
                    }

                    // TEXT frames (UTF-8) to match Python/Go browser frontend.
                    var text = Encoding.UTF8.GetString(data);
                    var bytes = Encoding.UTF8.GetBytes(text);
                    await ws.SendAsync(bytes, WebSocketMessageType.Text, true, token).ConfigureAwait(false);
                }
            }
            catch
            {
            }
            finally
            {
                linked.Cancel();
            }
        }, token);

        try
        {
            await Task.WhenAny(browserToRemote, remoteToBrowser).ConfigureAwait(false);
        }
        finally
        {
            linked.Cancel();
            try { await tr.DisconnectAsync().ConfigureAwait(false); } catch { /* ignore */ }
            if (tr is IAsyncDisposable ad)
            {
                await ad.DisposeAsync().ConfigureAwait(false);
            }
            else if (tr is IDisposable d)
            {
                d.Dispose();
            }
        }
    }
}
