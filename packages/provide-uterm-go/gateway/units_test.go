//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package gateway

import (
	"encoding/binary"
	"encoding/json"
	"testing"

	"golang.org/x/crypto/ssh"
)

// sshString builds a uint32-length-prefixed SSH string.
func sshString(s string) []byte {
	b := make([]byte, 4+len(s))
	binary.BigEndian.PutUint32(b, uint32(len(s)))
	copy(b[4:], s)
	return b
}

func TestParsePtyReqTerm(t *testing.T) {
	payload := append(sshString("xterm-256color"), 0, 0, 0, 80) // + dims tail
	if got := parsePtyReqTerm(payload); got != "xterm-256color" {
		t.Errorf("term = %q", got)
	}
	if got := parsePtyReqTerm([]byte{0, 0}); got != "" {
		t.Errorf("short payload = %q", got)
	}
}

func TestParseEnvReq(t *testing.T) {
	payload := append(sshString("COLORTERM"), sshString("truecolor")...)
	k, v, ok := parseEnvReq(payload)
	if !ok || k != "COLORTERM" || v != "truecolor" {
		t.Errorf("parseEnvReq = %q,%q,%v", k, v, ok)
	}
	if _, _, ok := parseEnvReq([]byte{0, 0, 0, 5}); ok {
		t.Error("truncated env should fail")
	}
	if _, _, ok := parseEnvReq(sshString("ONLY_NAME")); ok {
		t.Error("missing value should fail")
	}
}

func TestReadSSHStringShort(t *testing.T) {
	if _, _, ok := readSSHString([]byte{1, 2}); ok {
		t.Error("short header should fail")
	}
	if _, _, ok := readSSHString([]byte{0, 0, 0, 9, 'a'}); ok {
		t.Error("declared length beyond buffer should fail")
	}
}

func TestAppendColormode(t *testing.T) {
	if got := appendColormode("wss://h/ws", ""); got != "wss://h/ws" {
		t.Errorf("empty derived should not change url: %q", got)
	}
	if got := appendColormode("wss://h/ws", "256"); got != "wss://h/ws?colormode=256" {
		t.Errorf("got %q", got)
	}
	if got := appendColormode("wss://h/ws?x=1", "16"); got != "wss://h/ws?x=1&colormode=16" {
		t.Errorf("got %q", got)
	}
}

func TestBuildIdentityFrame(t *testing.T) {
	frame := buildIdentityFrame("user:bob", "SHA256:abc", map[string]any{"role": "op"})
	if frame["type"] != "identity" || frame["subject"] != "user:bob" || frame["transport"] != "ssh" {
		t.Fatalf("frame = %v", frame)
	}
	if frame["fingerprint"] != "SHA256:abc" {
		t.Errorf("fingerprint = %v", frame["fingerprint"])
	}
	if buildIdentityFrame("", "", nil) != nil {
		t.Error("empty subject must yield nil frame")
	}
}

func TestIdentityFrameFromPermissions(t *testing.T) {
	if identityFrameFromPermissions(nil) != nil {
		t.Error("nil perms → nil frame")
	}
	if identityFrameFromPermissions(&ssh.Permissions{}) != nil {
		t.Error("empty perms → nil frame")
	}
	claims, _ := json.Marshal(map[string]any{"team": "x"})
	perms := &ssh.Permissions{Extensions: map[string]string{
		extSubject: "user:z", extFingerprint: "SHA256:z", extClaims: string(claims),
	}}
	frame := identityFrameFromPermissions(perms)
	if frame["subject"] != "user:z" {
		t.Fatalf("frame = %v", frame)
	}
}

func TestAnyToStringAndAsInt64(t *testing.T) {
	if anyToString("x") != "x" {
		t.Error("string passthrough")
	}
	if anyToString(json.Number("7")) != "7" {
		t.Error("json.Number via Stringer")
	}
	if anyToString(struct{}{}) != "" {
		t.Error("unknown → empty")
	}
	if v, ok := asInt64(json.Number("42")); !ok || v != 42 {
		t.Errorf("asInt64 = %d,%v", v, ok)
	}
	if _, ok := asInt64("nope"); ok {
		t.Error("non-number → not ok")
	}
	if _, ok := asInt64(json.Number("1.5")); ok {
		t.Error("float json.Number → not an int")
	}
}
