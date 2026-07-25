//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package controlplane

import "context"

// Tx is a control-plane transaction. Port of control.plane.transaction.types.
// Transaction. commit persists; rollback discards. Both are idempotent after
// the transaction is closed.
type Tx interface {
	Commit(ctx context.Context) error
	Rollback(ctx context.Context) error
}

// SessionStore persists session lifecycle records. Port of
// control.plane.session.store.SessionStore. A nil first return means "absent"
// (Python None).
type SessionStore interface {
	Upsert(ctx context.Context, rec SessionRecord) error
	Get(ctx context.Context, sessionID string) (*SessionRecord, error)
	MarkDeleted(ctx context.Context, sessionID string, deletedAt float64) error
}

// TokenStore persists session tokens and single-use resume tokens. Port of
// control.plane.token.store.TokenStore.
type TokenStore interface {
	PutSessionToken(ctx context.Context, rec SessionTokenRecord) error
	GetSessionToken(ctx context.Context, sessionID, tokenKind string) (*SessionTokenRecord, error)
	CreateResumeToken(ctx context.Context, rec ResumeTokenRecord) error
	// GetResumeToken returns nil for an absent OR revoked token.
	GetResumeToken(ctx context.Context, tokenValue string) (*ResumeTokenRecord, error)
	RevokeResumeToken(ctx context.Context, tokenValue string, revokedAt float64) error
	// ConsumeResumeToken atomically revokes and returns the token on the first
	// call, and nil on any subsequent call (single-use).
	ConsumeResumeToken(ctx context.Context, tokenValue string, revokedAt float64) (*ResumeTokenRecord, error)
}

// ApprovalStore persists command-approval records. Port of
// control.plane.approval.store.ApprovalStore.
type ApprovalStore interface {
	PutApproval(ctx context.Context, rec ApprovalRecord) error
	GetApproval(ctx context.Context, approvalID string) (*ApprovalRecord, error)
	// ListPending returns pending approvals ordered by (created_at, approval_id).
	ListPending(ctx context.Context) ([]ApprovalRecord, error)
}

// LeaseStore persists hijack-lease records. Port of
// control.plane.lease.store.LeaseStore.
type LeaseStore interface {
	PutLease(ctx context.Context, rec LeaseRecord) error
	GetLease(ctx context.Context, sessionID string) (*LeaseRecord, error)
	ClearLease(ctx context.Context, sessionID string) error
}

// GraphicalTargetStore persists graphical-target definitions. Port of
// control.plane.graphical_target.store.GraphicalTargetStore. A nil first return
// from Get means "absent" (Python None).
//
// Tenant isolation is NOT enforced here: this is a row layer, and the caller
// already holds a Scope derived from the authenticated principal. Filtering by
// tenant here too would double-gate reads and hide scope bugs from the
// registry's own tests.
type GraphicalTargetStore interface {
	PutGraphicalTarget(ctx context.Context, rec GraphicalTargetRecord) error
	GetGraphicalTarget(ctx context.Context, targetID string) (*GraphicalTargetRecord, error)
	// ListGraphicalTargets returns every row ordered by target_id.
	ListGraphicalTargets(ctx context.Context) ([]GraphicalTargetRecord, error)
	// DeleteGraphicalTarget reports whether a row was actually removed.
	DeleteGraphicalTarget(ctx context.Context, targetID string) (bool, error)
}

// Engine is a control-plane backend. Port of the control.plane.bootstrap.
// ControlPlane protocol plus the store-factory methods the concrete engines
// expose. The store-factory methods take a Tx obtained from Begin; passing a Tx
// from a different engine type panics.
type Engine interface {
	// Capabilities returns the engine feature flags.
	Capabilities() EngineCapabilities

	Open(ctx context.Context) error
	Close(ctx context.Context) error
	Migrate(ctx context.Context) error
	Begin(ctx context.Context) (Tx, error)

	// Reap physically drops rows whose soft-delete/expiry timestamp is older
	// than now-retentionS and returns the number removed.
	Reap(ctx context.Context, now float64, retentionS int) (int, error)

	// GetAuditHead returns the persisted audit-chain head, or nil at genesis.
	GetAuditHead(ctx context.Context) (*AuditHead, error)
	// SetAuditHead persists the head monotonically: a lower-or-equal seq is a
	// no-op (anti-rollback guard).
	SetAuditHead(ctx context.Context, seq int64, recordHash string) error

	SessionStore(tx Tx) SessionStore
	TokenStore(tx Tx) TokenStore
	ApprovalStore(tx Tx) ApprovalStore
	LeaseStore(tx Tx) LeaseStore
	GraphicalTargetStore(tx Tx) GraphicalTargetStore
}
