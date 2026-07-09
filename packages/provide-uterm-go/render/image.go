//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package render

import (
	"bytes"
	"fmt"
	"image"
	"image/gif"
	"strings"

	// Register the stdlib decoders (PNG/JPEG/GIF; animated GIF handled
	// explicitly below).
	_ "image/jpeg"
	_ "image/png"
)

// RGBA8 is one 8-bit RGBA pixel.
type RGBA8 struct {
	R, G, B, A uint8
}

// PixelAt reads a pixel from a frame buffer.
type PixelAt func(x, y int) RGBA8

// RenderFrame renders a single ANSI frame from pixel data using lower-half
// blocks (U+2584) with fg = bottom pixel and bg = top pixel, packing two
// pixel rows per terminal row. Port of render.image.render_frame.
func RenderFrame(pixels PixelAt, pxW, pxH int, sgrFn SGRFunc) string {
	var out strings.Builder
	out.WriteString("\x1b[H")

	for y := 0; y < pxH; y += 2 {
		prevSGR := ""
		for x := range pxW {
			top := pixels(x, y)
			bottom := RGBA8{}
			if y+1 < pxH {
				bottom = pixels(x, y+1)
			}
			if top.A < 128 {
				top = RGBA8{A: top.A}
			}
			if bottom.A < 128 {
				bottom = RGBA8{A: bottom.A}
			}
			fg := RGB{int(bottom.R), int(bottom.G), int(bottom.B)}
			bg := RGB{int(top.R), int(top.G), int(top.B)}
			sgr := sgrFn(fg, bg)
			if sgr != prevSGR {
				out.WriteString(sgr)
				prevSGR = sgr
			}
			out.WriteString("▄")
		}
		out.WriteString("\x1b[0m\r\n")
	}
	return out.String()
}

// resizeBilinear scales src to w×h with bilinear sampling. The Python
// implementation uses PIL's Lanczos filter — a deliberate deviation: this is
// presentation-only output with no wire-compatibility requirement, and
// bilinear keeps the package stdlib-only.
func resizeBilinear(src image.Image, w, h int) []RGBA8 {
	bounds := src.Bounds()
	sw, sh := bounds.Dx(), bounds.Dy()
	out := make([]RGBA8, w*h)
	for y := range h {
		fy := (float64(y) + 0.5) * float64(sh) / float64(h)
		sy := min(max(int(fy-0.5), 0), sh-1)
		sy2 := min(sy+1, sh-1)
		wy := fy - 0.5 - float64(sy)
		if wy < 0 {
			wy = 0
		}
		for x := range w {
			fx := (float64(x) + 0.5) * float64(sw) / float64(w)
			sx := min(max(int(fx-0.5), 0), sw-1)
			sx2 := min(sx+1, sw-1)
			wx := fx - 0.5 - float64(sx)
			if wx < 0 {
				wx = 0
			}
			p00 := rgbaAt(src, bounds.Min.X+sx, bounds.Min.Y+sy)
			p10 := rgbaAt(src, bounds.Min.X+sx2, bounds.Min.Y+sy)
			p01 := rgbaAt(src, bounds.Min.X+sx, bounds.Min.Y+sy2)
			p11 := rgbaAt(src, bounds.Min.X+sx2, bounds.Min.Y+sy2)
			out[y*w+x] = RGBA8{
				R: lerp2(p00.R, p10.R, p01.R, p11.R, wx, wy),
				G: lerp2(p00.G, p10.G, p01.G, p11.G, wx, wy),
				B: lerp2(p00.B, p10.B, p01.B, p11.B, wx, wy),
				A: lerp2(p00.A, p10.A, p01.A, p11.A, wx, wy),
			}
		}
	}
	return out
}

func rgbaAt(img image.Image, x, y int) RGBA8 {
	r, g, b, a := img.At(x, y).RGBA()
	return RGBA8{uint8(r >> 8), uint8(g >> 8), uint8(b >> 8), uint8(a >> 8)}
}

func lerp2(v00, v10, v01, v11 uint8, wx, wy float64) uint8 {
	top := float64(v00)*(1-wx) + float64(v10)*wx
	bottom := float64(v01)*(1-wx) + float64(v11)*wx
	return uint8(top*(1-wy) + bottom*wy + 0.5)
}

// ImageToANSIFrames converts raw image bytes to ANSI terminal frames plus
// the source FPS (0 for static images). PNG, JPEG, and GIF are supported
// (animated GIFs yield one frame per image frame); each terminal row packs
// two pixel rows. Port of render.image.image_to_ansi_frames — formats beyond
// the stdlib decoders (e.g. WebP) are not supported.
func ImageToANSIFrames(data []byte, cols, rows int, mode ColorMode) ([]string, float64, error) {
	if cols <= 0 {
		cols = 80
	}
	if rows <= 0 {
		rows = 24
	}
	sgrFn, ok := SGRFunctions[mode]
	if !ok {
		return nil, 0, fmt.Errorf("unknown color mode %q", mode)
	}
	pxW, pxH := cols, rows*2

	// Animated GIF: decode every frame with its delay.
	if g, err := gif.DecodeAll(bytes.NewReader(data)); err == nil && len(g.Image) > 1 {
		fps := 0.0
		if len(g.Delay) > 0 && g.Delay[0] > 0 {
			fps = 100.0 / float64(g.Delay[0])
		}
		frames := make([]string, 0, len(g.Image))
		for _, frame := range g.Image {
			buf := resizeBilinear(frame, pxW, pxH)
			frames = append(frames, RenderFrame(func(x, y int) RGBA8 { return buf[y*pxW+x] }, pxW, pxH, sgrFn))
		}
		return frames, fps, nil
	}

	img, _, err := image.Decode(bytes.NewReader(data))
	if err != nil {
		return nil, 0, err
	}
	buf := resizeBilinear(img, pxW, pxH)
	frame := RenderFrame(func(x, y int) RGBA8 { return buf[y*pxW+x] }, pxW, pxH, sgrFn)
	return []string{frame}, 0, nil
}
