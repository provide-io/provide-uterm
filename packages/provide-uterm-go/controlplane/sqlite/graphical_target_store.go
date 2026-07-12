// SPDX-License-Identifier: AGPL-3.0-or-later

package sqlite

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"unicode/utf8"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
)

type graphicalTargetStore struct{ conn *sql.Conn }

func (s *graphicalTargetStore) Put(ctx context.Context, r cp.GraphicalTargetRecord) error {
	_, err := s.conn.ExecContext(ctx, `INSERT INTO cp_graphical_targets(
target_id, endpoint, tls_mode, ca_secret_ref, client_cert_secret_ref, client_key_secret_ref,
expected_server_name, allowed_vm_patterns, tenant_id, minimum_role, connect_timeout_s,
handshake_timeout_s, read_timeout_s, write_timeout_s, shutdown_timeout_s, max_grpc_message_bytes,
max_framebuffer_width, max_framebuffer_height, max_rectangles, max_clipboard_bytes,
max_pixel_allocation_bytes, allowed_cidrs, audit_labels, created_at, updated_at)
VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(target_id) DO UPDATE SET endpoint=excluded.endpoint, tls_mode=excluded.tls_mode,
ca_secret_ref=excluded.ca_secret_ref, client_cert_secret_ref=excluded.client_cert_secret_ref, -- pragma: allowlist secret
client_key_secret_ref=excluded.client_key_secret_ref, expected_server_name=excluded.expected_server_name, -- pragma: allowlist secret
allowed_vm_patterns=excluded.allowed_vm_patterns, tenant_id=excluded.tenant_id,
minimum_role=excluded.minimum_role, connect_timeout_s=excluded.connect_timeout_s,
handshake_timeout_s=excluded.handshake_timeout_s, read_timeout_s=excluded.read_timeout_s,
write_timeout_s=excluded.write_timeout_s, shutdown_timeout_s=excluded.shutdown_timeout_s,
max_grpc_message_bytes=excluded.max_grpc_message_bytes, max_framebuffer_width=excluded.max_framebuffer_width,
max_framebuffer_height=excluded.max_framebuffer_height, max_rectangles=excluded.max_rectangles,
max_clipboard_bytes=excluded.max_clipboard_bytes, max_pixel_allocation_bytes=excluded.max_pixel_allocation_bytes,
allowed_cidrs=excluded.allowed_cidrs, audit_labels=excluded.audit_labels,
created_at=excluded.created_at, updated_at=excluded.updated_at`,
		r.TargetID, r.Endpoint, r.TLSMode, r.CASecretRef, r.ClientCertSecretRef, r.ClientKeySecretRef,
		r.ExpectedServerName, r.AllowedVMPatterns.JSON(), r.TenantID, r.MinimumRole, r.ConnectTimeoutS,
		r.HandshakeTimeoutS, r.ReadTimeoutS, r.WriteTimeoutS, r.ShutdownTimeoutS, r.MaxGRPCMessageBytes,
		r.MaxFramebufferWidth, r.MaxFramebufferHeight, r.MaxRectangles, r.MaxClipboardBytes,
		r.MaxPixelAllocationBytes, r.AllowedCIDRs.JSON(), r.AuditLabels.JSON(), r.CreatedAt, r.UpdatedAt)
	return err
}

const graphicalTargetColumns = `target_id, endpoint, tls_mode, ca_secret_ref, client_cert_secret_ref,
client_key_secret_ref, expected_server_name, allowed_vm_patterns, tenant_id, minimum_role,
connect_timeout_s, handshake_timeout_s, read_timeout_s, write_timeout_s, shutdown_timeout_s,
max_grpc_message_bytes, max_framebuffer_width, max_framebuffer_height, max_rectangles,
max_clipboard_bytes, max_pixel_allocation_bytes, allowed_cidrs, audit_labels, created_at, updated_at`

type targetScanner interface{ Scan(...any) error }

func scanGraphicalTarget(row targetScanner) (*cp.GraphicalTargetRecord, error) {
	var r cp.GraphicalTargetRecord
	var patternsRaw, cidrsRaw, labelsRaw any
	err := row.Scan(&r.TargetID, &r.Endpoint, &r.TLSMode, &r.CASecretRef, &r.ClientCertSecretRef,
		&r.ClientKeySecretRef, &r.ExpectedServerName, &patternsRaw, &r.TenantID, &r.MinimumRole,
		&r.ConnectTimeoutS, &r.HandshakeTimeoutS, &r.ReadTimeoutS, &r.WriteTimeoutS, &r.ShutdownTimeoutS,
		&r.MaxGRPCMessageBytes, &r.MaxFramebufferWidth, &r.MaxFramebufferHeight, &r.MaxRectangles,
		&r.MaxClipboardBytes, &r.MaxPixelAllocationBytes, &cidrsRaw, &labelsRaw, &r.CreatedAt, &r.UpdatedAt)
	if err != nil {
		return nil, err
	}
	if r.AllowedVMPatterns, err = decodeStringTuple(patternsRaw, r.TargetID, "allowed_vm_patterns"); err != nil {
		return nil, err
	}
	if r.AllowedCIDRs, err = decodeStringTuple(cidrsRaw, r.TargetID, "allowed_cidrs"); err != nil {
		return nil, err
	}
	if r.AuditLabels, err = decodeAuditLabels(labelsRaw, r.TargetID); err != nil {
		return nil, err
	}
	return &r, nil
}

