//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package deckmux

import (
	"encoding/json"
	"fmt"
	"sync"
	"time"
)

// Caps for the untrusted selection/pin dicts a browser ships in a presence
// update — mirror _presence.py. These bound a memory-amplification /
// injection surface: a legitimate selection is a handful of small ints.
const (
	maxPresenceDictBytes = 2048
	maxPresenceDictKeys  = 16
)

// timeNow is the wall-clock source (Unix seconds), overridable in tests the
// way the Python suite patches _presence.time.
var timeNow = func() float64 { return float64(time.Now().UnixNano()) / 1e9 }

// validatedPresenceFields are the presence keys whose untrusted values are
// size/shape-checked before storage.
var validatedPresenceFields = map[string]struct{}{"selection": {}, "pin": {}}

// UserPresence is the ephemeral presence state for a single user in a session,
// mirroring the _presence.UserPresence dataclass. Selection/Pin/ScrollRange
// carry browser-supplied JSON values verbatim (assigned wholesale, never
// mutated in place, so concurrent readers holding a prior copy never race).
type UserPresence struct {
	UserID         string
	Name           string
	Color          string
	Role           string
	Initials       string
	ScrollLine     int
	ScrollRange    any // nil serializes as [0, 0]
	TotalLines     int
	Selection      map[string]any
	Pin            map[string]any
	Typing         bool
	QueuedKeys     string
	Cols           int
	Rows           int
	LastActivityAt float64
	IsOwner        bool
}

// IsIdle reports whether the user has been idle longer than thresholdS,
// mirroring UserPresence.is_idle.
func (p UserPresence) IsIdle(thresholdS float64) bool {
	return (timeNow() - p.LastActivityAt) > thresholdS
}

// ToDict serializes to a wire dict for JSON transport, mirroring
// UserPresence.to_dict (scroll_range as a 2-list, selection/pin as null when
// unset).
func (p UserPresence) ToDict() map[string]any {
	scrollRange := p.ScrollRange
	if scrollRange == nil {
		scrollRange = []int{0, 0}
	}
	return map[string]any{
		"user_id":      p.UserID,
		"name":         p.Name,
		"color":        p.Color,
		"role":         p.Role,
		"initials":     p.Initials,
		"scroll_line":  p.ScrollLine,
		"scroll_range": scrollRange,
		"total_lines":  p.TotalLines,
		"selection":    p.Selection,
		"pin":          p.Pin,
		"typing":       p.Typing,
		"queued_keys":  p.QueuedKeys,
		"cols":         p.Cols,
		"rows":         p.Rows,
		"is_owner":     p.IsOwner,
	}
}

// PresenceStore is a per-session ephemeral presence registry, mirroring
// _presence.PresenceStore. Unlike the single-threaded Python original it is
// safe for concurrent use. Iteration order is insertion order (like a Python
// dict), which several wire payloads depend on.
type PresenceStore struct {
	mu    sync.RWMutex
	users map[string]*UserPresence
	order []string // insertion order of user ids
}

// NewPresenceStore returns an empty store.
func NewPresenceStore() *PresenceStore {
	return &PresenceStore{users: make(map[string]*UserPresence)}
}

// Add inserts (or replaces) a user and returns a copy of the stored presence.
func (s *PresenceStore) Add(userID, name, color, role, initials string) UserPresence {
	s.mu.Lock()
	defer s.mu.Unlock()
	p := &UserPresence{
		UserID:         userID,
		Name:           name,
		Color:          color,
		Role:           role,
		Initials:       initials,
		LastActivityAt: timeNow(),
	}
	if _, exists := s.users[userID]; !exists {
		s.order = append(s.order, userID)
	}
	s.users[userID] = p
	return *p
}

// Update applies fields to an existing user, returning (copy, true) on
// success or (zero, false) when the user is absent. A nil-safe validation
// pass runs before any mutation, so a rejected selection/pin (error) leaves
// the stored user untouched. Mirrors PresenceStore.update.
func (s *PresenceStore) Update(userID string, fields map[string]any) (UserPresence, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p, ok := s.users[userID]
	if !ok {
		return UserPresence{}, false, nil
	}
	// Validate every field up front so a rejected value leaves state intact.
	for k, v := range fields {
		if !knownPresenceField(k) {
			return UserPresence{}, false, fmt.Errorf("unknown presence field: %s", k)
		}
		if _, guarded := validatedPresenceFields[k]; guarded {
			if err := validatePresenceDict(k, v); err != nil {
				return UserPresence{}, false, err
			}
		}
	}
	for k, v := range fields {
		setPresenceField(p, k, v)
	}
	p.LastActivityAt = timeNow()
	return *p, true, nil
}

// Remove deletes a user, returning the removed copy (or false when absent).
func (s *PresenceStore) Remove(userID string) (UserPresence, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	p, ok := s.users[userID]
	if !ok {
		return UserPresence{}, false
	}
	delete(s.users, userID)
	s.dropOrder(userID)
	return *p, true
}

// Get returns a copy of a user's presence (or false when absent).
func (s *PresenceStore) Get(userID string) (UserPresence, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	p, ok := s.users[userID]
	if !ok {
		return UserPresence{}, false
	}
	return *p, true
}

// GetAll returns copies of all users in insertion order.
func (s *PresenceStore) GetAll() []UserPresence {
	s.mu.RLock()
	defer s.mu.RUnlock()
	out := make([]UserPresence, 0, len(s.order))
	for _, uid := range s.order {
		out = append(out, *s.users[uid])
	}
	return out
}

// GetOwner returns the current owner (first flagged, in insertion order), if
// any.
func (s *PresenceStore) GetOwner() (UserPresence, bool) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, uid := range s.order {
		if p := s.users[uid]; p.IsOwner {
			return *p, true
		}
	}
	return UserPresence{}, false
}

