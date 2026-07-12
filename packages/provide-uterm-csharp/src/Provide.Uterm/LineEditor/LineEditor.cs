//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;

namespace Provide.Uterm.LineEditor;

/// <summary>Terminal-output callback. Errors propagate to the caller of ProcessChar.</summary>
public delegate void WriteFunc(string text);

/// <summary>
/// Generic line editor for terminal input with readline-style shortcuts.
/// Port of provide.uterm.line_editor / packages/provide-uterm-go/lineeditor.
/// </summary>
public sealed class LineEditor
{
    private readonly List<char> _buffer = new();
    private int _cursorPos;

    public int MaxLength { get; }
    public bool PasswordMode { get; }
    public WriteFunc? OnWrite { get; }

    public LineEditor(int maxLength = 80, bool passwordMode = false, WriteFunc? onWrite = null)
    {
        MaxLength = maxLength <= 0 ? 80 : maxLength;
        PasswordMode = passwordMode;
        OnWrite = onWrite;
    }

    private void Emit(string text) => OnWrite?.Invoke(text);

    private string Display(IReadOnlyList<char> s) =>
        PasswordMode ? new string('*', s.Count) : new string(s.ToArray());

    /// <summary>
    /// Process a single input character. When Enter completes a line returns (line, true).
    /// </summary>
    public (string Line, bool Done) ProcessChar(char ch)
    {
        switch (ch)
        {
            case '\r':
            case '\n':
            {
                var result = new string(_buffer.ToArray());
                _buffer.Clear();
                _cursorPos = 0;
                Emit("\r\n");
                return (result, true);
            }
            case '\x7f':
            case '\x08':
                if (_cursorPos > 0)
                {
                    var tail = _buffer.GetRange(_cursorPos, _buffer.Count - _cursorPos);
                    _buffer.RemoveAt(_cursorPos - 1);
                    _cursorPos--;
                    var seq = "\x08" + Display(tail) + " " + $"\x1b[{tail.Count + 1}D";
                    Emit(seq);
                }

                return ("", false);
            case '\x01': // Ctrl+A
                if (_cursorPos > 0)
                {
                    Emit($"\x1b[{_cursorPos}D");
                    _cursorPos = 0;
                }

                return ("", false);
            case '\x05': // Ctrl+E
            {
                var n = _buffer.Count - _cursorPos;
                if (n > 0)
                {
                    _cursorPos = _buffer.Count;
                    Emit($"\x1b[{n}C");
                }

                return ("", false);
            }
            case '\u0002': // Ctrl+B
                if (_cursorPos > 0)
                {
                    _cursorPos--;
                    Emit("\x1b[D");
                }

                return ("", false);
            case '\x06': // Ctrl+F
                if (_cursorPos < _buffer.Count)
                {
                    _cursorPos++;
                    Emit("\x1b[C");
                }

                return ("", false);
            case '\x15': // Ctrl+U
                if (_cursorPos > 0)
                {
                    var remaining = _buffer.GetRange(_cursorPos, _buffer.Count - _cursorPos);
                    _buffer.Clear();
                    _buffer.AddRange(remaining);
                    var seq = $"\x1b[{_cursorPos}D" + Display(remaining) + "\x1b[K";
                    if (remaining.Count > 0)
                    {
                        seq += $"\x1b[{remaining.Count}D";
                    }

                    _cursorPos = 0;
                    Emit(seq);
                }

                return ("", false);
            case '\x0b': // Ctrl+K
                if (_cursorPos < _buffer.Count)
                {
                    _buffer.RemoveRange(_cursorPos, _buffer.Count - _cursorPos);
                    Emit("\x1b[K");
                }

                return ("", false);
            case '\x17': // Ctrl+W
                if (_cursorPos > 0)
                {
                    var pos = _cursorPos;
                    while (pos > 0 && _buffer[pos - 1] == ' ')
                    {
                        pos--;
                    }

                    while (pos > 0 && _buffer[pos - 1] != ' ')
                    {
                        pos--;
                    }

                    var deleted = _cursorPos - pos;
                    var remaining = _buffer.GetRange(_cursorPos, _buffer.Count - _cursorPos);
                    _buffer.RemoveRange(pos, deleted);
                    var seq = $"\x1b[{deleted}D" + Display(remaining) + "\x1b[K";
                    if (remaining.Count > 0)
                    {
                        seq += $"\x1b[{remaining.Count}D";
                    }

                    _cursorPos = pos;
                    Emit(seq);
                }

                return ("", false);
        }

        if (_buffer.Count >= MaxLength)
        {
            Emit("\a");
            return ("", false);
        }

        var tailChars = _buffer.GetRange(_cursorPos, _buffer.Count - _cursorPos);
        _buffer.Insert(_cursorPos, ch);
        _cursorPos++;
        if (tailChars.Count == 0)
        {
            Emit(PasswordMode ? "*" : ch.ToString());
            return ("", false);
        }

        var redraw = new List<char> { ch };
        redraw.AddRange(tailChars);
        Emit(Display(redraw) + $"\x1b[{tailChars.Count}D");
        return ("", false);
    }

    public void Reset()
    {
        _buffer.Clear();
        _cursorPos = 0;
    }

    public string Buffer() => new(_buffer.ToArray());
}
