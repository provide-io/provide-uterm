//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Vt;

/// <summary>Single styled on-screen character (pyte Char).</summary>
public struct Char : IEquatable<Char>
{
    public string Data { get; set; }
    public string FG { get; set; }
    public string BG { get; set; }
    public bool Bold { get; set; }
    public bool Dim { get; set; }
    public bool Italics { get; set; }
    public bool Underscore { get; set; }
    public bool Strikethrough { get; set; }
    public bool Reverse { get; set; }
    public bool Blink { get; set; }

    public static Char DefaultPlain => new()
    {
        Data = " ",
        FG = "default",
        BG = "default",
    };

    public readonly bool Equals(Char other) =>
        Data == other.Data && FG == other.FG && BG == other.BG &&
        Bold == other.Bold && Dim == other.Dim && Italics == other.Italics &&
        Underscore == other.Underscore && Strikethrough == other.Strikethrough &&
        Reverse == other.Reverse && Blink == other.Blink;

    public override readonly bool Equals(object? obj) => obj is Char c && Equals(c);
    public override readonly int GetHashCode() => HashCode.Combine(Data, FG, BG, Bold, Underscore, Reverse, Blink);
    public static bool operator ==(Char a, Char b) => a.Equals(b);
    public static bool operator !=(Char a, Char b) => !a.Equals(b);
}

public struct Cursor
{
    public int X { get; set; }
    public int Y { get; set; }
    public Char Attrs { get; set; }
    public bool Hidden { get; set; }
}

public struct Margins
{
    public int Top { get; set; }
    public int Bottom { get; set; }
}

internal readonly struct WidthRange(int lo, int hi, int val)
{
    public readonly int Lo = lo;
    public readonly int Hi = hi;
    public readonly int Val = val;
}

internal sealed class Savepoint
{
    public Cursor Cursor;
    public int[] G0 = null!;
    public int[] G1 = null!;
    public int Charset;
    public bool Origin;
    public bool Wrap;
}

internal static class VtHelpers
{
    public static Char WithData(Char c, string data)
    {
        c.Data = data;
        return c;
    }

    public static Char CellAt(Dictionary<int, Char> line, int x, Char def) =>
        line.TryGetValue(x, out var c) ? c : def;
}
