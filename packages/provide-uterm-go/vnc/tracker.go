package vnc

import (
	"fmt"
	"image"
	"image/draw"
	"sync"
)

type FramebufferTracker struct {
	mu  sync.RWMutex
	img *image.RGBA
}

func NewFramebufferTracker(width, height int) *FramebufferTracker {
	return &FramebufferTracker{
		img: image.NewRGBA(image.Rect(0, 0, width, height)),
	}
}

func (t *FramebufferTracker) ApplyRawUpdate(x, y, w, h int, pixels []byte) error {
	if w < 0 || h < 0 {
		return fmt.Errorf("invalid dimensions: w=%d, h=%d", w, h)
	}
	expectedLen := int64(w) * int64(h) * 4
	if int64(len(pixels)) < expectedLen {
		return fmt.Errorf("invalid pixel buffer size: expected %d, got %d", expectedLen, len(pixels))
	}

	t.mu.Lock()
	defer t.mu.Unlock()

	src := &image.RGBA{
		Pix:    pixels,
		Stride: w * 4,
		Rect:   image.Rect(0, 0, w, h),
	}

	r := image.Rect(x, y, x+w, y+h)
	draw.Draw(t.img, r, src, image.Point{0, 0}, draw.Src)
	return nil
}

func (t *FramebufferTracker) GetImage() *image.RGBA {
	t.mu.RLock()
	defer t.mu.RUnlock()

	// Return a copy to avoid race conditions when encoding
	copyImg := image.NewRGBA(t.img.Rect)
	copy(copyImg.Pix, t.img.Pix)
	return copyImg
}
