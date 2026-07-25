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

// graphicalTargetStore is the SQLite GraphicalTargetStore. Port of
// control.plane.sqlite.graphical_target_store.SqliteGraphicalTargetStore.
type graphicalTargetStore struct {
	conn *sql.Conn
}

// graphicalTargetColumns is the explicit column list. Spelled out (rather than
// SELECT *) so a future migration that adds a column cannot silently shift the
// Scan order underneath the reader.
const graphicalTargetColumns = `target_id, tenant_id, display_name, protocol, endpoint, secret, ` +
	`width, height, is_system, is_static, ca_secret_ref, client_cert_secret_ref, ` +
	`client_key_secret_ref, config, created_by, created_at, updated_by, updated_at`

// PutGraphicalTarget inserts or updates a target row.
func (s *graphicalTargetStore) PutGraphicalTarget(ctx context.Context, rec cp.GraphicalTargetRecord) error {
	_, err := s.conn.ExecContext(ctx, `
            INSERT INTO cp_graphical_targets(
                target_id, tenant_id, display_name, protocol, endpoint, secret,
                width, height, is_system, is_static, ca_secret_ref,
                client_cert_secret_ref, client_key_secret_ref, config,
                created_by, created_at, updated_by, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(target_id) DO UPDATE SET
                tenant_id = excluded.tenant_id,
                display_name = excluded.display_name,
                protocol = excluded.protocol,
                endpoint = excluded.endpoint,
                secret = excluded.secret, -- pragma: allowlist secret
                width = excluded.width,
                height = excluded.height,
                is_system = excluded.is_system,
                is_static = excluded.is_static,
                ca_secret_ref = excluded.ca_secret_ref, -- pragma: allowlist secret
                client_cert_secret_ref = excluded.client_cert_secret_ref, -- pragma: allowlist secret
                client_key_secret_ref = excluded.client_key_secret_ref, -- pragma: allowlist secret
                config = excluded.config,
                created_by = excluded.created_by,
                created_at = excluded.created_at,
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
            `,
		rec.TargetID, rec.TenantID, rec.DisplayName, rec.Protocol, rec.Endpoint, rec.Secret,
		rec.Width, rec.Height, boolToInt(rec.IsSystem), boolToInt(rec.IsStatic), rec.CaSecretRef,
		rec.ClientCertSecretRef, rec.ClientKeySecretRef, graphicalConfigOrEmpty(rec.Config),
		rec.CreatedBy, rec.CreatedAt, rec.UpdatedBy, rec.UpdatedAt)
	return err
}

// graphicalConfigOrEmpty keeps the NOT NULL config column satisfied when a
// record carries the zero value: an empty string is not valid JSON, and the
// column's default only applies when the value is omitted entirely.
func graphicalConfigOrEmpty(config string) string {
	if config == "" {
		return "{}"
	}
	return config
}

// scanGraphicalTarget reads one row in graphicalTargetColumns order.
func scanGraphicalTarget(row interface{ Scan(...any) error }) (*cp.GraphicalTargetRecord, error) {
	var rec cp.GraphicalTargetRecord
	var isSystem, isStatic int64
	err := row.Scan(&rec.TargetID, &rec.TenantID, &rec.DisplayName, &rec.Protocol,
		&rec.Endpoint, &rec.Secret, &rec.Width, &rec.Height, &isSystem, &isStatic,
		&rec.CaSecretRef, &rec.ClientCertSecretRef, &rec.ClientKeySecretRef, &rec.Config,
		&rec.CreatedBy, &rec.CreatedAt, &rec.UpdatedBy, &rec.UpdatedAt)
	if err != nil {
		return nil, err
	}
	rec.IsSystem = isSystem != 0
	rec.IsStatic = isStatic != 0
	return &rec, nil
}

// GetGraphicalTarget returns the target, or nil if absent.
func (s *graphicalTargetStore) GetGraphicalTarget(
	ctx context.Context, targetID string,
) (*cp.GraphicalTargetRecord, error) {
	row := s.conn.QueryRowContext(ctx,
		"SELECT "+graphicalTargetColumns+" FROM cp_graphical_targets WHERE target_id = ?", targetID)
	rec, err := scanGraphicalTarget(row)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return rec, nil
}

// ListGraphicalTargets returns every row ordered by target_id.
func (s *graphicalTargetStore) ListGraphicalTargets(ctx context.Context) ([]cp.GraphicalTargetRecord, error) {
	rows, err := s.conn.QueryContext(ctx,
		"SELECT "+graphicalTargetColumns+" FROM cp_graphical_targets ORDER BY target_id")
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()

	out := make([]cp.GraphicalTargetRecord, 0)
	for rows.Next() {
		rec, scanErr := scanGraphicalTarget(rows)
		if scanErr != nil {
			return nil, scanErr
		}
		out = append(out, *rec)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return out, nil
}

// DeleteGraphicalTarget reports whether a row was actually removed.
func (s *graphicalTargetStore) DeleteGraphicalTarget(ctx context.Context, targetID string) (bool, error) {
	res, err := s.conn.ExecContext(ctx, "DELETE FROM cp_graphical_targets WHERE target_id = ?", targetID)
	if err != nil {
		return false, err
	}
	affected, err := res.RowsAffected()
	if err != nil {
		return false, err
	}
	return affected > 0, nil
}
