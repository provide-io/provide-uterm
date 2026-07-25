//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package controlplane

import (
	"database/sql/driver"
	"fmt"
)

// NullString is a comparable, nullable string. Unlike a *string it compares by
// value with ==, which the memory backend's optimistic-concurrency check relies
// on (Python dataclass equality). It implements database/sql's Scanner and
// driver.Valuer so records scan/bind directly against the SQLite backend.
//
// Port of the Python “str | None“ optional fields.
type NullString struct {
	String string
	Valid  bool // false == SQL NULL / Python None
}

// Str builds a present NullString.
func Str(v string) NullString { return NullString{String: v, Valid: true} }

// NullStr builds an absent (NULL) NullString.
func NullStr() NullString { return NullString{} }

// Value implements driver.Valuer.
func (n NullString) Value() (driver.Value, error) {
	if !n.Valid {
		return nil, nil
	}
	return n.String, nil
}

// Scan implements sql.Scanner.
func (n *NullString) Scan(src any) error {
	if src == nil {
		n.String, n.Valid = "", false
		return nil
	}
	switch v := src.(type) {
	case string:
		n.String, n.Valid = v, true
	case []byte:
		n.String, n.Valid = string(v), true
	default:
		return fmt.Errorf("controlplane: cannot scan %T into NullString", src)
	}
	return nil
}

// NullFloat is a comparable, nullable float64 (SQLite REAL). See NullString for
// why a value type is used instead of *float64.
//
// Port of the Python “float | None“ optional fields.
type NullFloat struct {
	Float64 float64
	Valid   bool
}

// Float builds a present NullFloat.
func Float(v float64) NullFloat { return NullFloat{Float64: v, Valid: true} }

// NullFlt builds an absent (NULL) NullFloat.
func NullFlt() NullFloat { return NullFloat{} }

// Value implements driver.Valuer.
func (n NullFloat) Value() (driver.Value, error) {
	if !n.Valid {
		return nil, nil
	}
	return n.Float64, nil
}

// Scan implements sql.Scanner.
func (n *NullFloat) Scan(src any) error {
	if src == nil {
		n.Float64, n.Valid = 0, false
		return nil
	}
	switch v := src.(type) {
	case float64:
		n.Float64, n.Valid = v, true
	case int64:
		n.Float64, n.Valid = float64(v), true
	default:
		return fmt.Errorf("controlplane: cannot scan %T into NullFloat", src)
	}
	return nil
}

// SessionRecord mirrors provide.uterm.control.plane.session.types.SessionRecord.
type SessionRecord struct {
	SessionID      string
	DisplayName    string
	ConnectorType  string
	Owner          NullString
	Visibility     string // "public" | "operator" | "private"
	LifecycleState string // "waiting" | "running" | "stopped" | "error" | "deleted"
	CreatedAt      float64
	UpdatedAt      float64
	DeletedAt      NullFloat
}

// SessionTokenRecord mirrors control.plane.token.types.SessionTokenRecord.
type SessionTokenRecord struct {
	SessionID  string
	TokenKind  string
	TokenValue string
	CreatedAt  float64
	ExpiresAt  NullFloat
	RevokedAt  NullFloat
}

// ResumeTokenRecord mirrors control.plane.token.types.ResumeTokenRecord.
type ResumeTokenRecord struct {
	TokenValue     string
	SessionID      string
	Role           string
	CreatedAt      float64
	ExpiresAt      float64
	WasHijackOwner bool // stored as INTEGER 0/1 in SQLite
	RevokedAt      NullFloat
}

// ApprovalRecord mirrors control.plane.approval.types.ApprovalRecord.
type ApprovalRecord struct {
	ApprovalID  string
	SessionID   string
	Command     string
	RequestedBy NullString
	State       string // "pending" | "approved" | "rejected"
	CreatedAt   float64
	ResolvedAt  NullFloat
	ResolvedBy  NullString
}

// LeaseRecord mirrors control.plane.lease.types.LeaseRecord.
type LeaseRecord struct {
	SessionID      string
	HijackID       string
	Owner          string
	LeaseExpiresAt float64
	CreatedAt      float64
	DeletedAt      NullFloat
}

// GraphicalTargetRecord mirrors control.plane.graphical_target.types.
// GraphicalTargetRecord — the persistence shape of graphical.Definition.
//
// Config holds the protocol-specific parameter object as canonical JSON text
// rather than a map. The memory backend's optimistic-concurrency check compares
// records with ==, so every field must be comparable; a map would not compile
// through detectConflict's “V comparable“ constraint. Storing the JSON also
// binds straight to the TEXT column with no marshalling in the store.
type GraphicalTargetRecord struct {
	TargetID            string
	TenantID            string
	DisplayName         string
	Protocol            string
	Endpoint            NullString
	Secret              NullString
	Width               int64
	Height              int64
	IsSystem            bool // stored as INTEGER 0/1 in SQLite
	IsStatic            bool // stored as INTEGER 0/1 in SQLite
	CaSecretRef         NullString
	ClientCertSecretRef NullString
	ClientKeySecretRef  NullString
	Config              string
	CreatedBy           NullString
	CreatedAt           float64
	UpdatedBy           NullString
	UpdatedAt           NullFloat
}

// AuditHead is the persisted audit-chain head “(seq, record_hash)“.
type AuditHead struct {
	Seq        int64
	RecordHash string
}
