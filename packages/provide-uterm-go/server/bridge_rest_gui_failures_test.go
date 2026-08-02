//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"errors"
	"image"
	"image/color"
	"net/http"
	"sync"
	"testing"
)

// emptyImage has zero-sized bounds, which png.Encode rejects. It models a
// console that handed back a framebuffer the encoder cannot serialise.
type emptyImage struct{}

func (emptyImage) ColorModel() color.Model { return color.RGBAModel }
func (emptyImage) Bounds() image.Rectangle { return image.Rect(0, 0, 0, 0) }
func (emptyImage) At(int, int) color.Color { return color.RGBA{} }

// flakyGUISession fails the Nth pointer/key injection (1-based; 0 never fails)
// and optionally fails or degrades Screenshot, so each individual inject error
// arm of the GUI routes is driven by a real failing console.
type flakyGUISession struct {
	mu            sync.Mutex
	pointerCalls  int
	keyCalls      int
	failPointerAt int
	failKeyAt     int
	screenshotErr error
	emptyFrame    bool
}

var errGUIConsole = errors.New("graphical console lost")

func (f *flakyGUISession) Screenshot() (image.Image, error) {
	if f.screenshotErr != nil {
		return nil, f.screenshotErr
	}
	if f.emptyFrame {
		return emptyImage{}, nil
	}
	return image.NewRGBA(image.Rect(0, 0, 2, 2)), nil
}

func (f *flakyGUISession) InjectPointer(int, int, uint8) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.pointerCalls++
	if f.pointerCalls == f.failPointerAt {
		return errGUIConsole
	}
	return nil
}

func (f *flakyGUISession) InjectKey(uint32, bool) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.keyCalls++
	if f.keyCalls == f.failKeyAt {
		return errGUIConsole
	}
	return nil
}

func (f *flakyGUISession) Close() error { return nil }

// installGUISession swaps the live console behind the attached session manager
// for sess, using the manager's own public Replace so the swap is lock-safe.
func installGUISession(t *testing.T, ts *testServer, workerID string, sess *flakyGUISession) {
	t.Helper()
	st, err := ts.hub.Registry.Require(workerID)
	if err != nil {
		t.Fatalf("require %s: %v", workerID, err)
	}
	mgr, ok := st.GraphicalSession.(*GraphicalSessionManager)
	if !ok {
		t.Fatalf("worker %s has no graphical session manager", workerID)
	}
	mgr.Replace(sess, nil, nil)
}

// guiOpsFailing builds the standard GUI-attached hijack, then swaps in sess.
func guiOpsFailing(t *testing.T, sess *flakyGUISession) (*testServer, string, map[string]string) {
	t.Helper()
	ts, hid := guiOpsServer(t)
	installGUISession(t, ts, "w1", sess)
	return ts, hid, tenantHeaders("admin", "acme")
}

func TestHijackGUIScreenshotConsoleFailure(t *testing.T) {
	ts, hid, hdr := guiOpsFailing(t, &flakyGUISession{screenshotErr: errGUIConsole})
	rec := ts.do("GET", "/worker/w1/hijack/"+hid+"/gui/screenshot", "", hdr)
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("screenshot: want 500, got %d %s", rec.Code, rec.Body.String())
	}
}

func TestHijackGUIScreenshotUnencodableFrame(t *testing.T) {
	ts, hid, hdr := guiOpsFailing(t, &flakyGUISession{emptyFrame: true})
	rec := ts.do("GET", "/worker/w1/hijack/"+hid+"/gui/screenshot", "", hdr)
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("screenshot: want 500, got %d %s", rec.Code, rec.Body.String())
	}
	if got := decode(t, rec.Body.Bytes())["error"]; got != "screenshot encode failed" {
		t.Fatalf("detail = %v", got)
	}
}

func TestHijackGUIClickPointerFailures(t *testing.T) {
	// The press and the release are separate injections; either failing must
	// surface as a 500 rather than a silent half-click.
	for _, failAt := range []int{1, 2} {
		ts, hid, hdr := guiOpsFailing(t, &flakyGUISession{failPointerAt: failAt})
		rec := ts.do("POST", "/worker/w1/hijack/"+hid+"/gui/click", `{"x":1,"y":2}`, hdr)
		if rec.Code != http.StatusInternalServerError {
			t.Fatalf("click failing at %d: want 500, got %d %s", failAt, rec.Code, rec.Body.String())
		}
	}
}

func TestHijackGUITypeKeyFailures(t *testing.T) {
	for _, failAt := range []int{1, 2} {
		ts, hid, hdr := guiOpsFailing(t, &flakyGUISession{failKeyAt: failAt})
		rec := ts.do("POST", "/worker/w1/hijack/"+hid+"/gui/type", `{"text":"a"}`, hdr)
		if rec.Code != http.StatusInternalServerError {
			t.Fatalf("type failing at %d: want 500, got %d %s", failAt, rec.Code, rec.Body.String())
		}
	}
}

func TestHijackGUIKeyFailures(t *testing.T) {
	for _, failAt := range []int{1, 2} {
		ts, hid, hdr := guiOpsFailing(t, &flakyGUISession{failKeyAt: failAt})
		rec := ts.do("POST", "/worker/w1/hijack/"+hid+"/gui/key", `{"key_name":"Enter"}`, hdr)
		if rec.Code != http.StatusInternalServerError {
			t.Fatalf("key failing at %d: want 500, got %d %s", failAt, rec.Code, rec.Body.String())
		}
	}
}

func TestHijackGUIDragPointerFailures(t *testing.T) {
	// press, move, release — three injections, each with its own error arm.
	for _, failAt := range []int{1, 2, 3} {
		ts, hid, hdr := guiOpsFailing(t, &flakyGUISession{failPointerAt: failAt})
		rec := ts.do("POST", "/worker/w1/hijack/"+hid+"/gui/drag",
			`{"start_x":0,"start_y":0,"end_x":4,"end_y":5}`, hdr)
		if rec.Code != http.StatusInternalServerError {
			t.Fatalf("drag failing at %d: want 500, got %d %s", failAt, rec.Code, rec.Body.String())
		}
	}
}
