//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

using Provide.Uterm.Gui;

namespace Provide.Uterm.Vnc;

/// <summary>
/// Framebuffer tracker for VNC/RFB raw updates.
/// Port of packages/provide-uterm-go/vnc/tracker.go.
/// </summary>
public sealed class FramebufferTracker
{
    private readonly object _lock = new();
    private RgbaImage _img;

    public FramebufferTracker(int width, int height) => _img = new RgbaImage(width, height);

    public void ApplyRawUpdate(int x, int y, int w, int h, byte[] pixels)
    {
        if (w < 0 || h < 0)
        {
            throw new ArgumentException($"invalid dimensions: w={w}, h={h}");
        }

        var expectedLen = (long)w * h * 4;
        if (pixels.LongLength < expectedLen)
        {
            throw new ArgumentException($"invalid pixel buffer size: expected {expectedLen}, got {pixels.Length}");
        }

        lock (_lock)
        {
            for (var row = 0; row < h; row++)
            {
                var dy = y + row;
                if (dy < 0 || dy >= _img.Height)
                {
                    continue;
                }

                for (var col = 0; col < w; col++)
                {
                    var dx = x + col;
                    if (dx < 0 || dx >= _img.Width)
                    {
                        continue;
                    }

                    var src = ((row * w) + col) * 4;
                    var dst = ((dy * _img.Width) + dx) * 4;
                    _img.Pixels[dst] = pixels[src];
                    _img.Pixels[dst + 1] = pixels[src + 1];
                    _img.Pixels[dst + 2] = pixels[src + 2];
                    _img.Pixels[dst + 3] = pixels[src + 3];
                }
            }
        }
    }

    public RgbaImage GetImage()
    {
        lock (_lock)
        {
            return _img.Clone();
        }
    }
}
