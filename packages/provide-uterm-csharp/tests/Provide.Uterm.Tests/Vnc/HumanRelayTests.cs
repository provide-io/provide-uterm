//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Policy;
using Provide.Uterm.Vnc;
using Xunit;

namespace Provide.Uterm.Tests.Vnc;

public class HumanRelayTests
{
    private static byte[] Handshake()
    {
        var h = new byte[14];
        System.Text.Encoding.ASCII.GetBytes("RFB 003.008\n").CopyTo(h, 0);
        h[12] = 1; // security None
        h[13] = 1; // ClientInit
        return h;
    }

    private static byte[] KeyEvent()
    {
        var k = new byte[8];
        k[0] = 4;
        return k;
    }

    [Fact]
    public void Allow_Inject_Key_Reaches_Upstream_After_Handshake()
    {
        using var src = new MemoryStream(Handshake().Concat(KeyEvent()).ToArray());
        using var dst = new MemoryStream();
        var policy = new StrictPolicyEngine();
        HumanRelay.PumpClientToServer(
            dst,
            src,
            (sid, lid, pid, role) => policy.CanInject(sid, lid, role) is null,
            "sess",
            "lease-1",
            "bob",
            "operator");
        Assert.Equal(14 + 8, dst.Length);
    }

    [Fact]
    public void Deny_Inject_Only_Handshake_On_Upstream()
    {
        using var src = new MemoryStream(Handshake().Concat(KeyEvent()).ToArray());
        using var dst = new MemoryStream();
        var policy = new StrictPolicyEngine();
        // Empty lease → fail closed.
        HumanRelay.PumpClientToServer(
            dst,
            src,
            (sid, lid, pid, role) => policy.CanInject(sid, lid, role) is null,
            "sess",
            "",
            "bob",
            "operator");
        Assert.Equal(14, dst.Length);
    }

    [Fact]
    public void Bad_Security_Type_Propagates_Error()
    {
        var raw = new byte[13];
        System.Text.Encoding.ASCII.GetBytes("RFB 003.008\n").CopyTo(raw, 0);
        raw[12] = 2;
        using var src = new MemoryStream(raw);
        using var dst = new MemoryStream();
        var ex = Assert.Throws<InvalidOperationException>(() =>
            HumanRelay.PumpClientToServer(dst, src, null, "s", "l", "p", "admin"));
        Assert.Contains("unsupported security type", ex.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task Server_To_Client_Copies_Raw_Bytes()
    {
        var payload = new byte[] { 0x01, 0x02, 0xFF };
        using var serverSrc = new MemoryStream(payload);
        using var clientDst = new MemoryStream();
        await HumanRelay.PumpServerToClientAsync(clientDst, serverSrc);
        Assert.Equal(payload, clientDst.ToArray());
    }

    [Fact]
    public async Task RelayAsync_Forwards_Key_When_Allowed()
    {
        // Server source blocks until cancelled so client filter can finish first.
        using var hang = new HangReadStream();
        using var clientSrc = new MemoryStream(Handshake().Concat(KeyEvent()).ToArray());
        using var upstreamDst = new MemoryStream();
        using var clientDst = new MemoryStream();

        var relay = HumanRelay.RelayAsync(
            clientSrc,
            upstreamDst,
            hang,
            clientDst,
            static (_, lid, _, role) => lid.Length > 0 && role is "operator" or "admin",
            "s",
            "lease-1",
            "bob",
            "operator");

        // Client filter hits EOF and cancels the hanging server pump.
        await relay.WaitAsync(TimeSpan.FromSeconds(5));
        Assert.Equal(14 + 8, upstreamDst.Length);
    }

    [Fact]
    public async Task RelayAsync_Rethrows_Filter_Error()
    {
        var raw = new byte[13];
        System.Text.Encoding.ASCII.GetBytes("RFB 003.008\n").CopyTo(raw, 0);
        raw[12] = 2;
        using var hang = new HangReadStream();
        using var clientSrc = new MemoryStream(raw);
        using var upstreamDst = new MemoryStream();
        using var clientDst = new MemoryStream();
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            HumanRelay.RelayAsync(
                clientSrc, upstreamDst, hang, clientDst,
                null, "s", "l", "p", "admin"));
    }

    [Fact]
    public async Task RelayAsync_Copies_Server_Bytes_To_Client()
    {
        var video = new byte[] { 9, 8, 7, 6 };
        // Hang the client filter so server→client pump can complete first without cancel race.
        using var hangClient = new HangReadStream();
        using var serverSrc = new MemoryStream(video);
        using var upstreamDst = new MemoryStream();
        using var clientDst = new MemoryStream();
        using var cts = new CancellationTokenSource(TimeSpan.FromMilliseconds(200));
        try
        {
            await HumanRelay.RelayAsync(
                hangClient, upstreamDst, serverSrc, clientDst,
                null, "s", "", "p", "viewer", cts.Token).WaitAsync(TimeSpan.FromSeconds(5));
        }
        catch (OperationCanceledException)
        {
            // expected after video copy when client hang is cancelled
        }

        Assert.Equal(video, clientDst.ToArray());
    }

    /// <summary>Read blocks until cancellation (keeps dual-pump server side alive).</summary>
    private sealed class HangReadStream : Stream
    {
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
            ReadAsync(buffer.AsMemory(offset, count), CancellationToken.None).AsTask().GetAwaiter().GetResult();

        public override async ValueTask<int> ReadAsync(
            Memory<byte> buffer, CancellationToken cancellationToken = default)
        {
            try
            {
                await Task.Delay(Timeout.Infinite, cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return 0;
            }

            return 0;
        }

        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
    }
}
