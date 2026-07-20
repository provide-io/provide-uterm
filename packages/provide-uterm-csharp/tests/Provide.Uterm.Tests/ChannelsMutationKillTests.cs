//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.Channels;
using Provide.Uterm.ControlChannel;

namespace Provide.Uterm.Tests;

/// <summary>Kill boolean mutants in Channels.cs (mutation perimeter).</summary>
public class ChannelsMutationKillTests
{
    [Fact]
    public void ParseChannelHello_NonEmptyNonFrame_IsNull()
    {
        // Line 125: IsNullOrEmpty || !IsControlFrame — or→and would fall through for "x".
        Assert.Null(Negotiated.ParseChannelHello("x"));
        Assert.Null(Negotiated.ParseChannelHello("hello without framing"));
    }

    [Fact]
    public void ParseChannelHello_WrongType_IsNull()
    {
        // Line 137: missing type or type != hello continues (or→and must still skip).
        var other = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "snapshot",
            ["channels"] = new Dictionary<string, object?> { ["term"] = 1 },
        });
        Assert.Null(Negotiated.ParseChannelHello(other));

        var noType = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["channels"] = new Dictionary<string, object?> { ["term"] = 1 },
        });
        Assert.Null(Negotiated.ParseChannelHello(noType));
    }

    [Fact]
    public void ParseChannelHello_ChannelsNotMap_IsNull()
    {
        // Line 142: channels present but not a map → null.
        var bad = ControlChannelCodec.EncodeControlFrame(new Dictionary<string, object?>
        {
            ["type"] = "hello",
            ["channels"] = "nope",
        });
        Assert.Null(Negotiated.ParseChannelHello(bad));
    }

    [Fact]
    public void RestoreGrants_CoerceInt_DoubleFloatAndJson()
    {
        // Lines 226-228: truncate + infinity guards on double/float.
        var n = Negotiated.Create(new Dictionary<string, int> { ["term"] = 3 }, "term");
        n.RestoreGrants(new Dictionary<string, object?> { ["term"] = 2.0 });
        Assert.Equal(2, n.ExportGrants()["term"]);

        n.RestoreGrants(new Dictionary<string, object?> { ["term"] = 1.0f });
        Assert.Equal(1, n.ExportGrants()["term"]);

        using var doc = JsonDocument.Parse("2");
        n.RestoreGrants(new Dictionary<string, object?> { ["term"] = doc.RootElement });
        Assert.Equal(2, n.ExportGrants()["term"]);

        Assert.Throws<ArgumentException>(() =>
            n.RestoreGrants(new Dictionary<string, object?> { ["term"] = 1.5 }));
        Assert.Throws<ArgumentException>(() =>
            n.RestoreGrants(new Dictionary<string, object?> { ["term"] = double.PositiveInfinity }));
        // Float infinity: kills line 227 and_or (truncate-eq || !IsInfinity would accept ∞).
        Assert.Throws<ArgumentException>(() =>
            n.RestoreGrants(new Dictionary<string, object?> { ["term"] = float.PositiveInfinity }));
        Assert.Throws<ArgumentException>(() =>
            n.RestoreGrants(new Dictionary<string, object?> { ["term"] = 1.5f }));
    }

    [Fact]
    public void Negotiate_IgnoresZeroAndUnsupportedVersions()
    {
        // Line 239: supported.TryGetValue && version > 0
        var n = Negotiated.Create(new Dictionary<string, int> { ["term"] = 2, ["ctrl"] = 1 }, "term");
        n.HandleHello(new Hello
        {
            Channels = new Dictionary<string, int>
            {
                ["term"] = 0, // must not grant (version > 0)
                ["ctrl"] = 1,
                ["missing"] = 5, // unsupported
            },
        });
        var g = n.ExportGrants();
        Assert.False(g.ContainsKey("term"));
        Assert.Equal(1, g["ctrl"]);
        Assert.False(g.ContainsKey("missing"));
    }
}
