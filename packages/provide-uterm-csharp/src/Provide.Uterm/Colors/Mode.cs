//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;

namespace Provide.Uterm.Colors;

public enum ColorMode
{
    Passthrough,
    Mode256,
    Mode16,
}

public static class ColorModes
{
    public const string Passthrough = "passthrough";
    public const string Mode256 = "256";
    public const string Mode16 = "16";

    public static ColorMode Parse(string mode) => mode switch
    {
        Passthrough => ColorMode.Passthrough,
        Mode256 => ColorMode.Mode256,
        _ => ColorMode.Mode16,
    };

    public static string ApplyColorMode(string data, ColorMode mode) => mode switch
    {
        ColorMode.Passthrough => data,
        ColorMode.Mode256 => Downgrade.DowngradeTo256(data),
        _ => Downgrade.DowngradeTo16(data),
    };

    public static string ApplyColorMode(string data, string mode) => ApplyColorMode(data, Parse(mode));

    public static byte[] ApplyColorModeBytes(byte[] data, ColorMode mode)
    {
        if (mode == ColorMode.Passthrough)
        {
            return data;
        }

        var target = mode == ColorMode.Mode256 ? ColorMode.Mode256 : ColorMode.Mode16;
        // Latin-1 one-to-one: run SGR regex over string form of bytes.
        var latin1 = Encoding.GetEncoding("ISO-8859-1");
        var text = latin1.GetString(data);
        var rewritten = Sgr.SgrRegexp.Replace(text, m => Sgr.RewriteParams(m.Groups[1].Value, target));
        return latin1.GetBytes(rewritten);
    }

    public static byte[] ApplyColorModeBytes(byte[] data, string mode) =>
        ApplyColorModeBytes(data, Parse(mode));
}
