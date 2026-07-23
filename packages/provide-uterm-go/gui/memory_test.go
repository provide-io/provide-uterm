//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package gui_test

import (
	"image"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/gui"
)

func TestMemoryGraphicalSessionScreenshotDimensions(t *testing.T) {
	s := gui.NewMemoryGraphicalSession(32, 24)
	img, err := s.Screenshot()
	if err != nil {
		t.Fatalf("screenshot: %v", err)
	}
	b := img.Bounds()
	if b.Dx() != 32 || b.Dy() != 24 {
		t.Fatalf("dims = %dx%d want 32x24", b.Dx(), b.Dy())
	}
}

func TestMemoryGraphicalSessionClampsDimensions(t *testing.T) {
	// Zero/negative clamp up to 1; oversize clamps down to the max dimension.
	s := gui.NewMemoryGraphicalSession(0, -5)
	img, _ := s.Screenshot()
	if img.Bounds().Dx() != 1 || img.Bounds().Dy() != 1 {
		t.Fatalf("min clamp = %v", img.Bounds())
	}
	big := gui.NewMemoryGraphicalSession(100000, 100000)
	bimg, _ := big.Screenshot()
	if bimg.Bounds().Dx() != 8192 || bimg.Bounds().Dy() != 8192 {
		t.Fatalf("max clamp = %v", bimg.Bounds())
	}
}

func TestMemoryGraphicalSessionInjectPointerPaintsWhite(t *testing.T) {
	s := gui.NewMemoryGraphicalSession(4, 4)
	// Left button in bounds → white pixel.
	if err := s.InjectPointer(1, 2, 1); err != nil {
		t.Fatalf("inject: %v", err)
	}
	img := mustRGBA(t, s)
	if got := img.RGBAAt(1, 2); got.R != 255 || got.G != 255 || got.B != 255 || got.A != 255 {
		t.Fatalf("pixel not white: %+v", got)
	}
	// A different pixel remains untouched (transparent black).
	if got := img.RGBAAt(0, 0); got.A != 0 {
		t.Fatalf("unexpected paint at origin: %+v", got)
	}
}

func TestMemoryGraphicalSessionInjectPointerNoOpCases(t *testing.T) {
	cases := []struct {
		name       string
		x, y       int
		buttonMask uint8
	}{
		{"no_button", 1, 1, 0},
		{"right_only", 1, 1, 4},
		{"neg_x", -1, 1, 1},
		{"neg_y", 1, -1, 1},
		{"x_oob", 4, 1, 1},
		{"y_oob", 1, 4, 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := gui.NewMemoryGraphicalSession(4, 4)
			if err := s.InjectPointer(tc.x, tc.y, tc.buttonMask); err != nil {
				t.Fatalf("inject: %v", err)
			}
			img := mustRGBA(t, s)
			for _, px := range img.Pix {
				if px != 0 {
					t.Fatalf("framebuffer mutated for %s", tc.name)
				}
			}
		})
	}
}

func TestMemoryGraphicalSessionClose(t *testing.T) {
	s := gui.NewMemoryGraphicalSession(2, 2)
	if err := s.Close(); err != nil {
		t.Fatalf("Close: %v", err)
	}
}

func TestMemoryGraphicalSessionInjectKeyNoOp(t *testing.T) {
	s := gui.NewMemoryGraphicalSession(2, 2)
	if err := s.InjectKey(0xFF0D, true); err != nil {
		t.Fatalf("inject key down: %v", err)
	}
	if err := s.InjectKey(0xFF0D, false); err != nil {
		t.Fatalf("inject key up: %v", err)
	}
	img := mustRGBA(t, s)
	for _, px := range img.Pix {
		if px != 0 {
			t.Fatalf("key injection mutated framebuffer")
		}
	}
}

func TestMemoryGraphicalSessionScreenshotIsDetached(t *testing.T) {
	// A returned screenshot must not alias the live framebuffer: mutating one
	// after painting must not affect a later screenshot.
	s := gui.NewMemoryGraphicalSession(2, 2)
	first := mustRGBA(t, s)
	first.Pix[0] = 42
	_ = s.InjectPointer(0, 0, 1)
	second := mustRGBA(t, s)
	if second.RGBAAt(0, 0).R != 255 {
		t.Fatalf("second screenshot missing paint")
	}
	if first.Pix[0] != 42 {
		t.Fatalf("first screenshot unexpectedly changed")
	}
}

func mustRGBA(t *testing.T, s *gui.MemoryGraphicalSession) *image.RGBA {
	t.Helper()
	img, err := s.Screenshot()
	if err != nil {
		t.Fatalf("screenshot: %v", err)
	}
	rgba, ok := img.(*image.RGBA)
	if !ok {
		t.Fatalf("screenshot not *image.RGBA: %T", img)
	}
	return rgba
}
