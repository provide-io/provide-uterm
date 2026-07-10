//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package sqlite

import (
	"context"
	"database/sql"
	"errors"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
)

// approvalStore is the SQLite ApprovalStore. Port of control.plane.sqlite.
// approval_store.SqliteApprovalStore.
type approvalStore struct{ conn *sql.Conn }

const approvalColumns = "approval_id, session_id, command, requested_by, state, created_at, resolved_at, resolved_by"

// PutApproval inserts or updates an approval row.
func (s *approvalStore) PutApproval(ctx context.Context, rec cp.ApprovalRecord) error {
	_, err := s.conn.ExecContext(ctx, `
            INSERT INTO cp_approvals(
                approval_id, session_id, command, requested_by, state,
                created_at, resolved_at, resolved_by
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(approval_id) DO UPDATE SET
                session_id = excluded.session_id,
                command = excluded.command,
                requested_by = excluded.requested_by,
                state = excluded.state,
                created_at = excluded.created_at,
                resolved_at = excluded.resolved_at,
                resolved_by = excluded.resolved_by
            `,
		rec.ApprovalID, rec.SessionID, rec.Command, rec.RequestedBy, rec.State,
		rec.CreatedAt, rec.ResolvedAt, rec.ResolvedBy)
	return err
}

// GetApproval returns the approval, or nil if absent.
func (s *approvalStore) GetApproval(ctx context.Context, approvalID string) (*cp.ApprovalRecord, error) {
	row := s.conn.QueryRowContext(ctx,
		"SELECT "+approvalColumns+" FROM cp_approvals WHERE approval_id = ?", approvalID)
	rec, err := scanApproval(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return rec, nil
}

// ListPending returns pending approvals ordered by (created_at, approval_id).
func (s *approvalStore) ListPending(ctx context.Context) ([]cp.ApprovalRecord, error) {
	rows, err := s.conn.QueryContext(ctx,
		"SELECT "+approvalColumns+
			" FROM cp_approvals WHERE state = 'pending' ORDER BY created_at ASC, approval_id ASC")
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()
	var out []cp.ApprovalRecord
	for rows.Next() {
		rec, serr := scanApproval(rows)
		if serr != nil {
			return nil, serr
		}
		out = append(out, *rec)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return out, nil
}

// scanRow abstracts *sql.Row and *sql.Rows so scanApproval serves both.
type scanRow interface{ Scan(dest ...any) error }

// scanApproval scans one approval row.
func scanApproval(r scanRow) (*cp.ApprovalRecord, error) {
	var rec cp.ApprovalRecord
	err := r.Scan(&rec.ApprovalID, &rec.SessionID, &rec.Command, &rec.RequestedBy, &rec.State,
		&rec.CreatedAt, &rec.ResolvedAt, &rec.ResolvedBy)
	if err != nil {
		return nil, err
	}
	return &rec, nil
}
