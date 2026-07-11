# Graphical Session Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the `gui.GraphicalSession` interface, a minimal `vnc` backend that forces `Raw` RFB encoding, and the `gui_*` MCP tools for AI interaction.

**Architecture:** We separate the generic interface (`gui`) from the protocol implementation (`vnc`). The VNC client handles the handshake and reads pixel updates into a `FramebufferTracker`. MCP tools operate against the `gui.GraphicalSession` interface, gated by the `HijackLeaseManager`.

**Tech Stack:** Go standard library (`image`, `net`), `testing`.

---

### Task 1: Define `gui.GraphicalSession` Interface

**Files:**
- Create: `packages/provide-uterm-go/gui/session.go`
- Create: `packages/provide-uterm-go/gui/session_test.go`

- [ ] **Step 1: Write the failing test**

```go
package gui_test

import (
	"image"
	"testing"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/gui"
)

type mockSession struct{}

func (m *mockSession) Screenshot() (image.Image, error) { return image.NewRGBA(image.Rect(0, 0, 10, 10)), nil }
func (m *mockSession) InjectPointer(x, y int, buttonMask uint8) error { return nil }
func (m *mockSession) InjectKey(keySym uint32, down bool) error { return nil }

func TestGraphicalSessionInterface(t *testing.T) {
	var s gui.GraphicalSession = &mockSession{}
	if _, err := s.Screenshot(); err != nil {
		t.Fatal(err)
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd packages/provide-uterm-go && go test ./gui`
Expected: FAIL with "build failed" or "undefined: gui.GraphicalSession"

- [ ] **Step 3: Write minimal implementation**

```go
package gui

import "image"

// GraphicalSession represents an active connection to a remote graphical console.
type GraphicalSession interface {
	Screenshot() (image.Image, error)
	InjectPointer(x, y int, buttonMask uint8) error
	InjectKey(keySym uint32, down bool) error
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd packages/provide-uterm-go && go test ./gui`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd packages/provide-uterm-go
git add gui/session.go gui/session_test.go
git commit -m "feat: define GraphicalSession interface"
```

### Task 2: Build `vnc.FramebufferTracker`

**Files:**
- Create: `packages/provide-uterm-go/vnc/tracker.go`
- Create: `packages/provide-uterm-go/vnc/tracker_test.go`

- [ ] **Step 1: Write the failing test**

```go
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd packages/provide-uterm-go && go test ./vnc`
Expected: FAIL with "undefined: vnc.NewFramebufferTracker"

- [ ] **Step 3: Write minimal implementation**

```go
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd packages/provide-uterm-go && go test ./vnc`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd packages/provide-uterm-go
git add vnc/tracker.go vnc/tracker_test.go
git commit -m "feat: implement vnc.FramebufferTracker"
```

### Task 3: MCP GUI Tools Skeleton

Since full MCP integration requires wiring up the session manager, we'll implement the tool handlers that assume an active session.

**Files:**
- Create: `packages/provide-uterm-go/mcp/gui_tools.go`
- Create: `packages/provide-uterm-go/mcp/gui_tools_test.go`

- [ ] **Step 1: Write the failing test**

```go
package mcp_test

import (
	"encoding/base64"
	"image"
	"testing"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/gui"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/mcp"
)

type stubSession struct{}
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd packages/provide-uterm-go && go test ./mcp`
Expected: FAIL with "undefined: mcp.HandleGUIScreenshot"

- [ ] **Step 3: Write minimal implementation**

```go
package mcp

import (
	"bytes"
	"encoding/base64"
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
	}
	// Simulate press then release
	if err := session.InjectPointer(x, y, mask); err != nil {
		return err
	}
	return session.InjectPointer(x, y, 0)
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd packages/provide-uterm-go && go test ./mcp`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd packages/provide-uterm-go
git add mcp/gui_tools.go mcp/gui_tools_test.go
git commit -m "feat: add gui MCP tool handlers"
```
