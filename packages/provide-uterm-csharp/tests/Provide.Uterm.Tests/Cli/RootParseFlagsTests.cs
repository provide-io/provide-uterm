//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Cli;
using Provide.Uterm.Conformance;

namespace Provide.Uterm.Tests.Cli;

// DevIdp worker tokens are base64url, so ~1 in 64 starts with '-'. `--token <tok>`
// used to drop such a token silently; like the Python CLI (argparse), the
// separate-argument form is now rejected and `--token=<tok>` is the way to pass it.
public class RootParseFlagsTests
{
    [Theory]
    [InlineData("-abc")]
    [InlineData("--abc")]
    [InlineData("--")]
    [InlineData("--once")]
    public void ValueFlag_SeparateArg_OptionLikeValue_IsRejected(string value)
    {
        var ex = Assert.Throws<ArgumentException>(() => Root.ParseFlags(["--token", value, "--url", "ws://h/t"]));
        Assert.Contains("argument --token: expected one argument", ex.Message, StringComparison.Ordinal);
        Assert.Contains("--token=VALUE", ex.Message, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("-")]
    [InlineData("-5")]
    [InlineData("-.5")]
    [InlineData("-1.5")]
    [InlineData("-a b")]
    public void ValueFlag_SeparateArg_ArgparseValue_IsKept(string value)
    {
        var f = Root.ParseFlags(["--token", value, "--url", "ws://h/t"]);
        Assert.Equal(value, f["token"]);
        Assert.Equal("ws://h/t", f["url"]);
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
    public void ShortPort_OptionLikeValue_IsRejected()
    {
        var ex = Assert.Throws<ArgumentException>(() => Root.ParseFlags(["-p", "-x"]));
        Assert.Contains("argument -p: expected one argument", ex.Message, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("once")]
    [InlineData("insecure-ssh")]
    [InlineData("allow-unauthenticated")]
    [InlineData("help")]
    [InlineData("version")]
    public void BooleanFlag_DoesNotConsumeNextArg(string flag)
    {
        var f = Root.ParseFlags([$"--{flag}", "--token=-abc", "pos"]);
        Assert.Equal("true", f[flag]);
        Assert.Equal("-abc", f["token"]);
        Assert.Equal("pos", f["_0"]);
    }

    [Fact]
    public void ValueFlag_Last_IsTrue()
    {
        var f = Root.ParseFlags(["--token"]);
        Assert.Equal("true", f["token"]);
    }

    [Fact]
    public void Execute_Tunnel_DashToken_SeparateArg_FailsWithUsageError()
    {
        using var e = new StringWriter();
        var code = Root.Execute(["tunnel", "--url", "ws://127.0.0.1:9/tunnel", "--token", "-tok_9x", "--once"], TextWriter.Null, e);
        Assert.Equal(1, code);
        Assert.Contains("error: argument --token: expected one argument", e.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public async Task LiveDriver_DashToken_SeparateArg_FailsWithUsageError()
    {
        using var e = new StringWriter();
        var code = await LiveDriver.ExecuteAsync(["client", "--token", "-tok_9x"], new StringWriter(), e);
        Assert.Equal(LiveDriver.UsageExitCode, code);
        Assert.Contains("error: argument --token: expected one argument", e.ToString(), StringComparison.Ordinal);
    }
}
