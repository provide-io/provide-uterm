//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Vt;

internal static class Charsets
{
    internal static readonly Dictionary<string, int[]> Maps = new()
    {
        ["B"] = CharsetTables.Lat1Map,
        ["0"] = CharsetTables.Vt100Map,
        ["U"] = CharsetTables.IbmpcMap,
        ["V"] = CharsetTables.Vax42Map,
    };

    private static readonly Dictionary<int[], string> Codes =
        Maps.ToDictionary(pair => pair.Value, pair => pair.Key);

    /// <summary>
    /// The designator a translation table was selected by. Every table a screen
    /// holds comes from <see cref="Maps"/>, so it always has one.
    /// </summary>
    internal static string CodeOf(int[] table) => Codes[table];
}
