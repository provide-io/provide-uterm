//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net.WebSockets;
using Provide.Uterm.Vnc;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>The WebSocket ⇄ Stream adapters the human VNC relay pumps through.</summary>
public sealed class Coverage100WsStreamsTests
{
    [Fact]
    public async Task Read_stream_joins_fragments_serves_partial_reads_and_ends_on_close_or_empty()
    {
        var ws = new ScriptedWebSocket();
        using var read = new WsBinaryReadStream(ws, CancellationToken.None);
        Assert.True(read.CanRead);
        Assert.False(read.CanSeek);
        Assert.False(read.CanWrite);
        Assert.Throws<NotSupportedException>(() => read.Length);
        Assert.Throws<NotSupportedException>(() => read.Position);
        Assert.Throws<NotSupportedException>(() => read.Position = 1);
        Assert.Throws<NotSupportedException>(() => read.Seek(0, SeekOrigin.Begin));
        Assert.Throws<NotSupportedException>(() => read.SetLength(1));
        Assert.Throws<NotSupportedException>(() => read.Write(new byte[1], 0, 1));
        read.Flush();

        ws.Enqueue(WebSocketMessageType.Binary, "ab"u8.ToArray(), endOfMessage: false);
        ws.Enqueue(WebSocketMessageType.Text, "cd"u8.ToArray());
        var buf = new byte[3];
        Assert.Equal(3, read.Read(buf, 0, 3)); // sync path, partial
        Assert.Equal("abc"u8.ToArray(), buf);
        Assert.Equal(1, await read.ReadAsync(buf));
        Assert.Equal((byte)'d', buf[0]);

        ws.Enqueue(WebSocketMessageType.Binary, Array.Empty<byte>());
        Assert.Equal(0, await read.ReadAsync(buf)); // empty message ends the stream
        ws.EnqueueClose();
        Assert.Equal(0, await read.ReadAsync(buf));
        ws.SetState(WebSocketState.Closed);
        Assert.Equal(0, await read.ReadAsync(buf));
    }

    [Fact]
    public async Task Write_stream_sends_binary_frames_only_while_open()
    {
        var ws = new ScriptedWebSocket();
        var write = new WsBinaryWriteStream(ws, CancellationToken.None);
        Assert.False(write.CanRead);
        Assert.False(write.CanSeek);
        Assert.True(write.CanWrite);
        Assert.Throws<NotSupportedException>(() => write.Length);
        Assert.Throws<NotSupportedException>(() => write.Position);
        Assert.Throws<NotSupportedException>(() => write.Position = 1);
        Assert.Throws<NotSupportedException>(() => write.Seek(0, SeekOrigin.Begin));
        Assert.Throws<NotSupportedException>(() => write.SetLength(1));
        Assert.Throws<NotSupportedException>(() => write.Read(new byte[1], 0, 1));
        write.Flush();

        await write.WriteAsync(ReadOnlyMemory<byte>.Empty);
        write.Write("xy"u8.ToArray(), 0, 2);
        var sent = Assert.Single(ws.Sent);
        Assert.Equal(WebSocketMessageType.Binary, sent.Type);
        Assert.Equal("xy"u8.ToArray(), sent.Data);

        ws.SetState(WebSocketState.CloseSent);
        await Assert.ThrowsAsync<IOException>(async () => await write.WriteAsync("z"u8.ToArray()));
        write.Dispose();
    }
}