// SetOwner marks userID as the sole owner (clearing any previous owner).
func (s *PresenceStore) SetOwner(userID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for uid, p := range s.users {
		p.IsOwner = uid == userID
	}
}

// ClearOwner clears the owner flag from every user.
func (s *PresenceStore) ClearOwner() {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, p := range s.users {
		p.IsOwner = false
	}
}

// PruneIdle removes users idle longer than thresholdS and returns their ids
// (in insertion order).
func (s *PresenceStore) PruneIdle(thresholdS float64) []string {
	s.mu.Lock()
	defer s.mu.Unlock()
	var stale []string
	for _, uid := range s.order {
		if s.users[uid].IsIdle(thresholdS) {
			stale = append(stale, uid)
		}
	}
	for _, uid := range stale {
		delete(s.users, uid)
		s.dropOrder(uid)
	}
	return stale
}

// GetSyncPayload builds a presence_sync message with every current user.
func (s *PresenceStore) GetSyncPayload(config map[string]any) map[string]any {
	s.mu.RLock()
	defer s.mu.RUnlock()
	users := make([]map[string]any, 0, len(s.order))
	for _, uid := range s.order {
		users = append(users, s.users[uid].ToDict())
	}
	return MakePresenceSync(users, config)
}

// TakenColors returns the set of colors currently in use.
func (s *PresenceStore) TakenColors() map[string]struct{} {
	s.mu.RLock()
	defer s.mu.RUnlock()
	taken := make(map[string]struct{}, len(s.users))
	for _, p := range s.users {
		taken[p.Color] = struct{}{}
	}
	return taken
}

// Count returns the number of users in the store.
func (s *PresenceStore) Count() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.users)
}

// dropOrder removes userID from the insertion-order slice. Caller holds the
// write lock.
func (s *PresenceStore) dropOrder(userID string) {
	for i, uid := range s.order {
		if uid == userID {
			s.order = append(s.order[:i], s.order[i+1:]...)
			return
		}
	}
}

// knownPresenceField reports whether key names a settable UserPresence field
// (mirrors the Python hasattr(p, k) guard).
func knownPresenceField(key string) bool {
	switch key {
	case "scroll_line", "scroll_range", "total_lines", "selection", "pin",
		"typing", "queued_keys", "cols", "rows", "is_owner", "initials",
		"name", "color", "role", "user_id", "last_activity_at":
		return true
	}
	return false
}

// setPresenceField sets a single validated field on p. All known fields are
// handled here (validation happened in the caller), so it never fails.
func setPresenceField(p *UserPresence, key string, value any) {
	switch key {
	case "scroll_line":
		p.ScrollLine = mustInt(value)
	case "total_lines":
		p.TotalLines = mustInt(value)
	case "cols":
		p.Cols = mustInt(value)
	case "rows":
		p.Rows = mustInt(value)
	case "scroll_range":
		p.ScrollRange = value
	case "selection":
		p.Selection = asDict(value)
	case "pin":
		p.Pin = asDict(value)
	case "typing":
		p.Typing = asBool(value)
	case "queued_keys":
		p.QueuedKeys = asString(value)
	case "is_owner":
		p.IsOwner = asBool(value)
	case "initials":
		p.Initials = asString(value)
	case "name":
		p.Name = asString(value)
	case "color":
		p.Color = asString(value)
	case "role":
		p.Role = asString(value)
	case "user_id":
		p.UserID = asString(value)
	case "last_activity_at":
		p.LastActivityAt = asFloat(value)
	}
}

// validatePresenceDict validates an untrusted selection/pin value before
// storage, mirroring _validate_presence_dict. nil is allowed (clears the
// field).
func validatePresenceDict(field string, value any) error {
	if value == nil {
		return nil
	}
	m, ok := value.(map[string]any)
	if !ok {
		return fmt.Errorf("invalid presence %s: must be a dict or None", field)
	}
	if len(m) > maxPresenceDictKeys {
		return fmt.Errorf("invalid presence %s: too many keys (%d > %d)", field, len(m), maxPresenceDictKeys)
	}
	// Deviation from Python (json.dumps default separators include spaces):
	// Go's compact json.Marshal yields a slightly smaller byte count, so the
	// exact 2048-byte boundary differs by inter-element whitespace only. No
	// caller probes that boundary.
	encoded, err := json.Marshal(m)
	if err == nil && len(encoded) > maxPresenceDictBytes {
		return fmt.Errorf("invalid presence %s: too large (%d > %d bytes)", field, len(encoded), maxPresenceDictBytes)
	}
	return nil
}

// asDict coerces a validated selection/pin value to map[string]any (nil for a
// cleared field).
func asDict(v any) map[string]any {
	if m, ok := v.(map[string]any); ok {
		return m
	}
	return nil
}

// mustInt coerces a JSON-ish numeric to int (JSON numbers decode as float64).
func mustInt(v any) int {
	switch n := v.(type) {
	case int:
		return n
	case int32:
		return int(n)
	case int64:
		return int(n)
	case float64:
		return int(n)
	case float32:
		return int(n)
	}
	return 0
}

// asFloat coerces a numeric to float64.
func asFloat(v any) float64 {
	switch n := v.(type) {
	case float64:
		return n
	case float32:
		return float64(n)
	case int:
		return float64(n)
	case int64:
		return float64(n)
	}
	return 0
}

// asBool coerces a value to bool (non-bool → false).
func asBool(v any) bool {
	b, _ := v.(bool)
	return b
}

// asString coerces a value to string (non-string → "").
func asString(v any) string {
	s, _ := v.(string)
	return s
}
