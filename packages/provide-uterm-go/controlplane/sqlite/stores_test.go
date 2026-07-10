//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sqlite_test

import (
	"context"
	"sync"
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/sqlite"
)

func newPlane(t *testing.T) *sqlite.Engine {
	t.Helper()
	e, _ := newPlaneWithPath(t)
	return e
}

func TestEngineDefaultsAndFactories(t *testing.T) {
	t.Parallel()
	e := newPlane(t)
	ctx := context.Background()
	if e.Capabilities() != cp.DefaultCapabilities() {
		t.Fatalf("capabilities = %+v", e.Capabilities())
	}
	tx, err := e.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	// Factories must not panic and must return usable stores.
	_ = e.SessionStore(tx)
	_ = e.TokenStore(tx)
	_ = e.ApprovalStore(tx)
	_ = e.LeaseStore(tx)
	_ = tx.Rollback(ctx)
}

func TestSessionStoreRoundTripAndMarkDeleted(t *testing.T) {
	t.Parallel()
	e := newPlane(t)
	ctx := context.Background()
	rec := cp.SessionRecord{
		SessionID: "s1", DisplayName: "Session One", ConnectorType: "shell", Owner: cp.Str("alice"),
		Visibility: "private", LifecycleState: "waiting", CreatedAt: 1.0, UpdatedAt: 2.0,
	}
	tx, _ := e.Begin(ctx)
	_ = e.SessionStore(tx).Upsert(ctx, rec)
	_ = tx.Commit(ctx)

	tx2, _ := e.Begin(ctx)
	got, _ := e.SessionStore(tx2).Get(ctx, "s1")
	_ = tx2.Rollback(ctx)
	if got == nil || *got != rec {
		t.Fatalf("round trip mismatch: %+v", got)
	}

	tx3, _ := e.Begin(ctx)
	_ = e.SessionStore(tx3).MarkDeleted(ctx, "s1", 9.0)
	_ = tx3.Commit(ctx)

	tx4, _ := e.Begin(ctx)
	del, _ := e.SessionStore(tx4).Get(ctx, "s1")
	_ = tx4.Rollback(ctx)
	if del == nil || del.LifecycleState != "deleted" || del.DeletedAt != cp.Float(9.0) {
		t.Fatalf("mark deleted mismatch: %+v", del)
	}
}

func TestTokenStoreRoundTrip(t *testing.T) {
	t.Parallel()
	e := newPlane(t)
	ctx := context.Background()
	sessionToken := cp.SessionTokenRecord{
		SessionID: "s1", TokenKind: "share", TokenValue: "share-token", CreatedAt: 1.0, ExpiresAt: cp.Float(2.0),
	}
	resumeToken := cp.ResumeTokenRecord{
		TokenValue: "resume-token", SessionID: "s1", Role: "viewer",
		CreatedAt: 1.0, ExpiresAt: 2.0, WasHijackOwner: true,
	}
	tx, _ := e.Begin(ctx)
	_ = e.TokenStore(tx).PutSessionToken(ctx, sessionToken)
	_ = e.TokenStore(tx).CreateResumeToken(ctx, resumeToken)
	_ = tx.Commit(ctx)

	tx2, _ := e.Begin(ctx)
	fetchedSession, _ := e.TokenStore(tx2).GetSessionToken(ctx, "s1", "share")
	fetchedResume, _ := e.TokenStore(tx2).GetResumeToken(ctx, "resume-token")
	_ = e.TokenStore(tx2).RevokeResumeToken(ctx, "resume-token", 3.0)
	_ = tx2.Commit(ctx)

	tx3, _ := e.Begin(ctx)
	revoked, _ := e.TokenStore(tx3).GetResumeToken(ctx, "resume-token")
	_ = tx3.Rollback(ctx)

	if fetchedSession == nil || *fetchedSession != sessionToken {
		t.Fatalf("session token mismatch: %+v", fetchedSession)
	}
	if fetchedResume == nil || *fetchedResume != resumeToken {
		t.Fatalf("resume token mismatch: %+v", fetchedResume)
	}
	if revoked != nil {
		t.Fatal("revoked resume token should read nil")
	}
}

