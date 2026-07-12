//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Threading.Channels;
using Provide.Uterm.Filters;
using Provide.Uterm.Transports;

namespace Provide.Uterm.Embed;

/// <summary>
/// Adapts an existing <see cref="IConnectionTransport"/> (SSH, WS, telnet) to
/// <see cref="IUpstreamPipe"/> for embed sessions. Payload is whatever the transport
/// returns (IAC already stripped for <see cref="TelnetTransport"/>).
/// </summary>
public sealed class ConnectionTransportUpstream : IUpstreamPipe, IAsyncDisposable
{
    private readonly IConnectionTransport _transport;
    private readonly string _host;
    private readonly int _port;
    private readonly ConnectOptions _options;
    private readonly TimeSpan _receiveTimeout;
    private readonly int _maxBytes;
    private int _connected;

    public ConnectionTransportUpstream(
        IConnectionTransport transport,
        string host,
        int port,
        ConnectOptions? options = null,
        TimeSpan? receiveTimeout = null,
        int maxBytes = 8192)
    {
        _transport = transport;
        _host = host;
        _port = port;
        _options = (options ?? new ConnectOptions()).WithDefaults();
        _receiveTimeout = receiveTimeout ?? TimeSpan.FromSeconds(30);
        _maxBytes = maxBytes;
    }

    public bool IsConnected => Volatile.Read(ref _connected) == 1 && _transport.IsConnected();

    public async Task ConnectAsync(CancellationToken cancellationToken = default)
    {
        await _transport.ConnectAsync(_host, _port, _options, cancellationToken).ConfigureAwait(false);
        Volatile.Write(ref _connected, 1);
    }

    public async Task DisconnectAsync(CancellationToken cancellationToken = default)
    {
        Volatile.Write(ref _connected, 0);
        await _transport.DisconnectAsync(cancellationToken).ConfigureAwait(false);
    }

    public Task SendAsync(ReadOnlyMemory<byte> data, CancellationToken cancellationToken = default) =>
        _transport.SendAsync(data.ToArray(), cancellationToken);

    public async Task<byte[]> ReceiveAsync(CancellationToken cancellationToken = default)
    {
        try
        {
            var chunk = await _transport.ReceiveAsync(_maxBytes, _receiveTimeout, cancellationToken)
                .ConfigureAwait(false);
            return chunk;
        }
        catch (IOException)
        {
            Volatile.Write(ref _connected, 0);
            return Array.Empty<byte>();
        }
        catch (InvalidOperationException)
        {
            Volatile.Write(ref _connected, 0);
            return Array.Empty<byte>();
        }
    }

    public async ValueTask DisposeAsync() => await DisconnectAsync().ConfigureAwait(false);
}

/// <summary>
/// In-memory duplex that feeds raw wire bytes through <see cref="TelnetIac"/> + policy —
/// for deterministic negotiation tests without TCP.
/// </summary>
public sealed class ScriptedTelnetUpstream : IUpstreamPipe, IAsyncDisposable
{
    private readonly ITelnetPolicy _policy;
    private readonly Channel<byte[]> _wireIn = Channel.CreateUnbounded<byte[]>();
    private readonly List<byte> _carry = new();
    private readonly List<byte[]> _sent = new();
    private int _connected;

    public Func<WireEventKind, byte[], string, ValueTask>? OnWire { get; set; }
    public IReadOnlyList<byte[]> SentWire
    {
        get { lock (_sent) return _sent.Select(s => s.ToArray()).ToList(); }
    }

    public ScriptedTelnetUpstream(ITelnetPolicy? policy = null) =>
        _policy = policy ?? new DefaultTelnetPolicy();

    public bool IsConnected => Volatile.Read(ref _connected) == 1;

    public Task ConnectAsync(CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        Volatile.Write(ref _connected, 1);
        return Task.CompletedTask;
    }

    public Task DisconnectAsync(CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        Volatile.Write(ref _connected, 0);
        _wireIn.Writer.TryComplete();
        return Task.CompletedTask;
    }

    public Task SendAsync(ReadOnlyMemory<byte> data, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var escaped = TelnetIac.Escape(data.Span);
        lock (_sent)
        {
            _sent.Add(escaped);
        }

        return Task.CompletedTask;
    }

    /// <summary>Push raw wire bytes (may include IAC) as if from the remote.</summary>
    public ValueTask PushWireAsync(ReadOnlyMemory<byte> wire, CancellationToken cancellationToken = default) =>
        _wireIn.Writer.WriteAsync(wire.ToArray(), cancellationToken);

    public async Task<byte[]> ReceiveAsync(CancellationToken cancellationToken = default)
    {
        while (true)
        {
            byte[] chunk;
            try
            {
                chunk = await _wireIn.Reader.ReadAsync(cancellationToken).ConfigureAwait(false);
            }
            catch (ChannelClosedException)
            {
                return Array.Empty<byte>();
            }

            _carry.AddRange(chunk);
            var (payload, events, consumed) = TelnetIac.Parse(CollectionsMarshalAsSpan(_carry), final: false);
            if (consumed > 0)
            {
                _carry.RemoveRange(0, consumed);
            }

            foreach (var ev in events)
            {
                if (OnWire is not null)
                {
                    if (ev.IsSubnegotiation)
                    {
                        await OnWire(WireEventKind.Negotiation, ev.SubPayload, "sb").ConfigureAwait(false);
                    }
                    else
                    {
                        await OnWire(WireEventKind.Iac, new[] { InputFilters.Iac, ev.Command, ev.Option }, "neg")
                            .ConfigureAwait(false);
                    }
                }

                ReadOnlyMemory<byte> reply = ev.IsSubnegotiation
                    ? _policy.OnSubnegotiation(ev.Option, ev.SubPayload)
                    : _policy.OnOption(ev.Command, ev.Option);
                if (!reply.IsEmpty)
                {
                    lock (_sent)
                    {
                        _sent.Add(reply.ToArray());
                    }
                }
            }

            if (payload.Length > 0)
            {
                return payload;
            }
        }
    }

    private static ReadOnlySpan<byte> CollectionsMarshalAsSpan(List<byte> list) =>
        System.Runtime.InteropServices.CollectionsMarshal.AsSpan(list);

    public async ValueTask DisposeAsync() => await DisconnectAsync().ConfigureAwait(false);
}
