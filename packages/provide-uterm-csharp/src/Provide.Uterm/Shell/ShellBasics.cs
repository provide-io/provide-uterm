//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;

namespace Provide.Uterm.Shell;

/// <summary>
/// Ushell keystroke line buffer. Port of packages/provide-uterm-go/shell/linebuffer.go.
/// Callers Feed keystrokes then drain <see cref="TakeEcho"/> and <see cref="TakeCompleted"/>.
/// </summary>
public sealed class LineBuffer
{
    private readonly List<char> _buf = new();
    private readonly StringBuilder _echo = new();
    private readonly List<string> _completed = new();
    public int MaxLength { get; set; } = 4096;

    /// <summary>Current uncommitted line (no echo side effects).</summary>
    public string Text => new(_buf.ToArray());

    /// <summary>Alias for <see cref="Text"/> (Go CurrentLine).</summary>
    public string CurrentLine() => Text;

    /// <summary>
    /// Process a keystroke chunk. Does not return a line — use
    /// <see cref="TakeCompleted"/> / <see cref="TakeEcho"/>.
    /// Legacy callers may use <see cref="FeedLegacy"/> which returns the first
    /// completed line from this chunk (or null).
    /// </summary>
    public void Feed(string chunk)
    {
        var i = 0;
        while (i < chunk.Length)
        {
            var ch = chunk[i];
            if (ch is '\r' or '\n')
            {
                if (ch == '\r' && i + 1 < chunk.Length && chunk[i + 1] == '\n')
                {
                    i++;
                }

                _echo.Append("\r\n");
                _completed.Add(new string(_buf.ToArray()));
                _buf.Clear();
                i++;
                continue;
            }

            if (ch is '\x7f' or '\x08')
            {
                if (_buf.Count > 0)
                {
                    _buf.RemoveAt(_buf.Count - 1);
                    _echo.Append("\x08 \x08");
                }

                i++;
                continue;
            }

            if (ch == '\x03')
            {
                _buf.Clear();
                _echo.Append("^C\r\n");
                _completed.Add("\x03");
                i++;
                continue;
            }

            if (ch == '\x04')
            {
                _echo.Append("\r\n");
                if (_buf.Count > 0)
                {
                    _completed.Add(new string(_buf.ToArray()));
                }
                else
                {
                    _completed.Add("\x04");
                }

                _buf.Clear();
                i++;
                continue;
            }

            if (ch == '\x1b')
            {
                i = ConsumeEscape(chunk, i);
                continue;
            }

            if (ch >= ' ' || ch == '\t')
            {
                if (_buf.Count < MaxLength)
                {
                    _buf.Add(ch);
                    _echo.Append(ch);
                }
            }

            i++;
        }
    }

    /// <summary>
    /// Backward-compatible Feed: process chunk and return the first completed line
    /// if any were produced, else null. Echo is discarded for simple callers.
    /// </summary>
    public string? FeedLegacy(string chunk)
    {
        Feed(chunk);
        _ = TakeEcho();
        var lines = TakeCompleted();
        return lines.Count > 0 ? lines[0] : null;
    }

    public string TakeEcho()
    {
        var s = _echo.ToString();
        _echo.Clear();
        return s;
    }

    public IReadOnlyList<string> TakeCompleted()
    {
        var lines = _completed.ToList();
        _completed.Clear();
        return lines;
    }

    public void Clear()
    {
        _buf.Clear();
        _echo.Clear();
    }

    private static int ConsumeEscape(string s, int i)
    {
        var j = i + 1;
        if (j < s.Length && s[j] == '[')
        {
            j++;
            while (j < s.Length && s[j] < 0x40)
            {
                j++;
            }

            if (j < s.Length && s[j] is >= (char)0x40 and <= (char)0x7e)
            {
                j++;
            }

            return j;
        }

        if (j < s.Length && s[j] == 'O')
        {
            j++;
            if (j < s.Length)
            {
                j++;
            }

            return j;
        }

        return j;
    }
}
