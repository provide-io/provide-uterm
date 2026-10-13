//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"encoding/json"
	"errors"
	"fmt"
	"testing"
	"time"

	jwt "github.com/golang-jwt/jwt/v5"

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
	if got, err := s.RevokeShareViewers("s1", 500.5); err != nil || got != 500.5 {
		t.Fatalf("RevokeShareViewers returned %v %v", got, err)
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
	_, _ = s.RevokeShareViewers("s1", 900)
	if !s.ShareViewerRevoked(shareViewer("s1", 600.0), "s1") {
		t.Error("a later revocation did not move the cutoff forward")
	}
}

func TestAFullCutoffTableRefusesNewSessionsInsteadOfEvicting(t *testing.T) {
	s := NewAuthorizationServiceWith(LocalAuthorizationProvider{})
	const base = 1e9
	for i := 0; i < ShareViewerRevocationsMax; i++ {
		if _, err := s.RevokeShareViewers(fmt.Sprintf("s%d", i), base+float64(i)); err != nil {
			t.Fatal(err)
		}
	}
	// A session already holding a cutoff can always move it forward.
	if got, err := s.RevokeShareViewers("s0", base+1e4); err != nil || got != base+1e4 {
		t.Fatalf("re-revoke: %v %v", got, err)
	}
	// A new one is refused rather than pushing a live cutoff out.
	if _, err := s.RevokeShareViewers("overflow", base+2e4); !errors.Is(err, ErrShareViewerRevocationsFull) {
		t.Fatalf("full table: %v", err)
	}
	if _, ok := s.ShareViewerRevocation("overflow"); ok {
		t.Error("refused revoke recorded a cutoff")
	}
	if got, _ := s.ShareViewerRevocation("s1"); got != base+1 {
		t.Errorf("s1 evicted: %v", got)
	}
	// Once the oldest cutoffs have aged out there is room again.
	later := base + 1 + ShareViewerRevocationTTLSeconds + 0.5
	if got, err := s.RevokeShareViewers("overflow", later); err != nil || got != later {
		t.Fatalf("after age-out: %v %v", got, err)
	}
	if _, ok := s.ShareViewerRevocation("s1"); ok {
		t.Error("stale s1 kept")
	}
	if got, _ := s.ShareViewerRevocation("s0"); got != base+1e4 {
		t.Errorf("live s0 lost: %v", got)
	}
}

