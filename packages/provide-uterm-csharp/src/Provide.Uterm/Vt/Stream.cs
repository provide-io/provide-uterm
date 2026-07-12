//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;

namespace Provide.Uterm.Vt;

/// <summary>
/// State machine that parses terminal input and dispatches to a Screen (pyte Stream).
/// </summary>
public sealed class VtStream
{
    private const int CtrlNul = 0x00;
    private const int CtrlBel = 0x07;
    private const int CtrlBs = 0x08;
    private const int CtrlHt = 0x09;
    private const int CtrlLf = 0x0a;
    private const int CtrlVt = 0x0b;
    private const int CtrlFf = 0x0c;
    private const int CtrlCr = 0x0d;
    private const int CtrlSo = 0x0e;
    private const int CtrlSi = 0x0f;
    private const int CtrlCan = 0x18;
    private const int CtrlSub = 0x1a;
    private const int CtrlEsc = 0x1b;
    private const int CtrlDel = 0x7f;
    private const int CtrlCsiC1 = 0x9b;
    private const int CtrlStC1 = 0x9c;
    private const int CtrlOscC1 = 0x9d;

    private const int StGround = 0;
    private const int StEscape = 1;
    private const int StSharp = 2;
    private const int StPercent = 3;
    private const int StCharset = 4;
    private const int StCsi = 5;
    private const int StCsiDollar = 6;
    private const int StOscCode = 7;
    private const int StOscParam = 8;

    public bool UseUtf8 { get; set; } = true;

    private readonly Screen _screen;
    private bool _takingPlainText = true;
    private int _state;

    private readonly List<int> _csiParams = new();
    private int _csiCurrent;
    private bool _csiPrivate;

    private int _oscCode;
    private readonly StringBuilder _oscParam = new();
    private bool _oscEsc;
    private int _charsetMode;

    public VtStream(Screen screen) => _screen = screen;

    private static bool IsSpecial(int r) =>
        r == CtrlNul || (r >= CtrlBel && r <= CtrlSi) ||
        r == CtrlEsc || r == CtrlDel || r == CtrlCsiC1 || r == CtrlOscC1;

    public void Feed(string data)
    {
        var runes = data.EnumerateRunes().Select(r => r.Value).ToArray();
        var length = runes.Length;
        var offset = 0;
        while (offset < length)
        {
            if (_takingPlainText)
            {
                var end = offset;
                while (end < length && !IsSpecial(runes[end]))
                {
                    end++;
                }

                if (end > offset)
                {
                    _screen.Draw(string.Concat(runes[offset..end].Select(r => char.ConvertFromUtf32(r))));
                    offset = end;
                }
                else
                {
                    _takingPlainText = false;
                }
            }
            else
            {
                _takingPlainText = Send(runes[offset]);
                offset++;
            }
        }
    }

    private bool Send(int r)
    {
        switch (_state)
        {
            case StGround:
                Ground(r);
                break;
            case StEscape:
                Escape(r);
                break;
            case StSharp:
                if (r == '8')
                {
                    _screen.AlignmentDisplay();
                }

                _state = StGround;
                break;
            case StPercent:
                _state = StGround;
                break;
            case StCharset:
                if (!UseUtf8)
                {
                    _screen.DefineCharset(char.ConvertFromUtf32(r), char.ConvertFromUtf32(_charsetMode));
                }

                _state = StGround;
                break;
            case StCsi:
                Csi(r);
                break;
            case StCsiDollar:
                _state = StGround;
                break;
            case StOscCode:
                OscBegin(r);
                break;
            case StOscParam:
                Osc(r);
                break;
        }

        return _state == StGround;
    }

    private void Ground(int r)
    {
        if (r == CtrlEsc)
        {
            _state = StEscape;
        }
        else if (IsBasic(r))
        {
            if ((r == CtrlSi || r == CtrlSo) && UseUtf8)
            {
                return;
            }

            DispatchBasic(r);
        }
        else if (r == CtrlCsiC1)
        {
            EnterCsi();
        }
        else if (r == CtrlOscC1)
        {
            _state = StOscCode;
        }
        else if (r is CtrlNul or CtrlDel)
        {
            // ignored
        }
        else
        {
            _screen.Draw(char.ConvertFromUtf32(r));
        }
    }

    private void Escape(int r)
    {
        switch (r)
        {
            case '[':
                EnterCsi();
                break;
            case ']':
                _state = StOscCode;
                break;
            case '#':
                _state = StSharp;
                break;
            case '%':
                _state = StPercent;
                break;
            case '(':
            case ')':
                _charsetMode = r;
                _state = StCharset;
                break;
            default:
                DispatchEscape(r);
                _state = StGround;
                break;
        }
    }

    private void EnterCsi()
    {
        _csiParams.Clear();
        _csiCurrent = 0;
        _csiPrivate = false;
        _state = StCsi;
    }

