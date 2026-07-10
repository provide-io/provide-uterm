//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package memory_test

import (
	"context"
	"reflect"
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/memory"
)

// A fixed "now" so retention math is deterministic. retentionS=100 => cutoff=900.
const (
	testNow    = 1000.0
	retentionS = 100
	cutoff     = testNow - retentionS // 900.0
)

func resume(tv string, expiresAt float64, revokedAt cp.NullFloat) cp.ResumeTokenRecord {
	return cp.ResumeTokenRecord{
		TokenValue: tv, SessionID: "s1", Role: "viewer",
		CreatedAt: 0, ExpiresAt: expiresAt, RevokedAt: revokedAt,
	}
}

func sessionToken(sid string, expiresAt, revokedAt cp.NullFloat) cp.SessionTokenRecord {
	return cp.SessionTokenRecord{
		SessionID: sid, TokenKind: "operator", TokenValue: "v-" + sid,
		CreatedAt: 0, ExpiresAt: expiresAt, RevokedAt: revokedAt,
	}
}

func session(sid string, deletedAt cp.NullFloat) cp.SessionRecord {
	state := "running"
	if deletedAt.Valid {
		state = "stopped"
	}
	return cp.SessionRecord{
		SessionID: sid, DisplayName: sid, ConnectorType: "shell", Owner: cp.NullStr(),
		Visibility: "private", LifecycleState: state, CreatedAt: 0, UpdatedAt: 0, DeletedAt: deletedAt,
	}
}

func lease(sid string, leaseExpiresAt float64, deletedAt cp.NullFloat) cp.LeaseRecord {
	return cp.LeaseRecord{
		SessionID: sid, HijackID: "h-" + sid, Owner: "alice",
		LeaseExpiresAt: leaseExpiresAt, CreatedAt: 0, DeletedAt: deletedAt,
	}
}

func approval(id string, resolvedAt cp.NullFloat) cp.ApprovalRecord {
	state := "pending"
	if resolvedAt.Valid {
		state = "approved"
	}
	return cp.ApprovalRecord{
		ApprovalID: id, SessionID: "s1", Command: "ls", RequestedBy: cp.NullStr(),
		State: state, CreatedAt: 0, ResolvedAt: resolvedAt,
	}
}

func TestReapSweepsSoftDeletedAndExpired(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.Config{Backend: cp.BackendMemory})
	s := e.State()

	s.ResumeTokens["rev-old"] = resume("rev-old", testNow+10000, cp.Float(cutoff-1))
	s.ResumeTokens["exp-old"] = resume("exp-old", cutoff-1, cp.NullFlt())
	s.ResumeTokens["valid"] = resume("valid", testNow+10000, cp.NullFlt())
	s.ResumeTokens["rev-new"] = resume("rev-new", testNow+10000, cp.Float(cutoff)) // boundary survives

	s.SessionTokens[key("s-exp")] = sessionToken("s-exp", cp.Float(cutoff-1), cp.NullFlt())
	s.SessionTokens[key("s-rev")] = sessionToken("s-rev", cp.NullFlt(), cp.Float(cutoff-1))
	s.SessionTokens[key("s-live")] = sessionToken("s-live", cp.NullFlt(), cp.NullFlt())

	s.Sessions["dead"] = session("dead", cp.Float(cutoff-1))
	s.Sessions["live"] = session("live", cp.NullFlt())

	s.Leases["lease-dead"] = lease("lease-dead", testNow+10000, cp.Float(cutoff-1))
	s.Leases["lease-exp"] = lease("lease-exp", cutoff-1, cp.NullFlt())
	s.Leases["lease-live"] = lease("lease-live", testNow+10000, cp.NullFlt())

	s.Approvals["a-old"] = approval("a-old", cp.Float(cutoff-1))
	s.Approvals["a-pending"] = approval("a-pending", cp.NullFlt())

	deleted, err := e.Reap(context.Background(), testNow, retentionS)
	if err != nil {
		t.Fatal(err)
	}
	if deleted != 8 {
		t.Fatalf("deleted = %d, want 8", deleted)
	}
	assertKeys(t, "resume", mapKeys(s.ResumeTokens), []string{"rev-new", "valid"})
	if len(s.SessionTokens) != 1 {
		t.Fatalf("session tokens survivors = %d, want 1", len(s.SessionTokens))
	}
	if _, ok := s.SessionTokens[key("s-live")]; !ok {
		t.Fatal("s-live should survive")
	}
	assertKeys(t, "sessions", mapKeys(s.Sessions), []string{"live"})
	assertKeys(t, "leases", mapKeys(s.Leases), []string{"lease-live"})
	assertKeys(t, "approvals", mapKeys(s.Approvals), []string{"a-pending"})
}

