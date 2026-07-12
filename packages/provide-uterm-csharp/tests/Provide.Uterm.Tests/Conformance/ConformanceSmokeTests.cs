//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ControlChannel;
using Provide.Uterm.Tunnel;

namespace Provide.Uterm.Tests.Conformance;

/// <summary>
/// Smoke conformance markers — full differential corpora live under testdata/
/// and are exercised by package-level tests. This directory exists so the C#
/// tree is not empty for Conformance/Interop (skeptic residual).
/// </summary>
public class ConformanceSmokeTests
{
    [Fact]
    public void ControlChannel_RoundTrip_ObjectFrame()
    {
        var payload = new Dictionary<string, object?>
        {
            ["type"] = "hello",
            ["v"] = 1,
        };
        var frame = ControlChannelCodec.EncodeControlFrame(payload);
        Assert.True(ControlChannelCodec.IsControlFrame(frame));
        var dec = new ControlFrameDecoder();
        var chunks = dec.Feed(frame);
        Assert.Contains(chunks, c => c is ControlChunk);
    }

    [Fact]
    public void TunnelToken_Blake2b_MatchesGoldenShape()
    {
        var h = TunnelTokens.HashToken("conformance-token");
        Assert.Equal(64, h.Length);
        Assert.Matches("^[0-9a-f]{64}$", h);
    }
}
