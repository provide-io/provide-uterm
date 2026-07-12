//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Buffers.Binary;
using System.IO.Compression;

namespace Provide.Uterm.Gui;

/// <summary>Minimal RGBA → PNG encoder (no external image package).</summary>
public static class Png
{
    private static readonly byte[] Signature = { 137, 80, 78, 71, 13, 10, 26, 10 };

    /// <summary>Encode raw RGBA8888 pixels as a PNG byte stream.</summary>
    public static byte[] EncodeRgba(int width, int height, ReadOnlySpan<byte> pixels)
    {
        if (width <= 0 || height <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(width), "invalid PNG dimensions");
        }

        var expected = checked(width * height * 4);
        if (pixels.Length < expected)
        {
            throw new ArgumentException($"pixel buffer too short: need {expected}, got {pixels.Length}");
        }

        // Filter type 0 row prefixes + raw RGBA.
        var raw = new byte[checked(height * (1 + width * 4))];
        var offset = 0;
        for (var y = 0; y < height; y++)
        {
            raw[offset++] = 0; // filter None
            var src = y * width * 4;
            pixels.Slice(src, width * 4).CopyTo(raw.AsSpan(offset));
            offset += width * 4;
        }

        var compressed = DeflateZlib(raw);
        using var ms = new MemoryStream(capacity: 8 + 25 + compressed.Length + 12 + 12);
        ms.Write(Signature);
        WriteChunk(ms, "IHDR", BuildIhdr(width, height));
        WriteChunk(ms, "IDAT", compressed);
        WriteChunk(ms, "IEND", ReadOnlySpan<byte>.Empty);
        return ms.ToArray();
    }

    private static byte[] BuildIhdr(int width, int height)
    {
        var buf = new byte[13];
        BinaryPrimitives.WriteInt32BigEndian(buf.AsSpan(0, 4), width);
        BinaryPrimitives.WriteInt32BigEndian(buf.AsSpan(4, 4), height);
        buf[8] = 8;  // bit depth
        buf[9] = 6;  // RGBA
        buf[10] = 0; // compression
        buf[11] = 0; // filter
        buf[12] = 0; // interlace
        return buf;
    }

    private static byte[] DeflateZlib(byte[] raw)
    {
        using var ms = new MemoryStream();
        // zlib header
        ms.WriteByte(0x78);
        ms.WriteByte(0x01);
        using (var ds = new DeflateStream(ms, CompressionLevel.Fastest, leaveOpen: true))
        {
            ds.Write(raw, 0, raw.Length);
        }

        // Adler-32 of uncompressed data
        var adler = Adler32(raw);
        Span<byte> adlerBytes = stackalloc byte[4];
        BinaryPrimitives.WriteUInt32BigEndian(adlerBytes, adler);
        ms.Write(adlerBytes);
        return ms.ToArray();
    }

    private static uint Adler32(ReadOnlySpan<byte> data)
    {
        uint a = 1, b = 0;
        foreach (var x in data)
        {
            a = (a + x) % 65521;
            b = (b + a) % 65521;
        }

        return (b << 16) | a;
    }

    private static void WriteChunk(Stream s, string type, ReadOnlySpan<byte> data)
    {
        Span<byte> len = stackalloc byte[4];
        BinaryPrimitives.WriteInt32BigEndian(len, data.Length);
        s.Write(len);
        var typeBytes = System.Text.Encoding.ASCII.GetBytes(type);
        s.Write(typeBytes);
        s.Write(data);
        // CRC over type+data
        var crcBuf = new byte[typeBytes.Length + data.Length];
        typeBytes.CopyTo(crcBuf, 0);
        data.CopyTo(crcBuf.AsSpan(typeBytes.Length));
        var crc = Crc32(crcBuf);
        Span<byte> crcBytes = stackalloc byte[4];
        BinaryPrimitives.WriteUInt32BigEndian(crcBytes, crc);
        s.Write(crcBytes);
    }

    private static uint Crc32(ReadOnlySpan<byte> data)
    {
        // System.IO.Hashing not always available; use polynomial table.
        uint crc = 0xFFFF_FFFF;
        foreach (var b in data)
        {
            crc ^= b;
            for (var i = 0; i < 8; i++)
            {
                var mask = (uint)-(crc & 1);
                crc = (crc >> 1) ^ (0xEDB88320u & mask);
            }
        }

        return ~crc;
    }
}
