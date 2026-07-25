//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package graphical

import (
	"context"
	"encoding/json"
	"sort"
	"sync"
	"time"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
)

// ControlPlaneRegistry is a Registry whose runtime targets live in the control
// plane, so they survive a restart. It has the same tenant-scope semantics as
// InMemoryRegistry — only the runtime storage differs.
//
// Static targets stay in memory. They are re-seeded from the config file on
// every boot and are immutable at the API boundary, so persisting them would
// create a second source of truth that could drift from the config file.
type ControlPlaneRegistry struct {
	engine cp.Engine

	// mu guards static (and serializes ops, mirroring InMemoryRegistry). The
	// control plane has its own transaction-level concurrency control; this lock
	// keeps the static overlay and the store consistent with each other.
	mu     sync.Mutex
	static map[string]*Definition
	closed bool
	now    func() time.Time
}

// NewControlPlaneRegistry constructs a registry over an opened, migrated engine.
func NewControlPlaneRegistry(engine cp.Engine) *ControlPlaneRegistry {
	return &ControlPlaneRegistry{
		engine: engine,
		static: map[string]*Definition{},
		now:    time.Now,
	}
}

// SetClock overrides the timestamp source (test hook).
func (r *ControlPlaneRegistry) SetClock(now func() time.Time) { r.now = now }

// Close marks the registry closed; every subsequent op returns CodeClosed. It
// does NOT close the engine — the engine's lifetime is the caller's.
func (r *ControlPlaneRegistry) Close() {
	r.mu.Lock()
	r.closed = true
	r.mu.Unlock()
}

func (r *ControlPlaneRegistry) ensureOpen(scope Scope) error {
	if r.closed {
		return newError(CodeClosed, "graphical target registry is closed")
	}
	if !scope.IsValid() {
		return newError(CodeForbidden, "graphical target tenant scope denied")
	}
	return nil
}

// backendError wraps a store failure. The underlying message is deliberately
// dropped: it can carry the database path or driver internals, and this value
// reaches the REST boundary.
func backendError() *Error {
	return newError(CodeBackend, "graphical target backend failed")
}

// withTx runs fn inside a transaction, committing when fn succeeds and rolling
// back otherwise. Read paths pass commit=false so a reader never takes a write
// lock on the SQLite backend.
func (r *ControlPlaneRegistry) withTx(commit bool, fn func(ctx context.Context, s cp.GraphicalTargetStore) error) error {
	ctx := context.Background()
	tx, err := r.engine.Begin(ctx)
	if err != nil {
		return backendError()
	}
	if err := fn(ctx, r.engine.GraphicalTargetStore(tx)); err != nil {
		_ = tx.Rollback(ctx)
		return err
	}
	if !commit {
		_ = tx.Rollback(ctx)
		return nil
	}
	if err := tx.Commit(ctx); err != nil {
		return newError(CodeConflict, "graphical target transaction conflicted")
	}
	return nil
}

