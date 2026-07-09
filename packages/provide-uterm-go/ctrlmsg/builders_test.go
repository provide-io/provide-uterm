//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package ctrlmsg

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"reflect"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlchannel"
)

// roundTrip encodes payload via controlchannel.EncodeControlFrame and decodes
// it back, mirroring the Python _round_trip helper.
func roundTrip(t *testing.T, payload map[string]any) map[string]any {
	t.Helper()
	encoded, err := controlchannel.EncodeControlFrame(payload)
	if err != nil {
		t.Fatalf("encode: %v", err)
	}
	dec := controlchannel.NewDecoder(controlchannel.DecoderOptions{})
	chunks, err := dec.Feed(encoded)
	if err != nil {
		t.Fatalf("feed: %v", err)
	}
	if len(chunks) != 1 {
		t.Fatalf("got %d chunks, want 1", len(chunks))
	}
	cc, ok := chunks[0].(controlchannel.ControlChunk)
	if !ok {
		t.Fatalf("chunk is %T, want ControlChunk", chunks[0])
	}
	return cc.Control
}

// mustMap unwraps a builder's (map, error) result, panicking on error so it
// can be composed as mustMap(MakeX(...)); a panic fails the running test.
func mustMap(m map[string]any, err error) map[string]any {
	if err != nil {
		panic("builder error: " + err.Error())
	}
	return m
}

// ---------------------------------------------------------------------------
// MakeIdentity
// ---------------------------------------------------------------------------

