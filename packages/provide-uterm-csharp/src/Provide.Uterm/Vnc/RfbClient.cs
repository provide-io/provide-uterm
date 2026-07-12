//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Buffers.Binary;
using System.Net.Sockets;
using System.Text;
using Provide.Uterm.Gui;

namespace Provide.Uterm.Vnc;

/// <summary>
/// Minimal RFB 3.3/3.8 client supporting security type None and Raw encoding.
/// Implements <see cref="IGraphicalSession"/> for GUI REST attach (mode=rfb).
/// </summary>
public sealed class RfbClient : IGraphicalSession, IAsyncDisposable
{
    public const int EncodingRaw = 0;
    public const int EncodingCopyRect = 1;
    public const byte SecurityNone = 1;

    public const int MaxDimension = RgbaImage.MaxDimension;

    private TcpClient? _tcp;
    private NetworkStream? _stream;
    private FramebufferTracker? _tracker;
    private readonly object _lock = new();
    private CancellationTokenSource? _loopCts;
    private Task? _loop;
    private int _width;
    private int _height;

    public int Width
    {
        get { lock (_lock) return _width; }
    }

    public int Height
    {
        get { lock (_lock) return _height; }
    }

    public bool IsConnected
    {
        get { lock (_lock) return _stream is not null; }
    }

    /// <summary>Connect, handshake (None security), parse ServerInit, start update loop.</summary>
    public async Task ConnectAsync(string host, int port, CancellationToken cancellationToken = default)
    {
        var tcp = new TcpClient();
        await tcp.ConnectAsync(host, port, cancellationToken).ConfigureAwait(false);
        var stream = tcp.GetStream();

        // ProtocolVersion
        var verBuf = new byte[12];
        await ReadExactAsync(stream, verBuf, cancellationToken).ConfigureAwait(false);
        var serverVer = Encoding.ASCII.GetString(verBuf);
        // Prefer 003.008 if offered, else echo what we got if it looks like RFB.
        var clientVer = serverVer.StartsWith("RFB ", StringComparison.Ordinal)
            ? (serverVer.Contains("003.008", StringComparison.Ordinal) ? "RFB 003.008\n" : serverVer)
            : "RFB 003.003\n";
        await stream.WriteAsync(Encoding.ASCII.GetBytes(clientVer), cancellationToken).ConfigureAwait(false);

        // Security
        byte securityType;
        if (clientVer.Contains("003.007", StringComparison.Ordinal) ||
            clientVer.Contains("003.008", StringComparison.Ordinal))
        {
            var nBuf = new byte[1];
            await ReadExactAsync(stream, nBuf, cancellationToken).ConfigureAwait(false);
            var n = nBuf[0];
            if (n == 0)
            {
                throw new InvalidOperationException("RFB security handshake failed (no types)");
            }

            var types = new byte[n];
            await ReadExactAsync(stream, types, cancellationToken).ConfigureAwait(false);
            if (!types.Contains(SecurityNone))
            {
                throw new InvalidOperationException("RFB server does not offer security type None");
            }

            await stream.WriteAsync(new byte[] { SecurityNone }, cancellationToken).ConfigureAwait(false);
            // SecurityResult (3.8)
            var result = new byte[4];
            await ReadExactAsync(stream, result, cancellationToken).ConfigureAwait(false);
            if (BinaryPrimitives.ReadUInt32BigEndian(result) != 0)
            {
                throw new InvalidOperationException("RFB security rejected");
            }

            securityType = SecurityNone;
        }
        else
        {
            // 3.3: server sends U32 security type
            var st = new byte[4];
            await ReadExactAsync(stream, st, cancellationToken).ConfigureAwait(false);
            securityType = (byte)BinaryPrimitives.ReadUInt32BigEndian(st);
            if (securityType != SecurityNone)
            {
                throw new InvalidOperationException($"unsupported RFB security type {securityType}");
            }
        }

        _ = securityType;
        // ClientInit shared-flag = 1
        await stream.WriteAsync(new byte[] { 1 }, cancellationToken).ConfigureAwait(false);

        // ServerInit
        var hdr = new byte[24];
        await ReadExactAsync(stream, hdr, cancellationToken).ConfigureAwait(false);
        var width = BinaryPrimitives.ReadUInt16BigEndian(hdr.AsSpan(0, 2));
        var height = BinaryPrimitives.ReadUInt16BigEndian(hdr.AsSpan(2, 2));
        if (width == 0 || height == 0 || width > MaxDimension || height > MaxDimension)
        {
            throw new InvalidOperationException($"RFB framebuffer dimensions out of range: {width}x{height}");
        }

        var nameLen = BinaryPrimitives.ReadUInt32BigEndian(hdr.AsSpan(20, 4));
        if (nameLen > 4096)
        {
            throw new InvalidOperationException("RFB desktop name too long");
        }

        if (nameLen > 0)
        {
            var name = new byte[nameLen];
            await ReadExactAsync(stream, name, cancellationToken).ConfigureAwait(false);
        }

        // Prefer 32bpp RGBA-ish raw
        await SendSetPixelFormatAsync(stream, cancellationToken).ConfigureAwait(false);
        await SendSetEncodingsAsync(stream, cancellationToken).ConfigureAwait(false);
        await SendFramebufferUpdateRequestAsync(stream, width, height, incremental: false, cancellationToken)
            .ConfigureAwait(false);

        lock (_lock)
        {
            _tcp = tcp;
            _stream = stream;
            _width = width;
            _height = height;
            _tracker = new FramebufferTracker(width, height);
            _loopCts = new CancellationTokenSource();
            _loop = Task.Run(() => ReadLoopAsync(_loopCts.Token));
        }
    }

