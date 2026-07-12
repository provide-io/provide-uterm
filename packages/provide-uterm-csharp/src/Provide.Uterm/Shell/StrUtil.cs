//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Shell;

/// <summary>Python-compatible string helpers. Port of Go shell/strutil.go.</summary>
internal static class StrUtil
{
    public static bool IsPyWs(char b) => b is ' ' or '\t' or '\n' or '\r' or '\f' or '\v';

    public static string[]? PySplit1(string s)
    {
        var start = 0;
        while (start < s.Length && IsPyWs(s[start]))
        {
            start++;
        }

        if (start == s.Length)
        {
            return null;
        }

        var j = start;
        while (j < s.Length && !IsPyWs(s[j]))
        {
            j++;
        }

        var head = s[start..j];
        var k = j;
        while (k < s.Length && IsPyWs(s[k]))
        {
            k++;
        }

        if (k == s.Length)
        {
            return new[] { head };
        }

        return new[] { head, s[k..] };
    }

    public static string PyStrip(string s)
    {
        var start = 0;
        var end = s.Length;
        while (start < end && s[start] < 0x80 && IsPyWs(s[start]))
        {
            start++;
        }

        while (end > start && s[end - 1] < 0x80 && IsPyWs(s[end - 1]))
        {
            end--;
        }

        return s[start..end];
    }

    public static string[] PyFields(string s) =>
        s.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);

    public static bool Truthy(object? v) => v switch
    {
        null => false,
        bool b => b,
        string s => s.Length > 0,
        int i => i != 0,
        long l => l != 0,
        double d => d != 0,
        float f => f != 0,
        _ => true,
    };

    public static string StrOrDefault(IReadOnlyDictionary<string, object?> m, string key, string def) =>
        m.TryGetValue(key, out var v) && v is not null ? Convert.ToString(v) ?? def : def;
}
