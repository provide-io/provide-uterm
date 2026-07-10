//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sqlite_test

import (
	"context"
	"os"
	"reflect"
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/sqlite"
)

// Fixed retention math: retentionS=100 => cutoff=900.
const (
	reapNow    = 1000.0
	reapReten  = 100
	reapCutoff = reapNow - reapReten // 900.0
)

// putResume inserts a resume token in its own committed transaction.
func putResume(t *testing.T, e *sqlite.Engine, rec cp.ResumeTokenRecord) {
	t.Helper()
	ctx := context.Background()
	tx, _ := e.Begin(ctx)
	if err := e.TokenStore(tx).CreateResumeToken(ctx, rec); err != nil {
		t.Fatal(err)
	}
	_ = tx.Commit(ctx)
}

func TestReapResumeTokens(t *testing.T) {
	t.Parallel()
	e, path := newPlaneWithPath(t)
	putResume(t, e, cp.ResumeTokenRecord{
		TokenValue: "rev-old", SessionID: "s1", Role: "viewer",
		ExpiresAt: reapNow + 10000, RevokedAt: cp.Float(reapCutoff - 1),
	}) // deleted
	putResume(t, e, cp.ResumeTokenRecord{
		TokenValue: "rev-new", SessionID: "s1", Role: "viewer",
		ExpiresAt: reapNow + 10000, RevokedAt: cp.Float(reapCutoff),
	}) // survives (boundary)
	putResume(t, e, cp.ResumeTokenRecord{
		TokenValue: "exp-old", SessionID: "s1", Role: "viewer", ExpiresAt: reapCutoff - 1,
	}) // deleted
	putResume(t, e, cp.ResumeTokenRecord{
		TokenValue: "valid", SessionID: "s1", Role: "viewer", ExpiresAt: reapNow + 10000,
	}) // survives

	deleted, err := e.Reap(context.Background(), reapNow, reapReten)
	if err != nil {
		t.Fatal(err)
	}
	if deleted != 2 {
		t.Fatalf("deleted = %d, want 2", deleted)
	}
	if got := survivors(t, path, "cp_resume_tokens", "token_value"); !reflect.DeepEqual(
		got, map[string]bool{"rev-new": true, "valid": true}) {
		t.Fatalf("survivors = %v", got)
	}
}

func TestReapSessionTokens(t *testing.T) {
	t.Parallel()
	e, path := newPlaneWithPath(t)
	ctx := context.Background()
	put := func(rec cp.SessionTokenRecord) {
		tx, _ := e.Begin(ctx)
		_ = e.TokenStore(tx).PutSessionToken(ctx, rec)
		_ = tx.Commit(ctx)
	}
	put(cp.SessionTokenRecord{SessionID: "s1", TokenKind: "operator", TokenValue: "v1", ExpiresAt: cp.Float(reapCutoff - 1)})
	put(cp.SessionTokenRecord{SessionID: "s2", TokenKind: "operator", TokenValue: "v2", ExpiresAt: cp.NullFlt()})
	put(cp.SessionTokenRecord{
		SessionID: "s3", TokenKind: "operator", TokenValue: "v3", ExpiresAt: cp.NullFlt(), RevokedAt: cp.Float(reapCutoff - 1),
	})

	deleted, _ := e.Reap(ctx, reapNow, reapReten)
	if deleted != 2 {
		t.Fatalf("deleted = %d, want 2", deleted)
	}
	if got := survivors(t, path, "cp_session_tokens", "session_id"); !reflect.DeepEqual(
		got, map[string]bool{"s2": true}) {
		t.Fatalf("survivors = %v", got)
	}
}

func TestReapSoftDeletedSessions(t *testing.T) {
	t.Parallel()
	e, path := newPlaneWithPath(t)
	ctx := context.Background()
	put := func(rec cp.SessionRecord) {
		tx, _ := e.Begin(ctx)
		_ = e.SessionStore(tx).Upsert(ctx, rec)
		_ = tx.Commit(ctx)
	}
	put(cp.SessionRecord{
		SessionID: "dead", DisplayName: "d", ConnectorType: "shell", Visibility: "private",
		LifecycleState: "stopped", DeletedAt: cp.Float(reapCutoff - 1),
	})
	put(cp.SessionRecord{
		SessionID: "live", DisplayName: "l", ConnectorType: "shell", Visibility: "private", LifecycleState: "running",
	})
	deleted, _ := e.Reap(ctx, reapNow, reapReten)
	if deleted != 1 || countRows(t, path, "cp_sessions") != 1 {
		t.Fatalf("deleted=%d rows=%d", deleted, countRows(t, path, "cp_sessions"))
	}
}

