//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package gui

import (
	"image"
	"image/color"
	"sync"
)

// maxDimension is the hard cap on a single framebuffer dimension (hostile
// dimension protection). Mirrors C# RgbaImage.MaxDimension.
const maxDimension = 8192

// MemoryGraphicalSession is an in-memory graphical session stub for tests and
// offline tooling. Port of the C# MemoryGraphicalSession (Gui/Session.cs): a
// fixed WxH RGBA framebuffer. A left-button pointer inside bounds paints the
// pixel white; key injection is a no-op.
type MemoryGraphicalSession struct {
	mu sync.Mutex
	fb *image.RGBA
}

// NewMemoryGraphicalSession builds a session with a width x height framebuffer.
// Dimensions are clamped to [1, maxDimension] so a hostile target definition
// can never allocate an unbounded buffer.
func NewMemoryGraphicalSession(width, height int) *MemoryGraphicalSession {
	if width < 1 {
		width = 1
	}
	if height < 1 {
		height = 1
	}
	if width > maxDimension {
		width = maxDimension
	}
	if height > maxDimension {
		height = maxDimension
	}
	return &MemoryGraphicalSession{fb: image.NewRGBA(image.Rect(0, 0, width, height))}
}

// Screenshot returns a detached copy of the framebuffer so callers never alias
// (or race against) the live pixels. Mirrors C# RgbaImage.Clone.
func (m *MemoryGraphicalSession) Screenshot() (image.Image, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	out := image.NewRGBA(m.fb.Rect)
	copy(out.Pix, m.fb.Pix)
	return out, nil
}

// InjectPointer paints the target pixel white when the left button is held and
// the coordinate is in bounds; otherwise it is a no-op. Mirrors the C# stub.
func (m *MemoryGraphicalSession) InjectPointer(x, y int, buttonMask uint8) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	b := m.fb.Rect
	if buttonMask&1 == 0 || x < 0 || y < 0 || x >= b.Dx() || y >= b.Dy() {
		return nil
	}
	m.fb.SetRGBA(x, y, color.RGBA{R: 255, G: 255, B: 255, A: 255})
	return nil
}

// InjectKey is a no-op (the memory session has no keyboard model).
func (m *MemoryGraphicalSession) InjectKey(keySym uint32, down bool) error {
	_ = keySym
	_ = down
	return nil
}
