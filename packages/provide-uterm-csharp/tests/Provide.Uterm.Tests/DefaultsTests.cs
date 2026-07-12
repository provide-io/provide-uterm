//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Defaults;

namespace Provide.Uterm.Tests;

public class DefaultsTests
{
    [Fact]
    public void Ports_MatchPythonGoDefaults()
    {
        Assert.Equal(2102, TerminalDefaults.TelnetPort);
        Assert.Equal(8780, TerminalDefaults.ServerPort);
        Assert.Equal(8765, TerminalDefaults.ProxyPort);
        Assert.Equal("127.0.0.1", TerminalDefaults.ServerHost);
        Assert.Equal("/ws/terminal", TerminalDefaults.ProxyWsPath);
    }

    [Fact]
    public void TokenFile_EndsWithSessionToken()
    {
        var path = TerminalDefaults.TokenFile();
        Assert.EndsWith(Path.Combine(".uterm", "session_token"), path);
    }
}
