//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Vt;

public partial class Screen
{
    public void SelectGraphicRendition(params int[] attrs)
    {
        if (attrs.Length == 0 || (attrs.Length == 1 && attrs[0] == 0))
        {
            _cursor.Attrs = DefaultChar();
            return;
        }

        var work = _cursor.Attrs;
        var i = 0;
        while (i < attrs.Length)
        {
            var attr = attrs[i++];
            if (attr == 0)
            {
                work = DefaultChar();
            }
            else if (Graphics.FgAnsi.TryGetValue(attr, out var fg))
            {
                work.FG = fg;
            }
            else if (Graphics.BgAnsi.TryGetValue(attr, out var bg))
            {
                work.BG = bg;
            }
            else if (ApplyTextAttr(ref work, attr))
            {
                // style
            }
            else if (Graphics.FgAixTerm.TryGetValue(attr, out var afg))
            {
                work.FG = afg;
            }
            else if (Graphics.BgAixTerm.TryGetValue(attr, out var abg))
            {
                work.BG = abg;
            }
            else if (attr is 38 or 48)
            {
                i = ApplyExtendedColor(ref work, attr == 38, attrs, i);
            }
        }

        _cursor.Attrs = work;
    }

    private static bool ApplyTextAttr(ref Char c, int attr)
    {
        switch (attr)
        {
            case 1: c.Bold = true; break;
            case 3: c.Italics = true; break;
            case 4: c.Underscore = true; break;
            case 5: c.Blink = true; break;
            case 7: c.Reverse = true; break;
            case 9: c.Strikethrough = true; break;
            case 22: c.Bold = false; break;
            case 23: c.Italics = false; break;
            case 24: c.Underscore = false; break;
            case 25: c.Blink = false; break;
            case 27: c.Reverse = false; break;
            case 29: c.Strikethrough = false; break;
            default: return false;
        }

        return true;
    }

    private static int ApplyExtendedColor(ref Char c, bool isFg, int[] attrs, int i)
    {
        if (i >= attrs.Length)
        {
            return i;
        }

        var n = attrs[i++];
        switch (n)
        {
            case 5:
                if (i >= attrs.Length)
                {
                    return i;
                }

                var m = attrs[i++];
                if (m < Graphics.FgBg256.Length)
                {
                    SetColor(ref c, isFg, Graphics.FgBg256[m]);
                }

                break;
            case 2:
                if (i + 3 > attrs.Length)
                {
                    return attrs.Length;
                }

                SetColor(ref c, isFg, $"{attrs[i]:x2}{attrs[i + 1]:x2}{attrs[i + 2]:x2}");
                i += 3;
                break;
        }

        return i;
    }

    private static void SetColor(ref Char c, bool isFg, string color)
    {
        if (isFg)
        {
            c.FG = color;
        }
        else
        {
            c.BG = color;
        }
    }
}
