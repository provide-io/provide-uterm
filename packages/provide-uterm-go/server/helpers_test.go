//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestErrorTypes(t *testing.T) {
	if (&SessionValidationError{Msg: "v"}).Error() != "v" {
		t.Fatal("validation error")
	}
	if (&SessionConflictError{Msg: "c"}).Error() != "c" {
		t.Fatal("conflict error")
	}
	if (&EgressBlockedError{Msg: "e"}).Error() != "e" {
		t.Fatal("egress error")
	}
}

func TestSmallHelpers(t *testing.T) {
	if itoa(42) != "42" {
		t.Fatal("itoa")
	}
	if clampInt(5, 1, 3) != 3 || clampInt(0, 1, 3) != 1 || clampInt(2, 1, 3) != 2 {
		t.Fatal("clampInt")
	}
	if truncate("abcdef", 3) != "abc" || truncate("ab", 5) != "ab" {
		t.Fatal("truncate")
	}
	if strDefault("", "d") != "d" || strDefault("x", "d") != "x" {
		t.Fatal("strDefault")
	}
	if optString("") != nil || *optString("h") != "h" {
		t.Fatal("optString")
	}
	if !hasAllTags([]string{"a", "b"}, []string{"a"}) || hasAllTags([]string{"a"}, []string{"z"}) {
		t.Fatal("hasAllTags")
	}
	st := &SessionStatus{SessionID: "id", DisplayName: "Name", Tags: []string{"tag1"}}
	if !sessionMatchesSearch(st, "name") || !sessionMatchesSearch(st, "tag1") || sessionMatchesSearch(st, "zzz") {
		t.Fatal("sessionMatchesSearch")
	}
	if got := splitCSV("a, b ,,c"); len(got) != 3 {
		t.Fatalf("splitCSV: %v", got)
	}
	if len(stringList([]any{"x", " ", "y"})) != 2 || len(stringList("notlist")) != 0 {
		t.Fatal("stringList")
	}
}

func TestBridgeHelpers(t *testing.T) {
	hid := newHijackID()
	if !hijackIDPattern.MatchString(hid) {
		t.Fatalf("bad hijack id: %q", hid)
	}
	for _, c := range []struct{ in, want string }{
		{"no_worker", "No worker connected for this session."},
		{"already_hijacked", "Worker is already hijacked."},
		{"open_mode", "Hijack not available in open input mode."},
		{"weird", "weird"},
	} {
		if got := acquireErrorMessage(c.in); got != c.want {
			t.Fatalf("acquireErrorMessage(%q)=%q", c.in, got)
		}
	}
	for _, c := range []struct{ in, want string }{
		{"no_worker", "No worker connected for this session."},
		{"open_mode", "Hijack not available in open input mode."},
		{"already_hijacked", "Already hijacked by another client."},
	} {
		if got := wsHijackErrorMessage(c.in); got != c.want {
			t.Fatalf("wsHijackErrorMessage(%q)=%q", c.in, got)
		}
	}
	if extractPromptID(nil) != nil {
		t.Fatal("extractPromptID nil")
	}
	if extractPromptID(map[string]any{"prompt_detected": map[string]any{"prompt_id": "p1"}}) != "p1" {
		t.Fatal("extractPromptID value")
	}
	if extractPromptID(map[string]any{"prompt_detected": "nope"}) != nil {
		t.Fatal("extractPromptID non-map")
	}
	if intField(map[string]any{"x": 3}, "x", 0) != 3 || intField(nil, "x", 9) != 9 {
		t.Fatal("intField")
	}
}

func TestSourceIPAndQueryInt(t *testing.T) {
	r := httptest.NewRequest("GET", "/x?n=250", http.NoBody)
	r.RemoteAddr = "10.0.0.5:1234"
	if sourceIP(r) != "10.0.0.5" {
		t.Fatalf("sourceIP: %q", sourceIP(r))
	}
	r.RemoteAddr = ""
	if sourceIP(r) != "unknown" {
		t.Fatal("sourceIP empty")
	}
	if queryInt(r, "n", 10, 1, 100) != 100 { // clamped to max
		t.Fatal("queryInt clamp max")
	}
	if queryInt(r, "missing", 7, 1, 100) != 7 {
		t.Fatal("queryInt default")
	}
	r2 := httptest.NewRequest("GET", "/x?n=bad", http.NoBody)
	if queryInt(r2, "n", 5, 1, 100) != 5 {
		t.Fatal("queryInt unparseable")
	}
	r3 := httptest.NewRequest("GET", "/x?n=0", http.NoBody)
	if queryInt(r3, "n", 5, 2, 100) != 2 {
		t.Fatal("queryInt clamp min")
	}
}

func TestDecodeJSONBodyAndFrames(t *testing.T) {
	if m, ok := decodeJSONBody(httptest.NewRequest("POST", "/", http.NoBody)); !ok || len(m) != 0 {
		t.Fatal("empty body")
	}
	bad := httptest.NewRequest("POST", "/", strings.NewReader("{bad"))
	if _, ok := decodeJSONBody(bad); ok {
		t.Fatal("malformed body should fail")
	}
	// encodeControlMap / encodeFrameControl produce a decodable control frame.
	payload, err := encodeControlMap(map[string]any{"type": "ping"})
	if err != nil || !strings.Contains(payload, "ping") {
		t.Fatalf("encodeControlMap: %v %q", err, payload)
	}
}

func TestBearerToken(t *testing.T) {
	r := httptest.NewRequest("GET", "/", http.NoBody)
	r.Header.Set("Authorization", "Bearer tok123")
	if bearerToken(r) != "tok123" {
		t.Fatal("bearer parse")
	}
	r.Header.Set("Authorization", "Basic zzz")
	if bearerToken(r) != "" {
		t.Fatal("non-bearer")
	}
}

func TestServerAccessors(t *testing.T) {
	ts := newTestServer(t, nil)
	if ts.srv.Metrics() == nil {
		t.Fatal("Metrics nil")
	}
	if ts.srv.Addr() == "" {
		t.Fatal("Addr empty")
	}
}
