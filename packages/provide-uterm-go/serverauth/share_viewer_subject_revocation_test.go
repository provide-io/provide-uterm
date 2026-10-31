//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"errors"
	"fmt"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
)

func share(subject string, iat any) *Principal {
	claims := map[string]any{}
	if iat != nil {
		claims["iat"] = iat
	}
	return &Principal{SubjectID: subject, Roles: NewSet("viewer"), Scopes: NewSet(), Claims: claims}
}

func TestASubjectCutoffRefusesThatSubjectsTokensUpToIt(t *testing.T) {
	cases := []struct {
		iat     float64
		revoked bool
	}{{499, true}, {500, true}, {500.5, true}, {501, false}}
	for _, c := range cases {
		s := &AuthorizationService{}
		if cutoff, err := s.RevokeShareViewerSubject("s1", "share:s1:a", 500.5); err != nil || cutoff != 500.5 {
			t.Fatalf("revoke = %v, %v", cutoff, err)
		}
		if got, ok := s.ShareViewerSubjectRevocation("s1", "share:s1:a"); !ok || got != 500.5 {
			t.Fatalf("held cutoff = %v, %v", got, ok)
		}
		if got := s.ShareViewerRevoked(share("share:s1:a", c.iat), "s1"); got != c.revoked {
			t.Errorf("iat %v: revoked = %v, want %v", c.iat, got, c.revoked)
		}
	}
}

func TestASubjectCutoffLeavesTheSessionsOtherViewersAlone(t *testing.T) {
	s := &AuthorizationService{}
	_, _ = s.RevokeShareViewerSubject("s1", "share:s1:a", 500)
	if s.ShareViewerRevoked(share("share:s1:b", 1.0), "s1") {
		t.Error("another subject of the session was refused")
	}
	if _, ok := s.ShareViewerRevocation("s1"); ok {
		t.Error("a subject revoke recorded a session cutoff")
	}
	if _, ok := s.ShareViewerSubjectRevocation("s2", "share:s1:a"); ok {
		t.Error("the subject cutoff was held for another session")
	}
	if s.ShareViewerRevoked(share("share:s1:a", 1.0), "s2") {
		t.Error("s1's subject cutoff refused it on s2")
	}
}

func TestASubjectCutoffRefusesATokenWithoutANumericIat(t *testing.T) {
	s := &AuthorizationService{}
	_, _ = s.RevokeShareViewerSubject("s1", "share:s1:a", 500)
	if !s.ShareViewerRevoked(share("share:s1:a", nil), "s1") {
		t.Error("a token without iat was admitted")
	}
}

func TestASubjectCutoffNeverRefusesAnyoneButAShareViewer(t *testing.T) {
	s := &AuthorizationService{}
	_, _ = s.RevokeShareViewerSubject("s1", "share:s1:a", 500)
	operator := share("share:s1:a", 1.0)
	operator.Roles = NewSet("viewer", "operator")
	if s.ShareViewerRevoked(operator, "s1") {
		t.Error("a share operator was refused")
	}
}

func TestTheLaterOfTheSessionAndSubjectCutoffsDecides(t *testing.T) {
	s := &AuthorizationService{}
	_, _ = s.RevokeShareViewers("s1", 300)
	_, _ = s.RevokeShareViewerSubject("s1", "share:s1:a", 500)
	if !s.ShareViewerRevoked(share("share:s1:a", 400.0), "s1") {
		t.Error("the later subject cutoff did not decide")
	}
	if s.ShareViewerRevoked(share("share:s1:b", 400.0), "s1") {
		t.Error("another subject was held to the subject cutoff")
	}
	_, _ = s.RevokeShareViewers("s1", 600)
	if !s.ShareViewerRevoked(share("share:s1:a", 550.0), "s1") {
		t.Error("the later session cutoff did not decide")
	}
}

