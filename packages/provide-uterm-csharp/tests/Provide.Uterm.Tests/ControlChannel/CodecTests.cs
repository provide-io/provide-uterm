//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Provide.Uterm.ControlChannel;
using Xunit;

namespace Provide.Uterm.Tests.ControlChannel;

public class CodecTests
{
    private static string MustEncode(Dictionary<string, object?> payload) =>
        ControlChannelCodec.EncodeControlFrame(payload);

    private static List<Chunk> FeedAll(global::Provide.Uterm.ControlChannel.Decoder d, params string[] chunks)
    {
        var outList = new List<Chunk>();
        foreach (var c in chunks)
        {
            outList.AddRange(d.Feed(c));
        }

        return outList;
    }

    [Fact]
    public void EncodeTerminalData_EscapesDle()
    {
        Assert.Equal("a\u0010\u0010b", ControlChannelCodec.EncodeTerminalData("a\u0010b"));
        Assert.Equal("plain", ControlChannelCodec.EncodeTerminalData("plain"));
    }

    [Fact]
    public void EncodeControlFrame_Shape()
    {
        var frame = MustEncode(new Dictionary<string, object?> { ["type"] = "ping" });
        var payload = "{\"type\":\"ping\"}";
        var want = "\u0010\u0002" + payload.Length.ToString("x8") + ":" + payload;
        Assert.Equal(want, frame);
        Assert.True(ControlChannelCodec.IsControlFrame(frame));
    }

    [Fact]
    public void EncodeControlFrame_UnicodeByteLength()
    {
        var frame = MustEncode(new Dictionary<string, object?> { ["msg"] = "héllo" });
        Assert.True(ControlChannelCodec.IsControlFrame(frame));
        var d = new global::Provide.Uterm.ControlChannel.Decoder();
        var events = FeedAll(d, frame);
        Assert.Single(events);
        var ctrl = Assert.IsType<ControlChunk>(events[0]);
        Assert.Equal("héllo", ctrl.Control["msg"]);
    }

    [Fact]
    public void EncodeControlFrame_NoHtmlEscaping()
    {
        var frame = MustEncode(new Dictionary<string, object?> { ["cmd"] = "<a&b>" });
        Assert.Contains("<a&b>", frame, StringComparison.Ordinal);
    }

    [Fact]
    public void RoundTrip_MixedDataAndControl()
    {
        var frame = MustEncode(new Dictionary<string, object?> { ["type"] = "hello", ["n"] = 1L });
        var stream = ControlChannelCodec.EncodeTerminalData("before\u0010dle") + frame + "after";
        var d = new global::Provide.Uterm.ControlChannel.Decoder();
        var events = FeedAll(d, stream);
        Assert.Equal(3, events.Count);
        Assert.Equal("before\u0010dle", Assert.IsType<DataChunk>(events[0]).Data);
        var ctrl = Assert.IsType<ControlChunk>(events[1]);
        Assert.Equal("hello", ctrl.Control["type"]);
        Assert.Equal(1L, Convert.ToInt64(ctrl.Control["n"]));
        Assert.Equal("after", Assert.IsType<DataChunk>(events[2]).Data);
    }

    [Fact]
    public void WsBytes_RoundTrip()
    {
        var raw = new byte[] { 0x00, 0x41, 0xFF, 0x80 };
        var s = WsBytes.WsBytesToChannelStr(raw);
        var back = WsBytes.ChannelStrToBytes(s);
        Assert.Equal(raw, back);
    }

    [Fact]
    public void IncrementalFeed_SplitsFrames()
    {
        var frame = MustEncode(new Dictionary<string, object?> { ["type"] = "ping" });
        var d = new global::Provide.Uterm.ControlChannel.Decoder();
        // Feed one byte at a time
        var partial = new List<Chunk>();
        foreach (var ch in frame)
        {
            partial.AddRange(d.Feed(ch.ToString()));
        }

        Assert.Single(partial);
        Assert.Equal("ping", Assert.IsType<ControlChunk>(partial[0]).Control["type"]);
    }
}
