//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using System.Text.Json;
using Provide.Uterm.Frames;

namespace Provide.Uterm.Tests.Frames;

public class FramesTests
{
    [Fact]
    public void MakeTermFrame_HasTypeAndData()
    {
        var f = FrameBuilders.MakeTermFrame("hi", 1.5);
        Assert.Equal(FrameTypeNames.Term, f.Type);
        Assert.Equal("hi", f.Data);
    }

    [Fact]
    public void EncodeDecode_RoundTrip_Pong()
    {
        var pong = FrameBuilders.MakePongFrame(123.0);
        var bytes = FrameCodec.EncodeFrame(pong);
        var decoded = FrameCodec.DecodeFrame(bytes);
        Assert.Equal(FrameTypeNames.Pong, decoded.FrameType);
        var json = Encoding.UTF8.GetString(bytes);
        Assert.Contains("\"type\":\"pong\"", json, StringComparison.Ordinal);
    }

    [Fact]
    public void MakeErrorFrame_CarriesMessage()
    {
        var f = FrameBuilders.MakeErrorFrame("boom");
        Assert.Equal(FrameTypeNames.Error, f.Type);
        Assert.Equal("boom", f.Message);
    }

    [Fact]
    public void MakeHijackStateFrame_Fields()
    {
        var f = FrameBuilders.MakeHijackStateFrame(true, "op", 99.0, "hijack");
        Assert.True(f.Hijacked);
        Assert.Equal("op", f.Owner);
        Assert.Equal(99.0, f.LeaseExpiresAt);
        Assert.Equal("hijack", f.InputMode);
    }

    [Fact]
    public void DecodeFrame_UnknownType_Throws()
    {
        Assert.ThrowsAny<Exception>(() => FrameCodec.DecodeFrame("{\"type\":\"nope\"}"u8.ToArray()));
    }

    [Fact]
    public void NewIdentityFrame_Defaults()
    {
        var f = FrameBuilders.NewIdentityFrame("alice");
        Assert.Equal("alice", f.Subject);
        Assert.Equal("ssh", f.Transport);
        Assert.Equal(1, f.Version);
    }
}
