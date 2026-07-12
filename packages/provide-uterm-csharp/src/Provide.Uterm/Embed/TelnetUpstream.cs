//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.Sockets;
using Provide.Uterm.Filters;

namespace Provide.Uterm.Embed;

/// <summary>
/// Policy-driven telnet upstream: owns TCP + IAC parse, answers via <see cref="ITelnetPolicy"/>,
/// exposes application bytes on <see cref="IUpstreamPipe"/> and optional wire diagnostics.
/// </summary>
public sealed class TelnetUpstream : IUpstreamPipe, IAsyncDisposable
{
    private readonly string _host;
    private readonly int _port;
    private readonly ITelnetPolicy _policy;
    private readonly TimeSpan _connectTimeout;
    private readonly byte[] _rxBuf = new byte[8192];
    private readonly List<byte> _pending = new();
    private readonly object _gate = new();
    private TcpClient? _client;
    private NetworkStream? _stream;
    private int _connected;

    /// <summary>Optional wire-event sink (IAC negotiation diagnostics).</summary>
    public Func<WireEventKind, byte[], string, ValueTask>? OnWire { get; set; }

    public TelnetUpstream(
        string host,
        int port,
        ITelnetPolicy? policy = null,
        TimeSpan? connectTimeout = null)
    {
        _host = host;
        _port = port;
        _policy = policy ?? new DefaultTelnetPolicy();
        _connectTimeout = connectTimeout ?? TimeSpan.FromSeconds(30);
    }

    public bool IsConnected
    {
        get
        {
            lock (_gate)
            {
                return Volatile.Read(ref _connected) == 1 && _client is { Connected: true };
            }
        }
    }

    public async Task ConnectAsync(CancellationToken cancellationToken = default)
    {
        var client = new TcpClient();
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        cts.CancelAfter(_connectTimeout);
        await client.ConnectAsync(_host, _port, cts.Token).ConfigureAwait(false);
        var stream = client.GetStream();

        // Offer BINARY + SGA (common BBS baseline); further answers via policy.
        var offer = new byte[]
        {
            InputFilters.Iac, InputFilters.Will, TelnetIac.OptBinary,
            InputFilters.Iac, InputFilters.Will, TelnetIac.OptSga,
        };
        await stream.WriteAsync(offer, cts.Token).ConfigureAwait(false);

        // Proactive NAWS/TTYPE from policy window/term.
        var (cols, rows) = _policy.WindowSize;
        var naws = _policy.OnSubnegotiation(TelnetIac.OptNaws, ReadOnlySpan<byte>.Empty);
        if (!naws.IsEmpty)
        {
            await stream.WriteAsync(naws, cts.Token).ConfigureAwait(false);
        }

        var ttype = _policy.OnSubnegotiation(TelnetIac.OptTtype, new byte[] { 1 }); // SEND
        if (!ttype.IsEmpty)
        {
            await stream.WriteAsync(ttype, cts.Token).ConfigureAwait(false);
        }

        _ = cols;
        _ = rows;

        lock (_gate)
        {
            _client = client;
            _stream = stream;
            _pending.Clear();
        }

        Volatile.Write(ref _connected, 1);
    }

    public Task DisconnectAsync(CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (_gate)
        {
            _stream?.Dispose();
            _client?.Dispose();
            _stream = null;
            _client = null;
            _pending.Clear();
        }

        Volatile.Write(ref _connected, 0);
        return Task.CompletedTask;
    }

    public async Task SendAsync(ReadOnlyMemory<byte> data, CancellationToken cancellationToken = default)
    {
        NetworkStream stream;
        lock (_gate)
        {
            stream = _stream ?? throw new InvalidOperationException("not connected");
        }

        var escaped = TelnetIac.Escape(data.Span);
        await stream.WriteAsync(escaped, cancellationToken).ConfigureAwait(false);
    }

    public async Task<byte[]> ReceiveAsync(CancellationToken cancellationToken = default)
    {
        while (true)
        {
            cancellationToken.ThrowIfCancellationRequested();
            // Drain any pending app bytes first
            lock (_gate)
            {
                if (_pending.Count > 0)
                {
                    var outb = _pending.ToArray();
                    _pending.Clear();
                    return outb;
                }
            }

            NetworkStream stream;
            lock (_gate)
            {
                stream = _stream ?? throw new InvalidOperationException("not connected");
            }

            int n;
            try
            {
                n = await stream.ReadAsync(_rxBuf.AsMemory(), cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                throw;
            }
            catch
            {
                Volatile.Write(ref _connected, 0);
                return Array.Empty<byte>();
            }

            if (n == 0)
            {
                Volatile.Write(ref _connected, 0);
                return Array.Empty<byte>();
            }

            var (payload, events, consumed) = TelnetIac.Parse(_rxBuf.AsSpan(0, n));
            _ = consumed;

            foreach (var ev in events)
            {
                await HandleControlAsync(ev, cancellationToken).ConfigureAwait(false);
            }

            if (payload.Length > 0)
            {
                return payload;
            }
        }
    }

    private async Task HandleControlAsync(TelnetControlEvent ev, CancellationToken ct)
    {
        if (OnWire is not null)
        {
            if (ev.IsSubnegotiation)
            {
                await OnWire(WireEventKind.Negotiation, ev.SubPayload, "sb:" + ev.Option).ConfigureAwait(false);
            }
            else
            {
                await OnWire(WireEventKind.Iac, new[] { InputFilters.Iac, ev.Command, ev.Option }, "neg").ConfigureAwait(false);
            }
        }

        ReadOnlyMemory<byte> reply = default;
        if (ev.IsSubnegotiation)
        {
            reply = _policy.OnSubnegotiation(ev.Option, ev.SubPayload);
        }
        else
        {
            reply = _policy.OnOption(ev.Command, ev.Option);
        }

        if (reply.IsEmpty)
        {
            return;
        }

        NetworkStream stream;
        lock (_gate)
        {
            stream = _stream ?? throw new InvalidOperationException("not connected");
        }

        await stream.WriteAsync(reply, ct).ConfigureAwait(false);
    }

    public async ValueTask DisposeAsync() => await DisconnectAsync().ConfigureAwait(false);
}

