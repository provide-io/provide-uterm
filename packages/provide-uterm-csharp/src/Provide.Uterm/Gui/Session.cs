//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Gui;

/// <summary>RGBA pixel framebuffer (portable, no System.Drawing dependency).</summary>
public sealed class RgbaImage
{
    /// <summary>Hard cap on a single dimension (hostile ServerInit protection).</summary>
    public const int MaxDimension = 8192;

    public int Width { get; }
    public int Height { get; }
    public byte[] Pixels { get; }

    public RgbaImage(int width, int height, byte[]? pixels = null)
    {
        if (width <= 0 || height <= 0 || width > MaxDimension || height > MaxDimension)
        {
            throw new ArgumentOutOfRangeException(
                nameof(width),
                $"invalid framebuffer dimensions: {width}x{height} (max {MaxDimension})");
        }

        var expected = checked(width * height * 4);
        Width = width;
        Height = height;
        if (pixels is null)
        {
            Pixels = new byte[expected];
        }
        else
        {
            if (pixels.Length != expected)
            {
                throw new ArgumentException(
                    $"pixel buffer length {pixels.Length} does not match {width}x{height} RGBA ({expected})");
            }

            Pixels = pixels;
        }
    }

    public RgbaImage Clone() => new(Width, Height, (byte[])Pixels.Clone());
}

/// <summary>
/// Graphical console session contract.
/// Port of packages/provide-uterm-go/gui.
/// </summary>
public interface IGraphicalSession
{
    RgbaImage Screenshot();
    void InjectPointer(int x, int y, byte buttonMask);
    void InjectKey(uint keySym, bool down);
}

/// <summary>In-memory graphical session stub for tests and offline tooling.</summary>
public sealed class MemoryGraphicalSession : IGraphicalSession
{
    private readonly RgbaImage _fb;

    public MemoryGraphicalSession(int width = 640, int height = 480) =>
        _fb = new RgbaImage(width, height);

    public RgbaImage Screenshot() => _fb.Clone();

    public void InjectPointer(int x, int y, byte buttonMask)
    {
        if ((buttonMask & 1) == 0 || x < 0 || y < 0 || x >= _fb.Width || y >= _fb.Height)
        {
            return;
        }

        var idx = ((y * _fb.Width) + x) * 4;
        _fb.Pixels[idx] = 255;
        _fb.Pixels[idx + 1] = 255;
        _fb.Pixels[idx + 2] = 255;
        _fb.Pixels[idx + 3] = 255;
    }

    public void InjectKey(uint keySym, bool down)
    {
        _ = keySym;
        _ = down;
    }
}