func TestReapSoftDeletedAndExpiredLeases(t *testing.T) {
	t.Parallel()
	e, path := newPlaneWithPath(t)
	ctx := context.Background()
	put := func(rec cp.LeaseRecord) {
		tx, _ := e.Begin(ctx)
		_ = e.LeaseStore(tx).PutLease(ctx, rec)
		_ = tx.Commit(ctx)
	}
	put(cp.LeaseRecord{SessionID: "dead", HijackID: "h1", Owner: "alice", LeaseExpiresAt: reapNow + 10000, DeletedAt: cp.Float(reapCutoff - 1)})
	put(cp.LeaseRecord{SessionID: "expired", HijackID: "h3", Owner: "carol", LeaseExpiresAt: reapCutoff - 1})
	put(cp.LeaseRecord{SessionID: "live", HijackID: "h2", Owner: "bob", LeaseExpiresAt: reapNow + 10000})

	deleted, _ := e.Reap(ctx, reapNow, reapReten)
	if deleted != 2 {
		t.Fatalf("deleted = %d, want 2", deleted)
	}
	if got := survivors(t, path, "cp_leases", "session_id"); !reflect.DeepEqual(got, map[string]bool{"live": true}) {
		t.Fatalf("survivors = %v", got)
	}
}

func TestReapResolvedApprovals(t *testing.T) {
	t.Parallel()
	e, path := newPlaneWithPath(t)
	ctx := context.Background()
	put := func(rec cp.ApprovalRecord) {
		tx, _ := e.Begin(ctx)
		_ = e.ApprovalStore(tx).PutApproval(ctx, rec)
		_ = tx.Commit(ctx)
	}
	put(cp.ApprovalRecord{ApprovalID: "a-old", SessionID: "s1", Command: "rm -rf", State: "approved", ResolvedAt: cp.Float(reapCutoff - 1)})
	put(cp.ApprovalRecord{ApprovalID: "a-pending", SessionID: "s1", Command: "ls", State: "pending"})
	deleted, _ := e.Reap(ctx, reapNow, reapReten)
	if deleted != 1 || countRows(t, path, "cp_approvals") != 1 {
		t.Fatalf("deleted=%d rows=%d", deleted, countRows(t, path, "cp_approvals"))
	}
}

func TestReapBoundaryIsStrict(t *testing.T) {
	t.Parallel()
	e, path := newPlaneWithPath(t)
	ctx := context.Background()
	tx, _ := e.Begin(ctx)
	_ = e.ApprovalStore(tx).PutApproval(ctx, cp.ApprovalRecord{
		ApprovalID: "at-cutoff", SessionID: "s1", Command: "ls", State: "approved", ResolvedAt: cp.Float(reapCutoff),
	})
	_ = tx.Commit(ctx)
	deleted, _ := e.Reap(ctx, reapNow, reapReten)
	if deleted != 0 || countRows(t, path, "cp_approvals") != 1 {
		t.Fatalf("boundary not strict: deleted=%d rows=%d", deleted, countRows(t, path, "cp_approvals"))
	}
}

func TestReapReturnsTotalAcrossTables(t *testing.T) {
	t.Parallel()
	e, _ := newPlaneWithPath(t)
	ctx := context.Background()
	putResume(t, e, cp.ResumeTokenRecord{TokenValue: "rt", SessionID: "s1", Role: "viewer", ExpiresAt: reapCutoff - 1})
	tx, _ := e.Begin(ctx)
	_ = e.SessionStore(tx).Upsert(ctx, cp.SessionRecord{
		SessionID: "dead", DisplayName: "d", ConnectorType: "shell", Visibility: "private",
		LifecycleState: "stopped", DeletedAt: cp.Float(reapCutoff - 1),
	})
	_ = e.ApprovalStore(tx).PutApproval(ctx, cp.ApprovalRecord{
		ApprovalID: "a", SessionID: "s1", Command: "ls", State: "approved", ResolvedAt: cp.Float(reapCutoff - 1),
	})
	_ = tx.Commit(ctx)
	deleted, _ := e.Reap(ctx, reapNow, reapReten)
	if deleted != 3 {
		t.Fatalf("deleted = %d, want 3", deleted)
	}
}

func TestReapTruncatesWALOnRealFile(t *testing.T) {
	t.Parallel()
	e, path := newPlaneWithPath(t)
	ctx := context.Background()
	for i := 0; i < 200; i++ {
		tx, _ := e.Begin(ctx)
		_ = e.ApprovalStore(tx).PutApproval(ctx, cp.ApprovalRecord{
			ApprovalID: "a-" + itoa3(i), SessionID: "s1", Command: "ls", State: "approved",
			ResolvedAt: cp.Float(reapCutoff - 1),
		})
		_ = tx.Commit(ctx)
	}
	deleted, _ := e.Reap(ctx, reapNow, reapReten)
	if deleted != 200 || countRows(t, path, "cp_approvals") != 0 {
		t.Fatalf("deleted=%d rows=%d", deleted, countRows(t, path, "cp_approvals"))
	}
	if info, err := os.Stat(path + "-wal"); err == nil && info.Size() != 0 {
		t.Fatalf("WAL not truncated: %d bytes", info.Size())
	}
}

func itoa3(i int) string {
	if i == 0 {
		return "0"
	}
	var b []byte
	for i > 0 {
		b = append([]byte{byte('0' + i%10)}, b...)
		i /= 10
	}
	return string(b)
}
