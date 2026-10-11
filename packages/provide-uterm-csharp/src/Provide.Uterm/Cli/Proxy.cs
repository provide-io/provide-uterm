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
        /// <summary>SSH username (required for --transport ssh).</summary>
        public string SshUser { get; init; } = "";
        /// <summary>SSH password (optional if key auth is added later).</summary>
        public string SshPassword { get; init; } = "";
        /// <summary>OpenSSH known_hosts file paths (required unless insecure).</summary>
        public List<string> KnownHostsFiles { get; init; } = new();
        /// <summary>Explicit insecure opt-in; never the default.</summary>
        public bool InsecureSkipHostKeyVerify { get; init; }
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
        }
        catch (OperationCanceledException)
        {
            // Cancelled before the listener came up (Ctrl-C / test cancellation).
            return;
        }

        // Cancellation is the normal end: wait for it without an exception.
        var cancelled = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        await using (cancellationToken.Register(() => cancelled.TrySetResult()).ConfigureAwait(false))
        {
            await cancelled.Task.ConfigureAwait(false);
        }

        await app.StopAsync(CancellationToken.None).ConfigureAwait(false);
    }

    /// <summary>
    /// Builds the upstream transport for <paramref name="opts"/>. A seam so the
    /// bridge can be driven against an in-memory transport; defaults to
    /// <see cref="CreateTransport"/>.
    /// </summary>
    internal static Func<Options, (IConnectionTransport Transport, ConnectOptions Connect)> TransportFactory { get; set; } = CreateTransport;

    internal static (IConnectionTransport Transport, ConnectOptions Connect) CreateTransport(Options opts)
    {
        var connectOpts = new ConnectOptions();
        var transport = opts.Transport.ToLowerInvariant();
        if (transport is "websocket" or "ws" or "wss")
        {
            var url = string.IsNullOrEmpty(opts.UpstreamWsUrl) ? $"wss://{opts.Host}:{opts.BbsPort}" : opts.UpstreamWsUrl;
            connectOpts.Ws = new WsOptions { Url = url, SendBinary = false };
            return (new WebSocketTransport(), connectOpts);
        }

        if (transport == "ssh")
        {
            connectOpts.Ssh = new SshOptions
            {
                User = opts.SshUser,
                Password = opts.SshPassword,
                KnownHostsFiles = opts.KnownHostsFiles.ToList(),
                InsecureSkipHostKeyVerify = opts.InsecureSkipHostKeyVerify,
            };
            return (new SshTransport(), connectOpts);
        }

        return (new TelnetTransport(), connectOpts);
    }

    internal static async Task BridgeAsync(WebSocket ws, Options opts, CancellationToken ct)
    {
        var (tr, connectOpts) = TransportFactory(opts);
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
        }, CancellationToken.None);

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
        }, CancellationToken.None);

        // Either pump ending cancels the other (each cancels the linked source
        // on its way out) and both swallow their own faults, so the join
        // cannot throw and the linked source outlives them.
        await Task.WhenAll(browserToRemote, remoteToBrowser).ConfigureAwait(false);
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
