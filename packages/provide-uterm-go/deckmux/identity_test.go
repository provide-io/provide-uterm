//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package deckmux

import (
	"testing"
)

func TestParseIdentityFrameHappyPath(t *testing.T) {
	frame := map[string]any{
		"type": "identity", "version": 1, "subject": "sre:alice",
		"claims":      map[string]any{"role": "oncall", "display_name": "Alice Liddell"},
		"fingerprint": "SHA256:abc", "transport": "ssh",
	}
	got := ParseIdentityFrame(frame, nil)
	if got == nil || got.Subject != "sre:alice" || got.Fingerprint != "SHA256:abc" {
		t.Fatalf("parse: %+v", got)
	}
	if got.Claims["role"] != "oncall" || got.Claims["display_name"] != "Alice Liddell" {
		t.Errorf("claims: %v", got.Claims)
	}
}

func TestParseIdentityFrameRejections(t *testing.T) {
	cases := []struct {
		name  string
		frame map[string]any
	}{
		{"wrong type", map[string]any{"type": "resume", "subject": "x"}},
		{"missing type", map[string]any{"subject": "x"}},
		{"unknown version", map[string]any{"type": "identity", "version": 99, "subject": "x"}},
		{"missing version", map[string]any{"type": "identity", "subject": "x"}},
		{"missing subject", map[string]any{"type": "identity", "version": 1}},
		{"empty subject", map[string]any{"type": "identity", "version": 1, "subject": ""}},
		{"non-string subject", map[string]any{"type": "identity", "version": 1, "subject": 42}},
		{"non-integral version", map[string]any{"type": "identity", "version": 1.5, "subject": "x"}},
		{"string version", map[string]any{"type": "identity", "version": "1", "subject": "x"}},
	}
	for _, c := range cases {
		if got := ParseIdentityFrame(c.frame, nil); got != nil {
			t.Errorf("%s: expected nil, got %+v", c.name, got)
		}
	}
}

func TestParseIdentityFrameVersionForms(t *testing.T) {
	for _, v := range []any{1, int64(1), float64(1)} {
		frame := map[string]any{"type": "identity", "version": v, "subject": "x"}
		if ParseIdentityFrame(frame, nil) == nil {
			t.Errorf("version %#v (%T) should be accepted", v, v)
		}
	}
}

func TestParseIdentityFrameClaimsAndFingerprintCoercion(t *testing.T) {
	// Malformed claims → empty map.
	f := map[string]any{"type": "identity", "version": 1, "subject": "x", "claims": "not a dict"}
	got := ParseIdentityFrame(f, nil)
	if got == nil || len(got.Claims) != 0 {
		t.Errorf("malformed claims: %+v", got)
	}
	// Missing claims → empty map.
	got = ParseIdentityFrame(map[string]any{"type": "identity", "version": 1, "subject": "x"}, nil)
	if got == nil || got.Claims == nil || len(got.Claims) != 0 || got.Fingerprint != "" {
		t.Errorf("missing claims/fingerprint: %+v", got)
	}
	// Non-string fingerprint → "".
	f = map[string]any{"type": "identity", "version": 1, "subject": "x", "fingerprint": 12345}
	if got := ParseIdentityFrame(f, nil); got == nil || got.Fingerprint != "" {
		t.Errorf("non-string fingerprint: %+v", got)
	}
}

func TestParseIdentityFrameSignature(t *testing.T) {
	// Secret required but signature missing.
	f := map[string]any{"type": "identity", "version": 1, "subject": "x"}
	if ParseIdentityFrame(f, []byte("my-secret")) != nil {
		t.Error("missing signature must reject")
	}
	// Invalid signature.
	f = map[string]any{"type": "identity", "version": 1, "subject": "x", "signature": "bad"}
	if ParseIdentityFrame(f, []byte("my-secret")) != nil {
		t.Error("invalid signature must reject")
	}
}

// TestGoldenSignedIdentity verifies HMAC-signed frames from the real Python
// make_identity — proving the canonical-string + claims-JSON port is exact.
func TestGoldenSignedIdentity(t *testing.T) {
	var cases []struct {
		Secret string         `json:"secret"`
		Frame  map[string]any `json:"frame"`
	}
	goldenCase(t, "signed_identity", &cases)
	if len(cases) == 0 {
		t.Fatal("no signed_identity golden")
	}
	for _, c := range cases {
		got := ParseIdentityFrame(c.Frame, []byte(c.Secret))
		if got == nil {
			t.Errorf("valid signed frame rejected: %v", c.Frame)
			continue
		}
		// Wrong secret must reject.
		if ParseIdentityFrame(c.Frame, []byte(c.Secret+"x")) != nil {
			t.Errorf("wrong secret accepted for subject %q", got.Subject)
		}
		// Tampered subject must reject.
		tampered := cloneFrame(c.Frame)
		tampered["subject"] = got.Subject + "tampered"
		if ParseIdentityFrame(tampered, []byte(c.Secret)) != nil {
			t.Errorf("tampered subject accepted")
		}
	}
}

