//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;

namespace Provide.Uterm.Vt;

/// <summary>
/// In-memory matrix of characters representing a terminal display (pyte Screen).
/// Sparse buffer: unset cells render as spaces with default attributes.
/// </summary>
public partial class Screen
{
    public Action<string>? WriteProcessInput { get; set; }

    private int _columns;
    private int _lines;
    private Dictionary<int, Dictionary<int, Char>> _buffer = new();
    private readonly Dictionary<int, bool> _mode = new();
    private Margins? _margins;
    private string _title = "";
    private string _iconName = "";
    private int _charset;
    private int[] _g0Charset = CharsetTables.Lat1Map;
    private int[] _g1Charset = CharsetTables.Vt100Map;
    private Dictionary<int, bool> _tabstops = new();
    private Cursor _cursor;
    private readonly List<Savepoint> _savepoints = new();
    private int _savedColumns = -1;

    public Screen(int columns, int lines)
    {
        _columns = columns;
        _lines = lines;
        Reset();
    }

    public int Columns => _columns;
    public int Lines => _lines;
    public Cursor Cursor => _cursor;
    public string Title => _title;
    public string IconName => _iconName;

    public bool TryGetMargins(out Margins m)
    {
        if (_margins is { } mm)
        {
            m = mm;
            return true;
        }

        m = default;
        return false;
    }

    public IReadOnlyList<int> Modes()
    {
        var outList = _mode.Keys.ToList();
        outList.Sort();
        return outList;
    }

    public IReadOnlyList<int> TabStops()
    {
        var outList = _tabstops.Keys.ToList();
        outList.Sort();
        return outList;
    }

    public Char DefaultCharPublic() => DefaultChar();

    internal Char DefaultChar()
    {
        var c = Char.DefaultPlain;
        c.Reverse = _mode.ContainsKey(ModeCodes.Decscnm);
        return c;
    }

    private Dictionary<int, Char> Line(int y)
    {
        if (!_buffer.TryGetValue(y, out var l))
        {
            l = new Dictionary<int, Char>();
            _buffer[y] = l;
        }

        return l;
    }

    public Char At(int y, int x)
    {
        if (_buffer.TryGetValue(y, out var l) && l.TryGetValue(x, out var c))
        {
            return c;
        }

        return DefaultChar();
    }

    public IReadOnlyList<string> Display()
    {
        var outList = new string[_lines];
        for (var y = 0; y < _lines; y++)
        {
            var line = Line(y);
            var def = DefaultChar();
            var b = new StringBuilder();
            var isWide = false;
            for (var x = 0; x < _columns; x++)
            {
                if (isWide)
                {
                    isWide = false;
                    continue;
                }

                var data = VtHelpers.CellAt(line, x, def).Data;
                if (!string.IsNullOrEmpty(data))
                {
                    var r = char.ConvertToUtf32(data, 0);
                    isWide = Wcwidth.RuneWidth(r) == 2;
                }

                b.Append(data);
            }

            outList[y] = b.ToString();
        }

        return outList;
    }

    public void Reset()
    {
        _buffer = new Dictionary<int, Dictionary<int, Char>>();
        _margins = null;
        _mode.Clear();
        _mode[ModeCodes.Decawm] = true;
        _mode[ModeCodes.Dectcem] = true;
        _title = "";
        _iconName = "";
        _charset = 0;
        _g0Charset = CharsetTables.Lat1Map;
        _g1Charset = CharsetTables.Vt100Map;
        _tabstops = new Dictionary<int, bool>();
        for (var x = 8; x < _columns; x += 8)
        {
            _tabstops[x] = true;
        }

        _cursor = new Cursor { Attrs = Char.DefaultPlain };
        CursorPosition(0, 0);
        _savedColumns = -1;
    }

    public void Resize(int lines, int columns)
    {
        if (lines == 0)
        {
            lines = _lines;
        }

        if (columns == 0)
        {
            columns = _columns;
        }

        if (lines == _lines && columns == _columns)
        {
            return;
        }

        if (lines < _lines)
        {
            SaveCursor();
            CursorPosition(0, 0);
            DeleteLines(_lines - lines);
            RestoreCursor();
        }

        if (columns < _columns)
        {
            foreach (var l in _buffer.Values)
            {
                for (var x = columns; x < _columns; x++)
                {
                    l.Remove(x);
                }
            }
        }

        _lines = lines;
        _columns = columns;
        SetMargins();
    }

