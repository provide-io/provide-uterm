//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"testing"
)

// guiOpsServer attaches a memory graphical target to worker w1 and returns the
// server plus a live hijack id under the acme-admin principal.
func guiOpsServer(t *testing.T) (*testServer, string) {
	t.Helper()
	ts := attachTestServer(t, memoryTarget("gt-mem", "acme"))
	ts.setupWorker(t, "w1")
	hdr := tenantHeaders("admin", "acme")
	if rec := ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-mem"}`, hdr); rec.Code != http.StatusOK {
		t.Fatalf("attach: %d %s", rec.Code, rec.Body.String())
	}
	rec := ts.do("POST", "/worker/w1/hijack/acquire", `{"owner":"gui","lease_s":120}`, hdr)
	if rec.Code != http.StatusOK {
		t.Fatalf("acquire: %d %s", rec.Code, rec.Body.String())
	}
	hid, _ := decode(t, rec.Body.Bytes())["hijack_id"].(string)
	if hid == "" {
		t.Fatalf("no hijack id: %s", rec.Body.String())
	}
	return ts, hid
}

func TestHijackGUIScreenshotSuccess(t *testing.T) {
	ts, hid := guiOpsServer(t)
	hdr := tenantHeaders("admin", "acme")
	rec := ts.do("GET", "/worker/w1/hijack/"+hid+"/gui/screenshot", "", hdr)
	if rec.Code != http.StatusOK {
		t.Fatalf("screenshot: %d %s", rec.Code, rec.Body.String())
	}
	body := decode(t, rec.Body.Bytes())
	if body["ok"] != true {
		t.Fatalf("body: %v", body)
	}
	if _, ok := body["screenshot"].(string); !ok || body["screenshot"] == "" {
		t.Fatalf("missing screenshot b64: %v", body)
	}
}

func TestHijackGUIClickButtons(t *testing.T) {
	ts, hid := guiOpsServer(t)
	hdr := tenantHeaders("admin", "acme")
	base := "/worker/w1/hijack/" + hid + "/gui/click"
	for _, btn := range []string{"left", "middle", "right", "other"} {
		rec := ts.do("POST", base, `{"x":5,"y":6,"button":"`+btn+`"}`, hdr)
		if rec.Code != http.StatusOK {
			t.Fatalf("click %s: %d %s", btn, rec.Code, rec.Body.String())
		}
	}
}

func TestHijackGUITypeAndKeys(t *testing.T) {
	ts, hid := guiOpsServer(t)
	hdr := tenantHeaders("admin", "acme")
	prefix := "/worker/w1/hijack/" + hid + "/gui/"

	rec := ts.do("POST", prefix+"type", `{"text":"ab"}`, hdr)
	if rec.Code != http.StatusOK {
		t.Fatalf("type: %d %s", rec.Code, rec.Body.String())
	}
	for _, key := range []string{"Enter", "Tab", "Esc", "Backspace", "Up", "Down", "Left", "Right", "Unknown"} {
		rec := ts.do("POST", prefix+"key", `{"key_name":"`+key+`"}`, hdr)
		if rec.Code != http.StatusOK {
			t.Fatalf("key %s: %d %s", key, rec.Code, rec.Body.String())
		}
	}
}

func TestHijackGUIDragSuccess(t *testing.T) {
	ts, hid := guiOpsServer(t)
	hdr := tenantHeaders("admin", "acme")
	rec := ts.do("POST", "/worker/w1/hijack/"+hid+"/gui/drag",
		`{"start_x":0,"start_y":0,"end_x":10,"end_y":12}`, hdr)
	if rec.Code != http.StatusOK {
		t.Fatalf("drag: %d %s", rec.Code, rec.Body.String())
	}
}

func TestHijackGUIOpsNoSession404(t *testing.T) {
	// Hijack without graphical attach → 404 "No graphical session".
	ts := newTestServer(t, nil)
	hid := acquireHijack(t, ts, "nogui")
	hdr := adminHeaders()
	paths := []struct {
		method, path, body string
	}{
		{"GET", "/worker/nogui/hijack/" + hid + "/gui/screenshot", ""},
		{"POST", "/worker/nogui/hijack/" + hid + "/gui/click", `{"x":1,"y":2}`},
		{"POST", "/worker/nogui/hijack/" + hid + "/gui/type", `{"text":"x"}`},
		{"POST", "/worker/nogui/hijack/" + hid + "/gui/key", `{"key_name":"Enter"}`},
		{"POST", "/worker/nogui/hijack/" + hid + "/gui/drag", `{"start_x":0,"start_y":0,"end_x":1,"end_y":1}`},
	}
	for _, p := range paths {
		rec := ts.do(p.method, p.path, p.body, hdr)
		if rec.Code != http.StatusNotFound {
			t.Fatalf("%s %s: want 404 got %d %s", p.method, p.path, rec.Code, rec.Body.String())
		}
	}
}

func TestHijackGUIOpsInvalidHijack404(t *testing.T) {
	ts := attachTestServer(t, memoryTarget("gt-mem", "acme"))
	ts.setupWorker(t, "w1")
	hdr := tenantHeaders("admin", "acme")
	_ = ts.do("POST", "/worker/w1/gui/attach", `{"target_id":"gt-mem"}`, hdr)
	ghost := "00000000-0000-4000-8000-000000000000"
	// Screenshot uses GetRestSession; click uses requireGraphicalSession — both 404.
	if rec := ts.do("GET", "/worker/w1/hijack/"+ghost+"/gui/screenshot", "", hdr); rec.Code != http.StatusNotFound {
		t.Fatalf("screenshot ghost: %d", rec.Code)
	}
	if rec := ts.do("POST", "/worker/w1/hijack/"+ghost+"/gui/click", `{"x":1,"y":2}`, hdr); rec.Code != http.StatusNotFound {
		t.Fatalf("click ghost: %d", rec.Code)
	}
}

func TestHijackGUIOpsAuthDenied(t *testing.T) {
	ts, hid := guiOpsServer(t)
	// Viewer can read screenshot (hubRead) on a public session but cannot click
	// (hubHijack). setupWorker registers as public owned by admin1.
	// Viewer lacks graphical hijack → 403 on click; screenshot may 200 or 403
	// depending on session visibility grants — assert click is denied.
	if rec := ts.do("POST", "/worker/w1/hijack/"+hid+"/gui/click", `{"x":1,"y":2}`, viewerHeaders()); rec.Code != http.StatusForbidden {
		t.Fatalf("viewer click: %d %s", rec.Code, rec.Body.String())
	}
}

func TestHijackGUIBadWorkerID(t *testing.T) {
	ts := newTestServer(t, nil)
	// Invalid path segment → bridgeParams fails (422).
	if rec := ts.do("GET", "/worker/bad%20id/hijack/h1/gui/screenshot", "", adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("bad worker screenshot: %d", rec.Code)
	}
	if rec := ts.do("POST", "/worker/bad%20id/hijack/h1/gui/click", `{}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("bad worker click: %d", rec.Code)
	}
	if rec := ts.do("POST", "/worker/bad%20id/hijack/h1/gui/type", `{}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("bad worker type: %d", rec.Code)
	}
	if rec := ts.do("POST", "/worker/bad%20id/hijack/h1/gui/key", `{}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("bad worker key: %d", rec.Code)
	}
	if rec := ts.do("POST", "/worker/bad%20id/hijack/h1/gui/drag", `{}`, adminHeaders()); rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("bad worker drag: %d", rec.Code)
	}
}
