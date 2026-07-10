//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sqlite_test

import (
	"context"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/sqlite"
)

// A non-numeric string stored in a REAL-affinity column stays TEXT, so scanning
// it into a float64 fails — this drives the non-NoRows scan-error branch of
// every store Get path deterministically.

func TestScanErrorsSurfaceFromStores(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	tests := []struct {
		name   string
		insert string
		read   func(t *testing.T, e *sqlite.Engine, ctx context.Context) error
	}{
		{
			name: "session",
			insert: "INSERT INTO cp_sessions(session_id, display_name, connector_type, visibility, " +
				"lifecycle_state, created_at, updated_at) VALUES('s1','n','pty','private','waiting','oops',1.0)",
			read: func(t *testing.T, e *sqlite.Engine, ctx context.Context) error {
				tx, _ := e.Begin(ctx)
				defer func() { _ = tx.Rollback(ctx) }()
				_, err := e.SessionStore(tx).Get(ctx, "s1")
				return err
			},
		},
		{
			name: "session_token",
			insert: "INSERT INTO cp_session_tokens(session_id, token_kind, token_value, created_at) " +
				"VALUES('s1','share','v','oops')",
			read: func(t *testing.T, e *sqlite.Engine, ctx context.Context) error {
				tx, _ := e.Begin(ctx)
				defer func() { _ = tx.Rollback(ctx) }()
				_, err := e.TokenStore(tx).GetSessionToken(ctx, "s1", "share")
				return err
			},
		},
		{
			name: "resume_token",
			insert: "INSERT INTO cp_resume_tokens(token_value, session_id, role, created_at, expires_at) " +
				"VALUES('r1','s1','viewer','oops',2.0)",
			read: func(t *testing.T, e *sqlite.Engine, ctx context.Context) error {
				tx, _ := e.Begin(ctx)
				defer func() { _ = tx.Rollback(ctx) }()
				_, err := e.TokenStore(tx).GetResumeToken(ctx, "r1")
				return err
			},
		},
		{
			name: "approval",
			insert: "INSERT INTO cp_approvals(approval_id, session_id, command, state, created_at) " +
				"VALUES('a1','s1','ls','pending','oops')",
			read: func(t *testing.T, e *sqlite.Engine, ctx context.Context) error {
				tx, _ := e.Begin(ctx)
				defer func() { _ = tx.Rollback(ctx) }()
				_, err := e.ApprovalStore(tx).GetApproval(ctx, "a1")
				return err
			},
		},
		{
			name: "approval_list_pending",
			insert: "INSERT INTO cp_approvals(approval_id, session_id, command, state, created_at) " +
				"VALUES('a1','s1','ls','pending','oops')",
			read: func(t *testing.T, e *sqlite.Engine, ctx context.Context) error {
				tx, _ := e.Begin(ctx)
				defer func() { _ = tx.Rollback(ctx) }()
				_, err := e.ApprovalStore(tx).ListPending(ctx)
				return err
			},
		},
		{
			name: "lease",
			insert: "INSERT INTO cp_leases(session_id, hijack_id, owner, lease_expires_at, created_at) " +
				"VALUES('s1','h','alice','oops',1.0)",
			read: func(t *testing.T, e *sqlite.Engine, ctx context.Context) error {
				tx, _ := e.Begin(ctx)
				defer func() { _ = tx.Rollback(ctx) }()
				_, err := e.LeaseStore(tx).GetLease(ctx, "s1")
				return err
			},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			e, path := newPlaneWithPath(t)
			raw := openRaw(t, path)
			if _, err := raw.Exec(tc.insert); err != nil {
				t.Fatalf("seed malformed row: %v", err)
			}
			if err := tc.read(t, e, ctx); err == nil {
				t.Fatal("expected a scan error from the malformed row")
			}
		})
	}
}