func TestGoldenPresenceFromIdentity(t *testing.T) {
	var cases []struct {
		Subject      string         `json:"subject"`
		Claims       map[string]any `json:"claims"`
		ConnectionID string         `json:"connection_id"`
		Role         string         `json:"role"`
		Result       map[string]any `json:"result"`
	}
	goldenCase(t, "presence_from_identity", &cases)
	if len(cases) == 0 {
		t.Fatal("no presence_from_identity golden")
	}
	for _, c := range cases {
		id := &ResolvedIdentity{Subject: c.Subject, Claims: c.Claims}
		p := PresenceFromIdentity(id, c.ConnectionID, nil, c.Role)
		jsonEqualValue(t, c.Subject, p.ToDict(), c.Result)
	}
}

func TestPresenceFromIdentityNonStringClaims(t *testing.T) {
	id := &ResolvedIdentity{Subject: "sre:alice", Claims: map[string]any{
		"display_name": 42, "display": nil, "role": []any{"a", "b"},
	}}
	p := PresenceFromIdentity(id, "c", nil, "")
	if p.Name != "alice" { // falls through to subject tail
		t.Errorf("name = %q", p.Name)
	}
	if p.Role != "" {
		t.Errorf("role = %q", p.Role)
	}
	if p.UserID != "sre:alice" {
		t.Errorf("user_id = %q", p.UserID)
	}
}

func TestPresenceFromIdentityColorRespectsTaken(t *testing.T) {
	def := GenerateColor("probe-conn", nil)
	id := &ResolvedIdentity{Subject: "x", Claims: map[string]any{}}
	p := PresenceFromIdentity(id, "probe-conn", nil, "")
	if p.Color != def {
		t.Errorf("empty taken color = %q, want %q", p.Color, def)
	}
}

func TestIdentityAsPrincipal(t *testing.T) {
	id := &ResolvedIdentity{Subject: "sre:alice", Claims: map[string]any{"display_name": "Alice Liddell"}}
	p := IdentityAsPrincipal(id)
	if p.SubjectID() != "sre:alice" || p.DisplayName() != "Alice Liddell" {
		t.Errorf("principal: %q / %q", p.SubjectID(), p.DisplayName())
	}
	if p.Identity != id {
		t.Error("identity not preserved")
	}
	// Fallback to subject tail.
	if IdentityAsPrincipal(&ResolvedIdentity{Subject: "sre:alice"}).DisplayName() != "alice" {
		t.Error("tail fallback")
	}
	// Fallback to full subject (no tail).
	if IdentityAsPrincipal(&ResolvedIdentity{Subject: "alice"}).DisplayName() != "alice" {
		t.Error("full subject fallback")
	}
	// Fallback to subject when tail is empty.
	if got := IdentityAsPrincipal(&ResolvedIdentity{Subject: "role:"}).DisplayName(); got != "role:" {
		t.Errorf("empty-tail fallback = %q", got)
	}
}

func TestIdentityHelpers(t *testing.T) {
	if firstNonempty("", "", "x") != "x" || firstNonempty("", "") != "" {
		t.Error("firstNonempty")
	}
	if strOrNone(42) != "" || strOrNone("  x  ") != "x" || strOrNone("   ") != "" {
		t.Error("strOrNone")
	}
	if nameFromSubject("alice") != "alice" || nameFromSubject("sre:alice") != "alice" ||
		nameFromSubject("role:") != "" {
		t.Error("nameFromSubject")
	}
}

func TestPythonCompactJSON(t *testing.T) {
	cases := []struct {
		in   any
		want string
	}{
		{map[string]any{}, "{}"},
		{map[string]any{"role": "oncall", "display_name": "Alice"}, `{"display_name":"Alice","role":"oncall"}`},
		{nil, "null"},
		{true, "true"},
		{false, "false"},
		{int(5), "5"},
		{int64(7), "7"},
		{float64(3.0), "3"},
		{float64(1.5), "1.5"},
		{[]any{1, "a", true, nil}, `[1,"a",true,null]`},
		{map[string]any{"n": map[string]any{"a": 1}}, `{"n":{"a":1}}`},
		{"héllo", "\"h\\u00e9llo\""},               // ensure_ascii escapes non-ASCII
		{"\x01", "\"\\u0001\""},                    // control char escaped
		{"line\nbreak\ttab", `"line\nbreak\ttab"`}, // short escapes
		{"quote\"back\\slash", `"quote\"back\\slash"`},
		{"\b\f\r", `"\b\f\r"`},
		{"😀", "\"\\ud83d\\ude00\""},   // surrogate pair above U+FFFF
		{"del\x7f", "\"del\\u007f\""}, // 0x7f escaped (outside 0x20..0x7e)
		{struct{ X int }{1}, "null"},  // unsupported type → null
	}
	for _, c := range cases {
		if got := pythonCompactJSON(c.in); got != c.want {
			t.Errorf("pythonCompactJSON(%#v) = %q, want %q", c.in, got, c.want)
		}
	}
}

// --- helpers ---

func cloneFrame(f map[string]any) map[string]any {
	out := make(map[string]any, len(f))
	for k, v := range f {
		out[k] = v
	}
	return out
}

func jsonEqualValue(t *testing.T, label string, got, want any) {
	t.Helper()
	gj := mustJSON(t, got)
	wj := mustJSON(t, want)
	if gj != wj {
		t.Errorf("%s mismatch:\n  got:  %s\n  want: %s", label, gj, wj)
	}
}