func TestTokenStoreConsume(t *testing.T) {
	t.Parallel()
	e := newPlane(t)
	ctx := context.Background()

	// Nonexistent -> nil.
	tx0, _ := e.Begin(ctx)
	if r, _ := e.TokenStore(tx0).ConsumeResumeToken(ctx, "no-such", 1.0); r != nil {
		t.Fatal("consume of missing token should be nil")
	}
	_ = tx0.Commit(ctx)

	rec := cp.ResumeTokenRecord{
		TokenValue: "consume-test", SessionID: "s1", Role: "admin", CreatedAt: 1.0, ExpiresAt: 9999.0,
	}
	tx, _ := e.Begin(ctx)
	_ = e.TokenStore(tx).CreateResumeToken(ctx, rec)
	_ = tx.Commit(ctx)

	tx2, _ := e.Begin(ctx)
	first, _ := e.TokenStore(tx2).ConsumeResumeToken(ctx, "consume-test", 2.0)
	_ = tx2.Commit(ctx)
	if first == nil || first.TokenValue != "consume-test" || first.SessionID != "s1" || first.RevokedAt.Valid {
		t.Fatalf("first consume mismatch: %+v", first)
	}

	tx3, _ := e.Begin(ctx)
	second, _ := e.TokenStore(tx3).ConsumeResumeToken(ctx, "consume-test", 3.0)
	_ = tx3.Commit(ctx)
	if second != nil {
		t.Fatal("second consume should be nil")
	}
}

func TestTokenStoreTreatsSQLLikeStringsAsData(t *testing.T) {
	t.Parallel()
	cases := []struct {
		sessionID, tokenKind, tokenValue, role string
	}{
		{"s1'; DROP TABLE cp_resume_tokens; --", "share", "tok'; DROP TABLE cp_session_tokens; --", "viewer"},
		{`s2" OR "1"="1`, "control", `tok" OR "1"="1`, "admin"},
		{"semi;colon", "share", "value with -- comment", "operator"},
	}
	for _, tc := range cases {
		t.Run(tc.sessionID, func(t *testing.T) {
			t.Parallel()
			e := newPlane(t)
			ctx := context.Background()
			st := cp.SessionTokenRecord{
				SessionID: tc.sessionID, TokenKind: tc.tokenKind, TokenValue: tc.tokenValue,
				CreatedAt: 1.0, ExpiresAt: cp.Float(2.0),
			}
			rt := cp.ResumeTokenRecord{
				TokenValue: "resume::" + tc.tokenValue, SessionID: tc.sessionID, Role: tc.role,
				CreatedAt: 1.0, ExpiresAt: 2.0,
			}
			tx, _ := e.Begin(ctx)
			_ = e.TokenStore(tx).PutSessionToken(ctx, st)
			_ = e.TokenStore(tx).CreateResumeToken(ctx, rt)
			_ = tx.Commit(ctx)

			tx2, _ := e.Begin(ctx)
			gotSession, _ := e.TokenStore(tx2).GetSessionToken(ctx, tc.sessionID, tc.tokenKind)
			gotResume, _ := e.TokenStore(tx2).GetResumeToken(ctx, "resume::"+tc.tokenValue)
			_ = tx2.Rollback(ctx)
			if gotSession == nil || *gotSession != st {
				t.Fatalf("session token not stored as data: %+v", gotSession)
			}
			if gotResume == nil || *gotResume != rt {
				t.Fatalf("resume token not stored as data: %+v", gotResume)
			}
		})
	}
}

func TestApprovalStoreRoundTripAndListPending(t *testing.T) {
	t.Parallel()
	e := newPlane(t)
	ctx := context.Background()
	rec := cp.ApprovalRecord{
		ApprovalID: "a1", SessionID: "s1", Command: "rm -rf /tmp/demo", RequestedBy: cp.Str("alice"),
		State: "pending", CreatedAt: 1.0,
	}
	tx, _ := e.Begin(ctx)
	_ = e.ApprovalStore(tx).PutApproval(ctx, rec)
	_ = tx.Commit(ctx)

	tx2, _ := e.Begin(ctx)
	fetched, _ := e.ApprovalStore(tx2).GetApproval(ctx, "a1")
	pending, _ := e.ApprovalStore(tx2).ListPending(ctx)
	_ = tx2.Rollback(ctx)
	if fetched == nil || *fetched != rec {
		t.Fatalf("approval round trip mismatch: %+v", fetched)
	}
	if len(pending) != 1 || pending[0].ApprovalID != "a1" {
		t.Fatalf("list pending mismatch: %+v", pending)
	}
}