func TestTunnelSharePrincipalsAreNotShareViewers(t *testing.T) {
	tunnelViewer := shareViewer("s1", nil)
	tunnelViewer.SubjectID = "share:s1:viewer"
	tunnelViewer.TunnelShare = true
	if IsShareViewer(tunnelViewer, "s1") {
		t.Fatal("a tunnel share viewer counted as a share viewer")
	}
	s := NewAuthorizationService()
	_, _ = s.RevokeShareViewers("s1", 500)
	if s.ShareViewerRevoked(tunnelViewer, "s1") || !s.CanReadSession(tunnelViewer, publicSession("s1")) {
		t.Fatal("the viewer cutoff refused a tunnel share viewer")
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
		_, _ = s.RevokeShareViewers("s1", 200)
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

func TestShareViewerCutoffsAgeOutAndCanBeForgotten(t *testing.T) {
	if ShareViewerRevocationTTLSeconds != 86400 {
		t.Fatalf("ttl %v", ShareViewerRevocationTTLSeconds)
	}
	s := NewAuthorizationService()
	_, _ = s.RevokeShareViewers("old", 1000)
	_, _ = s.RevokeShareViewers("edge", 2000)
	_, _ = s.RevokeShareViewers("new", 2000+ShareViewerRevocationTTLSeconds)
	if _, ok := s.ShareViewerRevocation("old"); ok {
		t.Error("a cutoff older than the ttl was kept")
	}
	if got, ok := s.ShareViewerRevocation("edge"); !ok || got != 2000 {
		t.Errorf("a cutoff exactly ttl old was dropped: %v %v", got, ok)
	}
	s.ForgetShareViewers("edge")
	s.ForgetShareViewers("never-revoked")
	if _, ok := s.ShareViewerRevocation("edge"); ok {
		t.Error("forget left the cutoff")
	}
	if _, ok := s.ShareViewerRevocation("new"); !ok {
		t.Error("forget dropped another session's cutoff")
	}
	// A re-revoke after a forget is ordered as the newest again.
	_, _ = s.RevokeShareViewers("edge", 3000+ShareViewerRevocationTTLSeconds)
	if _, ok := s.ShareViewerRevocation("edge"); !ok {
		t.Error("re-revoke after forget lost")
	}
}

// A token whose iat is in the future (beyond the clock skew) would be immune to
// every cutoff, so it is refused at authentication, as PyJWT refuses it.
func TestJWTWithAFutureIatIsRefused(t *testing.T) {
	idp := NewLocalIdentityProvider(jwtAuthConfig(), nil)
	mint := func(iatOffset int64) string {
		now := time.Now().Unix()
		tok, err := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{
			"sub": "share:s1:blackbetty:7", "roles": []string{"viewer"}, "iss": "provide-uterm",
			"aud": "provide-uterm-server", "iat": now + iatOffset, "exp": now + 7200,
		}).SignedString([]byte(testKey))
		if err != nil {
			t.Fatal(err)
		}
		return tok
	}
	if _, err := idp.PrincipalFromJWTToken(mint(3600)); err == nil {
		t.Error("a token issued an hour from now was accepted")
	}
	if _, err := idp.PrincipalFromJWTToken(mint(10)); err != nil {
		t.Errorf("a token within the clock skew was refused: %v", err)
	}
}

// held reports how many cutoffs the table holds.
func held(s *AuthorizationService) int {
	s.revocations.mu.Lock()
	defer s.revocations.mu.Unlock()
	return len(s.revocations.order)
}

func revokeBy(t *testing.T, s *AuthorizationService, sessionID string, now float64, revoker string) {
	t.Helper()
	if _, err := s.RevokeShareViewersBy(sessionID, now, revoker); err != nil {
		t.Fatalf("revoke %s: %v", sessionID, err)
	}
}

func cutoffOf(s *AuthorizationService, sessionID string) any {
	if got, ok := s.ShareViewerRevocation(sessionID); ok {
		return got
	}
	return nil
}

func TestOnePrincipalHoldsAtMostItsCapKeepingTheNewest(t *testing.T) {
	s := NewAuthorizationService()
	const limit = ShareViewerRevocationsPerPrincipalMax
	for i := 0; i < limit+2; i++ {
		revokeBy(t, s, fmt.Sprintf("s%d", i), 100+float64(i), "alice")
	}
	if cutoffOf(s, "s0") != nil || cutoffOf(s, "s1") != nil {
		t.Error("alice's two oldest cutoffs were kept")
	}
	if cutoffOf(s, "s2") != 102.0 || cutoffOf(s, fmt.Sprintf("s%d", limit+1)) != 100+float64(limit+1) {
		t.Error("alice's newest cutoffs were not kept")
	}
	if held(s) != limit {
		t.Errorf("held %d, want %d", held(s), limit)
	}
}

func TestThePerPrincipalCapLeavesOtherPrincipalsAlone(t *testing.T) {
	s := NewAuthorizationService()
	revokeBy(t, s, "bobs", 1, "bob")
	revokeBy(t, s, "admins", 1, "")
	for i := 0; i <= ShareViewerRevocationsPerPrincipalMax; i++ {
		revokeBy(t, s, fmt.Sprintf("s%d", i), 10+float64(i), "alice")
	}
	if cutoffOf(s, "bobs") != 1.0 || cutoffOf(s, "admins") != 1.0 {
		t.Error("alice's cap evicted someone else's cutoff")
	}
}

func TestMovingYourOwnCutoffForwardEvictsNothing(t *testing.T) {
	s := NewAuthorizationService()
	const limit = ShareViewerRevocationsPerPrincipalMax
	for i := 0; i < limit; i++ {
		revokeBy(t, s, fmt.Sprintf("s%d", i), 100+float64(i), "alice")
	}
	revokeBy(t, s, "s0", 500, "alice")
	if held(s) != limit || cutoffOf(s, "s0") != 500.0 {
		t.Fatalf("re-revoke: held %d, s0 %v", held(s), cutoffOf(s, "s0"))
	}
	// s0 is now alice's newest, so her next new session pushes s1 out.
	revokeBy(t, s, "extra", 600, "alice")
	if cutoffOf(s, "s0") != 500.0 || cutoffOf(s, "s1") != nil {
		t.Errorf("s0 %v s1 %v", cutoffOf(s, "s0"), cutoffOf(s, "s1"))
	}
}

func TestACutoffCountsAgainstWhoeverRevokedItLast(t *testing.T) {
	s := NewAuthorizationService()
	const limit = ShareViewerRevocationsPerPrincipalMax
	revokeBy(t, s, "shared", 1, "bob")
	for i := 0; i < limit-1; i++ {
		revokeBy(t, s, fmt.Sprintf("a%d", i), 10+float64(i), "alice")
	}
	// Alice takes "shared" over from bob: her cap is now full, nothing evicted.
	revokeBy(t, s, "shared", 500, "alice")
	if held(s) != limit {
		t.Fatalf("held %d", held(s))
	}
	revokeBy(t, s, "bob2", 600, "bob")
	if cutoffOf(s, "shared") != 500.0 || cutoffOf(s, "a0") != 10.0 {
		t.Fatal("bob's revoke evicted alice's cutoffs")
	}
	revokeBy(t, s, "a-next", 700, "alice")
	if cutoffOf(s, "a0") != nil {
		t.Error("alice's oldest was kept past her cap")
	}
}

func TestAnUncappedRevokeIsNeverEvictedByThePerPrincipalCap(t *testing.T) {
	s := NewAuthorizationService()
	for i := 0; i < ShareViewerRevocationsPerPrincipalMax+5; i++ {
		revokeBy(t, s, fmt.Sprintf("s%d", i), 100+float64(i), "")
	}
	if cutoffOf(s, "s0") != 100.0 || held(s) != ShareViewerRevocationsPerPrincipalMax+5 {
		t.Errorf("s0 %v held %d", cutoffOf(s, "s0"), held(s))
	}
}

func TestAPrincipalAtItsCapMakesItsOwnRoomInAFullTable(t *testing.T) {
	s := NewAuthorizationService()
	const base, limit = 1e9, ShareViewerRevocationsPerPrincipalMax
	for i := 0; i < limit; i++ {
		revokeBy(t, s, fmt.Sprintf("a%d", i), base+float64(i), "alice")
	}
	for i := 0; i < ShareViewerRevocationsMax-limit; i++ {
		revokeBy(t, s, fmt.Sprintf("s%d", i), base+float64(limit+i), "")
	}
	if got, err := s.RevokeShareViewersBy("a-new", base+2e4, "alice"); err != nil || got != base+2e4 {
		t.Fatalf("revoke at cap in a full table: %v %v", got, err)
	}
	if cutoffOf(s, "a0") != nil || cutoffOf(s, "s0") != base+float64(limit) {
		t.Errorf("a0 %v s0 %v", cutoffOf(s, "a0"), cutoffOf(s, "s0"))
	}
}

func TestAgeingOutAndForgettingFreeAPrincipalsSlots(t *testing.T) {
	s := NewAuthorizationService()
	const limit = ShareViewerRevocationsPerPrincipalMax
	revokeBy(t, s, "old", 1, "alice")
	revokeBy(t, s, "gone", 2+ShareViewerRevocationTTLSeconds, "alice")
	s.ForgetShareViewers("gone")
	for i := 0; i < limit; i++ {
		revokeBy(t, s, fmt.Sprintf("s%d", i), 10+ShareViewerRevocationTTLSeconds+float64(i), "alice")
	}
	if cutoffOf(s, "s0") != 10+ShareViewerRevocationTTLSeconds || held(s) != limit {
		t.Errorf("s0 %v held %d", cutoffOf(s, "s0"), held(s))
	}
}
