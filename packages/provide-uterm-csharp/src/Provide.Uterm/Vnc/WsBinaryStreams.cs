//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;

namespace Provide.Uterm.Vnc;

/// <summary>Read binary (or text) WebSocket messages as a sequential stream.</summary>
internal sealed class WsBinaryReadStream : Stream
{
    private readonly WebSocket _ws;
    private readonly CancellationToken _ct;
    private byte[] _pending = Array.Empty<byte>();
    private int _offset;

    public WsBinaryReadStream(WebSocket ws, CancellationToken ct)
    {
        _ws = ws;
        _ct = ct;
    }

    public override bool CanRead => true;
    public override bool CanSeek => false;
    public override bool CanWrite => false;
    public override long Length => throw new NotSupportedException();
    public override long Position
    {
        get => throw new NotSupportedException();
        set => throw new NotSupportedException();
    }

    public override void Flush()
    {
    }

    public override int Read(byte[] buffer, int offset, int count) =>
        ReadAsync(buffer.AsMemory(offset, count), _ct).AsTask().GetAwaiter().GetResult();

    public override async ValueTask<int> ReadAsync(
        Memory<byte> buffer, CancellationToken cancellationToken = default)
    {
        var ct = cancellationToken.CanBeCanceled ? cancellationToken : _ct;
        while (_offset >= _pending.Length)
        {
            if (_ws.State != WebSocketState.Open && _ws.State != WebSocketState.CloseReceived)
            {
                return 0;
            }

            var tmp = new byte[1 << 16];
            using var ms = new MemoryStream();
            ValueWebSocketReceiveResult result;
            do
            {
                result = await _ws.ReceiveAsync(tmp.AsMemory(), ct).ConfigureAwait(false);
                if (result.MessageType == WebSocketMessageType.Close)
                {
                    return 0;
                }

                if (result.MessageType is not (WebSocketMessageType.Binary or WebSocketMessageType.Text))
                {
                    continue;
                }

                ms.Write(tmp, 0, result.Count);
            }
            while (!result.EndOfMessage);

            _pending = ms.ToArray();
            _offset = 0;
            if (_pending.Length == 0)
            {
                return 0;
            }
        }

        var n = Math.Min(buffer.Length, _pending.Length - _offset);
        _pending.AsSpan(_offset, n).CopyTo(buffer.Span);
        _offset += n;
        return n;
    }

    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();
    public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
}

/// <summary>Write binary WebSocket frames as a stream.</summary>
internal sealed class WsBinaryWriteStream : Stream
{
    private readonly WebSocket _ws;
    private readonly CancellationToken _ct;
    private readonly SemaphoreSlim _gate = new(1, 1);

    public WsBinaryWriteStream(WebSocket ws, CancellationToken ct)
    {
        _ws = ws;
        _ct = ct;
    }

    public override bool CanRead => false;
    public override bool CanSeek => false;
    public override bool CanWrite => true;
    public override long Length => throw new NotSupportedException();
    public override long Position
    {
        get => throw new NotSupportedException();
        set => throw new NotSupportedException();
    }

    public override void Flush()
    {
    }

    public override void Write(byte[] buffer, int offset, int count) =>
        WriteAsync(buffer.AsMemory(offset, count), _ct).AsTask().GetAwaiter().GetResult();

    public override async ValueTask WriteAsync(
        ReadOnlyMemory<byte> buffer, CancellationToken cancellationToken = default)
    {
        if (buffer.IsEmpty)
        {
            return;
        }

        var ct = cancellationToken.CanBeCanceled ? cancellationToken : _ct;
        await _gate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            if (_ws.State != WebSocketState.Open)
            {
                throw new IOException("WebSocket is not open");
            }

            await _ws.SendAsync(buffer, WebSocketMessageType.Binary, true, ct).ConfigureAwait(false);
        }
        finally
        {
            _gate.Release();
        }
    }

    public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
    public override void SetLength(long value) => throw new NotSupportedException();
    public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();

    protected override void Dispose(bool disposing)
    {
        if (disposing)
        {
            _gate.Dispose();
        }

        base.Dispose(disposing);
    }
}