func TestApprovalListPendingOrdering(t *testing.T) {
	t.Parallel()
	e := newPlane(t)
	ctx := context.Background()
	mk := func(id string, createdAt float64) cp.ApprovalRecord {
		return cp.ApprovalRecord{ApprovalID: id, SessionID: "s1", Command: "ls", State: "pending", CreatedAt: createdAt}
	}
	tx, _ := e.Begin(ctx)
	st := e.ApprovalStore(tx)
	_ = st.PutApproval(ctx, mk("c", 30.0))
	_ = st.PutApproval(ctx, mk("a", 10.0))
	_ = st.PutApproval(ctx, mk("b-second", 20.0))
	_ = st.PutApproval(ctx, mk("b-first", 20.0))
	_ = tx.Commit(ctx)

	read, _ := e.Begin(ctx)
	pending, _ := e.ApprovalStore(read).ListPending(ctx)
	_ = read.Rollback(ctx)
	want := []string{"a", "b-first", "b-second", "c"}
	if len(pending) != len(want) {
		t.Fatalf("len = %d, want %d", len(pending), len(want))
	}
	for i, id := range want {
		if pending[i].ApprovalID != id {
			t.Fatalf("order = %v, want %v", pending, want)
		}
	}
}

func TestLeaseStoreRoundTripAndClear(t *testing.T) {
	t.Parallel()
	e := newPlane(t)
	ctx := context.Background()
	rec := cp.LeaseRecord{SessionID: "s1", HijackID: "h1", Owner: "alice", LeaseExpiresAt: 10.0, CreatedAt: 1.0}
	tx, _ := e.Begin(ctx)
	_ = e.LeaseStore(tx).PutLease(ctx, rec)
	_ = tx.Commit(ctx)

	tx2, _ := e.Begin(ctx)
	fetched, _ := e.LeaseStore(tx2).GetLease(ctx, "s1")
	_ = e.LeaseStore(tx2).ClearLease(ctx, "s1")
	_ = tx2.Commit(ctx)

	tx3, _ := e.Begin(ctx)
	cleared, _ := e.LeaseStore(tx3).GetLease(ctx, "s1")
	_ = tx3.Rollback(ctx)
	if fetched == nil || *fetched != rec {
		t.Fatalf("lease round trip mismatch: %+v", fetched)
	}
	if cleared != nil {
		t.Fatal("cleared lease should read nil")
	}
}

func TestMissingLookupsReturnNil(t *testing.T) {
	t.Parallel()
	e := newPlane(t)
	ctx := context.Background()
	tx, _ := e.Begin(ctx)
	if v, _ := e.SessionStore(tx).Get(ctx, "missing"); v != nil {
		t.Fatal("session")
	}
	if v, _ := e.TokenStore(tx).GetSessionToken(ctx, "missing", "resume"); v != nil {
		t.Fatal("session token")
	}
	if v, _ := e.TokenStore(tx).GetResumeToken(ctx, "missing"); v != nil {
		t.Fatal("resume token")
	}
	if v, _ := e.ApprovalStore(tx).GetApproval(ctx, "missing"); v != nil {
		t.Fatal("approval")
	}
	if v, _ := e.LeaseStore(tx).GetLease(ctx, "missing"); v != nil {
		t.Fatal("lease")
	}
	_ = tx.Rollback(ctx)
}

func TestConcurrentTransactions(t *testing.T) {
	t.Parallel()
	e := newPlane(t)
	ctx := context.Background()
	var wg sync.WaitGroup
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func(index int) {
			defer wg.Done()
			tx, err := e.Begin(ctx)
			if err != nil {
				t.Errorf("begin: %v", err)
				return
			}
			_ = e.TokenStore(tx).CreateResumeToken(ctx, cp.ResumeTokenRecord{
				TokenValue: id(index), SessionID: sid(index), Role: "viewer",
				CreatedAt: float64(index), ExpiresAt: float64(index + 10),
			})
			_ = tx.Commit(ctx)
		}(i)
	}
	wg.Wait()

	tx, _ := e.Begin(ctx)
	for i := 0; i < 5; i++ {
		got, _ := e.TokenStore(tx).GetResumeToken(ctx, id(i))
		if got == nil || got.SessionID != sid(i) {
			t.Fatalf("token %d missing or wrong: %+v", i, got)
		}
	}
	_ = tx.Rollback(ctx)
}

func id(i int) string  { return "resume-" + itoa(i) }
func sid(i int) string { return "s" + itoa(i) }
func itoa(i int) string {
	return string(rune('0' + i))
}
