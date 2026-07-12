//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Numerics;
using System.Security.Cryptography;
using System.Text;

namespace Provide.Uterm.DeckMux;

/// <summary>
/// Deterministic display-name / color / initials generators for collaborative presence.
/// Port of packages/provide-uterm-go/deckmux/names.go — tables are load-bearing.
/// </summary>
public static class IdentityNames
{
    private static readonly string[] Adjectives =
    [
        "red", "blue", "green", "amber", "silver", "coral", "jade", "onyx",
        "pearl", "ruby", "gold", "iron", "copper", "bronze", "crystal", "storm",
        "frost", "ember", "dusk", "dawn", "ash", "moss", "slate", "flint",
        "cedar", "birch", "maple", "sage", "thorn", "drift", "spark", "blaze",
    ];

    private static readonly string[] Animals =
    [
        "fox", "hawk", "wolf", "otter", "lynx", "crane", "bear", "deer",
        "eagle", "raven", "heron", "viper", "shark", "whale", "tiger", "panther",
        "falcon", "condor", "bison", "moose", "cobra", "gecko", "puma", "osprey",
        "badger", "ferret", "marten", "jackal", "ibis", "newt", "pike", "wren",
        "tanuki",
    ];

    private static readonly string[] Colors =
    [
        "#e74c3c", "#3498db", "#2ecc71", "#9b59b6", "#e67e22", "#1abc9c",
        "#f39c12", "#e91e63", "#00bcd4", "#8bc34a", "#ff5722", "#607d8b",
    ];

    private static BigInteger HashInt(string value)
    {
        var sum = SHA256.HashData(Encoding.UTF8.GetBytes(value));
        // big-endian unsigned integer of the full digest (Python int(hexdigest, 16))
        var hex = Convert.ToHexString(sum).ToLowerInvariant();
        return BigInteger.Parse("0" + hex, System.Globalization.NumberStyles.HexNumber);
    }

    private static int ModInt(BigInteger h, int m) => (int)(h % m);

    /// <summary>Deterministic two-word display name from a connection id (e.g. "Red Fox").</summary>
    public static string GenerateName(string connectionId)
    {
        var h = HashInt(connectionId);
        var adj = Adjectives[ModInt(h, Adjectives.Length)];
        var shifted = h >> 8;
        var animal = Animals[ModInt(shifted, Animals.Length)];
        return TitleWord(adj) + " " + TitleWord(animal);
    }

    /// <summary>Deterministic color hex, skipping colors already in taken.</summary>
    public static string GenerateColor(string connectionId, ISet<string>? taken = null)
    {
        taken ??= new HashSet<string>();
        var h = HashInt(connectionId);
        var bas = ModInt(h, Colors.Length);
        for (var offset = 0; offset < Colors.Length; offset++)
        {
            var color = Colors[(bas + offset) % Colors.Length];
            if (!taken.Contains(color))
            {
                return color;
            }
        }

        return Colors[bas];
    }

    /// <summary>2-character initials from a display name.</summary>
    public static string GenerateInitials(string name)
    {
        var parts = name.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length >= 2)
        {
            return (FirstRune(parts[0]) + FirstRune(parts[1])).ToUpperInvariant();
        }

        return FirstRunes(name, 2).ToUpperInvariant();
    }

    private static string TitleWord(string s)
    {
        if (s.Length == 0)
        {
            return "";
        }

        return char.ToUpperInvariant(s[0]) + s[1..];
    }

    private static string FirstRune(string s) => s.Length == 0 ? "" : s[0].ToString();

    private static string FirstRunes(string s, int n)
    {
        if (s.Length <= n)
        {
            return s;
        }

        return s[..n];
    }
}
