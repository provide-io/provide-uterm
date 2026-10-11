//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Buffers.Binary;
using System.Net;
using System.Net.Sockets;
using System.Text;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>
/// A loopback RFB server speaking just enough of the protocol for
/// <see cref="Provide.Uterm.Vnc.RfbClient"/>: version, security, init, and
/// whatever the test then sends through <see cref="AfterInit"/>. Every
/// handshake field is settable so each client refusal is reachable.
/// </summary>
internal sealed class FakeRfbServer : IAsyncDisposable
{
    private readonly TcpListener _listener = new(IPAddress.Loopback, 0);
    private readonly CancellationTokenSource _cts = new();
    private readonly Task _loop;
    private readonly List<Task> _sessions = new();

    public FakeRfbServer()
    {
        _listener.Start();
        Port = ((IPEndPoint)_listener.LocalEndpoint).Port;
        _loop = AcceptAsync();
    }

    public int Port { get; }
    public string Version { get; init; } = "RFB 003.008\n";
    public byte[] SecurityTypes { get; init; } = { 1 };
    public uint SecurityResult { get; init; }
    public uint LegacySecurityType { get; init; } = 1;
    public ushort Width { get; init; } = 4;
    public ushort Height { get; init; } = 2;
    public uint NameLength { get; init; } = 4;

    /// <summary>Runs after the client's init messages; the stream closes when it returns.</summary>
    public Func<NetworkStream, CancellationToken, Task>? AfterInit { get; init; }

    public TaskCompletionSource Accepted { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

    public static byte[] FramebufferUpdate(params (ushort X, ushort Y, ushort W, ushort H, int Enc, byte[] Body)[] rects)
    {
        var ms = new MemoryStream();
        ms.Write(new byte[] { 0, 0 });
        var n = new byte[2];
        BinaryPrimitives.WriteUInt16BigEndian(n, (ushort)rects.Length);
        ms.Write(n);
        foreach (var r in rects)
        {
            var h = new byte[12];
            BinaryPrimitives.WriteUInt16BigEndian(h.AsSpan(0), r.X);
            BinaryPrimitives.WriteUInt16BigEndian(h.AsSpan(2), r.Y);
            BinaryPrimitives.WriteUInt16BigEndian(h.AsSpan(4), r.W);
            BinaryPrimitives.WriteUInt16BigEndian(h.AsSpan(6), r.H);
            BinaryPrimitives.WriteInt32BigEndian(h.AsSpan(8), r.Enc);
            ms.Write(h);
            ms.Write(r.Body);
        }

        return ms.ToArray();
    }

    private async Task AcceptAsync()
    {
        while (true)
        {
            TcpClient client;
            try
            {
                client = await _listener.AcceptTcpClientAsync(_cts.Token);
            }
            catch (Exception)
            {
                return;
            }

            Accepted.TrySetResult();
            lock (_sessions)
            {
                _sessions.Add(Task.Run(() => ServeAsync(client)));
            }
        }
    }

    private async Task ServeAsync(TcpClient client)
    {
        using var _ = client;
        var ct = _cts.Token;
        try
        {
            var s = client.GetStream();
            await s.WriteAsync(Encoding.ASCII.GetBytes(Version), ct);
            var ver = await ReadAsync(s, 12, ct);
            var clientVer = Encoding.ASCII.GetString(ver);
            if (clientVer.Contains("003.007", StringComparison.Ordinal) || clientVer.Contains("003.008", StringComparison.Ordinal))
            {
                await s.WriteAsync(new[] { (byte)SecurityTypes.Length }, ct);
                if (SecurityTypes.Length == 0)
                {
                    return;
                }

                await s.WriteAsync(SecurityTypes, ct);
                if (!SecurityTypes.Contains((byte)1))
                {
                    return;
                }

                await ReadAsync(s, 1, ct);
                await s.WriteAsync(U32(SecurityResult), ct);
            }
            else
            {
                await s.WriteAsync(U32(LegacySecurityType), ct);
            }

            await ReadAsync(s, 1, ct); // ClientInit
            var init = new byte[24];
            BinaryPrimitives.WriteUInt16BigEndian(init.AsSpan(0), Width);
            BinaryPrimitives.WriteUInt16BigEndian(init.AsSpan(2), Height);
            BinaryPrimitives.WriteUInt32BigEndian(init.AsSpan(20), NameLength);
            await s.WriteAsync(init, ct);
            if (NameLength is > 0 and <= 4096)
            {
                await s.WriteAsync(new byte[NameLength], ct);
            }

            await ReadAsync(s, 20 + 12 + 10, ct); // SetPixelFormat, SetEncodings, FBUpdateRequest
            if (AfterInit is not null)
            {
                await AfterInit(s, ct);
            }
        }
        catch (Exception)
        {
            // the client went away mid-script
        }
    }

    public static async Task<byte[]> ReadAsync(Stream s, int n, CancellationToken ct)
    {
        var buf = new byte[n];
        var off = 0;
        while (off < n)
        {
            var r = await s.ReadAsync(buf.AsMemory(off), ct);
            if (r == 0)
            {
                throw new EndOfStreamException();
            }

            off += r;
        }

        return buf;
    }

    private static byte[] U32(uint v)
    {
        var b = new byte[4];
        BinaryPrimitives.WriteUInt32BigEndian(b, v);
        return b;
    }

    public async ValueTask DisposeAsync()
    {
        await _cts.CancelAsync();
        _listener.Stop();
        await _loop;
        Task[] sessions;
        lock (_sessions)
        {
            sessions = _sessions.ToArray();
        }

        await Task.WhenAll(sessions);
        _cts.Dispose();
    }
}
