package vnc_test

import (
	"image/color"
	"testing"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/vnc"
)

func TestFramebufferTracker(t *testing.T) {
	tracker := vnc.NewFramebufferTracker(100, 100)
	
	// Create a 2x2 red rect update
	pixels := []byte{
		255, 0, 0, 255,  255, 0, 0, 255,
		255, 0, 0, 255,  255, 0, 0, 255,
	}
	tracker.ApplyRawUpdate(10, 10, 2, 2, pixels)
	
	img := tracker.GetImage()
	c := img.At(10, 10).(color.RGBA)
	if c.R != 255 || c.G != 0 || c.B != 0 {
		t.Fatalf("expected red pixel, got %v", c)
	}
}
