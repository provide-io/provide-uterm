//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Provide.Uterm.TunnelClient;

namespace Provide.Uterm.Tests.TunnelClient;

public class TunnelClientTests
{
    [Fact]
    public void Codec_RoundTrip_AllChannels()
    {
        foreach (var ch in new byte[]
                 {
                     TunnelProtocol.ChannelControl,
                     TunnelProtocol.ChannelData,
                     TunnelProtocol.ChannelTcp,
                     TunnelProtocol.ChannelHttp,
                 })
        {
            var payload = Encoding.UTF8.GetBytes("hello");
            var frame = TunnelCodec.EncodeFrame(ch, payload, TunnelProtocol.FlagData);
            var decoded = TunnelCodec.DecodeFrame(frame);
            Assert.Equal(ch, decoded.Channel);
            Assert.Equal(payload, decoded.Payload);
            Assert.False(decoded.IsEof);
        }
    }

    [Fact]
    public void Codec_EofFlag()
    {
        var frame = TunnelCodec.EncodeFrame(TunnelProtocol.ChannelData, ReadOnlySpan<byte>.Empty, TunnelProtocol.FlagEof);
        var d = TunnelCodec.DecodeFrame(frame);
        Assert.True(d.IsEof);
    }

    [Fact]
    public void EncodeControl_RequiresType()
    {
        Assert.Throws<ArgumentException>(() =>
            TunnelCodec.EncodeControl(new Dictionary<string, object?> { ["x"] = 1 }));
        var bytes = TunnelCodec.EncodeControl(new Dictionary<string, object?> { ["type"] = "hello" });
        var f = TunnelCodec.DecodeFrame(bytes);
        Assert.True(f.IsControl);
        var msg = TunnelCodec.DecodeControl(f.Payload);
        Assert.Equal("hello", msg["type"]);
    }

    [Fact]
    public void DecodeFrame_TooShort_Throws()
    {
        Assert.Throws<ArgumentException>(() => TunnelCodec.DecodeFrame(new byte[] { 0x01 }));
    }

    [Fact]
    public async Task HttpInspectProxy_ForwardsAndRecords()
    {
        using var upstream = new System.Net.HttpListener();
        var upstreamPort = FreePort();
        upstream.Prefixes.Add($"http://127.0.0.1:{upstreamPort}/");
        upstream.Start();
        var upTask = Task.Run(async () =>
        {
            var ctx = await upstream.GetContextAsync();
            var body = Encoding.UTF8.GetBytes("upstream-body");
            ctx.Response.StatusCode = 200;
            ctx.Response.ContentLength64 = body.Length;
            await ctx.Response.OutputStream.WriteAsync(body);
            ctx.Response.Close();
        });

        var proxy = new HttpInspectProxy($"http://127.0.0.1:{upstreamPort}");
        await proxy.StartAsync("127.0.0.1", 0);
        try
        {
            using var http = new HttpClient();
            var resp = await http.GetStringAsync($"http://127.0.0.1:{proxy.Port}/path");
            Assert.Equal("upstream-body", resp);
            Assert.Contains(proxy.Transactions, t => t.Path.Contains("path", StringComparison.Ordinal) && t.Status == 200);
        }
        finally
        {
            await proxy.StopAsync();
            upstream.Stop();
        }
    }

    [Fact]
    public async Task PtyShare_StartsAndStops()
    {
        await using var share = new PtyShareSession("true");
        await share.StartAsync();
        // Process may exit quickly; Start must not throw.
        await share.DisposeAsync();
    }

    private static int FreePort()
    {
        var l = new System.Net.Sockets.TcpListener(System.Net.IPAddress.Loopback, 0);
        l.Start();
        var p = ((System.Net.IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return p;
    }
}
