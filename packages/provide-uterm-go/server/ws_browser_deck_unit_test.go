//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
)

// TestDeckPrincipalFor covers nil/anonymous/display-name arms of the DeckMux
// principal adapter (pure, no socket).
func TestDeckPrincipalFor(t *testing.T) {
	if deckPrincipalFor(&browserConn{}) != nil {
		t.Fatal("nil principal → nil")
	}
	if deckPrincipalFor(&browserConn{principal: &serverauth.Principal{SubjectID: ""}}) != nil {
		t.Fatal("empty subject → nil")
	}
	if deckPrincipalFor(&browserConn{principal: &serverauth.Principal{SubjectID: "anonymous"}}) != nil {
		t.Fatal("anonymous → nil")
	}
	p := deckPrincipalFor(&browserConn{principal: &serverauth.Principal{SubjectID: "u1"}})
	dp, ok := p.(deckPrincipalT)
	if !ok || dp.SubjectID() != "u1" || dp.DisplayName() != "" {
		t.Fatalf("basic principal = %#v", p)
	}
	name := "Ada"
	p = deckPrincipalFor(&browserConn{principal: &serverauth.Principal{SubjectID: "u2", DisplayName: &name}})
	dp = p.(deckPrincipalT)
	if dp.SubjectID() != "u2" || dp.DisplayName() != "Ada" {
		t.Fatalf("named principal = %#v", dp)
	}
}

// TestDeckPrincipalTestMode forces the UTERM_TEST_MODE nil path.
func TestDeckPrincipalTestMode(t *testing.T) {
	t.Setenv("UTERM_TEST_MODE", "1")
	ts := newTestServer(t, nil)
	bc := &browserConn{principal: &serverauth.Principal{SubjectID: "admin1"}}
	if got := ts.srv.deckPrincipal(bc); got != nil {
		t.Fatalf("test mode should force nil principal, got %#v", got)
	}
}

// TestDeckHandleAndDisconnect covers deckHandle + deckOnDisconnect without
// needing a live WS write (no presence_sync send).
func TestDeckHandleAndDisconnect(t *testing.T) {
	ts := newTestServer(t, nil)
	bc := &browserConn{principal: &serverauth.Principal{SubjectID: "admin1", Roles: serverauth.NewSet("admin")}}
	// Handle without prior connect: DeckMux returns an error (unknown browser)
	// which is logged at Debug — no panic.
	ts.srv.deckHandle("w-deck", bc, map[string]any{
		"type":   "presence_update",
		"fields": map[string]any{"typing": true},
	})
	ts.srv.deckOnDisconnect("w-deck", bc)
}
