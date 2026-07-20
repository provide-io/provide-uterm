//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import (
	"testing"
)

// guiFixture builds the GUI tool set on a fresh fake client.
func guiFixture() (*fakeClient, []serverTool) {
	f := &fakeClient{objResp: map[string]any{"ok": true}}
	return f, guiTools(f, adminAuth())
}

func TestGUIToolsDispatch(t *testing.T) {
	f, tools := guiFixture()

	// gui_hijack_begin reuses hijack_begin handler.
	res := invoke(t, findTool(t, tools, "gui_hijack_begin"), map[string]any{
		"worker_id": "w1", "lease_s": 30, "owner": "gui-op",
	})
	if res["success"] != true {
		t.Fatalf("gui_hijack_begin: %#v", res)
	}
	if f.last().Method != "Acquire" {
		t.Fatalf("begin method = %q", f.last().Method)
	}

	// gui_hijack_release reuses hijack_release handler.
	invoke(t, findTool(t, tools, "gui_hijack_release"), map[string]any{
		"worker_id": "w1", "hijack_id": "h1",
	})
	if f.last().Method != "Release" {
		t.Fatalf("release method = %q", f.last().Method)
	}

	invoke(t, findTool(t, tools, "gui_screenshot"), map[string]any{
		"worker_id": "w1", "hijack_id": "h1",
	})
	if call := f.last(); call.Method != "GUIScreenshot" || call.Params["workerID"] != "w1" || call.Params["hijackID"] != "h1" {
		t.Fatalf("screenshot call: %#v", call)
	}

	invoke(t, findTool(t, tools, "gui_click"), map[string]any{
		"worker_id": "w1", "hijack_id": "h1", "x": 10, "y": 20, "button": "right",
	})
	if call := f.last(); call.Method != "GUIClick" || call.Params["x"] != 10 || call.Params["y"] != 20 || call.Params["button"] != "right" {
		t.Fatalf("click call: %#v", call)
	}

	// button default when omitted.
	invoke(t, findTool(t, tools, "gui_click"), map[string]any{
		"worker_id": "w1", "hijack_id": "h1", "x": 1, "y": 2,
	})
	if f.last().Params["button"] != "left" {
		t.Fatalf("click default button = %#v", f.last().Params["button"])
	}

	invoke(t, findTool(t, tools, "gui_type"), map[string]any{
		"worker_id": "w1", "hijack_id": "h1", "text": "hi",
	})
	if call := f.last(); call.Method != "GUIType" || call.Params["text"] != "hi" {
		t.Fatalf("type call: %#v", call)
	}

	invoke(t, findTool(t, tools, "gui_key"), map[string]any{
		"worker_id": "w1", "hijack_id": "h1", "key_name": "Enter",
	})
	if call := f.last(); call.Method != "GUIKey" || call.Params["key_name"] != "Enter" {
		t.Fatalf("key call: %#v", call)
	}

	invoke(t, findTool(t, tools, "gui_drag"), map[string]any{
		"worker_id": "w1", "hijack_id": "h1",
		"start_x": 0, "start_y": 1, "end_x": 2, "end_y": 3,
	})
	call := f.last()
	if call.Method != "GUIDrag" ||
		call.Params["start_x"] != 0 || call.Params["start_y"] != 1 ||
		call.Params["end_x"] != 2 || call.Params["end_y"] != 3 {
		t.Fatalf("drag call: %#v", call)
	}
}

func TestGUIToolsAuthDenied(t *testing.T) {
	// Viewer lacks hijack_send/hijack_read → deny before RPC.
	f := &fakeClient{objResp: map[string]any{"ok": true}}
	auth := &AuthorizationContext{DefaultPrincipal: newPrincipal("v", "viewer")}
	tools := guiTools(f, auth)
	for _, name := range []string{"gui_screenshot", "gui_click", "gui_type", "gui_key", "gui_drag"} {
		res := invoke(t, findTool(t, tools, name), map[string]any{
			"worker_id": "w1", "hijack_id": "h1",
			"x": 1, "y": 2, "text": "a", "key_name": "Enter",
			"start_x": 0, "start_y": 0, "end_x": 1, "end_y": 1,
		})
		if res["error"] != "forbidden" && res["error"] != "permission_denied" && res["success"] != false {
			// Accept any structured deny that does not claim success.
			if res["success"] == true {
				t.Fatalf("%s should be denied for viewer: %#v", name, res)
			}
		}
	}
	if len(f.calls) != 0 {
		t.Fatalf("viewer must not RPC: %#v", f.calls)
	}
}
