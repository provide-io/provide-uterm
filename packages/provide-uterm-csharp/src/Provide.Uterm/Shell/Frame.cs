//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Shell;

/// <summary>Worker-protocol frame builders. Port of Go shell/frame.go.</summary>
public static class ShellFrames
{
    public const int MinProtocolVersion = 1;
    public const int MaxProtocolVersion = 1;
    public const int PreferredProtocolVersion = 1;

    public static double NowTs() => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0;

    public static Dictionary<string, object?> Term(string data) =>
        new()
        {
            ["type"] = "term",
            ["data"] = data,
            ["ts"] = NowTs(),
        };

    public static Dictionary<string, object?> WorkerHello(string inputMode) =>
        new()
        {
            ["type"] = "worker_hello",
            ["input_mode"] = inputMode,
            ["ts"] = NowTs(),
            ["protocol"] = new Dictionary<string, object?>
            {
                ["min"] = MinProtocolVersion,
                ["max"] = MaxProtocolVersion,
                ["preferred"] = PreferredProtocolVersion,
            },
        };
}
