//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Defaults;

/// <summary>
/// Default host/port constants for provide-uterm transports.
/// Port of provide.uterm.defaults / packages/provide-uterm-go/defaults.
/// </summary>
public static class TerminalDefaults
{
    public const string TelnetHost = "127.0.0.1";
    public const int TelnetPort = 2102;
    public const int SshPort = 2222;
    public const int GatewayTelnetPort = 2112;
    public const int GatewaySshPort = 2222;

    public const string BindAll = "0.0.0.0";
    public const int ProxyPort = 8765;
    public const string ProxyWsPath = "/ws/terminal";
    public const int ProxyPollMs = 50;
    public const string ServerHost = "127.0.0.1";
    public const int ServerPort = 8780;
    public const int TelnetRemotePort = 23;
    public const int SshRemotePort = 22;
    public const int WsPingInterval = 20;
    public const int WsPingTimeout = 20;
    public const int WsCloseTimeout = 10;
    public const int ReconnectMaxRetries = 5;
    public const double ReconnectBaseBackoffS = 0.5;
    public const double ReconnectMaxBackoffS = 30.0;

    /// <summary>Default resume-token file path (~/.uterm/session_token).</summary>
    public static string TokenFile()
    {
        var home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        return Path.Combine(home, ".uterm", "session_token");
    }
}
