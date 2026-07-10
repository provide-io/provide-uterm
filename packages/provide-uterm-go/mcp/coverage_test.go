//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import (
	"encoding/json"
	"reflect"
	"testing"
)

// TestToolBadIDRejections drives the id-rejection branch of every id-guarded
// tool, asserting each emits the structured invalid_id contract before any RPC.
func TestToolBadIDRejections(t *testing.T) {
	cases := []struct {
		tool string
		args map[string]any
	}{
		{"hijack_heartbeat", map[string]any{"worker_id": "../x", "hijack_id": "h"}},
		{"hijack_read", map[string]any{"worker_id": "../x", "hijack_id": "h"}},
		{"hijack_step", map[string]any{"worker_id": "../x", "hijack_id": "h"}},
		{"hijack_release", map[string]any{"worker_id": "../x", "hijack_id": "h"}},
		{"session_set_mode", map[string]any{"session_id": "../x", "mode": "open"}},
		{"worker_input_mode", map[string]any{"worker_id": "../x", "mode": "open"}},
		{"worker_disconnect", map[string]any{"worker_id": "../x"}},
		{"session_status", map[string]any{"session_id": "../x"}},
		{"session_read", map[string]any{"session_id": "../x"}},
		{"session_connect", map[string]any{"session_id": "../x"}},
		{"session_disconnect", map[string]any{"session_id": "../x"}},
		{"session_watch", map[string]any{"session_id": "../x"}},
		{"session_subscribe", map[string]any{"session_id": "../x"}},
		{"session_annotate", map[string]any{"session_id": "../x", "label": "l"}},
	}
	for _, tc := range cases {
		f := &fakeClient{objResp: map[string]any{"ok": true}, anyResp: map[string]any{"ok": true}}
		all := append(hijackTools(f, adminAuth()), sessionTools(f, adminAuth())...)
		res := invoke(t, findTool(t, all, tc.tool), tc.args)
		if res["error"] != "invalid_id" {
			t.Errorf("%s: expected invalid_id, got %#v", tc.tool, res)
		}
		if len(f.calls) != 0 {
			t.Errorf("%s: no RPC should fire on a bad id", tc.tool)
		}
	}
}

func TestOptInt(t *testing.T) {
	build := func(v any) map[string]any { return map[string]any{"k": v} }

	cases := []struct {
		in   any
		want *int
	}{
		{42, ptrInt(42)},
		{int64(9), ptrInt(9)},
		{float64(3.9), ptrInt(3)},
		{json.Number("11"), ptrInt(11)},
		{"5", ptrInt(5)},
		{"notnum", nil},
		{true, nil},
	}
	for _, tc := range cases {
		req := reqWith(build(tc.in))
		got := optInt(req, "k")
		if !reflect.DeepEqual(got, tc.want) {
			t.Errorf("optInt(%#v) = %v, want %v", tc.in, deref2(got), deref2(tc.want))
		}
	}
	if optInt(reqWith(map[string]any{}), "absent") != nil {
		t.Errorf("absent key -> nil")
	}
}

func deref2(p *int) any {
	if p == nil {
		return nil
	}
	return *p
}

func TestOptStringAndDeref(t *testing.T) {
	req := reqWith(map[string]any{"s": "v", "n": 3})
	if got := optString(req, "s"); got == nil || *got != "v" {
		t.Errorf("optString present wrong: %v", got)
	}
	if optString(req, "n") != nil {
		t.Errorf("non-string arg -> nil")
	}
	if optString(req, "absent") != nil {
		t.Errorf("absent -> nil")
	}
	if deref(nil) != "" || deref(ptrStr("x")) != "x" {
		t.Errorf("deref wrong")
	}
}

func TestEventScreen(t *testing.T) {
	if eventScreen(map[string]any{}) != "" {
		t.Errorf("no data -> empty")
	}
	if eventScreen(map[string]any{"data": "notmap"}) != "" {
		t.Errorf("non-map data -> empty")
	}
	if eventScreen(map[string]any{"data": map[string]any{"screen": "hi"}}) != "hi" {
		t.Errorf("string screen wrong")
	}
	if eventScreen(map[string]any{"data": map[string]any{"screen": 123}}) != "123" {
		t.Errorf("non-string screen should stringify")
	}
	if eventScreen(map[string]any{"data": map[string]any{}}) != "" {
		t.Errorf("nil screen -> empty")
	}
}

func TestClampHelpers(t *testing.T) {
	if clampInt(5, 1, 10) != 5 || clampInt(-1, 1, 10) != 1 || clampInt(99, 1, 10) != 10 {
		t.Errorf("clampInt wrong")
	}
	if clampFloat(5, 1, 10) != 5 || clampFloat(0, 1, 10) != 1 || clampFloat(99, 1, 10) != 10 {
		t.Errorf("clampFloat wrong")
	}
}

func TestNormaliseRolesDedup(t *testing.T) {
	p := newPrincipal("x", "admin", "admin", "viewer")
	if !reflect.DeepEqual(p.Roles, []string{"admin", "viewer"}) {
		t.Fatalf("roles should dedup+sort: %#v", p.Roles)
	}
}

func TestScreenFieldNonString(t *testing.T) {
	if screenField(map[string]any{"screen": 5}) != "" {
		t.Fatalf("non-string screen -> empty")
	}
	if screenField(map[string]any{}) != "" {
		t.Fatalf("missing screen -> empty")
	}
}

func TestUnescapeUnicode(t *testing.T) {
	// "\\u0041\\u0042" is the 12-char sequence AB -> "AB".
	if got := unescapeKeys("\\u0041\\u0042"); got != "AB" {
		t.Fatalf("unicode escape: %q", got)
	}
	if got := unescapeKeys(`\uZZZZ`); got != `\uZZZZ` {
		t.Fatalf("bad unicode kept literal: %q", got)
	}
}

func TestPyStrReprControlChars(t *testing.T) {
	if got := pyStrRepr("\x00\x7f"); got != `'\x00\x7f'` {
		t.Fatalf("control repr: %q", got)
	}
	if got := pyStrRepr(`a\b`); got != `'a\\b'` {
		t.Fatalf("backslash repr: %q", got)
	}
}
