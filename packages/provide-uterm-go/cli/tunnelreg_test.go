//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"os"
	"path/filepath"
	"testing"
)

func TestResolveWSEndpoint(t *testing.T) {
	// Absolute endpoints pass through unchanged.
	if got := resolveWSEndpoint("https://x", "wss://host/tunnel"); got != "wss://host/tunnel" {
		t.Fatalf("absolute endpoint mutated: %q", got)
	}
	// Relative + https → wss.
	if got := resolveWSEndpoint("https://warp.example/", "/tunnel/abc"); got != "wss://warp.example/tunnel/abc" {
		t.Fatalf("https rewrite = %q", got)
	}
	// Relative + http → ws.
	if got := resolveWSEndpoint("http://127.0.0.1:8780", "/t"); got != "ws://127.0.0.1:8780/t" {
		t.Fatalf("http rewrite = %q", got)
	}
}

func TestResolvedTunnelID(t *testing.T) {
	if id := (tunnelInfo{TunnelID: "a"}).resolvedTunnelID(); id != "a" {
		t.Fatalf("tunnel_id preferred: %q", id)
	}
	if id := (tunnelInfo{SessionID: "s"}).resolvedTunnelID(); id != "s" {
		t.Fatalf("session_id fallback: %q", id)
	}
	if id := (tunnelInfo{}).resolvedTunnelID(); id != "" {
		t.Fatalf("empty when neither set: %q", id)
	}
}

func TestExpandUser(t *testing.T) {
	home, err := os.UserHomeDir()
	if err != nil {
		t.Skip("no home dir")
	}
	if got := expandUser("~"); got != home {
		t.Fatalf("~ = %q, want %q", got, home)
	}
	if got := expandUser("~/x/y"); got != filepath.Join(home, "x/y") {
		t.Fatalf("~/x/y = %q", got)
	}
	if got := expandUser("/abs/path"); got != "/abs/path" {
		t.Fatalf("absolute path mutated: %q", got)
	}
	if got := expandUser("~user/notme"); got != "~user/notme" {
		t.Fatalf("~user should be left alone: %q", got)
	}
}

func TestReadTunnelToken(t *testing.T) {
	if tok := readTunnelToken("inline", "/whatever"); tok != "inline" {
		t.Fatalf("inline token should win: %q", tok)
	}
	if tok := readTunnelToken("", ""); tok != "" {
		t.Fatalf("empty file → empty token: %q", tok)
	}
	if tok := readTunnelToken("", "/no/such/file/xyz"); tok != "" {
		t.Fatalf("missing file → empty token: %q", tok)
	}
	dir := t.TempDir()
	p := filepath.Join(dir, "tok")
	if err := os.WriteFile(p, []byte("  secret \n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if tok := readTunnelToken("", p); tok != "secret" {
		t.Fatalf("file token = %q, want trimmed 'secret'", tok)
	}
}