    public void SetMargins(params int[] parameters)
    {
        var haveTop = parameters.Length >= 1;
        var haveBottom = parameters.Length >= 2;

        if ((!haveTop || parameters[0] == 0) && !haveBottom)
        {
            _margins = null;
            return;
        }

        var cur = _margins ?? new Margins { Top = 0, Bottom = _lines - 1 };
        var top = cur.Top;
        if (haveTop)
        {
            top = Math.Max(0, Math.Min(parameters[0] - 1, _lines - 1));
        }

        var bottom = cur.Bottom;
        if (haveBottom)
        {
            bottom = Math.Max(0, Math.Min(parameters[1] - 1, _lines - 1));
        }

        if (bottom - top >= 1)
        {
            _margins = new Margins { Top = top, Bottom = bottom };
            CursorPosition(0, 0);
        }
    }

    private (int top, int bottom) MarginsOrScreen()
    {
        if (_margins is { } m)
        {
            return (m.Top, m.Bottom);
        }

        return (0, _lines - 1);
    }

    public void SetMode(bool privateMode, params int[] modes)
    {
        var ml = modes.ToArray();
        if (privateMode)
        {
            for (var i = 0; i < ml.Length; i++)
            {
                ml[i] <<= 5;
            }
        }

        foreach (var m in ml)
        {
            _mode[m] = true;
        }

        if (ml.Contains(ModeCodes.Deccolm))
        {
            _savedColumns = _columns;
            Resize(0, 132);
            EraseInDisplay(2);
            CursorPosition(0, 0);
        }

        if (ml.Contains(ModeCodes.Decom))
        {
            CursorPosition(0, 0);
        }

        if (ml.Contains(ModeCodes.Decscnm))
        {
            foreach (var l in _buffer.Values)
            {
                foreach (var x in l.Keys.ToList())
                {
                    var c = l[x];
                    c.Reverse = true;
                    l[x] = c;
                }
            }

            SelectGraphicRendition(7);
        }

        if (ml.Contains(ModeCodes.Dectcem))
        {
            _cursor.Hidden = false;
        }
    }

    public void ResetMode(bool privateMode, params int[] modes)
    {
        var ml = modes.ToArray();
        if (privateMode)
        {
            for (var i = 0; i < ml.Length; i++)
            {
                ml[i] <<= 5;
            }
        }

        foreach (var m in ml)
        {
            _mode.Remove(m);
        }

        if (ml.Contains(ModeCodes.Deccolm))
        {
            if (_columns == 132 && _savedColumns != -1)
            {
                Resize(0, _savedColumns);
                _savedColumns = -1;
            }

            EraseInDisplay(2);
            CursorPosition(0, 0);
        }

        if (ml.Contains(ModeCodes.Decom))
        {
            CursorPosition(0, 0);
        }

        if (ml.Contains(ModeCodes.Decscnm))
        {
            foreach (var l in _buffer.Values)
            {
                foreach (var x in l.Keys.ToList())
                {
                    var c = l[x];
                    c.Reverse = false;
                    l[x] = c;
                }
            }

            SelectGraphicRendition(27);
        }

        if (ml.Contains(ModeCodes.Dectcem))
        {
            _cursor.Hidden = true;
        }
    }

    public void DefineCharset(string code, string mode)
    {
        if (!Charsets.Maps.TryGetValue(code, out var map))
        {
            return;
        }

        switch (mode)
        {
            case "(":
                _g0Charset = map;
                break;
            case ")":
                _g1Charset = map;
                break;
        }
    }

    public void ShiftIn() => _charset = 0;
    public void ShiftOut() => _charset = 1;
    public void SetTitle(string param) => _title = param;
    public void SetIconName(string param) => _iconName = param;
    public void Bell() { }

    public void AlignmentDisplay()
    {
        for (var y = 0; y < _lines; y++)
        {
            var line = Line(y);
            var def = DefaultChar();
            for (var x = 0; x < _columns; x++)
            {
                line[x] = VtHelpers.WithData(VtHelpers.CellAt(line, x, def), "E");
            }
        }
    }

    public void ReportDeviceAttributes(int mode, bool privateMode)
    {
        if (mode == 0 && !privateMode)
        {
            WriteProcess("\x1b[?6c");
        }
    }

    public void ReportDeviceStatus(int mode)
    {
        switch (mode)
        {
            case 5:
                WriteProcess("\x1b[0n");
                break;
            case 6:
                var x = _cursor.X + 1;
                var y = _cursor.Y + 1;
                if (_mode.ContainsKey(ModeCodes.Decom) && _margins is { } m)
                {
                    y -= m.Top;
                }

                WriteProcess($"\x1b[{y};{x}R");
                break;
        }
    }

    private void WriteProcess(string data) => WriteProcessInput?.Invoke(data);
}
