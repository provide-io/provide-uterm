//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Provide.Uterm.TunnelClient;

namespace Provide.Uterm.Tests;

public class TunnelCodecTests
{
    [Fact]
    public void EncodeDecode_RoundTrip()
    {
        var payload = Encoding.UTF8.GetBytes("hello");
        var frame = TunnelCodec.EncodeFrame(TunnelProtocol.ChannelData, payload, TunnelProtocol.FlagData);
        Assert.Equal(2 + payload.Length, frame.Length);
        Assert.Equal(TunnelProtocol.ChannelData, frame[0]);
        Assert.Equal(TunnelProtocol.FlagData, frame[1]);

        var decoded = TunnelCodec.DecodeFrame(frame);
        Assert.Equal(TunnelProtocol.ChannelData, decoded.Channel);
        Assert.Equal(payload, decoded.Payload);
        Assert.False(decoded.IsEof);
        Assert.False(decoded.IsControl);
    }

    [Fact]
    public void ChannelConstants_MatchWire()
    {
        Assert.Equal(0x00, TunnelProtocol.ChannelControl);
        Assert.Equal(0x01, TunnelProtocol.ChannelData);
        Assert.Equal(0x02, TunnelProtocol.ChannelTcp);
        Assert.Equal(0x03, TunnelProtocol.ChannelHttp);
        Assert.Equal(0x01, TunnelProtocol.FlagEof);
    }

    [Fact]
    public void DecodeFrame_TooShort_Throws()
    {
        Assert.Throws<ArgumentException>(() => TunnelCodec.DecodeFrame(new byte[] { 0x01 }));
    }

    [Fact]
    public void EncodeControl_RequiresType()
    {
        Assert.Throws<ArgumentException>(() =>
            TunnelCodec.EncodeControl(new Dictionary<string, object?> { ["x"] = 1 }));

        var frame = TunnelCodec.EncodeControl(new Dictionary<string, object?> { ["type"] = "open" });
        var decoded = TunnelCodec.DecodeFrame(frame);
        Assert.True(decoded.IsControl);
        var msg = TunnelCodec.DecodeControl(decoded.Payload);
        Assert.Equal("open", msg["type"]?.ToString());
    }

    [Fact]
    public void EofFlag()
    {
        var frame = TunnelCodec.EncodeFrame(TunnelProtocol.ChannelTcp, ReadOnlySpan<byte>.Empty, TunnelProtocol.FlagEof);
        var decoded = TunnelCodec.DecodeFrame(frame);
        Assert.True(decoded.IsEof);
    }
}
