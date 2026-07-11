package mcp_test

import (
	"encoding/base64"
	"image"
	"testing"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/gui"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/mcp"
)

type pointerCall struct {
	x, y int
	mask uint8
}

type stubSession struct{
	pointerCalls []pointerCall
}

var _ gui.GraphicalSession = (*stubSession)(nil)

func (s *stubSession) Screenshot() (image.Image, error) { 
	return image.NewRGBA(image.Rect(0, 0, 1, 1)), nil 
}
func (s *stubSession) InjectPointer(x, y int, buttonMask uint8) error {
	s.pointerCalls = append(s.pointerCalls, pointerCall{x, y, buttonMask})
	return nil 
}
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

func TestGUIClick(t *testing.T) {
	session := &stubSession{}
	err := mcp.HandleGUIClick(session, 100, 200, "left")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	if len(session.pointerCalls) != 2 {
		t.Fatalf("expected 2 pointer calls, got %d", len(session.pointerCalls))
	}

	if session.pointerCalls[0].mask != 1 || session.pointerCalls[0].x != 100 || session.pointerCalls[0].y != 200 {
		t.Errorf("unexpected first call: %+v", session.pointerCalls[0])
	}
	if session.pointerCalls[1].mask != 0 || session.pointerCalls[1].x != 100 || session.pointerCalls[1].y != 200 {
		t.Errorf("unexpected second call: %+v", session.pointerCalls[1])
	}

	err = mcp.HandleGUIClick(session, 0, 0, "unknown")
	if err == nil {
		t.Errorf("expected error for unknown button, got nil")
	}
}
