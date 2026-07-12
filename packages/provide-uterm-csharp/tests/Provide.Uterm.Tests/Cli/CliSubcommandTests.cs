//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Cli;

namespace Provide.Uterm.Tests.Cli;

public class CliSubcommandTests
{
    [Theory]
    [InlineData("proxy")]
    [InlineData("listen")]
    [InlineData("share")]
    [InlineData("tunnel")]
    [InlineData("inspect")]
    [InlineData("watch")]
    [InlineData("audit")]
    [InlineData("server")]
    public void Subcommand_Help_ExitsZero(string cmd)
    {
        using var o = new StringWriter();
        using var e = new StringWriter();
        var code = Root.Execute(new[] { cmd, "--help" }, o, e);
        Assert.Equal(0, code);
        Assert.Contains(cmd, o.ToString(), StringComparison.Ordinal);
    }

    [Fact]
    public void UnknownCommand_ExitsNonZero()
    {
        using var o = new StringWriter();
        using var e = new StringWriter();
        var code = Root.Execute(new[] { "nope" }, o, e);
        Assert.True(code != 0);
        Assert.Contains("unknown", e.ToString(), StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public void Version_Prints()
    {
        using var o = new StringWriter();
        var code = Root.Execute(new[] { "--version" }, o, TextWriter.Null);
        Assert.Equal(0, code);
        Assert.False(string.IsNullOrWhiteSpace(o.ToString()));
    }

    [Fact]
    public void Proxy_Run_ReportsBind()
    {
        using var o = new StringWriter();
        var code = Root.Execute(
            new[] { "proxy", "127.0.0.1", "23", "--port", "18765", "--once", "--bind", "127.0.0.1" },
            o,
            TextWriter.Null);
        Assert.Equal(0, code);
        Assert.Contains("18765", o.ToString(), StringComparison.Ordinal);
        Assert.DoesNotContain("stub", o.ToString(), StringComparison.OrdinalIgnoreCase);
    }
}
