//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"encoding/json"
	"fmt"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

func shareViewer(sessionID string, iat any, roles ...string) *Principal {
	if roles == nil {
		roles = []string{"viewer"}
	}
	claims := map[string]any{}
	if iat != nil {
		claims["iat"] = iat
	}
	return &Principal{SubjectID: "share:" + sessionID + ":blackbetty:7", Roles: NewSet(roles...), Scopes: NewSet(), Claims: claims}
}

func publicSession(id string) *serverconfig.SessionDefinition {
	return &serverconfig.SessionDefinition{SessionID: id, Visibility: "public"}
}

func TestIsShareViewer(t *testing.T) {
	scoped := shareViewer("s1", 1.0)
	scope := "s1"
	scoped.AdminSessionScope = &scope
	cases := []struct {
		name string
		p    *Principal
		want bool
	}{
		{"viewer share", shareViewer("s1", 1.0), true},
		{"no roles", shareViewer("s1", 1.0, []string{}...), true},
		{"other session", shareViewer("s2", 1.0), false},
		{"prefix collision", shareViewer("s1x", 1.0), false},
		{"operator share", shareViewer("s1", 1.0, "viewer", "operator"), false},
		{"admin share", shareViewer("s1", 1.0, "admin"), false},
		{"session-scoped admin", scoped, false},
		{"plain viewer", &Principal{SubjectID: "alice", Roles: NewSet("viewer")}, false},
		{"nil", nil, false},
	}
	for _, c := range cases {
		if got := IsShareViewer(c.p, "s1"); got != c.want {
			t.Errorf("%s: IsShareViewer = %v, want %v", c.name, got, c.want)
		}
	}
}

func TestShareViewerRevocationCutoff(t *testing.T) {
	s := NewAuthorizationService()
	if s.ShareViewerRevoked(shareViewer("s1", 1.0), "s1") {
		t.Fatal("revoked before any revocation")
	}
	if _, ok := s.ShareViewerRevocation("s1"); ok {
		t.Fatal("cutoff recorded before any revocation")
	}
	if got := s.RevokeShareViewers("s1", 500.5); got != 500.5 {
		t.Fatalf("RevokeShareViewers returned %v", got)
	}
	if got, ok := s.ShareViewerRevocation("s1"); !ok || got != 500.5 {
		t.Fatalf("ShareViewerRevocation = %v, %v", got, ok)
	}
	cases := []struct {
		iat  any
		want bool
	}{
		{499.0, true}, {500.0, true}, {500.5, true}, {501.0, false},
		{json.Number("500"), true}, {json.Number("501"), false}, {json.Number("bogus"), true},
		{nil, true}, {"600", true}, {true, true},
	}
	for _, c := range cases {
		if got := s.ShareViewerRevoked(shareViewer("s1", c.iat), "s1"); got != c.want {
			t.Errorf("iat %#v: revoked = %v, want %v", c.iat, got, c.want)
		}
	}
	if s.ShareViewerRevoked(shareViewer("s2", 1.0), "s2") {
		t.Error("revocation leaked to another session")
	}
	if s.ShareViewerRevoked(shareViewer("s1", 1.0, "admin"), "s1") {
		t.Error("revocation touched a non-viewer share principal")
	}
	s.RevokeShareViewers("s1", 900)
	if !s.ShareViewerRevoked(shareViewer("s1", 600.0), "s1") {
		t.Error("a later revocation did not move the cutoff forward")
	}
}

func TestShareViewerRevocationsAreBounded(t *testing.T) {
	s := NewAuthorizationServiceWith(LocalAuthorizationProvider{})
	for i := 0; i < ShareViewerRevocationsMax; i++ {
		s.RevokeShareViewers(fmt.Sprintf("s%d", i), float64(i))
	}
	s.RevokeShareViewers("s0", 1e6) // now the newest, so s1 falls out next
	s.RevokeShareViewers("overflow", 2e6)
	if _, ok := s.ShareViewerRevocation("s1"); ok {
		t.Error("oldest cutoff was not forgotten")
	}
	if got, _ := s.ShareViewerRevocation("s0"); got != 1e6 {
		t.Errorf("s0 cutoff = %v", got)
	}
	if got, _ := s.ShareViewerRevocation("s2"); got != 2 {
		t.Errorf("s2 cutoff = %v", got)
	}
	if got, _ := s.ShareViewerRevocation("overflow"); got != 2e6 {
		t.Errorf("overflow cutoff = %v", got)
	}
}

type allowAllReads struct{ LocalAuthorizationProvider }

func (allowAllReads) CanReadSession(*Principal, *serverconfig.SessionDefinition) bool   { return true }
func (allowAllReads) CanReadRecording(*Principal, *serverconfig.SessionDefinition) bool { return true }

func TestRevokedShareViewerCannotRead(t *testing.T) {
	for _, s := range []*AuthorizationService{NewAuthorizationService(), NewAuthorizationServiceWith(allowAllReads{})} {
		viewer := shareViewer("s1", 100.0)
		if !s.CanReadSession(viewer, publicSession("s1")) || !s.CanReadRecording(viewer, publicSession("s1")) {
			t.Fatal("share viewer could not read before revocation")
		}
		s.RevokeShareViewers("s1", 200)
		if s.CanReadSession(viewer, publicSession("s1")) {
			t.Error("revoked share viewer can still read the session")
		}
		if s.CanReadRecording(viewer, publicSession("s1")) {
			t.Error("revoked share viewer can still read the recording")
		}
		fresh := shareViewer("s1", 300.0)
		if !s.CanReadSession(fresh, publicSession("s1")) || !s.CanReadRecording(fresh, publicSession("s1")) {
			t.Error("a token issued after the cutoff was refused")
		}
	}
}