func textJSON(raw any, targetID, field string) (string, error) {
	text, ok := raw.(string)
	if !ok {
		return "", cp.DataError(fmt.Sprintf("graphical target %q has invalid %s: stored value is not text", targetID, field))
	}
	if !utf8.ValidString(text) {
		return "", cp.DataError(fmt.Sprintf("graphical target %q has invalid %s: stored text is not valid UTF-8", targetID, field))
	}
	return text, nil
}

func decodeStringTuple(raw any, targetID, field string) (cp.StringTuple, error) {
	text, err := textJSON(raw, targetID, field)
	if err != nil {
		return cp.StringTuple{}, err
	}
	var values []string
	if err := json.Unmarshal([]byte(text), &values); err != nil {
		var shape json.RawMessage
		if json.Unmarshal([]byte(text), &shape) != nil {
			return cp.StringTuple{}, cp.DataError(fmt.Sprintf("graphical target %q has invalid %s: malformed JSON", targetID, field))
		}
		return cp.StringTuple{}, cp.DataError(fmt.Sprintf("graphical target %q has invalid %s: expected a list of strings", targetID, field))
	}
	if values == nil {
		return cp.StringTuple{}, cp.DataError(fmt.Sprintf("graphical target %q has invalid %s: expected a list of strings", targetID, field))
	}
	return cp.NewStringTuple(values...), nil
}

func decodeAuditLabels(raw any, targetID string) (cp.AuditLabels, error) {
	text, err := textJSON(raw, targetID, "audit_labels")
	if err != nil {
		return cp.AuditLabels{}, err
	}
	var pairs []json.RawMessage
	if err := json.Unmarshal([]byte(text), &pairs); err != nil {
		var shape json.RawMessage
		if json.Unmarshal([]byte(text), &shape) != nil {
			return cp.AuditLabels{}, cp.DataError(fmt.Sprintf("graphical target %q has invalid audit_labels: malformed JSON", targetID))
		}
		return cp.AuditLabels{}, cp.DataError(fmt.Sprintf("graphical target %q has invalid audit_labels: expected label pairs", targetID))
	}
	if pairs == nil {
		return cp.AuditLabels{}, cp.DataError(fmt.Sprintf("graphical target %q has invalid audit_labels: expected label pairs", targetID))
	}
	labels := make([]cp.AuditLabel, 0, len(pairs))
	for _, rawPair := range pairs {
		var values []string
		if json.Unmarshal(rawPair, &values) != nil || len(values) != 2 {
			return cp.AuditLabels{}, cp.DataError(fmt.Sprintf("graphical target %q has invalid audit_labels: expected label pairs", targetID))
		}
		labels = append(labels, cp.AuditLabel{Key: values[0], Value: values[1]})
	}
	return cp.NewAuditLabels(labels...), nil
}

func (s *graphicalTargetStore) Get(ctx context.Context, targetID string) (*cp.GraphicalTargetRecord, error) {
	r, err := scanGraphicalTarget(s.conn.QueryRowContext(ctx, "SELECT "+graphicalTargetColumns+" FROM cp_graphical_targets WHERE target_id = ?", targetID))
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	return r, err
}

func (s *graphicalTargetStore) List(ctx context.Context) ([]cp.GraphicalTargetRecord, error) {
	rows, err := s.conn.QueryContext(ctx, "SELECT "+graphicalTargetColumns+" FROM cp_graphical_targets ORDER BY target_id ASC")
	if err != nil {
		return nil, err
	}
	defer func() { _ = rows.Close() }()
	var out []cp.GraphicalTargetRecord
	for rows.Next() {
		r, err := scanGraphicalTarget(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, *r)
	}
	return out, rows.Err()
}

func (s *graphicalTargetStore) Delete(ctx context.Context, targetID string) (bool, error) {
	result, err := s.conn.ExecContext(ctx, "DELETE FROM cp_graphical_targets WHERE target_id = ?", targetID)
	if err != nil {
		return false, err
	}
	n, err := result.RowsAffected()
	return n > 0, err
}
