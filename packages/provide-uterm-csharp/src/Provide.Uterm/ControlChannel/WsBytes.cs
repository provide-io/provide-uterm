//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Text;

namespace Provide.Uterm.ControlChannel;

/// <summary>
/// Lossless byte ↔ string shim for the inline DLE/STX control-frame stream.
/// Latin-1 maps bytes 0x00-0xFF to U+0000-U+00FF one-to-one.
/// </summary>
public static class WsBytes
{
    private static readonly Encoding Latin1 = Encoding.GetEncoding("ISO-8859-1");

    /// <summary>
    /// Coerce a binary WebSocket frame into the string form the Decoder expects.
    /// </summary>
    public static string WsBytesToChannelStr(ReadOnlySpan<byte> raw) =>
        Latin1.GetString(raw);

    /// <summary>
    /// Recover raw terminal bytes from a DataChunk.Data string.
    /// Codepoints above U+00FF are replaced with '?'.
    /// </summary>
    public static byte[] ChannelStrToBytes(string data)
    {
        var outBytes = new byte[data.Length];
        var i = 0;
        foreach (var r in data)
        {
            outBytes[i++] = r > 0xFF ? (byte)'?' : (byte)r;
        }

        // data.Length is char count; rune-aware would differ for surrogates,
        // but terminal data is latin-1 so char==byte.
        if (i != outBytes.Length)
        {
            Array.Resize(ref outBytes, i);
        }

        return outBytes;
    }
}
