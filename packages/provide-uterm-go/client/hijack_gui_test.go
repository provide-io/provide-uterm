//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package client

import (
	"context"
	"testing"
)

func TestHijackClientGUIMethods(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("GET", "/worker/w1/hijack/h1/gui/screenshot", fakeResponse{status: 200, body: map[string]any{"ok": true}})
	fs.on("POST", "/worker/w1/hijack/h1/gui/click", fakeResponse{status: 200, body: map[string]any{"ok": true}})
	fs.on("POST", "/worker/w1/hijack/h1/gui/type", fakeResponse{status: 200, body: map[string]any{"ok": true}})
	fs.on("POST", "/worker/w1/hijack/h1/gui/key", fakeResponse{status: 200, body: map[string]any{"ok": true}})
	fs.on("POST", "/worker/w1/hijack/h1/gui/drag", fakeResponse{status: 200, body: map[string]any{"ok": true}})

	c := NewHijackClient(fs.srv.URL)
	ctx := context.Background()
	if _, err := c.GUIScreenshot(ctx, "w1", "h1"); err != nil {
		t.Fatal(err)
	}
	if _, err := c.GUIClick(ctx, "w1", "h1", 1, 2, "left"); err != nil {
		t.Fatal(err)
	}
	if _, err := c.GUIType(ctx, "w1", "h1", "hi"); err != nil {
		t.Fatal(err)
	}
	if _, err := c.GUIKey(ctx, "w1", "h1", "Enter"); err != nil {
		t.Fatal(err)
	}
	if _, err := c.GUIDrag(ctx, "w1", "h1", 0, 0, 5, 5); err != nil {
		t.Fatal(err)
	}
	// invalid ids
	if _, err := c.GUIScreenshot(ctx, "bad!", "h1"); err == nil {
		t.Fatal("expected bad worker id error")
	}
}
