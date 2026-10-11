//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Cli;

namespace Provide.Uterm.Tests.Cli;

// A value flag must keep a value that begins with '-': DevIdp worker tokens are
// base64url, so ~1 in 64 starts with '-', and `--token <tok>` used to drop it.
public class RootParseFlagsTests
{
    [Theory]
    [InlineData("-abc")]
    [InlineData("--abc")]
    [InlineData("-")]
    [InlineData("--")]
    public void ValueFlag_SeparateArg_KeepsDashValue(string value)
    {
        var f = Root.ParseFlags(["--token", value, "--url", "ws://h/t"]);
        Assert.Equal(value, f["token"]);
        Assert.Equal("ws://h/t", f["url"]);
        Assert.False(f.ContainsKey("_0"));
    }

    [Theory]
    [InlineData("-abc")]
    [InlineData("--abc")]
    [InlineData("-")]
    [InlineData("--")]
    public void ValueFlag_EqualsForm_KeepsDashValue(string value)
    {
        var f = Root.ParseFlags([$"--token={value}", "--url", "ws://h/t"]);
        Assert.Equal(value, f["token"]);
        Assert.Equal("ws://h/t", f["url"]);
    }

    [Fact]
    public void ValueFlag_ValueLooksLikeBooleanFlag_IsStillTheValue()
    {
        var f = Root.ParseFlags(["--token", "--once"]);
        Assert.Equal("--once", f["token"]);
        Assert.False(f.ContainsKey("once"));
    }

    [Theory]
    [InlineData("once")]
    [InlineData("insecure-ssh")]
    [InlineData("allow-unauthenticated")]
    [InlineData("help")]
    [InlineData("version")]
    public void BooleanFlag_DoesNotConsumeNextArg(string flag)
    {
        var f = Root.ParseFlags([$"--{flag}", "-abc", "pos"]);
        Assert.Equal("true", f[flag]);
        Assert.Equal("pos", f["_0"]);
        Assert.False(f.ContainsKey("abc"));
    }

    [Fact]
    public void ValueFlag_Last_IsTrue()
    {
        var f = Root.ParseFlags(["--token"]);
        Assert.Equal("true", f["token"]);
    }

    [Fact]
    public void TunnelArgs_TokenStartingWithDash_IsKept()
    {
        // Previously "--token" parsed as "true" and "-tok_9x" was dropped, so the
        // tunnel client authenticated with the literal bearer "true".
        var f = Root.ParseFlags(["--url", "ws://127.0.0.1:9/tunnel", "--token", "-tok_9x", "--once"]);
        Assert.Equal("-tok_9x", f["token"]);
        Assert.Equal("true", f["once"]);
    }
}
