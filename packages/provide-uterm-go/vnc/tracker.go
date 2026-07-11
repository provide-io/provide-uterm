package vnc

import (
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

func (t *FramebufferTracker) ApplyRawUpdate(x, y, w, h int, pixels []byte) {
	t.mu.Lock()
	defer t.mu.Unlock()
	
	src := image.NewRGBA(image.Rect(0, 0, w, h))
	src.Pix = pixels
	src.Stride = w * 4

	r := image.Rect(x, y, x+w, y+h)
	draw.Draw(t.img, r, src, image.Point{0, 0}, draw.Src)
}

func (t *FramebufferTracker) GetImage() *image.RGBA {
	t.mu.RLock()
	defer t.mu.RUnlock()
	
	// Return a copy to avoid race conditions when encoding
	copyImg := image.NewRGBA(t.img.Rect)
	copy(copyImg.Pix, t.img.Pix)
	return copyImg
}