func TestMakeIdentityDefaultShape(t *testing.T) {
	got := mustMap(MakeIdentity("user:alice"))
	want := map[string]any{
		"type": "identity", "version": 1, "subject": "user:alice",
		"fingerprint": "", "transport": "ssh",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	if _, ok := got["claims"]; ok {
		t.Error("claims must be absent by default")
	}
}

func TestMakeIdentityFull(t *testing.T) {
	got := mustMap(MakeIdentity("user:bob",
		WithClaims(map[string]any{"role": "admin", "org": "acme"}),
		WithFingerprint("SHA256:abc123"), WithTransport("ws")))
	if got["subject"] != "user:bob" || got["fingerprint"] != "SHA256:abc123" || got["transport"] != "ws" {
		t.Fatalf("unexpected fields: %v", got)
	}
	if !reflect.DeepEqual(got["claims"], map[string]any{"role": "admin", "org": "acme"}) {
		t.Fatalf("claims = %v", got["claims"])
	}
}

func TestMakeIdentityClaimsCopied(t *testing.T) {
	original := map[string]any{"role": "user"}
	msg := mustMap(MakeIdentity("user:x", WithClaims(original)))
	msg["claims"].(map[string]any)["extra"] = "injected"
	if _, leaked := original["extra"]; leaked {
		t.Error("claims dict must be a fresh copy")
	}
}

func TestMakeIdentityEmptySubject(t *testing.T) {
	_, err := MakeIdentity("")
	if err == nil || err.Error() != "make_identity: 'subject' must be a non-empty string" {
		t.Fatalf("err = %v", err)
	}
}

func TestMakeIdentityFreshEachCall(t *testing.T) {
	a := mustMap(MakeIdentity("user:x"))
	b := mustMap(MakeIdentity("user:x"))
	a["mutated"] = true
	if _, ok := b["mutated"]; ok {
		t.Error("builder must return a fresh map each call")
	}
}

func TestMakeIdentityRoundTrip(t *testing.T) {
	payload := mustMap(MakeIdentity("user:alice", WithClaims(map[string]any{"role": "admin"}), WithFingerprint("fp1")))
	got := roundTrip(t, payload)
	if got["type"] != "identity" || got["subject"] != "user:alice" {
		t.Fatalf("round-trip lost fields: %v", got)
	}
	if !reflect.DeepEqual(got["claims"], map[string]any{"role": "admin"}) {
		t.Fatalf("claims = %v", got["claims"])
	}
}

func TestMakeIdentitySignatureStringSecret(t *testing.T) {
	msg := mustMap(MakeIdentity("user:alice",
		WithClaims(map[string]any{"scope": []any{"write", "read"}, "role": "admin"}),
		WithFingerprint("SHA256:abc"), WithTransport("ws"), WithSecret([]byte("proxy-secret"))))
	expectedPayload := []byte(`1:user:alice:SHA256:abc:ws:{"role":"admin","scope":["write","read"]}`)
	mac := hmac.New(sha256.New, []byte("proxy-secret"))
	mac.Write(expectedPayload)
	want := map[string]any{
		"type": "identity", "version": 1, "subject": "user:alice",
		"fingerprint": "SHA256:abc", "transport": "ws",
		"claims":    map[string]any{"scope": []any{"write", "read"}, "role": "admin"},
		"signature": hex.EncodeToString(mac.Sum(nil)),
	}
	if !reflect.DeepEqual(msg, want) {
		t.Fatalf("got %v\nwant %v", msg, want)
	}
}

func TestMakeIdentitySignatureNoClaims(t *testing.T) {
	msg := mustMap(MakeIdentity("user:alice", WithFingerprint("fp"), WithTransport("ssh"), WithSecret([]byte("proxy-secret"))))
	mac := hmac.New(sha256.New, []byte("proxy-secret"))
	mac.Write([]byte("1:user:alice:fp:ssh:{}"))
	if _, ok := msg["claims"]; ok {
		t.Error("claims must be absent")
	}
	if msg["signature"] != hex.EncodeToString(mac.Sum(nil)) {
		t.Fatalf("signature = %v", msg["signature"])
	}
}

func TestMakeIdentityEmptySecretUnsigned(t *testing.T) {
	msg := mustMap(MakeIdentity("user:alice", WithSecret([]byte(""))))
	if _, ok := msg["signature"]; ok {
		t.Error("empty secret must not sign")
	}
}

func TestMakeIdentityNilClaimsPresentAsEmpty(t *testing.T) {
	// WithClaims(nil) marks claims as provided; it renders as an empty object
	// and signs {}.
	msg := mustMap(MakeIdentity("user:x", WithClaims(nil), WithSecret([]byte("k"))))
	if !reflect.DeepEqual(msg["claims"], map[string]any{}) {
		t.Fatalf("claims = %v, want empty map", msg["claims"])
	}
	mac := hmac.New(sha256.New, []byte("k"))
	mac.Write([]byte("1:user:x::ssh:{}"))
	if msg["signature"] != hex.EncodeToString(mac.Sum(nil)) {
		t.Fatalf("signature = %v", msg["signature"])
	}
}

func TestMakeIdentityUnencodableClaimsWithSecret(t *testing.T) {
	// A claims value the canonical encoder cannot serialise surfaces as an
	// error only when signing is requested (the payload must be built).
	_, err := MakeIdentity("user:x",
		WithClaims(map[string]any{"bad": make(chan int)}), WithSecret([]byte("k")))
	if err == nil || !strings.Contains(err.Error(), "cannot encode claims") {
		t.Fatalf("err = %v", err)
	}
}

// ---------------------------------------------------------------------------
// MakeSessionToken / MakeResume
// ---------------------------------------------------------------------------

func TestMakeSessionToken(t *testing.T) {
	msg := mustMap(MakeSessionToken("tok-abc", nil))
	if msg["type"] != "session_token" || msg["token"] != "tok-abc" {
		t.Fatalf("unexpected: %v", msg)
	}
	if _, ok := msg["player_id"]; ok {
		t.Error("player_id must be absent when nil")
	}

	pid := 42
	msg = mustMap(MakeSessionToken("tok-xyz", &pid))
	if msg["player_id"] != 42 {
		t.Fatalf("player_id = %v", msg["player_id"])
	}

	zero := 0
	msg = mustMap(MakeSessionToken("tok", &zero))
	if msg["player_id"] != 0 {
		t.Errorf("player_id 0 must be included, got %v", msg["player_id"])
	}

	if _, err := MakeSessionToken("", nil); err == nil ||
		err.Error() != "make_session_token: 'token' must be a non-empty string" {
		t.Fatalf("err = %v", err)
	}

	got := roundTrip(t, mustMap(MakeSessionToken("tok-rt", &pid)))
	if got["type"] != "session_token" || got["token"] != "tok-rt" {
		t.Fatalf("round-trip: %v", got)
	}
}

func TestMakeResume(t *testing.T) {
	msg := mustMap(MakeResume("resume-tok", nil))
	if msg["type"] != "resume" || msg["token"] != "resume-tok" {
		t.Fatalf("unexpected: %v", msg)
	}
	if _, ok := msg["player_id"]; ok {
		t.Error("player_id must be absent when nil")
	}

	pid := 15
	msg = mustMap(MakeResume("resume-tok", &pid))
	if msg["player_id"] != 15 {
		t.Fatalf("player_id = %v", msg["player_id"])
	}

	if _, err := MakeResume("", nil); err == nil ||
		err.Error() != "make_resume: 'token' must be a non-empty string" {
		t.Fatalf("err = %v", err)
	}

	got := roundTrip(t, mustMap(MakeResume("resume-rt", nil)))
	if got["type"] != "resume" || got["token"] != "resume-rt" {
		t.Fatalf("round-trip: %v", got)
	}
}

// ---------------------------------------------------------------------------
// MakeResumeOk / MakeResumeFailed
// ---------------------------------------------------------------------------

func TestMakeResumeOk(t *testing.T) {
	if !reflect.DeepEqual(MakeResumeOk(), map[string]any{"type": "resume_ok"}) {
		t.Fatal("resume_ok shape")
	}
	a, b := MakeResumeOk(), MakeResumeOk()
	a["x"] = 1
	if _, ok := b["x"]; ok {
		t.Error("must return a fresh map")
	}
	got := roundTrip(t, MakeResumeOk())
	if !reflect.DeepEqual(got, map[string]any{"type": "resume_ok"}) {
		t.Fatalf("round-trip: %v", got)
	}
}

func TestMakeResumeFailed(t *testing.T) {
	msg := MakeResumeFailed(nil)
	if msg["type"] != "resume_failed" {
		t.Fatalf("type = %v", msg["type"])
	}
	if _, ok := msg["reason"]; ok {
		t.Error("reason must be absent when nil")
	}

	reason := "token expired"
	msg = MakeResumeFailed(&reason)
	if msg["reason"] != "token expired" {
		t.Fatalf("reason = %v", msg["reason"])
	}

	empty := ""
	msg = MakeResumeFailed(&empty)
	if r, ok := msg["reason"]; !ok || r != "" {
		t.Errorf("empty reason must be included, got %v ok=%v", r, ok)
	}
}

// ---------------------------------------------------------------------------
// MakeLinkPatterns
// ---------------------------------------------------------------------------

func TestMakeLinkPatternsBasics(t *testing.T) {
	msg := mustMap(MakeLinkPatterns([]map[string]any{{"pattern": `\bsector\b`, "action": "cmd"}}))
	if msg["type"] != "link_patterns" {
		t.Fatalf("type = %v", msg["type"])
	}
	patterns := msg["patterns"].([]any)
	if len(patterns) != 1 {
		t.Fatalf("len = %d", len(patterns))
	}
	p0 := patterns[0].(map[string]any)
	if p0["pattern"] != `\bsector\b` || p0["action"] != "cmd" {
		t.Fatalf("p0 = %v", p0)
	}
}

func TestMakeLinkPatternsOrderAndActions(t *testing.T) {
	msg := mustMap(MakeLinkPatterns([]map[string]any{
		{"pattern": "alpha", "action": "cmd"},
		{"pattern": "beta", "action": "url"},
		{"pattern": "gamma", "action": "key"},
	}))
	patterns := msg["patterns"].([]any)
	wantPat := []string{"alpha", "beta", "gamma"}
	wantAct := []string{"cmd", "url", "key"}
	for i, p := range patterns {
		m := p.(map[string]any)
		if m["pattern"] != wantPat[i] || m["action"] != wantAct[i] {
			t.Fatalf("entry %d = %v", i, m)
		}
	}

	for _, action := range []string{"cmd", "url", "key", "focus"} {
		m := mustMap(MakeLinkPatterns([]map[string]any{{"pattern": "x", "action": action}}))
		if m["patterns"].([]any)[0].(map[string]any)["action"] != action {
			t.Fatalf("action %q not accepted", action)
		}
	}
}

func TestMakeLinkPatternsValidationErrors(t *testing.T) {
	cases := []struct {
		name    string
		entries []map[string]any
		substrs []string
	}{
		{"missing pattern", []map[string]any{{"action": "cmd"}}, []string{"entry[0]", "pattern"}},
		{"missing action", []map[string]any{{"pattern": "x"}}, []string{"entry[0]", "action"}},
		{"invalid action", []map[string]any{{"pattern": "x", "action": "teleport"}},
			[]string{"entry[0]", "cmd", "url", "key", "focus"}},
		{"unknown field", []map[string]any{{"pattern": "x", "action": "cmd", "bogus": 1}},
			[]string{"entry[0]", "bogus"}},
		{"index 1", []map[string]any{{"pattern": "good", "action": "cmd"}, {"action": "cmd"}},
			[]string{"entry[1]", "pattern"}},
		{"pattern wrong type", []map[string]any{{"pattern": 7, "action": "cmd"}},
			[]string{"entry[0]", "pattern", "string"}},
		{"action wrong type", []map[string]any{{"pattern": "x", "action": 7}},
			[]string{"entry[0]", "action", "string"}},
		{"group wrong type", []map[string]any{{"pattern": "x", "action": "cmd", "group": 1.5}},
			[]string{"entry[0]", "group"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, err := MakeLinkPatterns(c.entries)
			if err == nil {
				t.Fatal("expected error")
			}
			for _, s := range c.substrs {
				if !strings.Contains(err.Error(), s) {
					t.Errorf("error %q missing %q", err.Error(), s)
				}
			}
		})
	}
}

func TestMakeLinkPatternsAllOptional(t *testing.T) {
	entry := map[string]any{
		"pattern": `\d+`, "action": "url", "id": "p.num", "flags": "gi", "group": 1,
		"payload": "https://example.com/", "hover": "Open link", "class": "external-link",
	}
	p := mustMap(MakeLinkPatterns([]map[string]any{entry}))["patterns"].([]any)[0].(map[string]any)
	for k, want := range map[string]any{
		"id": "p.num", "flags": "gi", "group": 1, "payload": "https://example.com/",
		"hover": "Open link", "class": "external-link",
	} {
		if p[k] != want {
			t.Errorf("%s = %v, want %v", k, p[k], want)
		}
	}
	// Input entry must not be mutated.
	if len(entry) != 8 {
		t.Errorf("input mutated: %v", entry)
	}
}

func TestMakeLinkPatternsOptionalAbsent(t *testing.T) {
	p := mustMap(MakeLinkPatterns([]map[string]any{{"pattern": "x", "action": "key"}}))["patterns"].([]any)[0].(map[string]any)
	for _, k := range []string{"id", "flags", "group", "payload", "hover", "class", "line_contains"} {
		if _, ok := p[k]; ok {
			t.Errorf("unexpected key %q", k)
		}
	}
}

func TestMakeLinkPatternsEmptyAndLineContains(t *testing.T) {
	msg := mustMap(MakeLinkPatterns([]map[string]any{}))
	if msg["type"] != "link_patterns" || len(msg["patterns"].([]any)) != 0 {
		t.Fatalf("empty: %v", msg)
	}

	msg = mustMap(MakeLinkPatterns([]map[string]any{
		{"pattern": `\((\d+)\)`, "action": "cmd", "line_contains": "Warps to Sector"},
	}))
	if msg["patterns"].([]any)[0].(map[string]any)["line_contains"] != "Warps to Sector" {
		t.Fatal("line_contains lost")
	}

	// group as string is accepted (int|str union).
	msg = mustMap(MakeLinkPatterns([]map[string]any{{"pattern": "x", "action": "cmd", "group": "named"}}))
	if msg["patterns"].([]any)[0].(map[string]any)["group"] != "named" {
		t.Fatal("string group lost")
	}
}

func TestMakeLinkPatternsRoundTrip(t *testing.T) {
	got := roundTrip(t, mustMap(MakeLinkPatterns([]map[string]any{{"pattern": "foo", "action": "focus"}})))
	if got["type"] != "link_patterns" {
		t.Fatalf("type = %v", got["type"])
	}
	p0 := got["patterns"].([]any)[0].(map[string]any)
	if p0["pattern"] != "foo" || p0["action"] != "focus" {
		t.Fatalf("p0 = %v", p0)
	}
}

// ---------------------------------------------------------------------------
// MakePresenceUpdate
// ---------------------------------------------------------------------------

func TestMakePresenceUpdate(t *testing.T) {
	msg := MakePresenceUpdate("u1", nil)
	if msg["type"] != "presence_update" || msg["user_id"] != "u1" {
		t.Fatalf("unexpected: %v", msg)
	}

	// Nil-valued fields are omitted.
	msg = MakePresenceUpdate("u1", map[string]any{"scroll_line": nil})
	if !reflect.DeepEqual(msg, map[string]any{"type": "presence_update", "user_id": "u1"}) {
		t.Fatalf("nil field not omitted: %v", msg)
	}

	// Extra fields are merged.
	msg = MakePresenceUpdate("u2", map[string]any{"scroll_line": 42, "cursor_col": 10})
	if msg["scroll_line"] != 42 || msg["cursor_col"] != 10 {
		t.Fatalf("extra fields lost: %v", msg)
	}

	got := roundTrip(t, MakePresenceUpdate("u3", map[string]any{"scroll_line": 5}))
	if got["type"] != "presence_update" || got["user_id"] != "u3" {
		t.Fatalf("round-trip: %v", got)
	}
}
