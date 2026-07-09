//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package render

import (
	"bytes"
	"image"
	"image/color"
	"image/gif"
	"image/png"
	"strings"
	"testing"
)

func TestRenderFrameHalfBlocks(t *testing.T) {
	// 2×2: top row red, bottom row blue.
	pixels := func(x, y int) RGBA8 {
		if y == 0 {
			return RGBA8{R: 255, A: 255}
		}
		return RGBA8{B: 255, A: 255}
	}
	got := RenderFrame(pixels, 2, 2, SGRTruecolor)
	if !strings.HasPrefix(got, "\x1b[H") {
		t.Fatalf("missing home: %q", got)
	}
	// fg = bottom (blue), bg = top (red); style emitted once, block twice.
	want := "\x1b[38;2;0;0;255;48;2;255;0;0m▄▄"
	if !strings.Contains(got, want) {
		t.Fatalf("got %q want fragment %q", got, want)
	}
	if !strings.HasSuffix(got, "\x1b[0m\r\n") {
		t.Fatalf("missing row terminator: %q", got)
	}
}

func TestRenderFrameOddHeightAndTransparency(t *testing.T) {
	// Height 1: bottom pixel is implicit transparent black.
	pixels := func(x, y int) RGBA8 { return RGBA8{R: 200, G: 100, B: 50, A: 255} }
	got := RenderFrame(pixels, 1, 1, SGRTruecolor)
	if !strings.Contains(got, "\x1b[38;2;0;0;0;48;2;200;100;50m▄") {
		t.Fatalf("got %q", got)
	}
	// Alpha < 128 forces black.
	trans := func(x, y int) RGBA8 { return RGBA8{R: 255, G: 255, B: 255, A: 10} }
	got = RenderFrame(trans, 1, 2, SGRTruecolor)
	if !strings.Contains(got, "\x1b[38;2;0;0;0;48;2;0;0;0m▄") {
		t.Fatalf("got %q", got)
	}
}

func makePNG(t *testing.T, w, h int, c color.Color) []byte {
	t.Helper()
	img := image.NewRGBA(image.Rect(0, 0, w, h))
	for y := range h {
		for x := range w {
			img.Set(x, y, c)
		}
	}
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func TestImageToANSIFramesStaticPNG(t *testing.T) {
	data := makePNG(t, 8, 8, color.RGBA{R: 255, A: 255})
	frames, fps, err := ImageToANSIFrames(data, 4, 2, ModeTruecolor)
	if err != nil {
		t.Fatal(err)
	}
	if len(frames) != 1 || fps != 0 {
		t.Fatalf("frames=%d fps=%v", len(frames), fps)
	}
	if !strings.Contains(frames[0], "▄") || !strings.Contains(frames[0], "255;0;0") {
		t.Fatalf("frame = %q", frames[0])
	}
	// Row count: rows terminal rows.
	if got := strings.Count(frames[0], "\r\n"); got != 2 {
		t.Fatalf("row count = %d", got)
	}
	// Defaults apply for cols/rows <= 0.
	frames, _, err = ImageToANSIFrames(data, 0, 0, Mode16)
	if err != nil || len(frames) != 1 {
		t.Fatalf("frames=%v err=%v", len(frames), err)
	}
}

func TestImageToANSIFramesAnimatedGIF(t *testing.T) {
	frame1 := image.NewPaletted(image.Rect(0, 0, 4, 4), []color.Color{color.RGBA{R: 255, A: 255}})
	frame2 := image.NewPaletted(image.Rect(0, 0, 4, 4), []color.Color{color.RGBA{B: 255, A: 255}})
	var buf bytes.Buffer
	err := gif.EncodeAll(&buf, &gif.GIF{
		Image: []*image.Paletted{frame1, frame2},
		Delay: []int{10, 10}, // 100ms per frame → 10 fps
	})
	if err != nil {
		t.Fatal(err)
	}
	frames, fps, err := ImageToANSIFrames(buf.Bytes(), 4, 2, Mode256)
	if err != nil {
		t.Fatal(err)
	}
	if len(frames) != 2 {
		t.Fatalf("frames = %d", len(frames))
	}
	if fps != 10.0 {
		t.Fatalf("fps = %v", fps)
	}
}

func TestImageToANSIFramesGIFZeroDelay(t *testing.T) {
	frame1 := image.NewPaletted(image.Rect(0, 0, 2, 2), []color.Color{color.Black})
	frame2 := image.NewPaletted(image.Rect(0, 0, 2, 2), []color.Color{color.White})
	var buf bytes.Buffer
	if err := gif.EncodeAll(&buf, &gif.GIF{Image: []*image.Paletted{frame1, frame2}, Delay: []int{0, 0}}); err != nil {
		t.Fatal(err)
	}
	_, fps, err := ImageToANSIFrames(buf.Bytes(), 2, 1, Mode16)
	if err != nil || fps != 0 {
		t.Fatalf("fps=%v err=%v", fps, err)
	}
}

func TestImageToANSIFramesErrors(t *testing.T) {
	if _, _, err := ImageToANSIFrames([]byte("not an image"), 4, 2, ModeTruecolor); err == nil {
		t.Fatal("expected decode error")
	}
	data := makePNG(t, 2, 2, color.White)
	if _, _, err := ImageToANSIFrames(data, 4, 2, ColorMode("bogus")); err == nil {
		t.Fatal("expected mode error")
	}
}

func TestResizeBilinearGradient(t *testing.T) {
	// A 2×1 black/white image scaled to 4×1 must interpolate monotonically.
	img := image.NewRGBA(image.Rect(0, 0, 2, 1))
	img.Set(0, 0, color.RGBA{A: 255})
	img.Set(1, 0, color.RGBA{R: 255, G: 255, B: 255, A: 255})
	out := resizeBilinear(img, 4, 1)
	for i := 1; i < 4; i++ {
		if out[i].R < out[i-1].R {
			t.Fatalf("not monotone: %v", out)
		}
	}
	if out[0].R > 64 || out[3].R < 192 {
		t.Fatalf("endpoints off: %v", out)
	}
}