    public RgbaImage Screenshot()
    {
        lock (_lock)
        {
            return _tracker?.GetImage() ?? new RgbaImage(1, 1);
        }
    }

    public void InjectPointer(int x, int y, byte buttonMask)
    {
        NetworkStream? stream;
        lock (_lock) stream = _stream;
        if (stream is null)
        {
            return;
        }

        var msg = EncodePointerEvent(x, y, buttonMask);
        try
        {
            stream.Write(msg);
        }
        catch
        {
            // ignore
        }
    }

    public void InjectKey(uint keySym, bool down)
    {
        NetworkStream? stream;
        lock (_lock) stream = _stream;
        if (stream is null)
        {
            return;
        }

        var msg = EncodeKeyEvent(keySym, down);
        try
        {
            stream.Write(msg);
        }
        catch
        {
            // ignore
        }
    }

    public static byte[] EncodePointerEvent(int x, int y, byte buttonMask)
    {
        x = Math.Clamp(x, 0, 65535);
        y = Math.Clamp(y, 0, 65535);
        var buf = new byte[6];
        buf[0] = 5;
        buf[1] = buttonMask;
        BinaryPrimitives.WriteUInt16BigEndian(buf.AsSpan(2, 2), (ushort)x);
        BinaryPrimitives.WriteUInt16BigEndian(buf.AsSpan(4, 2), (ushort)y);
        return buf;
    }

    public static byte[] EncodeKeyEvent(uint keySym, bool down)
    {
        var buf = new byte[8];
        buf[0] = 4;
        buf[1] = down ? (byte)1 : (byte)0;
        BinaryPrimitives.WriteUInt32BigEndian(buf.AsSpan(4, 4), keySym);
        return buf;
    }

    public async ValueTask DisposeAsync()
    {
        CancellationTokenSource? cts;
        Task? loop;
        lock (_lock)
        {
            cts = _loopCts;
            loop = _loop;
            _loopCts = null;
            _loop = null;
        }

        if (cts is not null)
        {
            await cts.CancelAsync().ConfigureAwait(false);
        }

        if (loop is not null)
        {
            try
            {
                await loop.ConfigureAwait(false);
            }
            catch
            {
            }
        }

        lock (_lock)
        {
            _stream?.Dispose();
            _tcp?.Dispose();
            _stream = null;
            _tcp = null;
            _tracker = null;
        }

        cts?.Dispose();
    }

