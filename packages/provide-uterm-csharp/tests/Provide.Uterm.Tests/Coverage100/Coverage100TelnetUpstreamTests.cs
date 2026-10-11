//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Net;
using System.Net.Sockets;
using Provide.Uterm.Embed;

namespace Provide.Uterm.Tests.Coverage100;

/// <summary>
/// The live TCP telnet upstream against a loopback peer: the offer it sends,
/// negotiation answers, payload framing, and every way the peer can go away.
/// </summary>
public sealed class Coverage100TelnetUpstreamTests
{
    private const byte Iac = 255, Will = 251, Do = 253, Sb = 250, Se = 240;

    private sealed class SilentPolicy : ITelnetPolicy
    {
        public string TerminalType => "SILENT";
        public (int Cols, int Rows) WindowSize => (80, 24);
        public ReadOnlyMemory<byte> OnOption(byte command, byte option) => ReadOnlyMemory<byte>.Empty;
        public ReadOnlyMemory<byte> OnSubnegotiation(byte option, ReadOnlySpan<byte> body) => ReadOnlyMemory<byte>.Empty;
    }

    private static async Task<(TelnetUpstream Up, TcpClient Peer, TcpListener Listener)> ConnectAsync(ITelnetPolicy? policy = null)
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        var up = new TelnetUpstream("127.0.0.1", port, policy, TimeSpan.FromSeconds(10));
        var accept = listener.AcceptTcpClientAsync();
        await up.ConnectAsync();
        return (up, await accept, listener);
    }

    private static async Task<byte[]> ReadAtLeastAsync(NetworkStream s, int n)
    {
        var buf = new byte[n];
        await s.ReadExactlyAsync(buf).AsTask().WaitAsync(TimeSpan.FromSeconds(10));
        return buf;
    }

    [Fact]
    public async Task Unconnected_upstream_refuses_io()
    {
        var up = new TelnetUpstream("127.0.0.1", 1);
        Assert.False(up.IsConnected);
        await Assert.ThrowsAsync<InvalidOperationException>(() => up.SendAsync(new byte[] { 1 }));
        await Assert.ThrowsAsync<InvalidOperationException>(() => up.ReceiveAsync());
        await up.DisposeAsync();
    }

    [Fact]
    public async Task Negotiation_is_answered_reported_and_skipped_until_payload_arrives()
    {
        var (up, peer, listener) = await ConnectAsync();
        using (peer)
        {
            var events = new List<(WireEventKind Kind, string Detail)>();
            up.OnWire = (kind, _, detail) =>
            {
                lock (events) events.Add((kind, detail));
                return ValueTask.CompletedTask;
            };
            var s = peer.GetStream();
            // WILL BINARY, WILL SGA, then the policy's NAWS and TTYPE IS.
            var offer = await ReadAtLeastAsync(s, 6);
            Assert.Equal(new byte[] { Iac, Will, 0, Iac, Will, 3 }, offer);
            Assert.True(up.IsConnected);

            // Negotiation then payload: whether they arrive in one read or two,
            // the receive answers the negotiation and returns only the payload.
            // First a negotiation-only read. The peer seeing the answer (after
            // the proactive NAWS (9) and TTYPE IS ANSI (10) the connect sent)
            // proves that read was consumed before the payload is written, so
            // the receive must go round its loop again for the payload.
            var receive = up.ReceiveAsync();
            await s.WriteAsync(new byte[] { Iac, Do, 1 });
            _ = await ReadAtLeastAsync(s, 9 + 10);
            Assert.Equal(new byte[] { Iac, Will, 1 }, await ReadAtLeastAsync(s, 3));
            await s.WriteAsync(new byte[] { Iac, Sb, 24, 1, Iac, Se, (byte)'h', (byte)'i' });
            Assert.Equal("hi"u8.ToArray(), await receive.WaitAsync(TimeSpan.FromSeconds(10)));
            Assert.Equal(new byte[] { Iac, Sb, 24, 0, (byte)'A', (byte)'N', (byte)'S', (byte)'I', Iac, Se }, await ReadAtLeastAsync(s, 10));
            lock (events)
            {
                Assert.Contains((WireEventKind.Iac, "neg"), events);
                Assert.Contains((WireEventKind.Negotiation, "sb:24"), events);
            }

            await up.SendAsync(new byte[] { Iac, 7 });
            Assert.Equal(new byte[] { Iac, Iac, 7 }, await ReadAtLeastAsync(s, 3)); // IAC escaped
            await up.DisconnectAsync();
            Assert.False(up.IsConnected);
        }

        listener.Stop();
    }

    [Fact]
    public async Task A_silent_policy_sends_only_the_offer_and_answers_nothing()
    {
        var (up, peer, listener) = await ConnectAsync(new SilentPolicy());
        using (peer)
        {
            var s = peer.GetStream();
            Assert.Equal(6, (await ReadAtLeastAsync(s, 6)).Length);
            await s.WriteAsync(new byte[] { Iac, Do, 1, (byte)'x' });
            Assert.Equal("x"u8.ToArray(), await up.ReceiveAsync().WaitAsync(TimeSpan.FromSeconds(10)));
            // Nothing beyond the 6-byte offer reached the peer.
            Assert.Equal(0, peer.Available);
        }

        await up.DisposeAsync();
        listener.Stop();
    }

    [Fact]
    public async Task Graceful_close_and_teardown_under_a_read_both_read_as_end_of_stream()
    {
        var (up, peer, listener) = await ConnectAsync();
        peer.Close();
        Assert.Empty(await up.ReceiveAsync().WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.False(up.IsConnected);
        await up.DisposeAsync();

        // Torn down under a pending read: the read faults and reads as EOF.
        // (ReceiveAsync captures the stream before its first await, so the
        // teardown always lands on that captured stream.)
        var (up2, peer2, _) = await ConnectAsync();
        using (peer2)
        {
            var pending = up2.ReceiveAsync();
            await up2.DisconnectAsync();
            Assert.Empty(await pending.WaitAsync(TimeSpan.FromSeconds(10)));
            Assert.False(up2.IsConnected);
        }

        listener.Stop();
    }
}
