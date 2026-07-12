//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package memory

import (
	"context"
	"sync"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
)

// Engine is the in-memory control-plane backend with shared mutable state. Port
// of control.plane.memory.engine.MemoryControlPlane.
type Engine struct {
	config cp.Config
	caps   cp.EngineCapabilities
	state  *State
	lock   sync.Mutex
	// auditMu guards audit-head access, mirroring Python's use of the same
	// asyncio lock for the head; kept separate here so head reads/writes never
	// contend with transaction commits.
	auditMu sync.Mutex
}

// New builds an in-memory Engine from config. Defaults are applied so a
// zero-value Config yields the Python defaults.
func New(config cp.Config) *Engine {
	config = config.Normalized()
	return &Engine{
		config: config,
		caps:   config.Capabilities,
		state:  newState(),
	}
}

// Capabilities returns the engine feature flags.
func (e *Engine) Capabilities() cp.EngineCapabilities { return e.caps }

// Open is a no-op for the memory backend.
func (e *Engine) Open(context.Context) error { return nil }

// Close is a no-op for the memory backend.
func (e *Engine) Close(context.Context) error { return nil }

// Migrate is a no-op for the memory backend.
func (e *Engine) Migrate(context.Context) error { return nil }

// Begin starts a new snapshot-isolated transaction.
func (e *Engine) Begin(context.Context) (cp.Tx, error) {
	e.lock.Lock()
	defer e.lock.Unlock()
	return newTransaction(e.state, &e.lock), nil
}

// State exposes the shared root state so tests can seed rows directly (mirroring
// the Python tests that reach into “plane._state“).
func (e *Engine) State() *State { return e.state }

// Reap physically drops rows whose soft-delete/expiry timestamp is older than
// now-retentionS (strict <, IS-NOT-NULL guards). Returns the number removed.
// Port of MemoryControlPlane.reap.
func (e *Engine) Reap(_ context.Context, now float64, retentionS int) (int, error) {
	cutoff := now - float64(retentionS)
	e.lock.Lock()
	defer e.lock.Unlock()
	s := e.state
	before := len(s.ResumeTokens) + len(s.SessionTokens) + len(s.Sessions) + len(s.Leases) + len(s.Approvals)

	for k, r := range s.ResumeTokens {
		if (r.RevokedAt.Valid && r.RevokedAt.Float64 < cutoff) || r.ExpiresAt < cutoff {
			delete(s.ResumeTokens, k)
		}
	}
	for k, r := range s.SessionTokens {
		if (r.RevokedAt.Valid && r.RevokedAt.Float64 < cutoff) ||
			(r.ExpiresAt.Valid && r.ExpiresAt.Float64 < cutoff) {
			delete(s.SessionTokens, k)
		}
	}
	for k, r := range s.Sessions {
		if r.DeletedAt.Valid && r.DeletedAt.Float64 < cutoff {
			delete(s.Sessions, k)
		}
	}
	for k, r := range s.Leases {
		if (r.DeletedAt.Valid && r.DeletedAt.Float64 < cutoff) || r.LeaseExpiresAt < cutoff {
			delete(s.Leases, k)
		}
	}
	for k, r := range s.Approvals {
		if r.ResolvedAt.Valid && r.ResolvedAt.Float64 < cutoff {
			delete(s.Approvals, k)
		}
	}

	after := len(s.ResumeTokens) + len(s.SessionTokens) + len(s.Sessions) + len(s.Leases) + len(s.Approvals)
	return before - after, nil
}

// GetAuditHead returns the in-memory audit-chain head, or nil if unset.
// NON-DURABLE: the head is lost on restart.
func (e *Engine) GetAuditHead(context.Context) (*cp.AuditHead, error) {
	e.auditMu.Lock()
	defer e.auditMu.Unlock()
	if e.state.AuditHead == nil {
		return nil, nil
	}
	head := *e.state.AuditHead
	return &head, nil
}

// SetAuditHead persists the head monotonically: a lower-or-equal seq is a no-op.
func (e *Engine) SetAuditHead(_ context.Context, seq int64, recordHash string) error {
	e.auditMu.Lock()
	defer e.auditMu.Unlock()
	if e.state.AuditHead != nil && e.state.AuditHead.Seq >= seq {
		return nil
	}
	e.state.AuditHead = &cp.AuditHead{Seq: seq, RecordHash: recordHash}
	return nil
}

// txState resolves a Tx to a memory transaction's working state, panicking on a
// foreign Tx (the same failure mode as passing the wrong engine's tx in Python).
func txState(tx cp.Tx) *State {
	mt, ok := tx.(*Transaction)
	if !ok {
		panic("memory: transaction is not a *memory.Transaction")
	}
	return mt.state
}

// SessionStore returns a session store bound to tx.
func (e *Engine) SessionStore(tx cp.Tx) cp.SessionStore { return &sessionStore{state: txState(tx)} }

// TokenStore returns a token store bound to tx.
func (e *Engine) TokenStore(tx cp.Tx) cp.TokenStore { return &tokenStore{state: txState(tx)} }

// ApprovalStore returns an approval store bound to tx.
func (e *Engine) ApprovalStore(tx cp.Tx) cp.ApprovalStore { return &approvalStore{state: txState(tx)} }

// LeaseStore returns a lease store bound to tx.
func (e *Engine) LeaseStore(tx cp.Tx) cp.LeaseStore { return &leaseStore{state: txState(tx)} }

// GraphicalTargetStore returns a graphical target store bound to tx.
func (e *Engine) GraphicalTargetStore(tx cp.Tx) cp.GraphicalTargetStore {
	return &graphicalTargetStore{state: txState(tx)}
}
