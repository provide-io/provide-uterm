//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.Sockets;
using System.Text;
using Provide.Uterm.Filters;

namespace Provide.Uterm.Transports;

/// <summary>TCP telnet transport with basic IAC negotiation (NAWS/TTYPE).</summary>
public sealed class TelnetTransport : IConnectionTransport, IAsyncDisposable
{
    private TcpClient? _client;
    private NetworkStream? _stream;
    private readonly object _lock = new();

    public async Task ConnectAsync(string host, int port, ConnectOptions? options = null, CancellationToken cancellationToken = default)
    {
        options = (options ?? new ConnectOptions()).WithDefaults();
        var client = new TcpClient();
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        cts.CancelAfter(options.Timeout);
        await client.ConnectAsync(host, port, cts.Token);
        var stream = client.GetStream();
        // Minimal WILL/DO negotiation for NAWS + TTYPE
        await stream.WriteAsync(new byte[]
        {
            InputFilters.Iac, InputFilters.Will, 31, // NAWS
            InputFilters.Iac, InputFilters.Will, 24, // TTYPE
        }, cts.Token);
        // Send NAWS size
        var cols = (ushort)options.Cols;
        var rows = (ushort)options.Rows;
        await stream.WriteAsync(new byte[]
        {
            InputFilters.Iac, InputFilters.Sb, 31,
            (byte)(cols >> 8), (byte)(cols & 0xff),
            (byte)(rows >> 8), (byte)(rows & 0xff),
            InputFilters.Iac, InputFilters.Se,
        }, cts.Token);
        // TTYPE IS
        var term = Encoding.ASCII.GetBytes(options.Term);
        var ttype = new List<byte> { InputFilters.Iac, InputFilters.Sb, 24, 0 };
        ttype.AddRange(term);
        ttype.Add(InputFilters.Iac);
        ttype.Add(InputFilters.Se);
        await stream.WriteAsync(ttype.ToArray(), cts.Token);

        lock (_lock)
        {
            _client = client;
            _stream = stream;
        }
    }

    public Task DisconnectAsync(CancellationToken cancellationToken = default)
    {
        lock (_lock)
        {
            _stream?.Dispose();
            _client?.Dispose();
            _stream = null;
            _client = null;
        }

        return Task.CompletedTask;
    }

    public async Task SendAsync(byte[] data, CancellationToken cancellationToken = default)
    {
        NetworkStream stream;
        lock (_lock)
        {
            stream = _stream ?? throw TransportErrors.NotConnected;
        }

        // Escape IAC
        var escaped = new List<byte>(data.Length);
        foreach (var b in data)
        {
            escaped.Add(b);
            if (b == InputFilters.Iac)
            {
                escaped.Add(InputFilters.Iac);
            }
        }

        await stream.WriteAsync(escaped.ToArray(), cancellationToken);
    }

    public async Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default)
    {
        NetworkStream stream;
        lock (_lock)
        {
            stream = _stream ?? throw TransportErrors.NotConnected;
        }

        using var cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        cts.CancelAfter(timeout);
        var buf = new byte[Math.Max(1, maxBytes)];
        try
        {
            var n = await stream.ReadAsync(buf.AsMemory(0, buf.Length), cts.Token);
            if (n == 0)
            {
                throw TransportErrors.ConnectionClosed;
            }

            // Strip IAC sequences into a clean payload
            return StripIac(buf.AsSpan(0, n));
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            return Array.Empty<byte>();
        }
    }

    public bool IsConnected()
    {
        lock (_lock)
        {
            return _client is { Connected: true };
        }
    }

    private static byte[] StripIac(ReadOnlySpan<byte> data)
    {
        using var ms = new MemoryStream(data.Length);
        for (var i = 0; i < data.Length; i++)
        {
            if (data[i] != InputFilters.Iac)
            {
                ms.WriteByte(data[i]);
                continue;
            }

            if (i + 1 >= data.Length)
            {
                break;
            }

            var cmd = data[++i];
            if (cmd == InputFilters.Iac)
            {
                ms.WriteByte(InputFilters.Iac);
                continue;
            }

            if (cmd is InputFilters.Will or InputFilters.Wont or InputFilters.Do or InputFilters.Dont)
            {
                if (i + 1 < data.Length)
                {
                    i++;
                }

                continue;
            }

            if (cmd == InputFilters.Sb)
            {
                while (i + 1 < data.Length)
                {
                    i++;
                    if (data[i] == InputFilters.Iac && i + 1 < data.Length && data[i + 1] == InputFilters.Se)
                    {
                        i++;
                        break;
                    }
                }
            }
        }

        return ms.ToArray();
    }

    public async ValueTask DisposeAsync() => await DisconnectAsync();
}
