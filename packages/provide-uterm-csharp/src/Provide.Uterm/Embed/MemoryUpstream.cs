//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Threading.Channels;

namespace Provide.Uterm.Embed;

/// <summary>
/// Deterministic in-process upstream for ordering/reconnect tests.
/// Push application bytes with <see cref="PushFromRemoteAsync"/>; capture host→remote with <see cref="Sent"/>.
/// </summary>
public sealed class MemoryUpstream : IUpstreamPipe, IAsyncDisposable
{
    private readonly Channel<byte[]> _inbound = Channel.CreateUnbounded<byte[]>(new UnboundedChannelOptions
    {
        SingleReader = true,
        SingleWriter = false,
    });
    private readonly List<byte[]> _sent = new();
    private readonly object _gate = new();
    private int _connected;
    private int _closed;

    public IReadOnlyList<byte[]> Sent
    {
        get { lock (_gate) return _sent.Select(b => b.ToArray()).ToList(); }
    }

    public bool IsConnected => Volatile.Read(ref _connected) == 1 && Volatile.Read(ref _closed) == 0;

    public Task ConnectAsync(CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        Volatile.Write(ref _connected, 1);
        return Task.CompletedTask;
    }

    public Task DisconnectAsync(CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (Interlocked.Exchange(ref _closed, 1) == 0)
        {
            _inbound.Writer.TryComplete();
        }

        Volatile.Write(ref _connected, 0);
        return Task.CompletedTask;
    }

    public Task SendAsync(ReadOnlyMemory<byte> data, CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (!IsConnected)
        {
            throw new InvalidOperationException("not connected");
        }

        var copy = data.ToArray();
        lock (_gate)
        {
            _sent.Add(copy);
        }

        return Task.CompletedTask;
    }

    public async Task<byte[]> ReceiveAsync(CancellationToken cancellationToken = default)
    {
        try
        {
            return await _inbound.Reader.ReadAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (ChannelClosedException)
        {
            return Array.Empty<byte>();
        }
    }

    /// <summary>Simulate remote→host application payload.</summary>
    public ValueTask PushFromRemoteAsync(ReadOnlyMemory<byte> data, CancellationToken cancellationToken = default)
    {
        if (Volatile.Read(ref _closed) != 0 || !_inbound.Writer.TryWrite(data.ToArray()))
        {
            throw new InvalidOperationException("upstream closed");
        }

        cancellationToken.ThrowIfCancellationRequested();
        return ValueTask.CompletedTask;
    }

    /// <summary>Signal EOF to the session reader.</summary>
    public void CompleteRemote()
    {
        _inbound.Writer.TryComplete();
        Volatile.Write(ref _connected, 0);
    }

    public async ValueTask DisposeAsync() => await DisconnectAsync().ConfigureAwait(false);
}