func TestReapReturnsZeroWhenNothingToRemove(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.Config{Backend: cp.BackendMemory})
	e.State().ResumeTokens["valid"] = resume("valid", testNow+10000, cp.NullFlt())
	deleted, err := e.Reap(context.Background(), testNow, retentionS)
	if err != nil || deleted != 0 {
		t.Fatalf("Reap = %d, %v; want 0, nil", deleted, err)
	}
	if len(e.State().ResumeTokens) != 1 {
		t.Fatal("valid token should survive")
	}
}

func TestNoOpLifecycle(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.DefaultConfig())
	ctx := context.Background()
	if e.Capabilities() != cp.DefaultCapabilities() {
		t.Fatal("unexpected capabilities")
	}
	for _, fn := range []func(context.Context) error{e.Open, e.Close, e.Migrate} {
		if err := fn(ctx); err != nil {
			t.Fatalf("lifecycle no-op returned error: %v", err)
		}
	}
}

func TestSessionStoreGetAndMarkDeleted(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.Config{Backend: cp.BackendMemory})
	ctx := context.Background()
	tx, _ := e.Begin(ctx)
	store := e.SessionStore(tx)

	// mark_deleted on a missing session is a no-op.
	if err := store.MarkDeleted(ctx, "missing", 5.0); err != nil {
		t.Fatal(err)
	}
	rec := session("s1", cp.NullFlt())
	rec.LifecycleState = "waiting"
	_ = store.Upsert(ctx, rec)
	got, _ := store.Get(ctx, "s1")
	if got == nil {
		t.Fatal("expected session")
	}
	_ = store.MarkDeleted(ctx, "s1", 5.0)
	del, _ := store.Get(ctx, "s1")
	if del == nil || del.LifecycleState != "deleted" || del.DeletedAt != cp.Float(5.0) {
		t.Fatalf("mark deleted mismatch: %+v", del)
	}
	_ = tx.Rollback(ctx)
}

func TestApprovalGetAndLeaseClear(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.Config{Backend: cp.BackendMemory})
	ctx := context.Background()
	tx, _ := e.Begin(ctx)
	approvals := e.ApprovalStore(tx)
	rec := cp.ApprovalRecord{ApprovalID: "a1", SessionID: "s1", Command: "ls", State: "pending", CreatedAt: 1.0}
	_ = approvals.PutApproval(ctx, rec)
	got, _ := approvals.GetApproval(ctx, "a1")
	if got == nil || *got != rec {
		t.Fatalf("get approval mismatch: %+v", got)
	}
	if miss, _ := approvals.GetApproval(ctx, "missing"); miss != nil {
		t.Fatal("missing approval should be nil")
	}

	leases := e.LeaseStore(tx)
	l := cp.LeaseRecord{SessionID: "s1", HijackID: "h", Owner: "u", LeaseExpiresAt: 9.0, CreatedAt: 1.0}
	_ = leases.PutLease(ctx, l)
	gl, _ := leases.GetLease(ctx, "s1")
	if gl == nil || *gl != l {
		t.Fatalf("get lease mismatch: %+v", gl)
	}
	_ = leases.ClearLease(ctx, "s1")
	if cl, _ := leases.GetLease(ctx, "s1"); cl != nil {
		t.Fatal("cleared lease should be nil")
	}
	_ = tx.Rollback(ctx)
}

func TestTokenStoreSessionTokenAndRevokeMissing(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.Config{Backend: cp.BackendMemory})
	ctx := context.Background()
	tx, _ := e.Begin(ctx)
	store := e.TokenStore(tx)
	tok := cp.SessionTokenRecord{
		SessionID: "s1", TokenKind: "resume", TokenValue: "tok", CreatedAt: 1.0, ExpiresAt: cp.Float(2.0),
	}
	_ = store.PutSessionToken(ctx, tok)
	got, _ := store.GetSessionToken(ctx, "s1", "resume")
	if got == nil || *got != tok {
		t.Fatalf("get session token mismatch: %+v", got)
	}
	if miss, _ := store.GetSessionToken(ctx, "s1", "missing"); miss != nil {
		t.Fatal("missing session token should be nil")
	}
	// Revoking a non-existent resume token is a no-op.
	if err := store.RevokeResumeToken(ctx, "nope", 3.0); err != nil {
		t.Fatal(err)
	}
	_ = tx.Rollback(ctx)
}

