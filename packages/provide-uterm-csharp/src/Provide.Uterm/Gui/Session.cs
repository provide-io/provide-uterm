//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

namespace Provide.Uterm.Gui;

/// <summary>RGBA pixel framebuffer (portable, no System.Drawing dependency).</summary>
public sealed class RgbaImage
{
    public int Width { get; }
    public int Height { get; }
    public byte[] Pixels { get; }

    public RgbaImage(int width, int height, byte[]? pixels = null)
    {
        Width = width;
        Height = height;
        Pixels = pixels ?? new byte[width * height * 4];
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
