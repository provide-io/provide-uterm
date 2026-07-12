//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Vt;

public partial class Screen
{
    public void Draw(string data)
    {
        var cs = _charset == 1 ? _g1Charset : _g0Charset;
        foreach (var runeVal in data.EnumerateRunes())
        {
            var charCode = runeVal.Value;
            if (charCode >= 0 && charCode < 256)
            {
                charCode = cs[charCode];
            }

            var charWidth = Wcwidth.RuneWidth(charCode);

            if (_cursor.X == _columns)
            {
                if (_mode.ContainsKey(ModeCodes.Decawm))
                {
                    CarriageReturn();
                    LineFeed();
                }
                else if (charWidth > 0)
                {
                    _cursor.X -= charWidth;
                }
            }

            if (_mode.ContainsKey(ModeCodes.Irm) && charWidth > 0)
            {
                InsertCharacters(charWidth);
            }

            var line = Line(_cursor.Y);
            if (charWidth == 1)
            {
                line[_cursor.X] = VtHelpers.WithData(_cursor.Attrs, char.ConvertFromUtf32(charCode));
            }
            else if (charWidth == 2)
            {
                line[_cursor.X] = VtHelpers.WithData(_cursor.Attrs, char.ConvertFromUtf32(charCode));
                if (_cursor.X + 1 < _columns)
                {
                    line[_cursor.X + 1] = VtHelpers.WithData(_cursor.Attrs, "");
                }
            }
            else if (charWidth == 0 && Wcwidth.CombiningClass(charCode) > 0)
            {
                if (_cursor.X > 0)
                {
                    var last = VtHelpers.CellAt(line, _cursor.X - 1, DefaultChar());
                    var normalized = Normalize.NfcNormalize(last.Data + char.ConvertFromUtf32(charCode));
                    line[_cursor.X - 1] = VtHelpers.WithData(last, normalized);
                }
                else if (_cursor.Y > 0)
                {
                    var prev = Line(_cursor.Y - 1);
                    var last = VtHelpers.CellAt(prev, _columns - 1, DefaultChar());
                    var normalized = Normalize.NfcNormalize(last.Data + char.ConvertFromUtf32(charCode));
                    prev[_columns - 1] = VtHelpers.WithData(last, normalized);
                }
            }
            else
            {
                return;
            }

            if (charWidth > 0)
            {
                _cursor.X = Math.Min(_cursor.X + charWidth, _columns);
            }
        }
    }

    public void Index()
    {
        var (top, bottom) = MarginsOrScreen();
        if (_cursor.Y == bottom)
        {
            for (var y = top; y < bottom; y++)
            {
                if (_buffer.TryGetValue(y + 1, out var src))
                {
                    _buffer[y] = src;
                }
                else
                {
                    _buffer[y] = new Dictionary<int, Char>();
                }
            }

            _buffer.Remove(bottom);
        }
        else
        {
            CursorDown(0);
        }
    }

    public void ReverseIndex()
    {
        var (top, bottom) = MarginsOrScreen();
        if (_cursor.Y == top)
        {
            for (var y = bottom; y > top; y--)
            {
                if (_buffer.TryGetValue(y - 1, out var src))
                {
                    _buffer[y] = src;
                }
                else
                {
                    _buffer[y] = new Dictionary<int, Char>();
                }
            }

            _buffer.Remove(top);
        }
        else
        {
            CursorUp(0);
        }
    }

    public void LineFeed()
    {
        Index();
        if (_mode.ContainsKey(ModeCodes.Lnm))
        {
            CarriageReturn();
        }
    }

    public void InsertLines(int count)
    {
        if (count == 0)
        {
            count = 1;
        }

        var (top, bottom) = MarginsOrScreen();
        if (top <= _cursor.Y && _cursor.Y <= bottom)
        {
            for (var y = bottom; y >= _cursor.Y; y--)
            {
                if (y + count <= bottom)
                {
                    if (_buffer.TryGetValue(y, out var src))
                    {
                        _buffer[y + count] = src;
                    }
                }

                _buffer.Remove(y);
            }

            CarriageReturn();
        }
    }

    public void DeleteLines(int count)
    {
        if (count == 0)
        {
            count = 1;
        }

        var (top, bottom) = MarginsOrScreen();
        if (top <= _cursor.Y && _cursor.Y <= bottom)
        {
            for (var y = _cursor.Y; y <= bottom; y++)
            {
                if (y + count <= bottom)
                {
                    if (_buffer.TryGetValue(y + count, out var src))
                    {
                        _buffer.Remove(y + count);
                        _buffer[y] = src;
                    }
                }
                else
                {
                    _buffer.Remove(y);
                }
            }

            CarriageReturn();
        }
    }

    public void InsertCharacters(int count)
    {
        if (count == 0)
        {
            count = 1;
        }

        var line = Line(_cursor.Y);
        var def = DefaultChar();
        for (var x = _columns; x >= _cursor.X; x--)
        {
            if (x + count <= _columns)
            {
                line[x + count] = VtHelpers.CellAt(line, x, def);
            }

            line.Remove(x);
        }
    }

    public void DeleteCharacters(int count)
    {
        if (count == 0)
        {
            count = 1;
        }

        var line = Line(_cursor.Y);
        for (var x = _cursor.X; x < _columns; x++)
        {
            if (x + count <= _columns)
            {
                if (line.TryGetValue(x + count, out var src))
                {
                    line.Remove(x + count);
                    line[x] = src;
                }
                else
                {
                    line[x] = DefaultChar();
                }
            }
            else
            {
                line.Remove(x);
            }
        }
    }

    public void EraseCharacters(int count)
    {
        if (count == 0)
        {
            count = 1;
        }

        var line = Line(_cursor.Y);
        for (var x = _cursor.X; x < Math.Min(_cursor.X + count, _columns); x++)
        {
            line[x] = _cursor.Attrs;
        }
    }

    public void EraseInLine(int how, bool privateMode = false)
    {
        _ = privateMode;
        int start, end;
        switch (how)
        {
            case 0:
                start = _cursor.X;
                end = _columns;
                break;
            case 1:
                start = 0;
                end = _cursor.X + 1;
                break;
            case 2:
                start = 0;
                end = _columns;
                break;
            default:
                return;
        }

        var line = Line(_cursor.Y);
        for (var x = start; x < end; x++)
        {
            line[x] = _cursor.Attrs;
        }
    }

    public void EraseInDisplay(int how)
    {
        int start, end;
        switch (how)
        {
            case 0:
                start = _cursor.Y + 1;
                end = _lines;
                break;
            case 1:
                start = 0;
                end = _cursor.Y;
                break;
            case 2:
            case 3:
                start = 0;
                end = _lines;
                break;
            default:
                return;
        }

        for (var y = start; y < end; y++)
        {
            var line = Line(y);
            foreach (var x in line.Keys.ToList())
            {
                line[x] = _cursor.Attrs;
            }
        }

        if (how is 0 or 1)
        {
            EraseInLine(how, false);
        }
    }
}