    private async Task ReadLoopAsync(CancellationToken ct)
    {
        NetworkStream stream;
        lock (_lock)
        {
            stream = _stream ?? throw new InvalidOperationException("not connected");
        }

        try
        {
            while (!ct.IsCancellationRequested)
            {
                var typeBuf = new byte[1];
                await ReadExactAsync(stream, typeBuf, ct).ConfigureAwait(false);
                if (typeBuf[0] != 0)
                {
                    // Skip unknown server messages conservatively by stopping.
                    break;
                }

                // FramebufferUpdate
                var hdr = new byte[3];
                await ReadExactAsync(stream, hdr, ct).ConfigureAwait(false);
                var nRects = BinaryPrimitives.ReadUInt16BigEndian(hdr.AsSpan(1, 2));
                if (nRects > 4096)
                {
                    throw new InvalidOperationException("RFB rectangle count too large");
                }

                for (var i = 0; i < nRects; i++)
                {
                    var rh = new byte[12];
                    await ReadExactAsync(stream, rh, ct).ConfigureAwait(false);
                    var x = BinaryPrimitives.ReadUInt16BigEndian(rh.AsSpan(0, 2));
                    var y = BinaryPrimitives.ReadUInt16BigEndian(rh.AsSpan(2, 2));
                    var w = BinaryPrimitives.ReadUInt16BigEndian(rh.AsSpan(4, 2));
                    var h = BinaryPrimitives.ReadUInt16BigEndian(rh.AsSpan(6, 2));
                    var enc = BinaryPrimitives.ReadInt32BigEndian(rh.AsSpan(8, 4));
                    if (w > MaxDimension || h > MaxDimension)
                    {
                        throw new InvalidOperationException("RFB rect dimensions too large");
                    }

                    if (enc == EncodingRaw)
                    {
                        var nbytes = checked(w * h * 4);
                        var pixels = new byte[nbytes];
                        await ReadExactAsync(stream, pixels, ct).ConfigureAwait(false);
                        lock (_lock)
                        {
                            _tracker?.ApplyRawUpdate(x, y, w, h, pixels);
                        }
                    }
                    else if (enc == EncodingCopyRect)
                    {
                        var src = new byte[4];
                        await ReadExactAsync(stream, src, ct).ConfigureAwait(false);
                        // Best-effort: ignore copyrect for tracker (optional).
                    }
                    else
                    {
                        throw new InvalidOperationException($"unsupported RFB encoding {enc}");
                    }
                }

                // Request incremental update
                int width, height;
                lock (_lock)
                {
                    width = _width;
                    height = _height;
                }

                await SendFramebufferUpdateRequestAsync(stream, width, height, incremental: true, ct)
                    .ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException)
        {
        }
        catch
        {
            // connection ended
        }
    }

    private static async Task SendSetPixelFormatAsync(NetworkStream stream, CancellationToken ct)
    {
        // 32bpp little-endian true color, 8-8-8
        var msg = new byte[20];
        msg[0] = 0; // SetPixelFormat
        // pad 3
        msg[4] = 32; // bits
        msg[5] = 24; // depth
        msg[6] = 0; // big endian
        msg[7] = 1; // true colour
        BinaryPrimitives.WriteUInt16BigEndian(msg.AsSpan(8, 2), 255); // r max
        BinaryPrimitives.WriteUInt16BigEndian(msg.AsSpan(10, 2), 255);
        BinaryPrimitives.WriteUInt16BigEndian(msg.AsSpan(12, 2), 255);
        msg[14] = 16; // r shift
        msg[15] = 8;
        msg[16] = 0;
        await stream.WriteAsync(msg, ct).ConfigureAwait(false);
    }

    private static async Task SendSetEncodingsAsync(NetworkStream stream, CancellationToken ct)
    {
        var msg = new byte[4 + 8];
        msg[0] = 2;
        BinaryPrimitives.WriteUInt16BigEndian(msg.AsSpan(2, 2), 2);
        BinaryPrimitives.WriteInt32BigEndian(msg.AsSpan(4, 4), EncodingRaw);
        BinaryPrimitives.WriteInt32BigEndian(msg.AsSpan(8, 4), EncodingCopyRect);
        await stream.WriteAsync(msg, ct).ConfigureAwait(false);
    }

    private static async Task SendFramebufferUpdateRequestAsync(
        NetworkStream stream, int width, int height, bool incremental, CancellationToken ct)
    {
        var msg = new byte[10];
        msg[0] = 3;
        msg[1] = incremental ? (byte)1 : (byte)0;
        BinaryPrimitives.WriteUInt16BigEndian(msg.AsSpan(2, 2), 0);
        BinaryPrimitives.WriteUInt16BigEndian(msg.AsSpan(4, 2), 0);
        BinaryPrimitives.WriteUInt16BigEndian(msg.AsSpan(6, 2), (ushort)width);
        BinaryPrimitives.WriteUInt16BigEndian(msg.AsSpan(8, 2), (ushort)height);
        await stream.WriteAsync(msg, ct).ConfigureAwait(false);
    }

    private static async Task ReadExactAsync(Stream stream, byte[] buffer, CancellationToken ct)
    {
        var off = 0;
        while (off < buffer.Length)
        {
            var n = await stream.ReadAsync(buffer.AsMemory(off, buffer.Length - off), ct).ConfigureAwait(false);
            if (n == 0)
            {
                throw new EndOfStreamException("RFB connection closed");
            }

            off += n;
        }
    }
}
