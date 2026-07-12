//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;

namespace Provide.Uterm.Shell;

/// <summary>
/// Ushell keystroke line buffer. Port of packages/provide-uterm-go/shell/linebuffer.go.
/// </summary>
public sealed class LineBuffer
{
    private readonly StringBuilder _buf = new();
    public int MaxLength { get; set; } = 4096;

    public string Text => _buf.ToString();

    /// <summary>
    /// Feed a keystroke chunk. Returns a completed line when the user submits,
    /// or null while still editing. Ctrl-C returns "\x03"; empty Ctrl-D returns "".
    /// </summary>
    public string? Feed(string chunk)
    {
        var i = 0;
        while (i < chunk.Length)
        {
            var ch = chunk[i];
            if (ch == '\x1b')
            {
                i++;
                if (i < chunk.Length && chunk[i] == '[')
                {
                    i++;
                    while (i < chunk.Length && chunk[i] is < '@' or > '~')
                    {
                        i++;
                    }

                    if (i < chunk.Length)
                    {
                        i++;
                    }

                    continue;
                }

                if (i < chunk.Length && chunk[i] == 'O')
                {
                    i += 2;
                    continue;
                }

                continue;
            }

            if (ch is '\r' or '\n')
            {
                if (ch == '\r' && i + 1 < chunk.Length && chunk[i + 1] == '\n')
                {
                    i++;
                }

                var line = _buf.ToString();
                _buf.Clear();
                return line;
            }

            if (ch is '\x7f' or '\x08')
            {
                if (_buf.Length > 0)
                {
                    _buf.Length--;
                }

                i++;
                continue;
            }

            if (ch == '\x03')
            {
                _buf.Clear();
                return "\x03";
            }

            if (ch == '\x04')
            {
                if (_buf.Length == 0)
                {
                    return "";
                }

                i++;
                continue;
            }

            if (ch == '\t' || !char.IsControl(ch))
            {
                if (_buf.Length < MaxLength)
                {
                    _buf.Append(ch);
                }
            }

            i++;
        }

        return null;
    }

    public void Clear() => _buf.Clear();
}
