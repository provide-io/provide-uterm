//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;
using Provide.Uterm.Vt;

namespace Provide.Uterm.Render;

/// <summary>
/// The terminal state a screen snapshot has to carry besides its cells. Port of
/// <c>provide.uterm.render.terminal_state</c>, emitting the same bytes.
/// </summary>
/// <remarks>
/// A viewer that joins a live session is painted from a snapshot and then fed
/// the rest of the stream, and that stream does not start over for it: each
/// byte assumes the cursor, pen and modes the bytes before it left. A program
/// that paints with relative movement (vitetris never sends CUP) drew its whole
/// board in the wrong place on a viewer whose snapshot left the cursor at the
/// bottom-right of the painted rows, and in the default background on one that
/// never saw its last SGR.
/// </remarks>
public static class TerminalState
{
    /// <summary>Application cursor keys, shifted as the screen stores private modes.</summary>
    private const int Decckm = 1 << 5;

    /// <summary>The alternate-screen modes, in the order re-entered.</summary>
    private static readonly int[] AltScreenModes = [47, 1047, 1049];

    /// <summary>Modes that change what a controlling viewer's keyboard and mouse send; restored only when set.</summary>
    private static readonly int[] InputModes = [1000, 1002, 1003, 1005, 1006, 1015, 2004];

    /// <summary>
    /// The SGR sequence that sets a fresh terminal's pen to <paramref name="attrs"/>:
    /// from a reset, every flag, then both colours. Reverse stays reverse.
    /// </summary>
    public static string PenSgr(Vt.Char attrs)
    {
        var codes = new List<int> { 0 };
        foreach (var (on, code) in new[]
                 {
                     (attrs.Bold, 1), (attrs.Italics, 3), (attrs.Underscore, 4),
                     (attrs.Blink, 5), (attrs.Reverse, 7), (attrs.Strikethrough, 9),
                 })
        {
            if (on)
            {
                codes.Add(code);
            }
        }

        var style = RenderBuffer.CellStyle(attrs);
        codes.AddRange(RenderBuffer.ColorSgr(style.FG, true));
        codes.AddRange(RenderBuffer.ColorSgr(style.BG, false));
        return RenderBuffer.SgrSequence(codes);
    }

    /// <summary>
    /// What must precede the painted rows: the alternate screen, if active. It
    /// cannot follow them, because entering it clears.
    /// </summary>
    public static string ScreenStatePrefix(Vt.Screen screen)
    {
        var entered = new StringBuilder();
        foreach (var mode in AltScreenModes.Where(mode => screen.HasMode(mode << 5)))
        {
            entered.Append($"\x1b[?{mode}h");
        }

        return entered.Length == 0 ? "" : entered.Append("\x1b[H").ToString();
    }

    /// <summary>
    /// Move to the cursor, reproducing the deferred wrap at the last column: a
    /// cursor that has just drawn there rests at X == columns, which no CUP can
    /// address, so the last cell is drawn again in its own style from the column
    /// before. A wide character there is redrawn whole.
    /// </summary>
    private static string CursorPlacement(Vt.Screen screen, int row)
    {
        var cursor = screen.Cursor;
        var columns = screen.Columns;
        if (cursor.X < columns)
        {
            return $"\x1b[{row};{cursor.X + 1}H";
        }

        var x = columns - 1;
        if (screen.At(cursor.Y, x).Data.Length == 0 && x > 0)
        {
            x--;
        }

        // Never a stub here: a stub only follows a wide character, and one in
        // the last column has just been stepped back over.
        var cell = screen.At(cursor.Y, x);
        return $"\x1b[{row};{x + 1}H{RenderBuffer.StyleToSgr(RenderBuffer.CellStyle(cell))}{cell.Data}";
    }

    /// <summary>
    /// The G0/G1 designations and the shift, unless nothing has touched them.
    /// </summary>
    /// <remarks>
    /// The power-on sets (G0 B, G1 0, shifted in) are sent once the stream has
    /// designated any: a viewer's G1 powers on as ASCII, so a program that
    /// designated G1 as graphics before it joined would otherwise draw letters at
    /// its next SO. Untouched, nothing is sent, because a snapshot is also read
    /// as text and strip_ansi leaves designations in.
    /// </remarks>
    private static string CharsetState(Vt.Screen screen)
    {
        var (g0, g1, shifted) = screen.CharsetState();
        if (g0 == "B" && g1 == "0" && !shifted && !screen.CharsetsDesignated)
        {
            return "";
        }

        return $"\x1b({g0}\x1b){g1}" + (shifted ? "\x0e" : "");
    }

    /// <summary>
    /// What must follow the painted rows to leave a terminal in <paramref name="screen"/>'s state.
    /// </summary>
    /// <param name="screen">The emulated screen.</param>
    /// <param name="charsets">
    /// Whether the stream honours designations and SO/SI at all
    /// (<c>VtStream.UseUtf8</c> off); one that ignores them has no such state.
    /// </param>
    /// <remarks>
    /// The order is load-bearing: DECSTBM and DECOM both home the cursor, so they
    /// come before it is placed; the deferred-wrap redraw needs autowrap on and
    /// insert mode off, so their real values come after it.
    /// </remarks>
    public static string ScreenStateSuffix(Vt.Screen screen, bool charsets)
    {
        var output = new StringBuilder();
        var hasRegion = screen.TryGetMargins(out var margins) &&
                        !(margins.Top == 0 && margins.Bottom == screen.Lines - 1);
        output.Append(hasRegion ? $"\x1b[{margins.Top + 1};{margins.Bottom + 1}r" : "\x1b[r");
        var origin = screen.HasMode(ModeCodes.Decom);
        output.Append(origin ? "\x1b[?6h" : "\x1b[?6l");
        output.Append("\x1b[?7h\x1b[4l");
        var row = screen.Cursor.Y + 1 - (origin && hasRegion ? margins.Top : 0);
        output.Append(CursorPlacement(screen, row));
        if (!screen.HasMode(ModeCodes.Decawm))
        {
            output.Append("\x1b[?7l");
        }

        if (screen.HasMode(ModeCodes.Irm))
        {
            output.Append("\x1b[4h");
        }

        output.Append(screen.HasMode(ModeCodes.Lnm) ? "\x1b[20h" : "\x1b[20l");
        output.Append(screen.HasMode(Decckm) ? "\x1b[?1h" : "\x1b[?1l");
        foreach (var mode in InputModes.Where(mode => screen.HasMode(mode << 5)))
        {
            output.Append($"\x1b[?{mode}h");
        }

        if (charsets)
        {
            output.Append(CharsetState(screen));
        }

        output.Append(PenSgr(screen.Cursor.Attrs));
        output.Append(screen.Cursor.Hidden ? "\x1b[?25l" : "\x1b[?25h");
        return output.ToString();
    }
}
