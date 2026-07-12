//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;

namespace Provide.Uterm.Shell;

/// <summary>ANSI helpers. Port of packages/provide-uterm-go/shell/output.go (byte-aligned with Python).</summary>
public static class ShellOutput
{
    public const string Reset = "\x1b[0m";
    public const string Bold = "\x1b[1m";
    public const string Dim = "\x1b[2m";
    public const string Green = "\x1b[32m";
    public const string Yellow = "\x1b[33m";
    public const string Red = "\x1b[31m";
    public const string Cyan = "\x1b[36m";
    public const string Blue = "\x1b[34m";
    public const string Magenta = "\x1b[35m";
    public const string ClearScreen = "\x1b[2J\x1b[H";

    public static readonly string Prompt = Green + "❯" + Reset + " ";
    public static readonly string Banner =
        Bold + Cyan + "ushell" + Reset + " " + Dim + "— Python REPL inside your terminal" + Reset + "\r\n" +
        Dim + "Type " + Reset + "help" + Dim + " for available commands." + Reset + "\r\n\r\n";

    public static string ErrorMsg(string text) => Red + "error:" + Reset + " " + text + "\r\n";
    public static string InfoMsg(string text) => Dim + text + Reset + "\r\n";
    public static string SuccessMsg(string text) => Green + text + Reset + "\r\n";
    public static string Heading(string text) => Bold + Cyan + text + Reset + "\r\n";

    public static string FmtKv(string key, string value, int width = 20) =>
        "  " + Dim + PadRight(key, width) + Reset + value + "\r\n";

    public static string FmtKvDefault(string key, string value) => FmtKv(key, value, 20);

    /// <summary>Legacy dictionary dump used by older call sites.</summary>
    public static string FmtKv(IReadOnlyDictionary<string, string> kv)
    {
        var sb = new StringBuilder();
        foreach (var (k, v) in kv)
        {
            sb.Append(FmtKvDefault(k, v));
        }

        return sb.ToString();
    }

    public static string FmtTable(IReadOnlyList<IReadOnlyList<string>> rows, IReadOnlyList<string>? headers)
    {
        if (rows.Count == 0)
        {
            return InfoMsg("(no results)");
        }

        var ncols = 0;
        foreach (var r in rows)
        {
            if (r.Count > ncols)
            {
                ncols = r.Count;
            }
        }

        var widths = new int[ncols];
        foreach (var r in rows)
        {
            for (var i = 0; i < r.Count; i++)
            {
                var w = r[i].Length; // ASCII table cells like Go's rune count for ASCII
                if (w > widths[i])
                {
                    widths[i] = w;
                }
            }
        }

        if (headers is not null)
        {
            for (var i = 0; i < headers.Count && i < ncols; i++)
            {
                var w = headers[i].Length;
                if (w > widths[i])
                {
                    widths[i] = w;
                }
            }
        }

        var lines = new List<string>();
        if (headers is not null)
        {
            var cells = new List<string>();
            for (var i = 0; i < headers.Count; i++)
            {
                var w = i < widths.Length ? widths[i] : 0;
                cells.Add(Bold + PadRight(headers[i], w) + Reset);
            }

            lines.Add("  " + string.Join("  ", cells));
            var dashes = widths.Select(w => new string('-', w)).ToArray();
            lines.Add("  " + string.Join("  ", dashes));
        }

        foreach (var r in rows)
        {
            var cells = new string[r.Count];
            for (var i = 0; i < r.Count; i++)
            {
                var w = i < widths.Length ? widths[i] : 0;
                cells[i] = PadRight(r[i], w);
            }

            lines.Add("  " + string.Join("  ", cells));
        }

        return string.Join("\r\n", lines) + "\r\n";
    }

    public static string PadRight(string s, int w)
    {
        if (s.Length >= w)
        {
            return s;
        }

        return s + new string(' ', w - s.Length);
    }
}
