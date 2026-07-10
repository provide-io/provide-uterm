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

// leaseStore is the SQLite LeaseStore. Port of control.plane.sqlite.lease_store.
// SqliteLeaseStore.
type leaseStore struct {
	eng  *Engine
	conn *sql.Conn
}

// PutLease inserts or updates a lease row.
func (s *leaseStore) PutLease(ctx context.Context, rec cp.LeaseRecord) error {
	_, err := s.conn.ExecContext(ctx, `
            INSERT INTO cp_leases(session_id, hijack_id, owner, lease_expires_at, created_at, deleted_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                hijack_id = excluded.hijack_id,
                owner = excluded.owner,
                lease_expires_at = excluded.lease_expires_at,
                created_at = excluded.created_at,
                deleted_at = excluded.deleted_at
            `,
		rec.SessionID, rec.HijackID, rec.Owner, rec.LeaseExpiresAt, rec.CreatedAt, rec.DeletedAt)
	return err
}

// GetLease returns the lease, or nil if absent OR soft-deleted (deleted_at set).
func (s *leaseStore) GetLease(ctx context.Context, sessionID string) (*cp.LeaseRecord, error) {
	row := s.conn.QueryRowContext(ctx,
		"SELECT session_id, hijack_id, owner, lease_expires_at, created_at, deleted_at "+
			"FROM cp_leases WHERE session_id = ?", sessionID)
	var rec cp.LeaseRecord
	err := row.Scan(&rec.SessionID, &rec.HijackID, &rec.Owner, &rec.LeaseExpiresAt, &rec.CreatedAt, &rec.DeletedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	if rec.DeletedAt.Valid {
		return nil, nil
	}
	return &rec, nil
}

// ClearLease soft-deletes a lease by stamping deleted_at with the engine clock.
func (s *leaseStore) ClearLease(ctx context.Context, sessionID string) error {
	_, err := s.conn.ExecContext(ctx,
		"UPDATE cp_leases SET deleted_at = ? WHERE session_id = ?", s.eng.now(), sessionID)
	return err
}
