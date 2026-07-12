//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Embed;
using Provide.Uterm.Filters;

namespace Provide.Uterm.Tests.Embed;

public class TelnetUpstreamTests
{
    [Fact]
    public void TelnetIac_Parse_AppBytes_And_Escape()
    {
        var raw = new byte[] { (byte)'H', (byte)'i', InputFilters.Iac, InputFilters.Iac, (byte)'!' };
        var (payload, events, consumed) = TelnetIac.Parse(raw);
        Assert.Equal(new byte[] { (byte)'H', (byte)'i', InputFilters.Iac, (byte)'!' }, payload);
        Assert.Empty(events);
        Assert.Equal(raw.Length, consumed);

        var escaped = TelnetIac.Escape(new byte[] { 1, 255, 2 });
        Assert.Equal(new byte[] { 1, 255, 255, 2 }, escaped);
    }

    [Fact]
    public void TelnetIac_Parse_Negotiate_And_Subneg()
    {
        // DO BINARY + incomplete trailing
        var doBin = new byte[] { InputFilters.Iac, InputFilters.Do, TelnetIac.OptBinary, (byte)'X' };
        var (p1, e1, c1) = TelnetIac.Parse(doBin);
        Assert.Equal(new[] { (byte)'X' }, p1);
        Assert.Single(e1);
        Assert.False(e1[0].IsSubnegotiation);
        Assert.Equal(InputFilters.Do, e1[0].Command);
        Assert.Equal(TelnetIac.OptBinary, e1[0].Option);
        Assert.Equal(doBin.Length, c1);

        // SB TTYPE SEND SE
        var sb = new byte[]
        {
            InputFilters.Iac, InputFilters.Sb, TelnetIac.OptTtype, 1,
            InputFilters.Iac, InputFilters.Se,
            (byte)'A',
        };
        var (p2, e2, _) = TelnetIac.Parse(sb);
        Assert.Equal(new[] { (byte)'A' }, p2);
        Assert.True(e2[0].IsSubnegotiation);
        Assert.Equal(TelnetIac.OptTtype, e2[0].Option);
        Assert.Equal(new byte[] { 1 }, e2[0].SubPayload);

        // truncated IAC at end without final → unconsumed
        var trunc = new byte[] { (byte)'Z', InputFilters.Iac };
        var (p3, e3, c3) = TelnetIac.Parse(trunc, final: false);
        Assert.Equal(new[] { (byte)'Z' }, p3);
        Assert.Empty(e3);
        Assert.Equal(1, c3);
        var (p4, _, c4) = TelnetIac.Parse(trunc, final: true);
        Assert.Equal(new[] { (byte)'Z', InputFilters.Iac }, p4);
        Assert.Equal(2, c4);

        // truncated DO (IAC DO without option) final flushes as literal
        var truncDo = new byte[] { InputFilters.Iac, InputFilters.Do };
        var (p5, e5, c5) = TelnetIac.Parse(truncDo, final: true);
        Assert.Equal(truncDo, p5);
        Assert.Empty(e5);
        Assert.Equal(2, c5);

        // incomplete SB without final leaves unconsumed
        var truncSb = new byte[] { InputFilters.Iac, InputFilters.Sb, TelnetIac.OptTtype, 1 };
        var (p6, e6, c6) = TelnetIac.Parse(truncSb, final: false);
        Assert.Empty(p6);
        Assert.Empty(e6);
        Assert.Equal(0, c6);
        var (p7, _, c7) = TelnetIac.Parse(truncSb, final: true);
        Assert.Equal(truncSb, p7);
        Assert.Equal(truncSb.Length, c7);

        // unknown 2-byte IAC command skipped
        var unk = new byte[] { InputFilters.Iac, 241, (byte)'Q' };
        var (p8, e8, _) = TelnetIac.Parse(unk);
        Assert.Equal(new[] { (byte)'Q' }, p8);
        Assert.Empty(e8);
    }

