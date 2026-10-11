//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using Provide.Uterm.Defaults;

namespace Provide.Uterm.Gateway;

/// <summary>
/// Telnet gateway listener that binds and accepts when configured.
/// Port of packages/provide-uterm-go/gateway/telnet.go (accept loop surface).
/// </summary>
public sealed class TelnetGateway : IAsyncDisposable
{
    private readonly TcpAcceptLoop _loop = new();
    public Func<TcpClient, CancellationToken, Task>? OnAccept { get; set; }

    /// <summary>
    /// When false (default), only loopback binds are allowed — telnet is
    /// plaintext/unauthenticated (matches Go/Python fail-closed gate).
    /// </summary>
    public bool AllowUnauthenticated { get; set; }

    public int Port { get; private set; }

    public Task StartAsync(string host = TerminalDefaults.BindAll, int port = 0, CancellationToken cancellationToken = default)
    {
        GatewayBindPolicy.RequireUnauthenticatedAllowed(host, AllowUnauthenticated);
        var bindPort = port == 0 ? TerminalDefaults.GatewayTelnetPort : port;
        Port = _loop.Start(host, bindPort, () => OnAccept, cancellationToken);
        return Task.CompletedTask;
    }

    public Task StopAsync() => _loop.StopAsync();

    public ValueTask DisposeAsync() => new(StopAsync());
}

/// <summary>
/// SSH gateway TCP accept loop. Callers attach <see cref="OnAccept"/> to run
/// SSH.NET handshakes (same layering as Go gateway/ssh.go).
/// </summary>
public sealed class SshGateway : IAsyncDisposable
{
    private readonly TcpAcceptLoop _loop = new();
    public Func<TcpClient, CancellationToken, Task>? OnAccept { get; set; }
    public int Port { get; private set; }

    public Task StartAsync(string host = TerminalDefaults.BindAll, int port = 0, CancellationToken cancellationToken = default)
    {
        var bindPort = port == 0 ? TerminalDefaults.GatewaySshPort : port;
        Port = _loop.Start(host, bindPort, () => OnAccept, cancellationToken);
        return Task.CompletedTask;
    }

    public Task StopAsync() => _loop.StopAsync();

    public ValueTask DisposeAsync() => new(StopAsync());
}

/// <summary>
/// The accept loop both gateways share: hand each accepted client to the
/// current handler (read per accept, so it may be set after start), or close
/// it when there is none — which still proves the bind/accept path works.
/// </summary>
internal sealed class TcpAcceptLoop
{
    private TcpListener? _listener;
    private CancellationTokenSource? _cts;
    private Task? _acceptLoop;

    public int Start(
        string host,
        int port,
        Func<Func<TcpClient, CancellationToken, Task>?> handler,
        CancellationToken cancellationToken)
    {
        var ip = host is "0.0.0.0" or "" ? IPAddress.Any : IPAddress.Parse(host);
        var listener = new TcpListener(ip, port);
        listener.Start();
        _listener = listener;
        _cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        _acceptLoop = AcceptLoopAsync(listener, handler, _cts.Token);
        return ((IPEndPoint)listener.LocalEndpoint).Port;
    }

    private static async Task AcceptLoopAsync(
        TcpListener listener,
        Func<Func<TcpClient, CancellationToken, Task>?> handlerOf,
        CancellationToken ct)
    {
        while (true)
        {
            TcpClient client;
            try
            {
                client = await listener.AcceptTcpClientAsync(ct).ConfigureAwait(false);
            }
            catch (Exception ex) when (ex is OperationCanceledException or ObjectDisposedException or SocketException)
            {
                // Cancelled, or the listener was stopped under the accept.
                return;
            }

            var handler = handlerOf();
            if (handler is null)
            {
                client.Dispose();
                continue;
            }

            _ = Task.Run(
                async () =>
                {
                    try
                    {
                        await handler(client, ct).ConfigureAwait(false);
                    }
                    catch
                    {
                        client.Dispose();
                    }
                },
                CancellationToken.None);
        }
    }

    public async Task StopAsync()
    {
        if (_cts is null)
        {
            return; // never started, or already stopped
        }

        await _cts.CancelAsync().ConfigureAwait(false);
        _listener!.Stop();
        // The loop catches its own accept failures, so this never throws.
        await _acceptLoop!.ConfigureAwait(false);
        _cts.Dispose();
        _cts = null;
        _listener = null;
        _acceptLoop = null;
    }
}
