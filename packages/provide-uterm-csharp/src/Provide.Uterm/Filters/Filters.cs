//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Filters;

/// <summary>
/// Character-level input filters for BBS/telnet terminal sessions.
/// Port of provide.uterm.filters / packages/provide-uterm-go/filters.
/// </summary>
public static class InputFilters
{
    // Telnet IAC constants (RFC 854).
    public const byte Iac = 255;
    public const byte Will = 251;
    public const byte Wont = 252;
    public const byte Do = 253;
    public const byte Dont = 254;
    public const byte Sb = 250;
    public const byte Se = 240;
    public const byte Esc = 0x1B;

    /// <summary>
    /// Consume and discard a telnet IAC command sequence. Call after the IAC byte has been read.
    /// </summary>
    public static void ConsumeIac(Stream stream)
    {
        var cmd = ReadByte(stream, out var eof);
        if (eof)
        {
            return;
        }

        switch (cmd)
        {
            case Will or Wont or Do or Dont:
                _ = ReadByte(stream, out _);
                return;
            case Sb:
                while (true)
                {
                    var sb = ReadByte(stream, out eof);
                    if (eof)
                    {
                        return;
                    }

                    if (sb == Iac)
                    {
                        var se = ReadByte(stream, out eof);
                        if (eof || se == Se)
                        {
                            return;
                        }
                    }
                }
        }
    }

    /// <summary>
    /// Consume and discard an ANSI escape sequence. Call after the ESC byte has been read.
    /// </summary>
    public static void ConsumeEscape(Stream stream)
    {
        var b = ReadByte(stream, out var eof);
        if (eof)
        {
            return;
        }

        switch (b)
        {
            case 0x5B: // '[' CSI
                while (true)
                {
                    var c = ReadByte(stream, out eof);
                    if (eof)
                    {
                        return;
                    }

                    if (c is >= 0x40 and <= 0x7E)
                    {
                        return;
                    }
                }
            case 0x4F: // 'O' SS3
                _ = ReadByte(stream, out _);
                return;
        }
    }

    // Overloads for BinaryReader / stream-like byte sources used by tests.
    public static void ConsumeIac(BinaryReader reader) => ConsumeIac(reader.BaseStream);

    public static void ConsumeEscape(BinaryReader reader) => ConsumeEscape(reader.BaseStream);

    private static byte ReadByte(Stream stream, out bool eof)
    {
        var v = stream.ReadByte();
        if (v < 0)
        {
            eof = true;
            return 0;
        }

        eof = false;
        return (byte)v;
    }
}
