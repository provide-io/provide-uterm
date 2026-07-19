//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/frames"
)

// TestBuildHelloFrameCapabilityDefaults proves the production browser-hello
// path stamps mcp_supported/vnc_supported (spec/behavior.json go defaults).
func TestBuildHelloFrameCapabilityDefaults(t *testing.T) {
	ts := newTestServer(t, nil)
	hf := ts.srv.buildHelloFrame("w1", "operator", true, map[string]any{
		"input_mode":    "raw",
		"is_hijacked":   false,
		"worker_online": true,
	})
	if hf.McpSupported == nil || !*hf.McpSupported {
		t.Fatalf("mcp_supported: %#v", hf.McpSupported)
	}
	if hf.VncSupported == nil || !*hf.VncSupported {
		t.Fatalf("vnc_supported: %#v", hf.VncSupported)
	}
	// Encode and re-decode to prove wire keys exist on the real path.
	raw, err := frames.EncodeFrame(hf)
	if err != nil {
		t.Fatal(err)
	}
	got, err := frames.DecodeFrame(raw)
	if err != nil {
		t.Fatal(err)
	}
	out, ok := got.(*frames.HelloFrame)
	if !ok {
		t.Fatalf("type %T", got)
	}
	if out.McpSupported == nil || !*out.McpSupported || out.VncSupported == nil || !*out.VncSupported {
		t.Fatalf("decoded caps missing: %#v", out)
	}
}
