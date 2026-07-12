//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text.RegularExpressions;

namespace Provide.Uterm.Ansi;

internal static partial class Dialects
{
    private static readonly Dictionary<string, int> PreviewColorMap = new()
    {
        ["k"] = 30, ["r"] = 31, ["g"] = 32, ["y"] = 33,
        ["b"] = 34, ["m"] = 35, ["c"] = 36, ["w"] = 37,
    };

    private static readonly Dictionary<string, (string Pol, string Col)> TildeMap = new()
    {
        ["1"] = ("+", "g"), ["2"] = ("+", "w"), ["3"] = ("+", "c"), ["4"] = ("+", "r"),
        ["5"] = ("+", "m"), ["6"] = ("+", "y"), ["7"] = ("-", "w"), ["0"] = ("-", "x"),
        ["r"] = ("+", "r"), ["R"] = ("+", "r"), ["g"] = ("+", "g"), ["G"] = ("+", "g"),
        ["y"] = ("+", "y"), ["Y"] = ("+", "y"), ["b"] = ("+", "b"), ["B"] = ("+", "b"),
        ["m"] = ("+", "m"), ["M"] = ("+", "m"), ["c"] = ("+", "c"), ["C"] = ("+", "c"),
        ["w"] = ("+", "w"), ["W"] = ("+", "w"), ["d"] = ("-", "w"), ["D"] = ("-", "w"),
        ["E"] = ("+", "r"),
    };

    private static readonly Dictionary<string, string> BraceTokenMap = new()
    {
        ["{+c}"] = "\x1b[1;36m", ["{-c}"] = "\x1b[0;36m",
        ["{+r}"] = "\x1b[1;31m", ["{-r}"] = "\x1b[0;31m",
        ["{+g}"] = "\x1b[1;32m", ["{-g}"] = "\x1b[0;32m",
        ["{+y}"] = "\x1b[1;33m", ["{-y}"] = "\x1b[0;33m",
        ["{+b}"] = "\x1b[1;34m", ["{-b}"] = "\x1b[0;34m",
        ["{+m}"] = "\x1b[1;35m", ["{-m}"] = "\x1b[0;35m",
        ["{+w}"] = "\x1b[1;37m", ["{+Bw}"] = "\x1b[1;37m",
        ["{-w}"] = "\x1b[0;37m", ["{+k}"] = "\x1b[1;30m",
        ["{-k}"] = "\x1b[0;30m", ["{-x}"] = "\x1b[0m",
        ["{NK}"] = "\x1b[0m", ["{T}"] = "\x1b[1m", ["{t}"] = "\x1b[0m",
    };

    private static string EmitColor(string polarity, string colorChar)
    {
        if (colorChar == "x")
        {
            return "\x1b[0m";
        }

        if (!PreviewColorMap.TryGetValue(colorChar, out var code))
        {
            return "";
        }

        return polarity == "+" ? $"\x1b[0;1;{code}m" : $"\x1b[0;{code}m";
    }

    [GeneratedRegex(@"\{([FBPT])(\d{1,3})\}")]
    private static partial Regex ExtTokenRe();

    private static int ExtPCode(int i) => i >= 8 ? 90 + (i - 8) : 30 + i;
    private static int ExtTCode(int i) => i >= 8 ? 100 + (i - 8) : 40 + i;

    internal static string HandleExtendedTokens(string text) =>
        ExtTokenRe().Replace(text, m =>
        {
            var kind = m.Groups[1].Value[0];
            var val = int.Parse(m.Groups[2].Value);
            return kind switch
            {
                'F' => $"\x1b[38;5;{val}m",
                'B' => $"\x1b[48;5;{val}m",
                'P' => $"\x1b[{ExtPCode(val % 16)}m",
                _ => $"\x1b[{ExtTCode(val % 16)}m",
            };
        });

    [GeneratedRegex(@"~(.)")]
    private static partial Regex TildeRe();

    private static readonly Dictionary<string, string> TildeLookup = BuildTildeLookup();

    private static Dictionary<string, string> BuildTildeLookup()
    {
        var m = new Dictionary<string, string>();
        foreach (var (code, pc) in TildeMap)
        {
            var seq = EmitColor(pc.Pol, pc.Col);
            if (seq.Length != 0)
            {
                m[code] = seq;
            }
        }

        return m;
    }

    internal static string HandleTildeCodes(string text) =>
        TildeRe().Replace(text, m =>
            TildeLookup.TryGetValue(m.Groups[1].Value, out var seq) ? seq : m.Value);

    [GeneratedRegex(@"\{[+\-][a-zA-Z]\}|\{NK\}|\{T\}|\{t\}")]
    private static partial Regex Brace3Re();

    [GeneratedRegex(@"\{[+\-]Bw\}")]
    private static partial Regex Brace4Re();

    private static string ReplaceBraceToken(Match m) =>
        BraceTokenMap.TryGetValue(m.Value, out var seq) ? seq : m.Value;

    internal static string HandleBraceTokens(string text)
    {
        text = Brace4Re().Replace(text, ReplaceBraceToken);
        return Brace3Re().Replace(text, ReplaceBraceToken);
    }

    [GeneratedRegex(@"\|(\d{2})")]
    private static partial Regex PipeRe();

    private static readonly int[] DosToAnsiFg = [30, 34, 32, 36, 31, 35, 33, 37];
    private static readonly int[] DosToAnsiBg = [40, 44, 42, 46, 41, 45, 43, 47];
    private static readonly Dictionary<string, string> PipeLookup = BuildPipeLookup();

    private static Dictionary<string, string> BuildPipeLookup()
    {
        var m = new Dictionary<string, string>();
        for (var i = 0; i < 24; i++)
        {
            var key = i.ToString("D2");
            m[key] = i switch
            {
                <= 7 => $"\x1b[{DosToAnsiFg[i]}m",
                <= 15 => $"\x1b[{DosToAnsiFg[i - 8] + 60}m",
                _ => $"\x1b[{DosToAnsiBg[i - 16]}m",
            };
        }

        return m;
    }

    internal static string HandlePipeCodes(string text) =>
        PipeRe().Replace(text, m =>
            PipeLookup.TryGetValue(m.Groups[1].Value, out var seq) ? seq : m.Value);
}
