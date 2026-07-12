//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Vt;

internal static class Normalize
{
    private const int HangulSBase = 0xAC00;
    private const int HangulLBase = 0x1100;
    private const int HangulVBase = 0x1161;
    private const int HangulTBase = 0x11A7;
    private const int HangulLCount = 19;
    private const int HangulVCount = 21;
    private const int HangulTCount = 28;
    private const int HangulNCount = HangulVCount * HangulTCount;
    private const int HangulSCount = HangulLCount * HangulNCount;

    private static void CanonicalDecomposeRune(List<int> output, int r)
    {
        if (r >= HangulSBase && r < HangulSBase + HangulSCount)
        {
            var sIndex = r - HangulSBase;
            output.Add(HangulLBase + sIndex / HangulNCount);
            output.Add(HangulVBase + (sIndex % HangulNCount) / HangulTCount);
            var t = sIndex % HangulTCount;
            if (t != 0)
            {
                output.Add(HangulTBase + t);
            }

            return;
        }

        if (UnicodeNormTables.CanonicalDecomp.TryGetValue(r, out var d))
        {
            CanonicalDecomposeRune(output, d.Item1);
            if (d.Item2 >= 0)
            {
                CanonicalDecomposeRune(output, d.Item2);
            }

            return;
        }

        output.Add(r);
    }

    private static void CanonicalOrder(List<int> rs)
    {
        for (var i = 1; i < rs.Count; i++)
        {
            var cc = Wcwidth.CombiningClass(rs[i]);
            if (cc == 0)
            {
                continue;
            }

            var j = i;
            while (j > 0)
            {
                var prev = Wcwidth.CombiningClass(rs[j - 1]);
                if (prev == 0 || prev <= cc)
                {
                    break;
                }

                (rs[j - 1], rs[j]) = (rs[j], rs[j - 1]);
                j--;
            }
        }
    }

    private static bool ComposePair(int a, int b, out int comp)
    {
        if (a >= HangulLBase && a < HangulLBase + HangulLCount &&
            b >= HangulVBase && b < HangulVBase + HangulVCount)
        {
            comp = HangulSBase + ((a - HangulLBase) * HangulVCount + (b - HangulVBase)) * HangulTCount;
            return true;
        }

        if (a >= HangulSBase && a < HangulSBase + HangulSCount && (a - HangulSBase) % HangulTCount == 0 &&
            b > HangulTBase && b < HangulTBase + HangulTCount)
        {
            comp = a + (b - HangulTBase);
            return true;
        }

        return UnicodeNormTables.CompositionPairs.TryGetValue((a, b), out comp!);
    }

    private static List<int> ComposeRunes(List<int> rs)
    {
        if (rs.Count == 0)
        {
            return rs;
        }

        var output = new List<int> { rs[0] };
        var starter = -1;
        var lastCC = Wcwidth.CombiningClass(rs[0]);
        if (lastCC == 0)
        {
            starter = 0;
        }

        for (var idx = 1; idx < rs.Count; idx++)
        {
            var r = rs[idx];
            var cc = Wcwidth.CombiningClass(r);
            if (starter >= 0 && (lastCC < cc || lastCC == 0))
            {
                if (ComposePair(output[starter], r, out var composed))
                {
                    output[starter] = composed;
                    continue;
                }
            }

            output.Add(r);
            if (cc == 0)
            {
                starter = output.Count - 1;
            }

            lastCC = cc;
        }

        return output;
    }

    internal static string NfcNormalize(string s)
    {
        var decomposed = new List<int>(s.Length + 2);
        foreach (var rune in s.EnumerateRunes())
        {
            CanonicalDecomposeRune(decomposed, rune.Value);
        }

        CanonicalOrder(decomposed);
        var composed = ComposeRunes(decomposed);
        return string.Concat(composed.Select(r => char.ConvertFromUtf32(r)));
    }
}