// Get returns the target, or (nil, nil) when absent or out of scope.
func (r *ControlPlaneRegistry) Get(scope Scope, targetID string) (*Definition, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := r.ensureOpen(scope); err != nil {
		return nil, err
	}
	if t, ok := r.static[targetID]; ok && scope.Permits(t.TenantID) {
		return t.Clone(), nil
	}

	var found *Definition
	err := r.withTx(false, func(ctx context.Context, s cp.GraphicalTargetStore) error {
		rec, err := s.GetGraphicalTarget(ctx, targetID)
		if err != nil {
			return backendError()
		}
		if rec == nil {
			return nil
		}
		def, cErr := recordToDefinition(*rec)
		if cErr != nil {
			return cErr
		}
		if scope.Permits(def.TenantID) {
			found = def
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return found, nil
}

// List returns runtime + static merged (static wins on id collision),
// tenant-filtered, sorted by target_id — same contract as InMemoryRegistry.
func (r *ControlPlaneRegistry) List(scope Scope) ([]*Definition, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := r.ensureOpen(scope); err != nil {
		return nil, err
	}

	merged := map[string]*Definition{}
	err := r.withTx(false, func(ctx context.Context, s cp.GraphicalTargetStore) error {
		rows, err := s.ListGraphicalTargets(ctx)
		if err != nil {
			return backendError()
		}
		for i := range rows {
			def, cErr := recordToDefinition(rows[i])
			if cErr != nil {
				return cErr
			}
			if scope.Permits(def.TenantID) {
				merged[def.TargetID] = def
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	for id, t := range r.static {
		if scope.Permits(t.TenantID) {
			merged[id] = t.Clone()
		}
	}

	ids := make([]string, 0, len(merged))
	for id := range merged {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	out := make([]*Definition, 0, len(ids))
	for _, id := range ids {
		out = append(out, merged[id].Clone())
	}
	return out, nil
}

// Create persists a new runtime target.
func (r *ControlPlaneRegistry) Create(scope Scope, target *Definition) (*Definition, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := r.ensureOpen(scope); err != nil {
		return nil, err
	}
	clone := target.Clone()
	if !scope.Permits(clone.TenantID) {
		return nil, newError(CodeForbidden, "graphical target tenant scope denied")
	}
	if err := clone.Validate(); err != nil {
		return nil, err
	}
	if _, ok := r.static[clone.TargetID]; ok {
		return nil, newError(CodeAlreadyExists, "graphical target already exists")
	}

	clone.CreatedAt = r.now()
	err := r.withTx(true, func(ctx context.Context, s cp.GraphicalTargetStore) error {
		existing, err := s.GetGraphicalTarget(ctx, clone.TargetID)
		if err != nil {
			return backendError()
		}
		if existing != nil {
			return newError(CodeAlreadyExists, "graphical target already exists")
		}
		rec, cErr := definitionToRecord(clone)
		if cErr != nil {
			return cErr
		}
		if err := s.PutGraphicalTarget(ctx, rec); err != nil {
			return backendError()
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return clone.Clone(), nil
}

// Update replaces an existing runtime target, preserving its creation stamps.
func (r *ControlPlaneRegistry) Update(scope Scope, target *Definition) (*Definition, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := r.ensureOpen(scope); err != nil {
		return nil, err
	}
	clone := target.Clone()
	if !scope.Permits(clone.TenantID) {
		return nil, newError(CodeForbidden, "graphical target tenant scope denied")
	}
	if err := clone.Validate(); err != nil {
		return nil, err
	}
	if _, ok := r.static[clone.TargetID]; ok {
		return nil, newError(CodeImmutable, "static graphical target is immutable")
	}

	err := r.withTx(true, func(ctx context.Context, s cp.GraphicalTargetStore) error {
		rec, err := s.GetGraphicalTarget(ctx, clone.TargetID)
		if err != nil {
			return backendError()
		}
		if rec == nil {
			return newError(CodeNotFound, "graphical target not found")
		}
		current, cErr := recordToDefinition(*rec)
		if cErr != nil {
			return cErr
		}
		if !scope.Permits(current.TenantID) {
			return newError(CodeForbidden, "graphical target tenant scope denied")
		}
		clone.CreatedAt = current.CreatedAt
		clone.CreatedBy = clonePtr(current.CreatedBy)
		updated := r.now()
		clone.UpdatedAt = &updated

		next, cErr := definitionToRecord(clone)
		if cErr != nil {
			return cErr
		}
		if err := s.PutGraphicalTarget(ctx, next); err != nil {
			return backendError()
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return clone.Clone(), nil
}

// Delete removes a runtime target.
func (r *ControlPlaneRegistry) Delete(scope Scope, targetID string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := r.ensureOpen(scope); err != nil {
		return err
	}
	if t, ok := r.static[targetID]; ok {
		if !scope.Permits(t.TenantID) {
			return newError(CodeForbidden, "graphical target tenant scope denied")
		}
		return newError(CodeImmutable, "static graphical target is immutable")
	}

	return r.withTx(true, func(ctx context.Context, s cp.GraphicalTargetStore) error {
		rec, err := s.GetGraphicalTarget(ctx, targetID)
		if err != nil {
			return backendError()
		}
		if rec == nil {
			return newError(CodeNotFound, "graphical target not found")
		}
		current, cErr := recordToDefinition(*rec)
		if cErr != nil {
			return cErr
		}
		if !scope.Permits(current.TenantID) {
			return newError(CodeForbidden, "graphical target tenant scope denied")
		}
		if _, err := s.DeleteGraphicalTarget(ctx, targetID); err != nil {
			return backendError()
		}
		return nil
	})
}

// AddStatic seeds an immutable system target. Static targets are not persisted
// (see the type comment), so this only touches the in-memory overlay.
func (r *ControlPlaneRegistry) AddStatic(target *Definition) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	clone := target.Clone()
	if err := clone.Validate(); err != nil {
		return err
	}
	clone.IsSystem = true
	if _, ok := r.static[clone.TargetID]; ok {
		return newError(CodeConflict, "duplicate graphical target_id")
	}
	r.static[clone.TargetID] = clone
	return nil
}

// definitionToRecord converts to the persistence shape. Times become epoch
// seconds to match every other cp_* table; Config becomes canonical JSON
// (sorted keys, courtesy of encoding/json's map ordering) so the same logical
// config always produces the same bytes.
func definitionToRecord(d *Definition) (cp.GraphicalTargetRecord, error) {
	config := "{}"
	if len(d.Config) > 0 {
		encoded, err := json.Marshal(d.Config)
		if err != nil {
			return cp.GraphicalTargetRecord{}, newError(CodeInvalid, "config is not serializable")
		}
		config = string(encoded)
	}
	rec := cp.GraphicalTargetRecord{
		TargetID:            d.TargetID,
		TenantID:            d.TenantID,
		DisplayName:         d.DisplayName,
		Protocol:            d.Protocol,
		Endpoint:            optString(d.Endpoint),
		Secret:              optString(d.Secret),
		Width:               int64(d.Width),
		Height:              int64(d.Height),
		IsSystem:            d.IsSystem,
		IsStatic:            d.IsStatic,
		CaSecretRef:         optString(d.CaSecretRef),
		ClientCertSecretRef: optString(d.ClientCertSecretRef),
		ClientKeySecretRef:  optString(d.ClientKeySecretRef),
		Config:              config,
		CreatedBy:           optString(d.CreatedBy),
		CreatedAt:           timeToEpoch(d.CreatedAt),
		UpdatedBy:           optString(d.UpdatedBy),
	}
	if d.UpdatedAt != nil {
		rec.UpdatedAt = cp.Float(timeToEpoch(*d.UpdatedAt))
	}
	return rec, nil
}

// recordToDefinition converts back from the persistence shape. A config blob
// that fails to decode degrades to an empty object rather than failing the
// read: the column is non-authoritative protocol metadata, and refusing to list
// every target because one row is malformed turns a cosmetic defect into an
// outage.
func recordToDefinition(rec cp.GraphicalTargetRecord) (*Definition, error) {
	config := map[string]any{}
	if rec.Config != "" {
		decoded := map[string]any{}
		if err := json.Unmarshal([]byte(rec.Config), &decoded); err == nil {
			config = decoded
		}
	}
	d := &Definition{
		TargetID:            rec.TargetID,
		TenantID:            rec.TenantID,
		DisplayName:         rec.DisplayName,
		Protocol:            rec.Protocol,
		Endpoint:            nullToPtr(rec.Endpoint),
		Secret:              nullToPtr(rec.Secret),
		Width:               int(rec.Width),
		Height:              int(rec.Height),
		IsSystem:            rec.IsSystem,
		IsStatic:            rec.IsStatic,
		CaSecretRef:         nullToPtr(rec.CaSecretRef),
		ClientCertSecretRef: nullToPtr(rec.ClientCertSecretRef),
		ClientKeySecretRef:  nullToPtr(rec.ClientKeySecretRef),
		Config:              config,
		CreatedBy:           nullToPtr(rec.CreatedBy),
		CreatedAt:           epochToTime(rec.CreatedAt),
		UpdatedBy:           nullToPtr(rec.UpdatedBy),
	}
	if rec.UpdatedAt.Valid {
		updated := epochToTime(rec.UpdatedAt.Float64)
		d.UpdatedAt = &updated
	}
	return d, nil
}

func optString(s *string) cp.NullString {
	if s == nil {
		return cp.NullStr()
	}
	return cp.Str(*s)
}

func nullToPtr(n cp.NullString) *string {
	if !n.Valid {
		return nil
	}
	v := n.String
	return &v
}

// timeToEpoch/epochToTime round-trip through float seconds. The zero time maps
// to 0 rather than a large negative epoch so an unset CreatedAt stays legible
// in the table.
func timeToEpoch(t time.Time) float64 {
	if t.IsZero() {
		return 0
	}
	return float64(t.UnixNano()) / 1e9
}

func epochToTime(v float64) time.Time {
	if v == 0 {
		return time.Time{}
	}
	return time.Unix(0, int64(v*1e9)).UTC()
}
