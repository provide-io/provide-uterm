package vnc_test

import (
	"image/color"
	"strings"
	"testing"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/vnc"
)

func TestFramebufferTracker(t *testing.T) {
	t.Run("HappyPath", func(t *testing.T) {
		tracker := vnc.NewFramebufferTracker(100, 100)
		pixels := []byte{
			255, 0, 0, 255,  255, 0, 0, 255,
			255, 0, 0, 255,  255, 0, 0, 255,
		}
		err := tracker.ApplyRawUpdate(10, 10, 2, 2, pixels)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}
		img := tracker.GetImage()
		c := img.At(10, 10).(color.RGBA)
		if c.R != 255 || c.G != 0 || c.B != 0 {
			t.Fatalf("expected red pixel, got %v", c)
		}
	})

	t.Run("ShortPixels", func(t *testing.T) {
		tracker := vnc.NewFramebufferTracker(100, 100)
		pixels := []byte{255, 0, 0, 255} // Only 1 pixel, but asking for 2x2
		err := tracker.ApplyRawUpdate(10, 10, 2, 2, pixels)
		if err == nil {
			t.Fatalf("expected error for short pixel buffer, got nil")
		}
		if !strings.Contains(err.Error(), "invalid pixel buffer size") {
			t.Fatalf("unexpected error message: %v", err)
		}
	})

	t.Run("NegativeDimensions", func(t *testing.T) {
		tracker := vnc.NewFramebufferTracker(100, 100)
		err := tracker.ApplyRawUpdate(10, 10, -2, 2, []byte{})
		if err == nil {
			t.Fatalf("expected error for negative width, got nil")
		}
		if !strings.Contains(err.Error(), "invalid dimensions") {
			t.Fatalf("unexpected error message: %v", err)
		}
	})

	t.Run("OutOfBoundsClipping", func(t *testing.T) {
		tracker := vnc.NewFramebufferTracker(10, 10)
		pixels := make([]byte, 20*20*4)
		// Draw a 20x20 rect at 5,5 (should clip to 10,10)
		for i := 0; i < len(pixels); i += 4 {
			pixels[i] = 0     // R
			pixels[i+1] = 255 // G
			pixels[i+2] = 0   // B
			pixels[i+3] = 255 // A
		}
		err := tracker.ApplyRawUpdate(5, 5, 20, 20, pixels)
		if err != nil {
			t.Fatalf("unexpected error: %v", err)
		}

		img := tracker.GetImage()
		// Inside bounds (5,5 to 9,9) should be green
		c := img.At(5, 5).(color.RGBA)
		if c.G != 255 {
			t.Fatalf("expected green pixel inside bounds, got %v", c)
		}
		// At exactly bounds (10,10 is out of bounds of 0-9)
		// we verify it didn't panic or draw outside (draw.Draw handles clipping).
	})
}
