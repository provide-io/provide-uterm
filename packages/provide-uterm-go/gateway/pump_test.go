//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package gateway

import (
	"bytes"
	"encoding/json"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/colors"
)

func TestApplyRedirect(t *testing.T) {
	cur := "wss://host.example/ws/terminal?x=1"
	cases := []struct {
		path string
		want string
		ok   bool
	}{
		{"/ws/game?room=5", "wss://host.example/ws/game?room=5", true},
		{"/plain", "wss://host.example/plain", true},
		{"", "", false},
		{"//evil.example/x", "", false},
		{"https://evil.example/x", "", false},
		{"relative/path", "", false},
	}
	for _, c := range cases {
		got, ok := applyRedirect(cur, c.path)
		if ok != c.ok || got != c.want {
			t.Errorf("applyRedirect(%q) = (%q,%v), want (%q,%v)", c.path, got, ok, c.want, c.ok)
		}
	}
}

func TestNormalizeCRLF(t *testing.T) {
	if got := normalizeCRLF([]byte("a\nb\r\nc")); !bytes.Equal(got, []byte("a\r\nb\r\nc")) {
		t.Errorf("normalizeCRLF = %q", got)
	}
}

func TestTelnetWriteTransform(t *testing.T) {
	// DEL(0x7f) → BS(0x08), bare \n → \r\n.
	tf := telnetWriteTransform(colors.ModePassthrough)
	got := tf([]byte{'a', 0x7f, '\n'})
	if !bytes.Equal(got, []byte{'a', 0x08, '\r', '\n'}) {
		t.Errorf("transform = %v", got)
	}
}

func TestSSHWriteTransformNoCRLF(t *testing.T) {
	// SSH keeps its own line discipline: no CRLF/DEL rewriting.
	tf := sshWriteTransform(colors.ModePassthrough)
	got := tf([]byte{'a', 0x7f, '\n'})
	if !bytes.Equal(got, []byte{'a', 0x7f, '\n'}) {
		t.Errorf("ssh transform mutated bytes: %v", got)
	}
}

func TestHandleControlFrameSessionToken(t *testing.T) {
	st := &controlState{}
	var written bytes.Buffer
	frame := map[string]any{"type": "session_token", "token": "abc", "player_id": json.Number("42")}
	if !handleControlFrame(frame, st, func(b []byte) error { written.Write(b); return nil }) {
		t.Fatal("session_token should be handled")
	}
	if st.token == nil || st.token.token != "abc" || !st.token.hasPID || st.token.playerID != 42 {
		t.Fatalf("token state = %+v", st.token)
	}
	// Resume frame carries the player id back.
	rf := resumeFrame(st.token)
	if rf["type"] != "resume" || rf["token"] != "abc" || rf["player_id"] != int64(42) {
		t.Errorf("resumeFrame = %v", rf)
	}
}

func TestHandleControlFrameResumeAndRedirect(t *testing.T) {
	st := &controlState{token: &tokenRec{token: "x"}}
	var written bytes.Buffer
	w := func(b []byte) error { written.Write(b); return nil }

	handleControlFrame(map[string]any{"type": "resume_ok"}, st, w)
	if !bytes.Contains(written.Bytes(), []byte("[Session resumed]")) {
		t.Errorf("resume_ok did not write banner: %q", written.String())
	}
	handleControlFrame(map[string]any{"type": "resume_failed"}, st, w)
	if st.token != nil {
		t.Error("resume_failed should clear token")
	}
	handleControlFrame(map[string]any{"type": "redirect", "path": "/new"}, st, w)
	if st.redirect != "/new" {
		t.Errorf("redirect = %q", st.redirect)
	}
}

func TestHandleControlFrameUnknown(t *testing.T) {
	st := &controlState{}
	if handleControlFrame(map[string]any{"type": "hello"}, st, func([]byte) error { return nil }) {
		t.Error("unknown frame type should not be handled")
	}
	if handleControlFrame(map[string]any{"type": "session_token"}, st, func([]byte) error { return nil }) {
		t.Error("session_token without token should not be handled")
	}
}

func TestIsLoopbackBindHost(t *testing.T) {
	for _, h := range []string{"127.0.0.1", "localhost", "::1", "[::1]", "127.5.5.5"} {
		if !isLoopbackBindHost(h) {
			t.Errorf("%q should be loopback", h)
		}
	}
	for _, h := range []string{"0.0.0.0", "192.168.1.1", "example.com", ""} {
		if isLoopbackBindHost(h) {
			t.Errorf("%q should NOT be loopback", h)
		}
	}
}
