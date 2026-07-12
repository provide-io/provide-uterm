//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Vt;

public partial class Screen
{
    private void EnsureHBounds() =>
        _cursor.X = Math.Min(Math.Max(0, _cursor.X), _columns - 1);

    private void EnsureVBounds(bool useMargins)
    {
        var top = 0;
        var bottom = _lines - 1;
        if ((useMargins || _mode.ContainsKey(ModeCodes.Decom)) && _margins is { } m)
        {
            top = m.Top;
            bottom = m.Bottom;
        }

        _cursor.Y = Math.Min(Math.Max(top, _cursor.Y), bottom);
    }

    public void CursorUp(int count)
    {
        if (count == 0)
        {
            count = 1;
        }

        var (top, _) = MarginsOrScreen();
        _cursor.Y = Math.Max(_cursor.Y - count, top);
    }

    public void CursorUp1(int count)
    {
        CursorUp(count);
        CarriageReturn();
    }

    public void CursorDown(int count)
    {
        if (count == 0)
        {
            count = 1;
        }

        var (_, bottom) = MarginsOrScreen();
        _cursor.Y = Math.Min(_cursor.Y + count, bottom);
    }

    public void CursorDown1(int count)
    {
        CursorDown(count);
        CarriageReturn();
    }

    public void CursorBack(int count)
    {
        if (_cursor.X == _columns)
        {
            _cursor.X--;
        }

        if (count == 0)
        {
            count = 1;
        }

        _cursor.X -= count;
        EnsureHBounds();
    }

    public void CursorForward(int count)
    {
        if (count == 0)
        {
            count = 1;
        }

        _cursor.X += count;
        EnsureHBounds();
    }

    public void CursorPosition(int line, int column)
    {
        if (column == 0)
        {
            column = 1;
        }

        column--;
        if (line == 0)
        {
            line = 1;
        }

        line--;

        if (_margins is { } m && _mode.ContainsKey(ModeCodes.Decom))
        {
            line += m.Top;
            if (line < m.Top || line > m.Bottom)
            {
                return;
            }
        }

        _cursor.X = column;
        _cursor.Y = line;
        EnsureHBounds();
        EnsureVBounds(false);
    }

    public void CursorToColumn(int column)
    {
        if (column == 0)
        {
            column = 1;
        }

        _cursor.X = column - 1;
        EnsureHBounds();
    }

    public void CursorToLine(int line)
    {
        if (line == 0)
        {
            line = 1;
        }

        _cursor.Y = line - 1;
        if (_mode.ContainsKey(ModeCodes.Decom) && _margins is { } m)
        {
            _cursor.Y += m.Top;
        }

        EnsureVBounds(false);
    }

    public void CarriageReturn() => _cursor.X = 0;
    public void Backspace() => CursorBack(0);

    public void Tab()
    {
        var column = _columns - 1;
        foreach (var stop in TabStops())
        {
            if (_cursor.X < stop)
            {
                column = stop;
                break;
            }
        }

        _cursor.X = column;
    }

    public void SetTabStop() => _tabstops[_cursor.X] = true;

    public void ClearTabStop(int how)
    {
        switch (how)
        {
            case 0:
                _tabstops.Remove(_cursor.X);
                break;
            case 3:
                _tabstops = new Dictionary<int, bool>();
                break;
        }
    }

    public void SaveCursor()
    {
        _savepoints.Add(new Savepoint
        {
            Cursor = _cursor,
            G0 = _g0Charset,
            G1 = _g1Charset,
            Charset = _charset,
            Origin = _mode.ContainsKey(ModeCodes.Decom),
            Wrap = _mode.ContainsKey(ModeCodes.Decawm),
        });
    }

    public void RestoreCursor()
    {
        if (_savepoints.Count > 0)
        {
            var sp = _savepoints[^1];
            _savepoints.RemoveAt(_savepoints.Count - 1);
            _g0Charset = sp.G0;
            _g1Charset = sp.G1;
            _charset = sp.Charset;
            if (sp.Origin)
            {
                SetMode(false, ModeCodes.Decom);
            }

            if (sp.Wrap)
            {
                SetMode(false, ModeCodes.Decawm);
            }

            _cursor = sp.Cursor;
            EnsureHBounds();
            EnsureVBounds(true);
            return;
        }

        ResetMode(false, ModeCodes.Decom);
        CursorPosition(0, 0);
    }
}
