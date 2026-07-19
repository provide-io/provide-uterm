//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package frames

import (
	"encoding/json"
	"testing"
	"testing/quick"
)

func TestHelloCapabilityRoundTrip(t *testing.T) {
	h := MakeHelloFrameWithDefaults()
	h.WorkerID = Ptr("w1")
	raw, err := EncodeFrame(h)
	if err != nil {
		t.Fatal(err)
	}
	got, err := DecodeFrame(raw)
	if err != nil {
		t.Fatal(err)
	}
	hf, ok := got.(*HelloFrame)
	if !ok {
		t.Fatalf("type %T", got)
	}
	if hf.McpSupported == nil || !*hf.McpSupported {
		t.Fatalf("mcp: %#v", hf.McpSupported)
	}
	if hf.VncSupported == nil || !*hf.VncSupported {
		t.Fatalf("vnc: %#v", hf.VncSupported)
	}
}

func TestHelloJSONFuzzPreservesCapabilityBools(t *testing.T) {
	fn := func(mcp, vnc bool) bool {
		payload := map[string]any{
			"type":          "hello",
			"mcp_supported": mcp,
			"vnc_supported": vnc,
		}
		raw, err := json.Marshal(payload)
		if err != nil {
			return false
		}
		got, err := DecodeFrame(raw)
		if err != nil {
			return false
		}
		hf, ok := got.(*HelloFrame)
		if !ok {
			return false
		}
		if hf.McpSupported == nil || *hf.McpSupported != mcp {
			return false
		}
		if hf.VncSupported == nil || *hf.VncSupported != vnc {
			return false
		}
		return true
	}
	if err := quick.Check(fn, &quick.Config{MaxCount: 64}); err != nil {
		t.Fatal(err)
	}
}
