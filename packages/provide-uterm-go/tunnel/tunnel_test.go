//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package tunnel

import (
	"sync"
	"testing"
)

func TestHashTokenEmptyIsEmpty(t *testing.T) {
	if got := HashToken(""); got != "" {
		t.Fatalf("HashToken(\"\") = %q, want empty", got)
	}
}

func TestHashTokenDeterministicAndHex(t *testing.T) {
	a := HashToken("abc")
	b := HashToken("abc")
	if a != b {
		t.Fatalf("HashToken not deterministic: %q != %q", a, b)
	}
	if len(a) != 64 { // 32-byte digest → 64 hex chars
		t.Fatalf("digest length = %d, want 64", len(a))
	}
	if HashToken("abc") == HashToken("abz") {
		t.Fatal("distinct inputs hashed equal")
	}
}

func TestVerifyToken(t *testing.T) {
	h := HashToken("secret-token")
	cases := []struct {
		name        string
		plain, hash string
		want        bool
	}{
		{"match", "secret-token", h, true},
		{"wrong-plain", "other", h, false},
		{"empty-plain", "", h, false},
		{"empty-hash", "secret-token", "", false},
		{"both-empty", "", "", false},
		{"length-mismatch", "secret-token", "deadbeef", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := VerifyToken(tc.plain, tc.hash); got != tc.want {
				t.Fatalf("VerifyToken(%q,%q) = %v, want %v", tc.plain, tc.hash, got, tc.want)
			}
		})
	}
}

