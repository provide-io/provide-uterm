//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Security.Cryptography;
using System.Text;
using Provide.Uterm.Render;
using Provide.Uterm.Screen;
using Provide.Uterm.Session;
using Provide.Uterm.Vt;

namespace Provide.Uterm.Emulator;

/// <summary>VT/ANSI terminal emulator backed by Vt.Screen (port of provide.uterm.emulator).</summary>
public sealed class TerminalEmulator
{
    private const int RawTailMax = 4096;

    private int _cols;
    private int _rows;
    private readonly string _term;
    private readonly Vt.Screen _screen;
    private readonly VtStream _stream;
    private bool _dirty = true;
    private Snapshot? _last;
    private string _rawTail = "";

    /// <summary>
    /// Guards every read and write of the screen below.
    /// </summary>
    /// <remarks>
    /// A session's reader loop feeds this emulator from a background task while
    /// callers read it — <c>TransportSession.ApplyData</c> calls
    /// <see cref="Process(byte[])"/> and <see cref="GetSnapshot"/>, and anyone
    /// holding <c>TransportSession.Emulator()</c> can call
    /// <see cref="GetSnapshot"/> at the same moment. Without this, the reader
    /// mutates the screen's collections while another thread walks them, which
    /// .NET detects and throws on: "Operations that change non-concurrent
    /// collections must have exclusive access."
    ///
    /// It lives here rather than in TransportSession on purpose. The session
    /// hands the raw emulator out through <c>Emulator()</c>, so a lock held only
    /// by the session would guard its own calls and leave every external caller
    /// racing. Guarding the object itself is what makes it safe regardless of
    /// who reached it.
    ///
    /// <c>lock</c> is reentrant, so an internal call between these methods is
    /// fine. <see cref="Screen"/> still hands out the mutable screen directly
    /// and is NOT protected by this — it is a pre-existing hole in the API,
    /// narrower than the one being closed here.
    /// </remarks>
    private readonly object _gate = new();

    public TerminalEmulator(int cols = 0, int rows = 0, string term = "")
    {
        if (cols <= 0)
        {
            cols = 80;
        }

        if (rows <= 0)
        {
            rows = 25;
        }

        if (string.IsNullOrEmpty(term))
        {
            term = "ANSI";
        }

        _cols = cols;
        _rows = rows;
        _term = term;
        _screen = new Vt.Screen(cols, rows);
        _stream = new VtStream(_screen);
    }

    public void Process(ReadOnlySpan<byte> data)
    {
        var text = Cp437.Decode(data);
        lock (_gate)
        {
            ProcessLocked(text);
        }
    }

    private void ProcessLocked(string text)
    {
        _stream.Feed(text);
        if (text.Length > 0)
        {
            var combined = _rawTail + text;
            // Go strings are UTF-8 bytes; len(combined) is byte count.
            var utf8 = Encoding.UTF8.GetBytes(combined);
            if (utf8.Length > RawTailMax)
            {
                var cut = utf8.Length - RawTailMax;
                while (cut < utf8.Length && (utf8[cut] & 0xC0) == 0x80)
                {
                    cut++;
                }

                combined = Encoding.UTF8.GetString(utf8, cut, utf8.Length - cut);
            }

            _rawTail = combined;
        }

        _dirty = true;
    }

    public void Process(byte[] data) => Process(data.AsSpan());

    public string RawTail => _rawTail;

    private static bool IsSpace(Rune r) =>
        Rune.IsWhiteSpace(r) || (r.Value >= 0x1c && r.Value <= 0x1f);

    private static string TrimRightFunc(string s)
    {
        var runes = s.EnumerateRunes().ToList();
        var end = runes.Count;
        while (end > 0 && IsSpace(runes[end - 1]))
        {
            end--;
        }

        if (end == runes.Count)
        {
            return s;
        }

        return string.Concat(runes.Take(end).Select(r => r.ToString()));
    }

    private static string TrimRightColonSpace(string s) => s.TrimEnd(' ', ':');

    private bool IsCursorAtEnd()
    {
        var cursor = _screen.Cursor;
        var lines = _screen.Display();
        for (var rowIdx = lines.Count - 1; rowIdx >= 0; rowIdx--)
        {
            var line = TrimRightFunc(lines[rowIdx]);
            if (line.Length == 0)
            {
                continue;
            }

            if (cursor.Y == rowIdx)
            {
                return cursor.X >= line.EnumerateRunes().Count() - 2;
            }

            return cursor.Y > rowIdx;
        }

        return true;
    }

    public Snapshot GetSnapshot()
    {
        lock (_gate)
        {
            return GetSnapshotLocked();
        }
    }

    private Snapshot GetSnapshotLocked()
    {
        if (_last is null || _dirty)
        {
            var screenText = string.Join("\n", _screen.Display());
            var digest = SHA256.HashData(Encoding.UTF8.GetBytes(screenText));
            var cursor = _screen.Cursor;
            var snap = new Snapshot
            {
                Screen = screenText,
                ScreenHash = Convert.ToHexString(digest).ToLowerInvariant(),
                Cursor = new Session.Cursor { X = cursor.X, Y = cursor.Y },
                Cols = _cols,
                Rows = _rows,
                Term = _term,
                CursorAtEnd = IsCursorAtEnd(),
                HasTrailingSpace = TrimRightFunc(screenText) != TrimRightColonSpace(screenText),
                RawTail = _rawTail,
            };
            _last = snap;
            _dirty = false;
        }

        return new Snapshot
        {
            Screen = _last.Screen,
            ScreenHash = _last.ScreenHash,
            Cursor = _last.Cursor,
            Cols = _last.Cols,
            Rows = _last.Rows,
            Term = _last.Term,
            CursorAtEnd = _last.CursorAtEnd,
            HasTrailingSpace = _last.HasTrailingSpace,
            RawTail = _last.RawTail,
            CapturedAt = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000.0,
            PromptDetected = _last.PromptDetected,
        };
    }

    public string AnsiScreen()
    {
        lock (_gate)
        {
            return string.Join("\n", RenderBuffer.RenderScreenLines(_screen, _cols, _rows));
        }
    }

    /// <summary>Go/spec-compatible alias (ANSI acronym capitalization).</summary>
    public string ANSIScreen() => AnsiScreen();

    public Vt.Screen Screen => _screen;
    public int Cols => _cols;
    public int Rows => _rows;

    public void Reset()
    {
        lock (_gate)
        {
            _screen.Reset();
            _dirty = true;
        }
    }

    public void Resize(int cols, int rows)
    {
        lock (_gate)
        {
            _cols = cols;
            _rows = rows;
            _screen.Resize(rows, cols);
            _dirty = true;
        }
    }
}
