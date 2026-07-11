package mcp

import (
	"bytes"
	"encoding/base64"
	"fmt"
	"image/png"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/gui"
)

// HandleGUIScreenshot captures the session and returns a base64 encoded PNG.
func HandleGUIScreenshot(session gui.GraphicalSession) (string, error) {
	img, err := session.Screenshot()
	if err != nil {
		return "", err
	}
	
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		return "", err
	}
	
	return base64.StdEncoding.EncodeToString(buf.Bytes()), nil
}

// HandleGUIClick translates an MCP click tool call into an InjectPointer call.
func HandleGUIClick(session gui.GraphicalSession, x, y int, button string) error {
	var mask uint8 = 0
	switch button {
	case "left":
		mask = 1
	case "middle":
		mask = 2
	case "right":
		mask = 4
	default:
		return fmt.Errorf("unsupported button: %q, expected left, middle, or right", button)
	}
	// Simulate press then release
	if err := session.InjectPointer(x, y, mask); err != nil {
		return err
	}
	return session.InjectPointer(x, y, 0)
}
