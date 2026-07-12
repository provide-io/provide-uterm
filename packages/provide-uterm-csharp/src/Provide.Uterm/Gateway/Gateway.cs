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
    private TcpListener? _listener;
    private CancellationTokenSource? _cts;
    private Task? _acceptLoop;
    public Func<TcpClient, CancellationToken, Task>? OnAccept { get; set; }

    public int Port { get; private set; }

    public Task StartAsync(string host = TerminalDefaults.BindAll, int port = 0, CancellationToken cancellationToken = default)
    {
        if (port == 0)
        {
            port = TerminalDefaults.GatewayTelnetPort;
        }

        var ip = host is "0.0.0.0" or "" ? IPAddress.Any : IPAddress.Parse(host);
        var listener = new TcpListener(ip, port);
        listener.Start();
        Port = ((IPEndPoint)listener.LocalEndpoint).Port;
        _listener = listener;
        _cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        _acceptLoop = AcceptLoopAsync(_cts.Token);
        return Task.CompletedTask;
    }

    private async Task AcceptLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested && _listener is not null)
        {
            TcpClient client;
            try
            {
                client = await _listener.AcceptTcpClientAsync(ct);
            }
            catch (OperationCanceledException)
            {
                break;
            }
            catch (ObjectDisposedException)
            {
                break;
            }

            var handler = OnAccept;
            if (handler is not null)
            {
                _ = Task.Run(async () =>
                {
                    try
                    {
                        await handler(client, ct);
                    }
                    catch
                    {
                        client.Dispose();
                    }
                }, ct);
            }
            else
            {
                client.Dispose();
            }
        }
    }

    public async Task StopAsync()
    {
        if (_cts is not null)
        {
            await _cts.CancelAsync();
        }

        _listener?.Stop();
        if (_acceptLoop is not null)
        {
            try
            {
                await _acceptLoop;
            }
            catch
            {
            }
        }

        _cts?.Dispose();
        _cts = null;
        _listener = null;
    }

    public ValueTask DisposeAsync() => new(StopAsync());
}

/// <summary>
/// SSH gateway TCP accept loop. Callers attach <see cref="OnAccept"/> to run
/// SSH.NET handshakes (same layering as Go gateway/ssh.go).
/// </summary>
public sealed class SshGateway : IAsyncDisposable
{
    private TcpListener? _listener;
    private CancellationTokenSource? _cts;
    private Task? _acceptLoop;
    public Func<TcpClient, CancellationToken, Task>? OnAccept { get; set; }
    public int Port { get; private set; }

    public Task StartAsync(string host = TerminalDefaults.BindAll, int port = 0, CancellationToken cancellationToken = default)
    {
        if (port == 0)
        {
            port = TerminalDefaults.GatewaySshPort;
        }

        var ip = host is "0.0.0.0" or "" ? IPAddress.Any : IPAddress.Parse(host);
        var listener = new TcpListener(ip, port);
        listener.Start();
        Port = ((IPEndPoint)listener.LocalEndpoint).Port;
        _listener = listener;
        _cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        _acceptLoop = AcceptLoopAsync(_cts.Token);
        return Task.CompletedTask;
    }

    private async Task AcceptLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested && _listener is not null)
        {
            TcpClient client;
            try
            {
                client = await _listener.AcceptTcpClientAsync(ct);
            }
            catch (OperationCanceledException)
            {
                break;
            }
            catch (ObjectDisposedException)
            {
                break;
            }

            var handler = OnAccept;
            if (handler is not null)
            {
                _ = Task.Run(async () =>
                {
                    try
                    {
                        await handler(client, ct);
                    }
                    catch
                    {
                        client.Dispose();
                    }
                }, ct);
            }
            else
            {
                // Accept-and-close when no handler — proves bind/accept works.
                client.Dispose();
            }
        }
    }

    public async Task StopAsync()
    {
        if (_cts is not null)
        {
            await _cts.CancelAsync();
        }

        _listener?.Stop();
        if (_acceptLoop is not null)
        {
            try
            {
                await _acceptLoop;
            }
            catch
            {
            }
        }

        _cts?.Dispose();
        _cts = null;
        _listener = null;
    }

    public ValueTask DisposeAsync() => new(StopAsync());
}
