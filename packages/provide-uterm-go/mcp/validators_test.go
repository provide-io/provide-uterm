//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package mcp

import (
	"reflect"
	"strings"
	"testing"
)

func ptrStr(s string) *string { return &s }
func ptrInt(i int) *int       { return &i }

func TestPyStrRepr(t *testing.T) {
	cases := map[string]string{
		"":          "''",
		"../etc":    "'../etc'",
		"a/b":       "'a/b'",
		"a'b":       `"a'b"`, // contains ' but no " -> double-quoted
		"a'\"b":     `'a\'"b'`,
		"tab\there": `'tab\there'`,
		"nl\n":      `'nl\n'`,
	}
	for in, want := range cases {
		if got := pyStrRepr(in); got != want {
			t.Errorf("pyStrRepr(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestCheckSafeIDMessages(t *testing.T) {
	if err := checkSafeID("good.id-1", "worker_id"); err != nil {
		t.Fatalf("valid id rejected: %v", err)
	}
	bad := map[string]string{
		"":     "invalid worker_id: ''",
		".":    "invalid worker_id: '.'",
		"..":   "invalid worker_id: '..'",
		"../x": "invalid worker_id: '../x'",
		"a/b":  "invalid worker_id: 'a/b'",
		"a b":  "invalid worker_id: 'a b'",
	}
	for in, want := range bad {
		err := checkSafeID(in, "worker_id")
		if err == nil || err.Error() != want {
			t.Errorf("checkSafeID(%q) = %v, want %q", in, err, want)
		}
	}
}

func TestRejectBadID(t *testing.T) {
	if r := rejectBadID("ok", "session_id"); r != nil {
		t.Fatalf("valid id rejected: %#v", r)
	}
	r := rejectBadID("..", "session_id")
	if r["error"] != "invalid_id" || r["detail"] != "invalid session_id: '..'" || r["success"] != false {
		t.Fatalf("unexpected rejection: %#v", r)
	}
}

func TestRejectBadIDsOrder(t *testing.T) {
	r := rejectBadIDs(idPair{"ok", "worker_id"}, idPair{"../bad", "hijack_id"})
	if r == nil || r["detail"] != "invalid hijack_id: '../bad'" {
		t.Fatalf("expected second id rejection, got %#v", r)
	}
	if rejectBadIDs(idPair{"a", "worker_id"}, idPair{"b", "hijack_id"}) != nil {
		t.Fatalf("all-valid ids should not reject")
	}
}

func TestCompileUserPattern(t *testing.T) {
	if _, err := compileUserPattern("hello.*"); err != nil {
		t.Fatalf("valid pattern rejected: %v", err)
	}
	long := strings.Repeat("a", MaxUserPatternLen+1)
	if _, err := compileUserPattern(long); err == nil || err.Error() != "pattern too long (max 512 chars)" {
		t.Fatalf("length guard wrong: %v", err)
	}
	_, err := compileUserPattern("(a+)+")
	want := "pattern rejected: catastrophic-backtracking construct (nested quantifier or quantified backreference)"
	if err == nil || err.Error() != want {
		t.Fatalf("catastrophic guard wrong: %v", err)
	}
	if _, err := compileUserPattern("("); err == nil || !strings.HasPrefix(err.Error(), "invalid pattern:") {
		t.Fatalf("malformed pattern should surface invalid pattern: %v", err)
	}
}

func TestRejectBadPattern(t *testing.T) {
	if rejectBadPattern(nil) != nil {
		t.Fatalf("nil pattern must be allowed")
	}
	if rejectBadPattern(ptrStr("ok")) != nil {
		t.Fatalf("valid pattern must be allowed")
	}
	r := rejectBadPattern(ptrStr("(a+)+"))
	if r["error"] != "invalid_pattern" || !strings.Contains(r["detail"].(string), "catastrophic") {
		t.Fatalf("unexpected pattern rejection: %#v", r)
	}
}

func TestCompiledPatternOrRejection(t *testing.T) {
	re, rej := compiledPatternOrRejection(nil)
	if re != nil || rej != nil {
		t.Fatalf("nil pattern -> (nil,nil)")
	}
	re, rej = compiledPatternOrRejection(ptrStr("ab+"))
	if re == nil || rej != nil {
		t.Fatalf("valid pattern should compile: %v %v", re, rej)
	}
	re, rej = compiledPatternOrRejection(ptrStr("(a*)*"))
	if re != nil || rej == nil {
		t.Fatalf("bad pattern -> (nil, rejection)")
	}
}

func TestPySplitlines(t *testing.T) {
	got := pySplitlines("a\nb\r\nc\rd")
	want := []string{"a", "b", "c", "d"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("pySplitlines = %#v, want %#v", got, want)
	}
	if pySplitlines("trailing\n")[0] != "trailing" || len(pySplitlines("trailing\n")) != 1 {
		t.Fatalf("trailing newline must not add an empty element")
	}
}

func TestTrimTail(t *testing.T) {
	if trimTail("a\nb\nc", nil) != "a\nb\nc" {
		t.Fatalf("nil tail is a no-op")
	}
	if trimTail("a\nb\nc\nd", ptrInt(2)) != "c\nd" {
		t.Fatalf("tail=2 wrong")
	}
	if trimTail("a\nb", ptrInt(9)) != "a\nb" {
		t.Fatalf("tail larger than content is a no-op")
	}
	if trimTail("a\nb", ptrInt(0)) != "a\nb" {
		t.Fatalf("tail=0 is a no-op")
	}
}

func TestCleanSnapshotModes(t *testing.T) {
	snap := map[string]any{"screen": "\x1b[31mhi\x1b[0m\nthere", "cursor": 1, "cols": 80, "rows": 24, "extra": "keep"}

	text := cleanSnapshot(snap, "text", nil)
	if text["screen"] != "hi\nthere" || len(text) != 1 {
		t.Fatalf("text mode wrong: %#v", text)
	}
	rendered := cleanSnapshot(snap, "rendered", nil)
	if rendered["screen"] != "hi\nthere" || rendered["cursor"] != 1 || rendered["cols"] != 80 || rendered["rows"] != 24 {
		t.Fatalf("rendered mode wrong: %#v", rendered)
	}
	if _, ok := rendered["extra"]; ok {
		t.Fatalf("rendered must drop non-layout fields")
	}
	raw := cleanSnapshot(snap, "raw", nil)
	if raw["extra"] != "keep" || !strings.Contains(raw["screen"].(string), "\x1b[31m") {
		t.Fatalf("raw mode must preserve everything: %#v", raw)
	}
	rawTail := cleanSnapshot(map[string]any{"screen": "1\n2\n3", "k": "v"}, "raw", ptrInt(1))
	if rawTail["screen"] != "3" || rawTail["k"] != "v" {
		t.Fatalf("raw+tail wrong: %#v", rawTail)
	}
	textTail := cleanSnapshot(map[string]any{"screen": "1\n2\n3"}, "text", ptrInt(2))
	if textTail["screen"] != "2\n3" {
		t.Fatalf("text+tail wrong: %#v", textTail)
	}
}

func TestValidateSessionCreateConfig(t *testing.T) {
	if r := validateSessionCreateConfig("shell", nil, nil, nil); r != nil {
		t.Fatalf("shell should be allowed: %#v", r)
	}
	if r := validateSessionCreateConfig("rce", nil, nil, nil); r["error"] != "invalid_connector_type" || r["connector_type"] != "rce" {
		t.Fatalf("bad connector: %#v", r)
	}
	if r := validateSessionCreateConfig("ssh", nil, ptrInt(0), nil); r["error"] != "invalid_port" || r["port"] != 0 {
		t.Fatalf("port 0: %#v", r)
	}
	if r := validateSessionCreateConfig("ssh", nil, ptrInt(70000), nil); r["error"] != "invalid_port" {
		t.Fatalf("port 70000: %#v", r)
	}
	if r := validateSessionCreateConfig("ssh", ptrStr("file:///etc/passwd"), nil, nil); r["error"] != "invalid_url_scheme" || r["scheme"] != "file" {
		t.Fatalf("file scheme: %#v", r)
	}
	if r := validateSessionCreateConfig("ssh", ptrStr("noscheme"), nil, nil); r["scheme"] != "<missing>" {
		t.Fatalf("missing scheme: %#v", r)
	}
	if r := validateSessionCreateConfig("ws", ptrStr("ws://localhost:9000/x"), nil, nil); r["error"] != "invalid_host" || r["host"] != "localhost" {
		t.Fatalf("url host SSRF: %#v", r)
	}
	if r := validateSessionCreateConfig("ssh", nil, nil, ptrStr("10.0.0.5")); r["error"] != "invalid_host" || r["host"] != "10.0.0.5" {
		t.Fatalf("direct host SSRF: %#v", r)
	}
	if r := validateSessionCreateConfig("ssh", ptrStr("ssh://example.com:22"), ptrInt(22), ptrStr("example.com")); r != nil {
		t.Fatalf("public host should be allowed: %#v", r)
	}
}

func TestURLHostname(t *testing.T) {
	if urlHostname("http://Example.COM:80/x") != "example.com" {
		t.Fatalf("hostname should be lowercased and port-stripped")
	}
	if urlHostname("://::::bad") != "" {
		t.Fatalf("unparseable url -> empty host")
	}
}
