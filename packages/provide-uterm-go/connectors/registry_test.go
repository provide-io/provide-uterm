//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package connectors

import (
	"context"
	"reflect"
	"testing"
)

func TestRegisteredTypes(t *testing.T) {
	got := RegisteredTypes()
	want := []string{"shell", "ssh", "telnet", "websocket"}
	// want ⊆ got (custom registrations may add more).
	set := map[string]bool{}
	for _, g := range got {
		set[g] = true
	}
	for _, w := range want {
		if !set[w] {
			t.Fatalf("RegisteredTypes missing %q: %v", w, got)
		}
	}
	// Sorted.
	sorted := append([]string(nil), got...)
	if !reflect.DeepEqual(got, sortedCopy(sorted)) {
		t.Fatalf("RegisteredTypes not sorted: %v", got)
	}
}

func sortedCopy(s []string) []string {
	out := append([]string(nil), s...)
	for i := 1; i < len(out); i++ {
		for j := i; j > 0 && out[j-1] > out[j]; j-- {
			out[j-1], out[j] = out[j], out[j-1]
		}
	}
	return out
}

func TestBuildUnknownType(t *testing.T) {
	if _, err := Build("s", "n", "nonexistent", nil); err == nil {
		t.Fatal("expected unsupported connector_type error")
	}
}

func TestBuildEachBuiltin(t *testing.T) {
	cases := []struct {
		typ    string
		config map[string]any
	}{
		{"shell", nil},
		{"telnet", map[string]any{"host": "h", "port": 23}},
		{"ssh", map[string]any{"host": "h", "insecure_no_host_check": true}},
		{"websocket", map[string]any{"url": "ws://127.0.0.1:8/x"}},
	}
	for _, tc := range cases {
		c, err := Build("s", "n", tc.typ, tc.config)
		if err != nil || c == nil {
			t.Fatalf("Build(%s): %v %v", tc.typ, c, err)
		}
	}
}

func TestBuildRejectsUnknownConfigKeys(t *testing.T) {
	cases := []struct {
		typ    string
		config map[string]any
	}{
		{"shell", map[string]any{"bogus": 1}},
		{"telnet", map[string]any{"bogus": 1}},
		{"ssh", map[string]any{"insecure_no_host_check": true, "bogus": 1}},
		{"websocket", map[string]any{"url": "ws://h/x", "bogus": 1}},
	}
	for _, tc := range cases {
		if _, err := Build("s", "n", tc.typ, tc.config); err == nil {
			t.Fatalf("Build(%s) should reject unknown config key", tc.typ)
		}
	}
}

func TestRegisterCustomFactory(t *testing.T) {
	called := false
	Register("custom-fake", func(sessionID, displayName string, _ map[string]any) (Connector, error) {
		called = true
		c, _ := newShell(sessionID, displayName, nil)
		return c, nil
	})
	c, err := Build("s", "n", "custom-fake", nil)
	if err != nil || c == nil || !called {
		t.Fatalf("custom factory: %v %v called=%v", c, err, called)
	}
	// Custom type appears in RegisteredTypes.
	found := false
	for _, ty := range RegisteredTypes() {
		if ty == "custom-fake" {
			found = true
		}
	}
	if !found {
		t.Fatal("custom-fake missing from RegisteredTypes")
	}
}

func TestWebSocketConstructionErrors(t *testing.T) {
	cases := map[string]map[string]any{
		"missing url":  {},
		"bad scheme":   {"url": "http://example.com"},
		"missing host": {"url": "ws:///path"},
		"unparseable":  {"url": "ws://%zz"},
	}
	for name, cfg := range cases {
		if _, err := newWebSocket("s", "n", cfg); err == nil {
			t.Fatalf("%s: expected error", name)
		}
	}
}

