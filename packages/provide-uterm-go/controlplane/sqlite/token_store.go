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

// tokenStore is the SQLite TokenStore. Port of control.plane.sqlite.token_store.
// SqliteTokenStore.
type tokenStore struct {
	eng  *Engine
	conn *sql.Conn
}

// PutSessionToken inserts or updates a session token.
func (s *tokenStore) PutSessionToken(ctx context.Context, rec cp.SessionTokenRecord) error {
	_, err := s.conn.ExecContext(ctx, `
            INSERT INTO cp_session_tokens(session_id, token_kind, token_value, created_at, expires_at, revoked_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, token_kind) DO UPDATE SET
                token_value = excluded.token_value,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at,
                revoked_at = excluded.revoked_at
            `,
		rec.SessionID, rec.TokenKind, rec.TokenValue, rec.CreatedAt, rec.ExpiresAt, rec.RevokedAt)
	return err
}

// GetSessionToken returns the session token, or nil if absent (no revoked
// filter, matching Python).
func (s *tokenStore) GetSessionToken(
	ctx context.Context, sessionID, tokenKind string,
) (*cp.SessionTokenRecord, error) {
	row := s.conn.QueryRowContext(ctx,
		"SELECT session_id, token_kind, token_value, created_at, expires_at, revoked_at "+
			"FROM cp_session_tokens WHERE session_id = ? AND token_kind = ?",
		sessionID, tokenKind)
	var rec cp.SessionTokenRecord
	err := row.Scan(&rec.SessionID, &rec.TokenKind, &rec.TokenValue, &rec.CreatedAt, &rec.ExpiresAt, &rec.RevokedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &rec, nil
}

// CreateResumeToken inserts or updates a resume token (was_hijack_owner stored
// as INTEGER 0/1).
func (s *tokenStore) CreateResumeToken(ctx context.Context, rec cp.ResumeTokenRecord) error {
	_, err := s.conn.ExecContext(ctx, `
            INSERT INTO cp_resume_tokens(
                token_value, session_id, role, created_at, expires_at, was_hijack_owner, revoked_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(token_value) DO UPDATE SET
                session_id = excluded.session_id,
                role = excluded.role,
                created_at = excluded.created_at,
                expires_at = excluded.expires_at,
                was_hijack_owner = excluded.was_hijack_owner,
                revoked_at = excluded.revoked_at
            `,
		rec.TokenValue, rec.SessionID, rec.Role, rec.CreatedAt, rec.ExpiresAt,
		boolToInt(rec.WasHijackOwner), rec.RevokedAt)
	return err
}

// GetResumeToken returns the resume token, or nil if absent OR revoked.
func (s *tokenStore) GetResumeToken(ctx context.Context, tokenValue string) (*cp.ResumeTokenRecord, error) {
	rec, revoked, err := s.scanResume(ctx, tokenValue)
	if err != nil || rec == nil || revoked {
		return nil, err
	}
	return rec, nil
}

// RevokeResumeToken marks a resume token revoked (no-op if absent).
func (s *tokenStore) RevokeResumeToken(ctx context.Context, tokenValue string, revokedAt float64) error {
	_, err := s.conn.ExecContext(ctx,
		"UPDATE cp_resume_tokens SET revoked_at = ? WHERE token_value = ?", revokedAt, tokenValue)
	return err
}

// ConsumeResumeToken atomically revokes and returns the token on first use, nil
// thereafter. Port of SqliteTokenStore.consume_resume_token.
func (s *tokenStore) ConsumeResumeToken(
	ctx context.Context, tokenValue string, revokedAt float64,
) (*cp.ResumeTokenRecord, error) {
	rec, revoked, err := s.scanResume(ctx, tokenValue)
	if err != nil || rec == nil || revoked {
		return nil, err
	}
	if s.eng.consumeUpdateHook != nil {
		s.eng.consumeUpdateHook(ctx, tokenValue)
	}
	res, err := s.conn.ExecContext(ctx,
		"UPDATE cp_resume_tokens SET revoked_at = ? WHERE token_value = ? AND revoked_at IS NULL",
		revokedAt, tokenValue)
	if err != nil {
		return nil, err
	}
	n, err := res.RowsAffected()
	if err != nil {
		return nil, err
	}
	if n != 1 {
		return nil, nil
	}
	// Return the record with revoked_at cleared, matching Python which returns
	// the pre-revoke record shape (revoked_at=None).
	rec.RevokedAt = cp.NullFlt()
	return rec, nil
}

// scanResume reads a resume token row. It returns (nil, false, nil) when absent
// and (rec, revoked, nil) otherwise, where revoked reports revoked_at != NULL.
func (s *tokenStore) scanResume(ctx context.Context, tokenValue string) (*cp.ResumeTokenRecord, bool, error) {
	row := s.conn.QueryRowContext(ctx,
		"SELECT token_value, session_id, role, created_at, expires_at, was_hijack_owner, revoked_at "+
			"FROM cp_resume_tokens WHERE token_value = ?", tokenValue)
	var (
		rec       cp.ResumeTokenRecord
		hijackInt int64
	)
	err := row.Scan(&rec.TokenValue, &rec.SessionID, &rec.Role, &rec.CreatedAt, &rec.ExpiresAt,
		&hijackInt, &rec.RevokedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	rec.WasHijackOwner = hijackInt != 0
	return &rec, rec.RevokedAt.Valid, nil
}

// boolToInt maps a Go bool to SQLite INTEGER 0/1.
func boolToInt(b bool) int64 {
	if b {
		return 1
	}
	return 0
}
