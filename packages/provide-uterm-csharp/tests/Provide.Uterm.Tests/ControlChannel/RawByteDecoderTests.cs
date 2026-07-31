//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Provide.Uterm.ControlChannel;

namespace Provide.Uterm.Tests.ControlChannel;

public sealed class RawByteDecoderTests
{
    [Fact]
    public void BinaryControlFrameUsesOriginalUtf8ByteLength()
    {
        var encoded = Encoding.UTF8.GetBytes(ControlChannelCodec.EncodeControlFrame(
            new Dictionary<string, object?> { ["type"] = "snapshot", ["text"] = "café 東京" }));

        var chunk = Assert.IsType<ControlChunk>(Assert.Single(
            new ControlFrameDecoder().FeedBytes(encoded, preserveRawData: true)));

        Assert.Equal("café 東京", chunk.Control["text"]);
    }

    [Fact]
    public void BinaryTerminalDataKeepsOneToOneByteMapping()
    {
        byte[] raw = [0xff, 0x80, (byte)'A'];

        var chunk = Assert.IsType<DataChunk>(Assert.Single(
            new ControlFrameDecoder().FeedBytes(raw, preserveRawData: true)));

        Assert.Equal(raw, Encoding.Latin1.GetBytes(chunk.Data));
    }
}
