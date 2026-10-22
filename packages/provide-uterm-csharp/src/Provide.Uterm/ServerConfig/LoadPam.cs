//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Tomlyn.Model;

namespace Provide.Uterm.ServerConfig;

/// <summary>
/// Checking <c>[pam]</c> the way the reference does
/// (<c>config_schema.PamConfig</c>), for the one key it refuses values of.
///
/// This port has no PAM bridge and no capture connector, so nothing reads the
/// section and it carries no model. One server.toml is meant to be readable by
/// any port, so a <c>capture_socket_mode</c> the reference would not bind a
/// capture socket with is still refused here, in its words.
/// </summary>
internal static class PamLoader
{
    /// <summary>
    /// Owner-only, and owner and group: the two modes a capture socket may
    /// have. Written from their octal digits, since C# has no octal literal.
    /// </summary>
    private static readonly long[] CaptureSocketModes = [Convert.ToInt64("600", 8), Convert.ToInt64("660", 8)];

    public static void Apply(TomlTable t)
    {
        if (!t.TryGetValue("capture_socket_mode", out var value))
        {
            return;
        }

        // A TOML integer is a long. Anything else (a string, a float, a bool)
        // is refused rather than read as a number, as the reference's
        // before-validator refuses it ahead of Pydantic's own int parsing.
        if (value is not long mode)
        {
            throw new ArgumentException("pam.capture_socket_mode must be an integer: 0o600 or 0o660");
        }

        if (!CaptureSocketModes.Contains(mode))
        {
            throw new ArgumentException("pam.capture_socket_mode must be 0o600 or 0o660, got: " + Octal(mode));
        }
    }

    /// <summary>
    /// <paramref name="value"/> as Python's <c>oct()</c> writes it: the sign
    /// ahead of the <c>0o</c> prefix. <c>Convert.ToString(long, 8)</c> would
    /// write a negative value in two's complement instead.
    /// </summary>
    private static string Octal(long value)
    {
        // The magnitude as unsigned, so long.MinValue has one too.
        var magnitude = value < 0 ? (ulong)(-(value + 1)) + 1 : (ulong)value;
        var digits = new System.Text.StringBuilder();
        do
        {
            digits.Insert(0, (char)('0' + (int)(magnitude % 8)));
            magnitude /= 8;
        }
        while (magnitude > 0);

        return (value < 0 ? "-0o" : "0o") + digits;
    }
}
