//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Vt;

internal static class Wcwidth
{
    internal static int LookupRange(WidthRange[] table, int r)
    {
        // binary search for first range with Hi >= r
        var lo = 0;
        var hi = table.Length - 1;
        while (lo <= hi)
        {
            var mid = (lo + hi) >> 1;
            if (table[mid].Hi < r)
            {
                lo = mid + 1;
            }
            else if (table[mid].Lo > r)
            {
                hi = mid - 1;
            }
            else
            {
                return table[mid].Val;
            }
        }

        // sort.Search style: find first hi >= r
        lo = 0;
        hi = table.Length;
        while (lo < hi)
        {
            var mid = (lo + hi) >> 1;
            if (table[mid].Hi >= r)
            {
                hi = mid;
            }
            else
            {
                lo = mid + 1;
            }
        }

        if (lo < table.Length && table[lo].Lo <= r)
        {
            return table[lo].Val;
        }

        return int.MinValue; // not found sentinel used only internally via Try
    }

    internal static bool TryLookupRange(WidthRange[] table, int r, out int val)
    {
        var lo = 0;
        var hi = table.Length;
        while (lo < hi)
        {
            var mid = (lo + hi) >> 1;
            if (table[mid].Hi >= r)
            {
                hi = mid;
            }
            else
            {
                lo = mid + 1;
            }
        }

        if (lo < table.Length && table[lo].Lo <= r)
        {
            val = table[lo].Val;
            return true;
        }

        val = 0;
        return false;
    }

    internal static int RuneWidth(int r) =>
        TryLookupRange(UnicodeWidthTables.WcwidthRanges, r, out var v) ? v : 1;

    internal static int CombiningClass(int r) =>
        TryLookupRange(UnicodeWidthTables.CombiningRanges, r, out var v) ? v : 0;
}