func TestAFullSessionRefusesANewSubjectButMovesAHeldOneForward(t *testing.T) {
	s := &AuthorizationService{}
	for n := 0; n < ShareViewerSubjectRevocationsPerSessionMax; n++ {
		if _, err := s.RevokeShareViewerSubject("s1", fmt.Sprintf("share:s1:%d", n), 100+float64(n)); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := s.RevokeShareViewerSubject("s1", "share:s1:new", 500); !errors.Is(err, ErrShareViewerSubjectRevocationsFull) {
		t.Fatalf("a new subject past the cap: %v", err)
	}
	if ErrShareViewerSubjectRevocationsFull.Error() != "share-viewer subject revocation table is full for this session" {
		t.Errorf("error text %q", ErrShareViewerSubjectRevocationsFull)
	}
	if _, ok := s.ShareViewerSubjectRevocation("s1", "share:s1:new"); ok {
		t.Error("the refused subject was recorded")
	}
	if got, _ := s.ShareViewerSubjectRevocation("s1", "share:s1:0"); got != 100 {
		t.Errorf("the oldest subject was evicted: %v", got)
	}
	if got, err := s.RevokeShareViewerSubject("s1", "share:s1:0", 600); err != nil || got != 600 {
		t.Errorf("a held subject past the cap: %v, %v", got, err)
	}
	if _, err := s.RevokeShareViewerSubject("s2", "share:s2:a", 600); err != nil {
		t.Errorf("another session was capped by s1: %v", err)
	}
}

func TestSubjectCutoffsOlderThanTheTTLAgeOut(t *testing.T) {
	s := &AuthorizationService{}
	_, _ = s.RevokeShareViewerSubject("s1", "share:s1:a", 10)
	_, _ = s.RevokeShareViewerSubject("s1", "share:s1:b", 20)
	// Exactly the TTL old is kept; older goes.
	_, _ = s.RevokeShareViewerSubject("s2", "share:s2:a", 20+ShareViewerRevocationTTLSeconds)
	if _, ok := s.ShareViewerSubjectRevocation("s1", "share:s1:a"); ok {
		t.Error("a cutoff older than the TTL was kept")
	}
	if got, ok := s.ShareViewerSubjectRevocation("s1", "share:s1:b"); !ok || got != 20 {
		t.Errorf("a cutoff exactly the TTL old: %v, %v", got, ok)
	}
}

func TestAnAgedOutSubjectFreesItsSessionsSlot(t *testing.T) {
	s := &AuthorizationService{}
	for n := 0; n < ShareViewerSubjectRevocationsPerSessionMax; n++ {
		_, _ = s.RevokeShareViewerSubject("s1", fmt.Sprintf("share:s1:%d", n), float64(n))
	}
	if _, err := s.RevokeShareViewerSubject("s1", "share:s1:new", 1+ShareViewerRevocationTTLSeconds); err != nil {
		t.Fatalf("no slot was freed: %v", err)
	}
	if _, ok := s.ShareViewerSubjectRevocation("s1", "share:s1:0"); ok {
		t.Error("the aged-out subject was kept")
	}
}

func TestAReRevokedSubjectAgesOutFromItsNewestCutoff(t *testing.T) {
	s := &AuthorizationService{}
	_, _ = s.RevokeShareViewerSubject("s1", "share:s1:a", 10)
	_, _ = s.RevokeShareViewerSubject("s1", "share:s1:b", 20)
	_, _ = s.RevokeShareViewerSubject("s1", "share:s1:a", 30)
	_, _ = s.RevokeShareViewerSubject("s2", "share:s2:a", 25+ShareViewerRevocationTTLSeconds)
	if _, ok := s.ShareViewerSubjectRevocation("s1", "share:s1:b"); ok {
		t.Error("the older subject was kept")
	}
	if got, ok := s.ShareViewerSubjectRevocation("s1", "share:s1:a"); !ok || got != 30 {
		t.Errorf("the re-revoked subject: %v, %v", got, ok)
	}
}

func TestForgettingASessionDropsItsSubjectCutoffsOnly(t *testing.T) {
	s := &AuthorizationService{}
	_, _ = s.RevokeShareViewerSubject("s1", "share:s1:a", 10)
	_, _ = s.RevokeShareViewerSubject("s1", "share:s1:b", 11)
	_, _ = s.RevokeShareViewerSubject("s2", "share:s2:a", 12)
	s.ForgetShareViewers("s1")
	for _, subject := range []string{"share:s1:a", "share:s1:b"} {
		if _, ok := s.ShareViewerSubjectRevocation("s1", subject); ok {
			t.Errorf("%s kept after forgetting s1", subject)
		}
	}
	if got, ok := s.ShareViewerSubjectRevocation("s2", "share:s2:a"); !ok || got != 12 {
		t.Errorf("s2's subject: %v, %v", got, ok)
	}
	for n := 0; n < ShareViewerSubjectRevocationsPerSessionMax; n++ {
		if _, err := s.RevokeShareViewerSubject("s1", fmt.Sprintf("share:s1:%d", n), 20); err != nil {
			t.Fatalf("s1's slots were not freed: %v", err)
		}
	}
}

func TestIsShareSubjectOf(t *testing.T) {
	longest := "share:s1:" + strings.Repeat("x", ShareViewerSubjectMaxBytes-len("share:s1:"))
	cases := []struct {
		subject string
		want    bool
	}{
		{"share:s1:blackbetty:7", true},
		{longest, true},
		{longest + "x", false},
		{"share:s1:" + strings.Repeat("é", (ShareViewerSubjectMaxBytes-len("share:s1:"))/2+1), false},
		{"share:s1:", false},
		{"share:s1", false},
		{"share:s1x:7", false},
		{"share:s2:7", false},
		{"alice", false},
	}
	for _, c := range cases {
		if got := IsShareSubjectOf(c.subject, "s1"); got != c.want {
			t.Errorf("IsShareSubjectOf(%q) = %v, want %v", c.subject, got, c.want)
		}
	}
	if ShareViewerSubjectMaxBytes != 512 || ShareViewerSubjectRevocationsPerSessionMax != 100 {
		t.Error("the subject caps changed")
	}
}

func TestAWholeSessionRevokeDropsTheSubjectCutoffsItSubsumes(t *testing.T) {
	s := &AuthorizationService{}
	_, _ = s.RevokeShareViewerSubject("s1", "share:s1:a", 100)
	_, _ = s.RevokeShareViewerSubject("s1", "share:s1:b", 200)
	_, _ = s.RevokeShareViewerSubject("s1", "share:s1:c", 300)
	_, _ = s.RevokeShareViewerSubject("s2", "share:s2:a", 100)
	_, _ = s.RevokeShareViewers("s1", 200)
	for _, subject := range []string{"share:s1:a", "share:s1:b"} {
		if _, ok := s.ShareViewerSubjectRevocation("s1", subject); ok {
			t.Errorf("%s at or before the session cutoff was kept", subject)
		}
	}
	if got, ok := s.ShareViewerSubjectRevocation("s1", "share:s1:c"); !ok || got != 300 {
		t.Errorf("the later subject cutoff: %v, %v", got, ok)
	}
	if got, ok := s.ShareViewerSubjectRevocation("s2", "share:s2:a"); !ok || got != 100 {
		t.Errorf("another session's subject cutoff: %v, %v", got, ok)
	}
	if !s.ShareViewerRevoked(share("share:s1:a", 150.0), "s1") || !s.ShareViewerRevoked(share("share:s1:c", 250.0), "s1") {
		t.Error("dropping a subsumed cutoff admitted a token")
	}
}

func TestAWholeSessionRevokeByAPrincipalAlsoFreesTheSessionsSlots(t *testing.T) {
	s := &AuthorizationService{}
	for n := 0; n < ShareViewerSubjectRevocationsPerSessionMax; n++ {
		_, _ = s.RevokeShareViewerSubject("s1", fmt.Sprintf("share:s1:%d", n), 100)
	}
	_, _, _ = s.RevokeShareViewersBy("s1", 100, "owner1")
	if _, err := s.RevokeShareViewerSubject("s1", "share:s1:new", 101); err != nil {
		t.Fatalf("no slot was freed: %v", err)
	}
}

func TestARefusedWholeSessionRevokeDropsNoSubjectCutoff(t *testing.T) {
	s := &AuthorizationService{}
	for n := 0; n < ShareViewerRevocationsMax; n++ {
		_, _ = s.RevokeShareViewers(fmt.Sprintf("other-%d", n), 100)
	}
	_, _ = s.RevokeShareViewerSubject("s1", "share:s1:a", 100)
	if _, err := s.RevokeShareViewers("s1", 200); !errors.Is(err, ErrShareViewerRevocationsFull) {
		t.Fatalf("a full session table: %v", err)
	}
	if got, ok := s.ShareViewerSubjectRevocation("s1", "share:s1:a"); !ok || got != 100 {
		t.Errorf("a refused revoke dropped a subject cutoff: %v, %v", got, ok)
	}
}

func TestAFullSubjectTableRefusesANewSubjectButMovesAHeldOneForward(t *testing.T) {
	s := &AuthorizationService{}
	per := ShareViewerSubjectRevocationsPerSessionMax
	for n := 0; n < ShareViewerSubjectRevocationsMax; n++ {
		if _, err := s.RevokeShareViewerSubject(fmt.Sprintf("s%d", n/per), fmt.Sprintf("share:s%d:%d", n/per, n%per), 100); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := s.RevokeShareViewerSubject("fresh", "share:fresh:a", 200); !errors.Is(err, ErrShareViewerRevocationsFull) {
		t.Fatalf("a new subject past the global cap: %v", err)
	}
	if _, ok := s.ShareViewerSubjectRevocation("fresh", "share:fresh:a"); ok {
		t.Error("the refused subject was recorded")
	}
	if got, err := s.RevokeShareViewerSubject("s0", "share:s0:0", 200); err != nil || got != 200 {
		t.Errorf("a held subject past the global cap: %v, %v", got, err)
	}
	later := 100.5 + ShareViewerRevocationTTLSeconds
	if _, err := s.RevokeShareViewerSubject("fresh", "share:fresh:a", later); err != nil {
		t.Errorf("ageing out made no room: %v", err)
	}
	if ShareViewerSubjectRevocationsMax != 10000 {
		t.Error("the global subject cap changed")
	}
}

// TestAWholeSessionRevokeNeverLetsACheckSeeNeitherCutoff races checks against
// the revoke that records a session cutoff and drops the subject cutoff it
// subsumes. Every state the table passes through refuses the token, so no
// check may admit it. Reading the two cutoffs under separate lock
// acquisitions could fall between them and see neither; with the checkers
// spinning on every core that interleaving shows up within a few thousand
// revokes.
func TestAWholeSessionRevokeNeverLetsACheckSeeNeitherCutoff(t *testing.T) {
	s := &AuthorizationService{}
	const checkers = 4
	for i := 0; i < 5000; i++ {
		sessionID := fmt.Sprintf("s%d", i)
		subject := "share:" + sessionID + ":a"
		_, _ = s.RevokeShareViewerSubject(sessionID, subject, 100)
		token := share(subject, 50.0)
		var stop atomic.Bool
		var admitted atomic.Bool
		var started, done sync.WaitGroup
		for c := 0; c < checkers; c++ {
			started.Add(1)
			done.Add(1)
			go func() {
				defer done.Done()
				started.Done()
				for !stop.Load() {
					if !s.ShareViewerRevoked(token, sessionID) {
						admitted.Store(true)
					}
				}
			}()
		}
		started.Wait()
		_, _ = s.RevokeShareViewers(sessionID, 200)
		stop.Store(true)
		done.Wait()
		if admitted.Load() {
			t.Fatalf("iteration %d: a token refused before and after the revoke was admitted during it", i)
		}
	}
}
