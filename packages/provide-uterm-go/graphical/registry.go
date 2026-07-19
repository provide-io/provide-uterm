//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package graphical

import (
	"sort"
	"strings"
	"sync"
	"time"
)

// Scope ports GraphicalTargetScope — a tenant-isolation capability derived from
// the authenticated principal, NEVER from client input. It is either a single
// tenant scope or the system scope (used for seeded/system targets).
type Scope struct {
	tenantID *string
	isSystem bool
}

// ScopeForTenant ports GraphicalTargetScope.TryForTenant: a non-empty tenant id
// yields a tenant scope; empty/blank yields (zero, false).
func ScopeForTenant(tenantID string) (Scope, bool) {
	if trimmed := strings.TrimSpace(tenantID); trimmed == "" {
		return Scope{}, false
	}
	t := tenantID
	return Scope{tenantID: &t, isSystem: false}, true
}

// SystemScope ports GraphicalTargetScope.System.
func SystemScope() Scope { return Scope{tenantID: nil, isSystem: true} }

// IsValid ports GraphicalTargetScope.IsValid: exactly one of system / tenant.
func (s Scope) IsValid() bool { return s.isSystem != (s.tenantID != nil) }

// Permits ports GraphicalTargetScope.Permits: the system scope permits any
// target; a tenant scope permits only targets owned by that tenant.
func (s Scope) Permits(tenantID string) bool {
	if !s.IsValid() {
		return false
	}
	if s.isSystem {
		return true
	}
	return s.tenantID != nil && tenantID == *s.tenantID
}

// Registry ports IGraphicalTargetRegistry.
type Registry interface {
	Get(scope Scope, targetID string) (*Definition, error)
	List(scope Scope) ([]*Definition, error)
	Create(scope Scope, target *Definition) (*Definition, error)
	Update(scope Scope, target *Definition) (*Definition, error)
	Delete(scope Scope, targetID string) error
	AddStatic(target *Definition) error
}

// InMemoryRegistry ports InMemoryGraphicalTargetRegistry — a thread-safe
// registry with immutable seeded static targets + mutable runtime targets.
type InMemoryRegistry struct {
	mu      sync.Mutex
	static  map[string]*Definition
	runtime map[string]*Definition
	closed  bool
	now     func() time.Time
}

// NewInMemoryRegistry constructs an empty registry.
func NewInMemoryRegistry() *InMemoryRegistry {
	return &InMemoryRegistry{
		static:  map[string]*Definition{},
		runtime: map[string]*Definition{},
		now:     time.Now,
	}
}

// SetClock overrides the timestamp source (test hook).
func (r *InMemoryRegistry) SetClock(now func() time.Time) { r.now = now }

// Close marks the registry closed; every subsequent op returns CodeClosed. This
// exposes the C# _closed branch (which has no public setter there) for tests.
func (r *InMemoryRegistry) Close() {
	r.mu.Lock()
	r.closed = true
	r.mu.Unlock()
}

func (r *InMemoryRegistry) ensureOpen(scope Scope) error {
	if r.closed {
		return newError(CodeClosed, "graphical target registry is closed")
	}
	if !scope.IsValid() {
		return newError(CodeForbidden, "graphical target tenant scope denied")
	}
	return nil
}

// Get ports InMemoryGraphicalTargetRegistry.Get.
func (r *InMemoryRegistry) Get(scope Scope, targetID string) (*Definition, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := r.ensureOpen(scope); err != nil {
		return nil, err
	}
	if t, ok := r.static[targetID]; ok && scope.Permits(t.TenantID) {
		return t.Clone(), nil
	}
	if t, ok := r.runtime[targetID]; ok && scope.Permits(t.TenantID) {
		return t.Clone(), nil
	}
	return nil, nil
}

// List ports InMemoryGraphicalTargetRegistry.List — runtime + static merged
// (static wins on id collision), tenant-filtered, sorted by target_id.
func (r *InMemoryRegistry) List(scope Scope) ([]*Definition, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := r.ensureOpen(scope); err != nil {
		return nil, err
	}
	merged := map[string]*Definition{}
	for id, t := range r.runtime {
		if scope.Permits(t.TenantID) {
			merged[id] = t.Clone()
		}
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

// Create ports InMemoryGraphicalTargetRegistry.Create.
func (r *InMemoryRegistry) Create(scope Scope, target *Definition) (*Definition, error) {
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
	if _, ok := r.runtime[clone.TargetID]; ok {
		return nil, newError(CodeAlreadyExists, "graphical target already exists")
	}
	clone.CreatedAt = r.now()
	r.runtime[clone.TargetID] = clone
	return clone.Clone(), nil
}

// Update ports InMemoryGraphicalTargetRegistry.Update.
func (r *InMemoryRegistry) Update(scope Scope, target *Definition) (*Definition, error) {
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
	current, ok := r.runtime[clone.TargetID]
	if !ok {
		return nil, newError(CodeNotFound, "graphical target not found")
	}
	if !scope.Permits(current.TenantID) {
		return nil, newError(CodeForbidden, "graphical target tenant scope denied")
	}
	clone.CreatedAt = current.CreatedAt
	clone.CreatedBy = clonePtr(current.CreatedBy)
	updated := r.now()
	clone.UpdatedAt = &updated
	r.runtime[clone.TargetID] = clone
	return clone.Clone(), nil
}

// Delete ports InMemoryGraphicalTargetRegistry.Delete.
func (r *InMemoryRegistry) Delete(scope Scope, targetID string) error {
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
	current, ok := r.runtime[targetID]
	if !ok {
		return newError(CodeNotFound, "graphical target not found")
	}
	if !scope.Permits(current.TenantID) {
		return newError(CodeForbidden, "graphical target tenant scope denied")
	}
	delete(r.runtime, targetID)
	return nil
}

// AddStatic ports InMemoryGraphicalTargetRegistry.AddStatic — seed an immutable
// system target. Duplicate ids are a programming error (returns *Error).
func (r *InMemoryRegistry) AddStatic(target *Definition) error {
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