func TestResumeTokenCreateGetRevoke(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.Config{Backend: cp.BackendMemory})
	ctx := context.Background()
	rec := cp.ResumeTokenRecord{
		TokenValue: "resume-1", SessionID: "worker-1", Role: "viewer",
		CreatedAt: 1.0, ExpiresAt: 2.0, WasHijackOwner: true,
	}
	tx, _ := e.Begin(ctx)
	_ = e.TokenStore(tx).CreateResumeToken(ctx, rec)
	_ = tx.Commit(ctx)

	tx2, _ := e.Begin(ctx)
	fetched, _ := e.TokenStore(tx2).GetResumeToken(ctx, "resume-1")
	_ = e.TokenStore(tx2).RevokeResumeToken(ctx, "resume-1", 3.0)
	_ = tx2.Commit(ctx)

	tx3, _ := e.Begin(ctx)
	revoked, _ := e.TokenStore(tx3).GetResumeToken(ctx, "resume-1")
	_ = tx3.Rollback(ctx)

	if fetched == nil || *fetched != rec {
		t.Fatalf("fetched mismatch: %+v", fetched)
	}
	if revoked != nil {
		t.Fatal("revoked token should read as nil")
	}
}

func TestConsumeResumeToken(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.Config{Backend: cp.BackendMemory})
	ctx := context.Background()

	// Non-existent -> nil.
	tx0, _ := e.Begin(ctx)
	if r, _ := e.TokenStore(tx0).ConsumeResumeToken(ctx, "no-such", 1.0); r != nil {
		t.Fatal("consume of missing token should be nil")
	}
	_ = tx0.Commit(ctx)

	rec := cp.ResumeTokenRecord{
		TokenValue: "consume-test", SessionID: "worker-1", Role: "admin", CreatedAt: 1.0, ExpiresAt: 9999.0,
	}
	tx, _ := e.Begin(ctx)
	_ = e.TokenStore(tx).CreateResumeToken(ctx, rec)
	_ = tx.Commit(ctx)

	tx2, _ := e.Begin(ctx)
	first, _ := e.TokenStore(tx2).ConsumeResumeToken(ctx, "consume-test", 2.0)
	_ = tx2.Commit(ctx)
	if first == nil || first.TokenValue != "consume-test" || first.RevokedAt.Valid {
		t.Fatalf("first consume mismatch: %+v", first)
	}

	tx3, _ := e.Begin(ctx)
	second, _ := e.TokenStore(tx3).ConsumeResumeToken(ctx, "consume-test", 3.0)
	_ = tx3.Commit(ctx)
	if second != nil {
		t.Fatal("second consume should be nil")
	}
}

func TestAuditHeadMemory(t *testing.T) {
	t.Parallel()
	e := memory.New(cp.Config{Backend: cp.BackendMemory})
	ctx := context.Background()
	if h, _ := e.GetAuditHead(ctx); h != nil {
		t.Fatal("fresh head should be nil")
	}
	_ = e.SetAuditHead(ctx, 1, "aa")
	if h, _ := e.GetAuditHead(ctx); h == nil || *h != (cp.AuditHead{Seq: 1, RecordHash: "aa"}) {
		t.Fatalf("head mismatch: %+v", h)
	}
	_ = e.SetAuditHead(ctx, 2, "bb")
	// Monotonic: lower and equal are no-ops.
	_ = e.SetAuditHead(ctx, 1, "zz")
	_ = e.SetAuditHead(ctx, 2, "zz")
	if h, _ := e.GetAuditHead(ctx); *h != (cp.AuditHead{Seq: 2, RecordHash: "bb"}) {
		t.Fatalf("monotonic guard failed: %+v", h)
	}
	_ = e.SetAuditHead(ctx, 3, "cc")
	if h, _ := e.GetAuditHead(ctx); *h != (cp.AuditHead{Seq: 3, RecordHash: "cc"}) {
		t.Fatalf("advance failed: %+v", h)
	}
}

// --- helpers ---

func key(sid string) memory.SessionTokenKey {
	return memory.SessionTokenKey{SessionID: sid, TokenKind: "operator"}
}

// mapKeys returns the string keys of a map as a set-like slice.
func mapKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func assertKeys(t *testing.T, label string, got, want []string) {
	t.Helper()
	gset := map[string]bool{}
	for _, k := range got {
		gset[k] = true
	}
	wset := map[string]bool{}
	for _, k := range want {
		wset[k] = true
	}
	if !reflect.DeepEqual(gset, wset) {
		t.Fatalf("%s survivors = %v, want %v", label, got, want)
	}
}
