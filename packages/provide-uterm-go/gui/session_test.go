package gui_test

import (
	"image"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/gui"
)

type mockSession struct{}

func (m *mockSession) Screenshot() (image.Image, error) {
	return image.NewRGBA(image.Rect(0, 0, 10, 10)), nil
}
func (m *mockSession) InjectPointer(x, y int, buttonMask uint8) error { return nil }
func (m *mockSession) InjectKey(keySym uint32, down bool) error       { return nil }

func TestGraphicalSessionInterface(t *testing.T) {
	var s gui.GraphicalSession = &mockSession{}
	if _, err := s.Screenshot(); err != nil {
		t.Fatal(err)
	}
}