    private void Csi(int r)
    {
        if (r == '?')
        {
            _csiPrivate = true;
        }
        else if (r is CtrlBel or CtrlBs or CtrlHt or CtrlLf or CtrlVt or CtrlFf or CtrlCr)
        {
            DispatchBasic(r);
        }
        else if (r is ' ' or '>')
        {
            // Secondary DA not supported
        }
        else if (r is CtrlCan or CtrlSub)
        {
            _screen.Draw(char.ConvertFromUtf32(r));
            _state = StGround;
        }
        else if (r >= '0' && r <= '9')
        {
            if (_csiCurrent < 100000)
            {
                _csiCurrent = _csiCurrent * 10 + (r - '0');
            }
        }
        else if (r == '$')
        {
            _state = StCsiDollar;
        }
        else
        {
            _csiParams.Add(Math.Min(_csiCurrent, 9999));
            if (r == ';')
            {
                _csiCurrent = 0;
            }
            else
            {
                DispatchCsi(r, _csiParams.ToArray(), _csiPrivate);
                _state = StGround;
            }
        }
    }

    private void OscBegin(int r)
    {
        if (r is 'R' or 'P')
        {
            _state = StGround;
            return;
        }

        _oscCode = r;
        _oscParam.Clear();
        _oscEsc = false;
        _state = StOscParam;
    }

    private void Osc(int r)
    {
        if (_oscEsc)
        {
            _oscEsc = false;
            if (r == '\\')
            {
                OscDispatch();
                return;
            }

            _oscParam.Append((char)CtrlEsc);
            _oscParam.Append(char.ConvertFromUtf32(r));
            return;
        }

        switch (r)
        {
            case CtrlEsc:
                _oscEsc = true;
                break;
            case CtrlStC1:
            case CtrlBel:
                OscDispatch();
                break;
            default:
                _oscParam.Append(char.ConvertFromUtf32(r));
                break;
        }
    }

    private void OscDispatch()
    {
        var param = _oscParam.ToString();
        if (param.Length > 0)
        {
            var first = param.EnumerateRunes().First();
            param = param[first.Utf16SequenceLength..];
        }

        if (_oscCode is '0' or '1')
        {
            _screen.SetIconName(param);
        }

        if (_oscCode is '0' or '2')
        {
            _screen.SetTitle(param);
        }

        _state = StGround;
    }

    private static bool IsBasic(int r) => r >= CtrlBel && r <= CtrlSi;

    private void DispatchBasic(int r)
    {
        switch (r)
        {
            case CtrlBel:
                _screen.Bell();
                break;
            case CtrlBs:
                _screen.Backspace();
                break;
            case CtrlHt:
                _screen.Tab();
                break;
            case CtrlLf:
            case CtrlVt:
            case CtrlFf:
                _screen.LineFeed();
                break;
            case CtrlCr:
                _screen.CarriageReturn();
                break;
            case CtrlSo:
                _screen.ShiftOut();
                break;
            case CtrlSi:
                _screen.ShiftIn();
                break;
        }
    }

    private void DispatchEscape(int r)
    {
        switch (r)
        {
            case 'c':
                _screen.Reset();
                break;
            case 'D':
                _screen.Index();
                break;
            case 'E':
                _screen.LineFeed();
                break;
            case 'H':
                _screen.SetTabStop();
                break;
            case 'M':
                _screen.ReverseIndex();
                break;
            case '7':
                _screen.SaveCursor();
                break;
            case '8':
                _screen.RestoreCursor();
                break;
        }
    }

    private void DispatchCsi(int final, int[] parameters, bool privateMode)
    {
        int P(int i) => i < parameters.Length ? parameters[i] : 0;
        var scr = _screen;
        switch (final)
        {
            case '@':
                scr.InsertCharacters(P(0));
                break;
            case 'A':
                scr.CursorUp(P(0));
                break;
            case 'B':
            case 'e':
                scr.CursorDown(P(0));
                break;
            case 'C':
            case 'a':
                scr.CursorForward(P(0));
                break;
            case 'D':
                scr.CursorBack(P(0));
                break;
            case 'E':
                scr.CursorDown1(P(0));
                break;
            case 'F':
                scr.CursorUp1(P(0));
                break;
            case 'G':
            case '`':
                scr.CursorToColumn(P(0));
                break;
            case 'H':
            case 'f':
                scr.CursorPosition(P(0), P(1));
                break;
            case 'J':
                scr.EraseInDisplay(P(0));
                break;
            case 'K':
                scr.EraseInLine(P(0), privateMode);
                break;
            case 'L':
                scr.InsertLines(P(0));
                break;
            case 'M':
                scr.DeleteLines(P(0));
                break;
            case 'P':
                scr.DeleteCharacters(P(0));
                break;
            case 'X':
                scr.EraseCharacters(P(0));
                break;
            case 'c':
                scr.ReportDeviceAttributes(P(0), privateMode);
                break;
            case 'd':
                scr.CursorToLine(P(0));
                break;
            case 'g':
                scr.ClearTabStop(P(0));
                break;
            case 'h':
                scr.SetMode(privateMode, parameters);
                break;
            case 'l':
                scr.ResetMode(privateMode, parameters);
                break;
            case 'm':
                scr.SelectGraphicRendition(parameters);
                break;
            case 'n':
                scr.ReportDeviceStatus(P(0));
                break;
            case 'r':
                scr.SetMargins(parameters);
                break;
        }
    }
}