    [Fact]
    public async Task ScriptedTelnet_PolicyAnswers_And_EmbedSession()
    {
        var policy = new DefaultTelnetPolicy { TerminalType = "ANSI-BBS" };
        var up = new ScriptedTelnetUpstream(policy);
        var wires = new List<WireEventKind>();
        up.OnWire = (k, _, _) =>
        {
            wires.Add(k);
            return ValueTask.CompletedTask;
        };

        await up.ConnectAsync();
        // Remote DO BINARY → policy WILL reply captured in SentWire
        await up.PushWireAsync(new byte[] { InputFilters.Iac, InputFilters.Do, TelnetIac.OptBinary });
        // App data after IAC
        await up.PushWireAsync(new byte[] { (byte)'G', (byte)'O' });

        var hub = new EmbedHub();
        var session = await hub.CreateSessionAsync();
        await session.ConnectUpstreamAsync(up);
        var client = await session.AttachClientAsync(new ClientAttachOptions
        {
            Metadata = new ClientMetadata { ClientId = "c1" },
        });

        // First receive may be empty if only IAC processed then GO
        var app = await client.ReceiveAsync();
        Assert.Equal(new[] { (byte)'G', (byte)'O' }, app);
        Assert.Contains(WireEventKind.Iac, wires);
        Assert.NotEmpty(up.SentWire);

        await session.SendToUpstreamAsync(new byte[] { 255, 1 }); // IAC byte in app
        Assert.Contains(up.SentWire, b => b.SequenceEqual(new byte[] { 255, 255, 1 }));

        await session.DisposeAsync();
    }

    [Fact]
    public async Task ConnectionTransportUpstream_WrapsMemoryLikeTransport()
    {
        var fake = new LoopTransport();
        var up = new ConnectionTransportUpstream(fake, "h", 1);
        await up.ConnectAsync();
        Assert.True(up.IsConnected);
        await up.SendAsync(new byte[] { 9 });
        Assert.Equal(new byte[] { 9 }, fake.LastSent);
        fake.NextReceive = new byte[] { 7 };
        Assert.Equal(new byte[] { 7 }, await up.ReceiveAsync());
        fake.ThrowIo = true;
        Assert.Empty(await up.ReceiveAsync());
        fake.ThrowIo = false;
        fake.ThrowInvalid = true;
        Assert.Empty(await up.ReceiveAsync());
        await up.DisconnectAsync();
        await up.DisposeAsync();
    }

    [Fact]
    public async Task ScriptedTelnet_Subneg_And_Disconnect()
    {
        var up = new ScriptedTelnetUpstream(new DefaultTelnetPolicy { TerminalType = "X" });
        await up.ConnectAsync();
        // TTYPE SEND subnegotiation
        await up.PushWireAsync(new byte[]
        {
            InputFilters.Iac, InputFilters.Sb, TelnetIac.OptTtype, 1,
            InputFilters.Iac, InputFilters.Se,
        });
        await up.PushWireAsync(new byte[] { (byte)'Z' });
        Assert.Equal(new[] { (byte)'Z' }, await up.ReceiveAsync());
        Assert.Contains(up.SentWire, b => b.Length > 4 && b[0] == InputFilters.Iac);
        await up.DisconnectAsync();
        Assert.Empty(await up.ReceiveAsync());
        await up.DisposeAsync();
    }

    private sealed class LoopTransport : Provide.Uterm.Transports.IConnectionTransport
    {
        public byte[]? LastSent { get; private set; }
        public byte[] NextReceive { get; set; } = Array.Empty<byte>();
        public bool ThrowIo { get; set; }
        public bool ThrowInvalid { get; set; }
        private bool _ok;

        public Task ConnectAsync(string host, int port, Provide.Uterm.Transports.ConnectOptions? options = null, CancellationToken cancellationToken = default)
        {
            _ok = true;
            return Task.CompletedTask;
        }

        public Task DisconnectAsync(CancellationToken cancellationToken = default)
        {
            _ok = false;
            return Task.CompletedTask;
        }

        public Task SendAsync(byte[] data, CancellationToken cancellationToken = default)
        {
            LastSent = data;
            return Task.CompletedTask;
        }

        public Task<byte[]> ReceiveAsync(int maxBytes, TimeSpan timeout, CancellationToken cancellationToken = default)
        {
            if (ThrowIo)
            {
                throw new IOException("closed");
            }

            if (ThrowInvalid)
            {
                throw new InvalidOperationException("not connected");
            }

            return Task.FromResult(NextReceive);
        }

        public bool IsConnected() => _ok;
    }
}
