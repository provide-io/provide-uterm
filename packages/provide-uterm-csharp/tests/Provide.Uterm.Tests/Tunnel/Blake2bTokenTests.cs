//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.Json;
using Provide.Uterm.Tunnel;

namespace Provide.Uterm.Tests.Tunnel;

/// <summary>
/// Byte-for-byte BLAKE2b-256 parity with Python/Go tunnel token hashes.
/// </summary>
public class Blake2bTokenTests
{
    [Fact]
    public void HashToken_Empty_IsEmpty()
    {
        Assert.Equal("", TunnelTokens.HashToken(""));
    }

    [Fact]
    public void HashToken_KnownVector()
    {
        // First case from testdata/tunnel/token_hash_golden.json (Python blake2b-256).
        var path = TestData.PathTo("tunnel", "token_hash_golden.json");
        using var doc = System.Text.Json.JsonDocument.Parse(File.ReadAllText(path));
        var first = doc.RootElement.GetProperty("cases")[0];
        Assert.Equal(
            first.GetProperty("blake2b_hex").GetString(),
            TunnelTokens.HashToken(first.GetProperty("plain").GetString()!));
    }

    [Fact]
    public void HashToken_GoldenCorpus_MatchesPython()
    {
        var path = TestData.PathTo("tunnel", "token_hash_golden.json");
        Assert.True(File.Exists(path), path);
        using var doc = JsonDocument.Parse(File.ReadAllText(path));
        foreach (var c in doc.RootElement.GetProperty("cases").EnumerateArray())
        {
            var plain = c.GetProperty("plain").GetString()!;
            var want = c.GetProperty("blake2b_hex").GetString()!;
            Assert.Equal(want, TunnelTokens.HashToken(plain));
        }
    }

    [Fact]
    public void VerifyToken_EmptyNeverAuthenticates()
    {
        Assert.False(TunnelTokens.VerifyToken("", "abc"));
        Assert.False(TunnelTokens.VerifyToken("abc", ""));
        var h = TunnelTokens.HashToken("secret");
        Assert.True(TunnelTokens.VerifyToken("secret", h));
        Assert.False(TunnelTokens.VerifyToken("other", h));
    }
}
