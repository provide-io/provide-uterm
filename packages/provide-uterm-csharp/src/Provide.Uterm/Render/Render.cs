//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;

namespace Provide.Uterm.Render;

/// <summary>
/// Terminal render helpers: SGR segments, palette, simple buffer.
/// Port of packages/provide-uterm-go/render (minimal but real surface).
/// </summary>
public readonly struct Rgb
{
    public byte R { get; init; }
    public byte G { get; init; }
    public byte B { get; init; }

    public Rgb(byte r, byte g, byte b)
    {
        R = r;
        G = g;
        B = b;
    }
}

public sealed class TextSegment
{
    public string Text { get; init; } = "";
    public int? Fg { get; init; }
    public int? Bg { get; init; }
    public bool Bold { get; init; }
    public bool Underline { get; init; }
    public bool Reverse { get; init; }
}

public static class Sgr
{
    public static string Reset => "\x1b[0m";

    public static string Encode(TextSegment seg)
    {
        var parts = new List<string>();
        if (seg.Bold)
        {
            parts.Add("1");
        }

        if (seg.Underline)
        {
            parts.Add("4");
        }

        if (seg.Reverse)
        {
            parts.Add("7");
        }

        if (seg.Fg is int fg)
        {
            parts.Add(fg < 8 ? (30 + fg).ToString() : fg < 16 ? (90 + fg - 8).ToString() : $"38;5;{fg}");
        }

        if (seg.Bg is int bg)
        {
            parts.Add(bg < 8 ? (40 + bg).ToString() : bg < 16 ? (100 + bg - 8).ToString() : $"48;5;{bg}");
        }

        if (parts.Count == 0)
        {
            return seg.Text;
        }

        return "\x1b[" + string.Join(';', parts) + "m" + seg.Text + Reset;
    }

    public static string EncodeMany(IEnumerable<TextSegment> segments)
    {
        var sb = new StringBuilder();
        foreach (var seg in segments)
        {
            sb.Append(Encode(seg));
        }

        return sb.ToString();
    }
}

/// <summary>Simple screen buffer of cells used by renderers.</summary>
public sealed class CellGrid
{
    public int Cols { get; }
    public int Rows { get; }
    private readonly char[,] _cells;

    public CellGrid(int cols, int rows)
    {
        Cols = cols <= 0 ? 80 : cols;
        Rows = rows <= 0 ? 25 : rows;
        _cells = new char[Rows, Cols];
        Clear();
    }

    public void Clear()
    {
        for (var y = 0; y < Rows; y++)
        {
            for (var x = 0; x < Cols; x++)
            {
                _cells[y, x] = ' ';
            }
        }
    }

    public void Put(int x, int y, char ch)
    {
        if (x is < 0 || y is < 0 || x >= Cols || y >= Rows)
        {
            return;
        }

        _cells[y, x] = ch;
    }

    public string ToPlainText()
    {
        var sb = new StringBuilder(Rows * (Cols + 1));
        for (var y = 0; y < Rows; y++)
        {
            for (var x = 0; x < Cols; x++)
            {
                sb.Append(_cells[y, x]);
            }

            if (y < Rows - 1)
            {
                sb.Append('\n');
            }
        }

        return sb.ToString();
    }
}

public static class ImageRender
{
    /// <summary>
    /// Convert a grayscale byte buffer (row-major) into crude ANSI half-block frames.
    /// </summary>
    public static IReadOnlyList<string> ImageToAnsiFrames(byte[] pixels, int width, int height, int maxFrames = 1)
    {
        if (width <= 0 || height <= 0)
        {
            return Array.Empty<string>();
        }

        var sb = new StringBuilder();
        for (var y = 0; y < height; y += 2)
        {
            for (var x = 0; x < width; x++)
            {
                var top = pixels[(y * width) + x];
                var bottom = y + 1 < height ? pixels[((y + 1) * width) + x] : (byte)0;
                var ch = top > 128 && bottom > 128 ? '█' : top > 128 ? '▀' : bottom > 128 ? '▄' : ' ';
                sb.Append(ch);
            }

            sb.Append("\r\n");
        }

        return new[] { sb.ToString() };
    }
}
