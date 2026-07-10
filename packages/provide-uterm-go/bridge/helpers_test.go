//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package bridge

import (
	"testing"
	"time"
)

func TestSafeInt(t *testing.T) {
	cases := []struct {
		name   string
		val    any
		def    int
		minVal int
		want   int
	}{
		{"nil", nil, 7, 0, 7},
		{"int", 3, 0, 0, 3},
		{"int64", int64(4), 0, 0, 4},
		{"float64 truncates", 5.9, 0, 0, 5},
		{"bool true", true, 0, 0, 1},
		{"bool false", false, 9, 0, 0}, // int(False) == 0, and 0 >= minVal 0
		{"string ok", "12", 0, 0, 12},
		{"string bad", "nope", 8, 0, 8},
		{"unsupported type", []int{1}, 6, 0, 6},
		{"below min", 2, 5, 3, 5},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := safeInt(tc.val, tc.def, tc.minVal); got != tc.want {
				t.Fatalf("safeInt(%v,%d,%d) = %d, want %d", tc.val, tc.def, tc.minVal, got, tc.want)
			}
		})
	}
}

func TestTruthy(t *testing.T) {
	cases := []struct {
		val  any
		want bool
	}{
		{nil, false},
		{true, true},
		{false, false},
		{0, false},
		{3, true},
		{int64(0), false},
		{int64(2), true},
		{0.0, false},
		{1.5, true},
		{"", false},
		{"x", true},
		{[]int{}, true},
	}
	for _, tc := range cases {
		if got := truthy(tc.val); got != tc.want {
			t.Fatalf("truthy(%v) = %v, want %v", tc.val, got, tc.want)
		}
	}
}

func TestSnapHelpers(t *testing.T) {
	m := map[string]any{
		"screen":        "hi",
		"cols":          float64(132),
		"rows":          float64(0), // falsy → default
		"cursor_at_end": false,
		"cursor":        map[string]any{"x": 1, "y": 2},
	}
	if snapString(m, "screen", "d") != "hi" {
		t.Fatal("snapString present")
	}
	if snapString(m, "missing", "d") != "d" {
		t.Fatal("snapString default")
	}
	if snapInt(m, "cols", 80) != 132 {
		t.Fatal("snapInt present")
	}
	if snapInt(m, "rows", 25) != 25 {
		t.Fatal("snapInt falsy zero → default")
	}
	if snapInt(m, "missing", 25) != 25 {
		t.Fatal("snapInt missing → default")
	}
	if snapBool(m, "cursor_at_end", true) != false {
		t.Fatal("snapBool present false")
	}
	if snapBool(m, "missing", true) != true {
		t.Fatal("snapBool missing → default")
	}
	if _, ok := snapCursor(m).(map[string]any); !ok {
		t.Fatal("snapCursor present")
	}
	def := snapCursor(map[string]any{}).(map[string]any)
	if def["x"] != 0 || def["y"] != 0 {
		t.Fatalf("snapCursor default = %v", def)
	}
	// nil value falls back to default cursor.
	if _, ok := snapCursor(map[string]any{"cursor": nil}).(map[string]any); !ok {
		t.Fatal("snapCursor nil → default")
	}
}

func TestToWSURL(t *testing.T) {
	cases := []struct {
		manager string
		path    string
		want    string
	}{
		{"http://localhost:8000", "/ws/worker/bot1/term", "ws://localhost:8000/ws/worker/bot1/term"},
		{"https://manager.example.com", "/path", "wss://manager.example.com/path"},
		{"http://host:8000/", "/path", "ws://host:8000/path"},
		{"ftp://host", "/p", "ftp://host/p"}, // untouched scheme
	}
	for _, tc := range cases {
		if got := toWSURL(tc.manager, tc.path); got != tc.want {
			t.Fatalf("toWSURL(%q,%q) = %q, want %q", tc.manager, tc.path, got, tc.want)
		}
	}
}

func TestIsMalformedWSURL(t *testing.T) {
	cases := []struct {
		url  string
		want bool
	}{
		{"ws://host/p", false},
		{"wss://host/p", false},
		{"ftp://host/p", true},
		{"://bad", true}, // url.Parse error (missing scheme)
	}
	for _, tc := range cases {
		if got := isMalformedWSURL(tc.url); got != tc.want {
			t.Fatalf("isMalformedWSURL(%q) = %v, want %v", tc.url, got, tc.want)
		}
	}
}

func TestHandlePermanentError(t *testing.T) {
	for _, status := range []int{401, 403, 404} {
		b := New(Config{Worker: &mockWorker{}, WorkerID: "w", ManagerURL: "http://x"})
		b.setRunning(true)
		if !b.handlePermanentError(status, false) {
			t.Fatalf("status %d should be permanent", status)
		}
		if b.isRunning() {
			t.Fatalf("status %d should stop the bridge", status)
		}
	}
	// Malformed URL is permanent.
	b := New(Config{Worker: &mockWorker{}, WorkerID: "w", ManagerURL: "http://x"})
	b.setRunning(true)
	if !b.handlePermanentError(0, true) {
		t.Fatal("malformed URL should be permanent")
	}
	if b.isRunning() {
		t.Fatal("malformed URL should stop the bridge")
	}
	// A transient status (e.g. 500) is not permanent.
	b2 := New(Config{Worker: &mockWorker{}, WorkerID: "w", ManagerURL: "http://x"})
	b2.setRunning(true)
	if b2.handlePermanentError(500, false) {
		t.Fatal("status 500 should not be permanent")
	}
	if !b2.isRunning() {
		t.Fatal("transient error should keep the bridge running")
	}
}

func TestNewDefaults(t *testing.T) {
	// Zero-valued optional fields select the documented defaults.
	b := New(Config{Worker: &mockWorker{}, WorkerID: "w", ManagerURL: "http://x"})
	if b.inputMode != "open" {
		t.Fatalf("inputMode default = %q, want open", b.inputMode)
	}
	if b.maxWSMessageBytes != 1_048_576 {
		t.Fatalf("maxWSMessageBytes default = %d", b.maxWSMessageBytes)
	}
	if b.dialTimeout != 30*time.Second {
		t.Fatalf("dialTimeout default = %v", b.dialTimeout)
	}
	if b.logger == nil {
		t.Fatal("logger should default to the telemetry logger")
	}
	// Explicit values win, and a too-small maxWSMessageBytes is floored.
	b2 := New(Config{
		Worker: &mockWorker{}, WorkerID: "w", ManagerURL: "http://x",
		InputMode: "hijack", MaxWSMessageBytes: 10, DialTimeout: time.Second,
	})
	if b2.inputMode != "hijack" || b2.maxWSMessageBytes != 1_048_576 || b2.dialTimeout != time.Second {
		t.Fatalf("explicit config not applied: %+v", b2)
	}
}
