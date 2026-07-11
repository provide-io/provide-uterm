package mcp_test

import (
	"encoding/base64"
	"image"
	"testing"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/gui"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/mcp"
)

type stubSession struct{}
var _ gui.GraphicalSession = (*stubSession)(nil)

func (s *stubSession) Screenshot() (image.Image, error) { 
	return image.NewRGBA(image.Rect(0, 0, 1, 1)), nil 
}
func (s *stubSession) InjectPointer(x, y int, buttonMask uint8) error { return nil }
func (s *stubSession) InjectKey(keySym uint32, down bool) error { return nil }

func TestGUIScreenshot(t *testing.T) {
	session := &stubSession{}
	pngBase64, err := mcp.HandleGUIScreenshot(session)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := base64.StdEncoding.DecodeString(pngBase64); err != nil {
		t.Fatal("expected valid base64")
	}
}
