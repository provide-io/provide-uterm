//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.ServerConfig;

namespace Provide.Uterm.Tests;

/// <summary>
/// <c>[session_persistence]</c> loads as the reference loads it: this port keeps
/// no capture session, but it accepts a config that asks for them and refuses a
/// bound the reference refuses, with the reference's own text.
/// </summary>
public sealed class ServerConfigSessionPersistenceTests
{
    private static UtermServerConfig Load(string body)
    {
        var path = Path.Combine(Path.GetTempPath(), "uterm-session-persistence-" + Guid.NewGuid().ToString("N") + ".toml");
        File.WriteAllText(path, "[session_persistence]\n" + body + "\n");
        try
        {
            return ConfigLoader.Load(path);
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Fact]
    public void TheDefaultsAreOffWithAThousandRows()
    {
        var settings = new UtermServerConfig().SessionPersistence;
        Assert.False(settings.PersistCapture);
        Assert.Equal(0, settings.PreviousKeepDays);
        Assert.Equal(1000, settings.PreviousScrollbackLines);
        Assert.Null(settings.StateDir);
    }

    [Theory]
    [InlineData("state/", "state")]
    [InlineData("", ".")]
    [InlineData("./a//b/", "a/b")]
    public void AStateDirectoryThatIsNotAbsoluteIsRefusedAsTheReferenceQuotesIt(string written, string shown)
    {
        var error = Assert.Throws<ArgumentException>(() => Load("state_dir = \"" + written + "\""));
        Assert.Equal("session_persistence.state_dir must be an absolute path, got: " + shown, error.Message);
    }

    [Theory]
    [InlineData("persist_capture = true")]
    [InlineData("previous_keep_days = 1")]
    [InlineData("persist_capture = true\nstate_dir = 5")]
    public void KeepingSomethingWithNowhereToKeepItIsRefused(string body)
    {
        var error = Assert.Throws<ArgumentException>(() => Load(body));
        Assert.Equal(
            "session_persistence.state_dir is required when session_persistence.persist_capture "
            + "is on or session_persistence.previous_keep_days is above 0",
            error.Message);
    }

    [Fact]
    public void EveryKeyIsRead()
    {
        var settings = Load(
            "persist_capture = true\nprevious_keep_days = 7\nprevious_scrollback_lines = 50\nstate_dir = \"/var/lib/u//s/\"")
            .SessionPersistence;
        Assert.True(settings.PersistCapture);
        Assert.Equal(7, settings.PreviousKeepDays);
        Assert.Equal(50, settings.PreviousScrollbackLines);
        Assert.Equal("/var/lib/u/s", settings.StateDir);
    }

    [Theory]
    [InlineData("previous_keep_days = 0", 0, 1000)]
    [InlineData("previous_keep_days = 3650\nstate_dir = \"/s\"", 3650, 1000)]
    [InlineData("previous_keep_days = \"14\"\nstate_dir = \"/s\"", 14, 1000)]
    [InlineData("previous_keep_days = 7.0\nstate_dir = \"/s\"", 7, 1000)]
    [InlineData("previous_keep_days = \"a week\"", 0, 1000)]
    [InlineData("previous_scrollback_lines = 0", 0, 0)]
    [InlineData("previous_scrollback_lines = 10000", 0, 10000)]
    public void AValueInsideItsBoundsIsKept(string line, int days, int lines)
    {
        var settings = Load(line).SessionPersistence;
        Assert.Equal(days, settings.PreviousKeepDays);
        Assert.Equal(lines, settings.PreviousScrollbackLines);
    }

    [Theory]
    [InlineData("-1")]
    [InlineData("3651")]
    [InlineData("4294967296")]
    public void PreviousDaysOutsideTheirBoundsAreRefused(string value)
    {
        var error = Assert.Throws<ArgumentException>(() => Load("previous_keep_days = " + value));
        Assert.Equal(
            "session_persistence.previous_keep_days must be between 0 (keep no previous sessions) and 3650, got: " + value,
            error.Message);
    }

    [Theory]
    [InlineData("-1")]
    [InlineData("10001")]
    public void ScrollbackOutsideItsBoundsIsRefused(string value)
    {
        var error = Assert.Throws<ArgumentException>(() => Load("previous_scrollback_lines = " + value));
        Assert.Equal(
            "session_persistence.previous_scrollback_lines must be between 0 and 10000, got: " + value,
            error.Message);
    }

    [Fact]
    public void APersistSwitchThatIsNotABooleanIsLeftOff()
    {
        Assert.False(Load("persist_capture = \"yes\"").SessionPersistence.PersistCapture);
    }

    [Fact]
    public void AKeyNobodyDefinedIsRefusedByName()
    {
        var error = Assert.Throws<ArgumentException>(() => Load("persist_captures = true"));
        Assert.Equal("session_persistence.persist_captures: Extra inputs are not permitted", error.Message);
    }
}
