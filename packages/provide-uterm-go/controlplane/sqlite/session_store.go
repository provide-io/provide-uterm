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

// sessionStore is the SQLite SessionStore. Port of control.plane.sqlite.
// session_store.SqliteSessionStore.
type sessionStore struct{ conn *sql.Conn }

// Upsert inserts or updates a session row.
func (s *sessionStore) Upsert(ctx context.Context, rec cp.SessionRecord) error {
	_, err := s.conn.ExecContext(ctx, `
            INSERT INTO cp_sessions(
                session_id, display_name, connector_type, owner, visibility,
                lifecycle_state, created_at, updated_at, deleted_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                display_name = excluded.display_name,
                connector_type = excluded.connector_type,
                owner = excluded.owner,
                visibility = excluded.visibility,
                lifecycle_state = excluded.lifecycle_state,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                deleted_at = excluded.deleted_at
            `,
		rec.SessionID, rec.DisplayName, rec.ConnectorType, rec.Owner, rec.Visibility,
		rec.LifecycleState, rec.CreatedAt, rec.UpdatedAt, rec.DeletedAt)
	return err
}

// Get returns the session, or nil if absent.
func (s *sessionStore) Get(ctx context.Context, sessionID string) (*cp.SessionRecord, error) {
	row := s.conn.QueryRowContext(ctx,
		"SELECT session_id, display_name, connector_type, owner, visibility, "+
			"lifecycle_state, created_at, updated_at, deleted_at FROM cp_sessions WHERE session_id = ?",
		sessionID)
	var rec cp.SessionRecord
	err := row.Scan(&rec.SessionID, &rec.DisplayName, &rec.ConnectorType, &rec.Owner, &rec.Visibility,
		&rec.LifecycleState, &rec.CreatedAt, &rec.UpdatedAt, &rec.DeletedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &rec, nil
}

// MarkDeleted soft-deletes a session.
func (s *sessionStore) MarkDeleted(ctx context.Context, sessionID string, deletedAt float64) error {
	_, err := s.conn.ExecContext(ctx, `
            UPDATE cp_sessions
            SET lifecycle_state = 'deleted',
                deleted_at = ?,
                updated_at = ?
            WHERE session_id = ?
            `,
		deletedAt, deletedAt, sessionID)
	return err
}