func TestGenerateTokenUniqueAndURLSafe(t *testing.T) {
	seen := make(map[string]struct{})
	for range 200 {
		tok := GenerateToken()
		if len(tok) != 43 { // 32 bytes RawURLEncoding → 43 chars
			t.Fatalf("token length = %d, want 43: %q", len(tok), tok)
		}
		for _, c := range tok {
			ok := (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
				(c >= '0' && c <= '9') || c == '-' || c == '_'
			if !ok {
				t.Fatalf("non-url-safe char %q in %q", c, tok)
			}
		}
		if _, dup := seen[tok]; dup {
			t.Fatalf("duplicate token %q", tok)
		}
		seen[tok] = struct{}{}
	}
}

func TestInviteMatchesTokenHash(t *testing.T) {
	inv := &Invite{TunnelToken: "tok"}
	if !InviteMatchesTokenHash(inv, HashToken("tok")) {
		t.Fatal("expected match")
	}
	if InviteMatchesTokenHash(inv, HashToken("nope")) {
		t.Fatal("unexpected match")
	}
	if InviteMatchesTokenHash(nil, HashToken("tok")) {
		t.Fatal("nil invite must not match")
	}
}

// --- token record store ---

func TestTokenRecordCRUD(t *testing.T) {
	s := NewMemStore()
	if _, ok := s.GetToken("t1"); ok {
		t.Fatal("expected absent")
	}
	if _, ok := s.DeleteToken("t1"); ok {
		t.Fatal("delete absent should report ok=false")
	}
	rec := TokenRecord{TunnelType: "http", SharePage: "inspect", CreatedAt: 1, ExpiresAt: 2}
	s.PutToken("t1", rec)
	got, ok := s.GetToken("t1")
	if !ok || got.TunnelType != "http" {
		t.Fatalf("GetToken = %+v ok=%v", got, ok)
	}
	list := s.ListTokens()
	if len(list) != 1 || list["t1"].SharePage != "inspect" {
		t.Fatalf("ListTokens = %+v", list)
	}
	// snapshot is a copy: mutating it must not affect the store.
	list["t1"] = TokenRecord{}
	if g, _ := s.GetToken("t1"); g.TunnelType != "http" {
		t.Fatal("ListTokens must return a copy")
	}
	del, ok := s.DeleteToken("t1")
	if !ok || del.TunnelType != "http" {
		t.Fatalf("DeleteToken = %+v ok=%v", del, ok)
	}
	if _, ok := s.GetToken("t1"); ok {
		t.Fatal("expected deleted")
	}
}

// --- invites ---

func ip(v string) *string { return &v }

func TestIssueAndConsumeInvite(t *testing.T) {
	s := NewMemStore()
	now := 1000.0
	share, control := s.IssueInvites("sess1", "share-tok", "control-tok", now+3600, now, ip("1.2.3.4"))
	if share == "" || control == "" || share == control {
		t.Fatalf("bad invites share=%q control=%q", share, control)
	}

	// viewer invite → viewer role, share token.
	inv := s.ConsumeInvite(share, "sess1", now+1)
	if inv == nil || inv.Role != RoleViewer || inv.TunnelToken != "share-tok" {
		t.Fatalf("viewer invite = %+v", inv)
	}
	if inv.IssuedIP == nil || *inv.IssuedIP != "1.2.3.4" {
		t.Fatalf("issued ip not carried: %+v", inv.IssuedIP)
	}
	// single-use: second consume fails.
	if again := s.ConsumeInvite(share, "sess1", now+1); again != nil {
		t.Fatal("invite must be single-use")
	}

	// operator invite → operator role, control token.
	invOp := s.ConsumeInvite(control, "sess1", now+1)
	if invOp == nil || invOp.Role != RoleOperator || invOp.TunnelToken != "control-tok" {
		t.Fatalf("operator invite = %+v", invOp)
	}
}

func TestConsumeInviteExpiryClampedToTunnel(t *testing.T) {
	s := NewMemStore()
	now := 1000.0
	// Tunnel expires in 10s, well below InviteTTLS=300 → invite expiry clamps to
	// tunnel expiry (now+10).
	share, _ := s.IssueInvites("sess1", "share-tok", "control-tok", now+10, now, nil)
	if inv := s.ConsumeInvite(share, "sess1", now+11); inv != nil {
		t.Fatal("invite should be expired at tunnel expiry")
	}
}

func TestConsumeInviteExpiryUsesInviteTTL(t *testing.T) {
	s := NewMemStore()
	now := 1000.0
	// Tunnel expiry far away → invite TTL (300s) governs.
	share, _ := s.IssueInvites("sess1", "share-tok", "control-tok", now+100000, now, nil)
	if inv := s.ConsumeInvite(share, "sess1", now+299); inv == nil {
		t.Fatal("invite should still be valid before InviteTTLS")
	}
	share2, _ := s.IssueInvites("sess1", "share-tok", "control-tok", now+100000, now, nil)
	if inv := s.ConsumeInvite(share2, "sess1", now+301); inv != nil {
		t.Fatal("invite should be expired after InviteTTLS")
	}
}

func TestConsumeInviteRejections(t *testing.T) {
	s := NewMemStore()
	now := 1000.0
	share, _ := s.IssueInvites("sess1", "share-tok", "control-tok", now+3600, now, nil)

	if inv := s.ConsumeInvite("", "sess1", now); inv != nil {
		t.Fatal("empty invite must fail")
	}
	if inv := s.ConsumeInvite("   ", "sess1", now); inv != nil {
		t.Fatal("whitespace invite must fail")
	}
	if inv := s.ConsumeInvite("no-such-invite", "sess1", now); inv != nil {
		t.Fatal("unknown invite must fail")
	}
	// wrong session id → burns the invite AND returns nil.
	if inv := s.ConsumeInvite(share, "other-sess", now+1); inv != nil {
		t.Fatal("session mismatch must fail")
	}
	if inv := s.ConsumeInvite(share, "sess1", now+1); inv != nil {
		t.Fatal("invite must have been consumed even on failed validation")
	}
}

func TestConsumeInviteEmptyTunnelToken(t *testing.T) {
	s := NewMemStore()
	now := 1000.0
	// An invite whose stored tunnel token is blank must not validate.
	share, _ := s.IssueInvites("sess1", "   ", "control-tok", now+3600, now, nil)
	if inv := s.ConsumeInvite(share, "sess1", now+1); inv != nil {
		t.Fatal("blank tunnel token must fail")
	}
}

func TestDiscardInvitesForSession(t *testing.T) {
	s := NewMemStore()
	now := 1000.0
	shareA, _ := s.IssueInvites("sessA", "sa", "ca", now+3600, now, nil)
	shareB, _ := s.IssueInvites("sessB", "sb", "cb", now+3600, now, nil)
	s.DiscardInvitesForSession("sessA")
	if inv := s.ConsumeInvite(shareA, "sessA", now+1); inv != nil {
		t.Fatal("sessA invites should be gone")
	}
	if inv := s.ConsumeInvite(shareB, "sessB", now+1); inv == nil {
		t.Fatal("sessB invites should remain")
	}
}

func TestSweepExpired(t *testing.T) {
	s := NewMemStore()
	now := 1000.0
	shareFresh, _ := s.IssueInvites("fresh", "sf", "cf", now+100000, now, nil)
	shareStale, _ := s.IssueInvites("stale", "ss", "cs", now+10, now, nil)
	// Sweep at a time past the stale tunnel expiry but before fresh.
	s.SweepExpired(now + 50)
	if inv := s.ConsumeInvite(shareStale, "stale", now+50); inv != nil {
		t.Fatal("stale invite should be swept")
	}
	if inv := s.ConsumeInvite(shareFresh, "fresh", now+50); inv == nil {
		t.Fatal("fresh invite should survive sweep")
	}
}

// TestConsumeInviteInvalidRole covers the defensive role guard (mirrors the
// Python role check). IssueInvites can never produce a bad role, so the record
// is injected directly to exercise the branch.
func TestConsumeInviteInvalidRole(t *testing.T) {
	s := NewMemStore()
	raw := GenerateToken()
	s.invites[HashToken(raw)] = inviteRecord{
		sessionID:   "sess1",
		role:        Role("bogus"),
		tunnelToken: "tok",
		expiresAt:   1e12,
	}
	if inv := s.ConsumeInvite(raw, "sess1", 1); inv != nil {
		t.Fatalf("invalid role must fail, got %+v", inv)
	}
}

// TestConcurrentAccess exercises the store under -race.
func TestConcurrentAccess(t *testing.T) {
	s := NewMemStore()
	var wg sync.WaitGroup
	for i := range 50 {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			id := "t"
			s.PutToken(id, TokenRecord{CreatedAt: float64(n)})
			_, _ = s.GetToken(id)
			s.ListTokens()
			share, _ := s.IssueInvites("sess", "st", "ct", 1e12, 0, nil)
			s.ConsumeInvite(share, "sess", 1)
			s.SweepExpired(0)
			s.DiscardInvitesForSession("sess")
			s.DeleteToken(id)
		}(i)
	}
	wg.Wait()
}
