//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import (
	"strings"
	"testing"
)

// mustPrefix is only ever fed compile-time constants, so a bad one is a
// programming error that must fail loudly at init rather than be skipped.
func TestMustPrefixPanicsOnInvalidCIDR(t *testing.T) {
	defer func() {
		r := recover()
		msg, ok := r.(string)
		if !ok || !strings.Contains(msg, "invalid built-in prefix not-a-cidr") {
			t.Fatalf("panic = %#v", r)
		}
	}()
	mustPrefix("not-a-cidr")
}

func TestBodyIsRepeatedUnitEmptyBody(t *testing.T) {
	if bodyIsRepeatedUnit("") {
		t.Fatal("an empty group body is not a repeated unit")
	}
}

func TestPyStrReprEscapesCarriageReturn(t *testing.T) {
	if got := pyStrRepr("a\rb"); got != `'a\rb'` {
		t.Fatalf("pyStrRepr = %q", got)
	}
}

func TestHijackHeartbeatDispatch(t *testing.T) {
	f, tools := hijackFixture()
	res := invoke(t, findTool(t, tools, "hijack_heartbeat"), map[string]any{
		"worker_id": "w1", "hijack_id": "h1", "lease_s": 30,
	})
	if res["success"] != true || res["detail"] != "ok" {
		t.Fatalf("unexpected result: %#v", res)
	}
	call := f.last()
	if call.Method != "Heartbeat" || call.Params["workerID"] != "w1" || call.Params["hijackID"] != "h1" || call.Params["lease_s"] != 30 {
		t.Fatalf("wrong Heartbeat call: %#v", call)
	}
}

func TestHijackSendBadIDRejectedBeforeRPC(t *testing.T) {
	f, tools := hijackFixture()
	res := invoke(t, findTool(t, tools, "hijack_send"), map[string]any{
		"worker_id": "w1", "hijack_id": "../h", "keys": "ls\n",
	})
	if res["error"] != "invalid_id" || res["detail"] != `invalid hijack_id: '../h'` {
		t.Fatalf("expected invalid_id, got %#v", res)
	}
	if len(f.calls) != 0 {
		t.Fatalf("no RPC may fire on a bad id: %#v", f.calls)
	}
}

func TestSessionCreateForwardsURL(t *testing.T) {
	f, tools := sessionFixture()
	invoke(t, findTool(t, tools, "session_create"), map[string]any{
		"connector_type": "websocket", "url": "wss://example.com/term",
	})
	call := f.last()
	cfg, _ := call.Params["config"].(map[string]any)
	if call.Method != "QuickConnect" || cfg["url"] != "wss://example.com/term" {
		t.Fatalf("url not forwarded: %#v", call)
	}
}

func TestSessionWatchRejectsBadPatternBeforeRPC(t *testing.T) {
	f, tools := sessionFixture()
	res := invoke(t, findTool(t, tools, "session_watch"), map[string]any{"session_id": "s1", "pattern": "(a*)*"})
	if res["error"] != "invalid_pattern" {
		t.Fatalf("expected invalid_pattern, got %#v", res)
	}
	if len(f.calls) != 0 {
		t.Fatalf("no RPC may fire after a pattern rejection: %#v", f.calls)
	}
}

// Non-object entries in the event list are skipped, not matched or crashed on.
func TestSessionSubscribeSkipsNonObjectEvents(t *testing.T) {
	f := &fakeClient{anyResp: map[string]any{"events": []any{
		"not an event",
		map[string]any{"data": map[string]any{"screen": "$ ready"}},
	}}}
	tools := sessionTools(f, adminAuth())
	res := invoke(t, findTool(t, tools, "session_subscribe"), map[string]any{"session_id": "s1", "pattern": `ready`})
	if res["matched_pattern"] != true {
		t.Fatalf("expected the object event to match, got %#v", res)
	}
	f = &fakeClient{anyResp: map[string]any{"events": []any{"ready"}}}
	tools = sessionTools(f, adminAuth())
	res = invoke(t, findTool(t, tools, "session_subscribe"), map[string]any{"session_id": "s1", "pattern": `ready`})
	if res["matched_pattern"] != false {
		t.Fatalf("a bare string event must not count as a match, got %#v", res)
	}
}
