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

// GraphicalTargetRecord contains only target configuration and secret
// references; resolved certificate or private-key bytes do not belong here.
// Every field is comparable, making the record immutable-by-value and safe for
// optimistic conflict detection.
type GraphicalTargetRecord struct {
	TargetID                string
	Endpoint                string
	TLSMode                 string
	CASecretRef             NullString
	ClientCertSecretRef     NullString
	ClientKeySecretRef      NullString
	ExpectedServerName      NullString
	AllowedVMPatterns       StringTuple
	TenantID                NullString
	MinimumRole             string
	ConnectTimeoutS         float64
	HandshakeTimeoutS       float64
	ReadTimeoutS            float64
	WriteTimeoutS           float64
	ShutdownTimeoutS        float64
	MaxGRPCMessageBytes     int64
	MaxFramebufferWidth     int64
	MaxFramebufferHeight    int64
	MaxRectangles           int64
	MaxClipboardBytes       int64
	MaxPixelAllocationBytes int64
	AllowedCIDRs            StringTuple
	AuditLabels             AuditLabels
	CreatedAt               float64
	UpdatedAt               float64
}

// AuditHead is the persisted audit-chain head “(seq, record_hash)“.
type AuditHead struct {
	Seq        int64
	RecordHash string
}
