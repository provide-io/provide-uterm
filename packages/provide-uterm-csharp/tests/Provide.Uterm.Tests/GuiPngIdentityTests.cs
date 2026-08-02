//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using System.Security.Cryptography;
using System.Text.Json;
using Provide.Uterm.Gui;
using Xunit;

namespace Provide.Uterm.Tests;

/// <summary>
/// This port's PNG encoder against the Python reference's corpus, byte for byte.
/// </summary>
/// <remarks>
/// A screenshot is a wire format, so "both are valid PNGs of the same picture"
/// is not the bar -- the bar is that every port emits the same bytes. Holding
/// that requires all three encoders to agree on the deflate stream, which they
/// only do because each compresses at level 9 with the run-length strategy:
/// zlib's default match-finding differs between builds (node ships one on Linux
/// that encodes a 1x1 white pixel in 13 bytes where CPython's takes 11), and
/// Z_FIXED does not settle it either.
///
/// This port used CompressionLevel.Fastest, a different level again, and
/// nothing caught it because no C# test read this corpus. That is what this
/// file is for.
/// </remarks>
public sealed class GuiPngIdentityTests
{
    [Fact]
    public void EncoderReproducesTheReferenceCorpusByteForByte()
    {
        using var document = JsonDocument.Parse(File.ReadAllBytes(GoldenPath()));
        var compared = 0;

        foreach (var record in document.RootElement.GetProperty("pngs").EnumerateArray())
        {
            if (record.TryGetProperty("error", out var error) && error.ValueKind is not JsonValueKind.Null)
            {
                continue; // rejected inputs are covered by the encoder's own tests
            }

            var width = record.GetProperty("width").GetInt32();
            var height = record.GetProperty("height").GetInt32();
            var expected = Convert.FromBase64String(record.GetProperty("value").GetProperty("png").GetString()!);

            var actual = Png.EncodeRgba(width, height, PixelsOf(expected, width, height));

            Assert.Equal(record.GetProperty("value").GetProperty("length").GetInt32(), actual.Length);
            Assert.Equal(
                record.GetProperty("value").GetProperty("sha256").GetString(),
                Convert.ToHexString(SHA256.HashData(actual)).ToLowerInvariant());
            compared++;
        }

        Assert.True(compared > 0, "no corpus PNGs were compared");
    }

    /// <summary>The pixels a corpus PNG holds, so the encoder is fed what produced it.</summary>
    /// <remarks>Only what this encoder writes is read back: one IDAT, filter 0 on every row.</remarks>
    private static byte[] PixelsOf(byte[] png, int width, int height)
    {
        var raw = Array.Empty<byte>();
        for (var offset = 8; offset < png.Length;)
        {
            var length = System.Buffers.Binary.BinaryPrimitives.ReadInt32BigEndian(png.AsSpan(offset, 4));
            if (System.Text.Encoding.ASCII.GetString(png, offset + 4, 4) == "IDAT")
            {
                using var source = new MemoryStream(png, offset + 8, length);
                using var inflate = new System.IO.Compression.ZLibStream(
                    source, System.IO.Compression.CompressionMode.Decompress);
                using var into = new MemoryStream();
                inflate.CopyTo(into);
                raw = into.ToArray();
                break;
            }

            offset += 12 + length;
        }

        var rowLength = width * 4;
        var pixels = new byte[rowLength * height];
        for (var y = 0; y < height; y++)
        {
            Array.Copy(raw, (y * (rowLength + 1)) + 1, pixels, y * rowLength, rowLength);
        }

        return pixels;
    }

    private static string GoldenPath()
    {
        var parts = new[] { "packages", "provide-uterm-ts", "testdata", "guisession_golden.json" };
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
        {
            var candidate = Path.Combine(new[] { dir.FullName }.Concat(parts).ToArray());
            if (File.Exists(candidate))
            {
                return candidate;
            }
        }

        throw new FileNotFoundException("guisession_golden.json not found above " + AppContext.BaseDirectory);
    }
}