func TestSSHConstructionPolicy(t *testing.T) {
	// known_hosts absent and not insecure → refuse.
	if _, err := newSSH("s", "n", map[string]any{"host": "h"}); err == nil {
		t.Fatal("ssh without known_hosts/insecure should be refused")
	}
	// known_hosts path accepted.
	if _, err := newSSH("s", "n", map[string]any{"host": "h", "known_hosts": "/etc/ssh/known_hosts"}); err != nil {
		t.Fatalf("ssh with known_hosts: %v", err)
	}
	// client_key_path rejected.
	if _, err := newSSH("s", "n", map[string]any{
		"host": "h", "insecure_no_host_check": true, "client_key_path": "/id",
	}); err == nil {
		t.Fatal("client_key_path should be rejected")
	}
	// client_key (PEM string) accepted.
	c, err := newSSH("s", "n", map[string]any{
		"host": "h", "insecure_no_host_check": true, "client_key": "PEMDATA",
	})
	if err != nil || c == nil {
		t.Fatalf("ssh with client_key: %v %v", c, err)
	}
	// client_key_data ([]byte) accepted.
	if _, err := newSSH("s", "n", map[string]any{
		"host": "h", "insecure_no_host_check": true, "client_key_data": []byte("PEM"),
	}); err != nil {
		t.Fatalf("ssh with client_key_data bytes: %v", err)
	}
	// client_key_data (string) accepted.
	if _, err := newSSH("s", "n", map[string]any{
		"host": "h", "insecure_no_host_check": true, "client_key_data": "PEM",
	}); err != nil {
		t.Fatalf("ssh with client_key_data string: %v", err)
	}
}

func TestConfigHelpers(t *testing.T) {
	cc := map[string]any{
		"s": "v", "empty": "", "i": 3, "i64": int64(4), "f": float64(5),
		"b": true, "list": []any{"a", "b"}, "strs": []string{"x"}, "one": "solo", "es": "",
	}
	if configStr(nil, "s", "fb") != "fb" || configStr(cc, "s", "fb") != "v" || configStr(cc, "empty", "fb") != "fb" {
		t.Fatal("configStr")
	}
	if configInt(nil, "i", 9) != 9 || configInt(cc, "i", 0) != 3 || configInt(cc, "i64", 0) != 4 ||
		configInt(cc, "f", 0) != 5 || configInt(cc, "s", 7) != 7 {
		t.Fatal("configInt")
	}
	if configBool(nil, "b", true) != true || configBool(cc, "b", false) != true || configBool(cc, "missing", true) != true {
		t.Fatal("configBool")
	}
	if got := configStrList(cc, "list"); len(got) != 2 || got[0] != "a" {
		t.Fatalf("configStrList []any: %v", got)
	}
	if got := configStrList(cc, "strs"); len(got) != 1 || got[0] != "x" {
		t.Fatalf("configStrList []string: %v", got)
	}
	if got := configStrList(cc, "one"); len(got) != 1 || got[0] != "solo" {
		t.Fatalf("configStrList string: %v", got)
	}
	if configStrList(nil, "x") != nil || configStrList(cc, "missing") != nil || configStrList(cc, "i") != nil {
		t.Fatal("configStrList absent/wrong-type should be nil")
	}
	if configStrList(cc, "es") != nil {
		t.Fatal("configStrList empty string should be nil")
	}
	if got := configStrList(map[string]any{"e": []any{1, 2}}, "e"); got != nil {
		t.Fatalf("configStrList non-string []any should be nil: %v", got)
	}
}

func TestBuildStartsRealShell(t *testing.T) {
	// End-to-end through Build: a shell connector starts and stops cleanly.
	c, err := Build("bs", "Build Shell", "shell", nil)
	if err != nil {
		t.Fatalf("Build shell: %v", err)
	}
	ctx := context.Background()
	if err := c.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	if !c.IsConnected() {
		t.Fatal("shell should be connected")
	}
	if err := c.Stop(ctx); err != nil {
		t.Fatalf("Stop: %v", err)
	}
}
