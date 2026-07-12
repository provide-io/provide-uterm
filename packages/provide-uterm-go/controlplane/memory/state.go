//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package memory

import cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"

// SessionTokenKey is the composite (session_id, token_kind) key. Comparable, so
// it is usable as a map key — mirrors the Python tuple key.
type SessionTokenKey struct {
	SessionID string
	TokenKind string
}

// State is the shared mutable control-plane state. Port of
// control.plane.memory.transaction.MemoryState. The six tables are keyed
// exactly as the Python dicts; audit_head is tracked separately (non-durable)
// and is not part of transaction snapshots.
type State struct {
	SessionTokens    map[SessionTokenKey]cp.SessionTokenRecord
	ResumeTokens     map[string]cp.ResumeTokenRecord
	Sessions         map[string]cp.SessionRecord
	Approvals        map[string]cp.ApprovalRecord
	Leases           map[string]cp.LeaseRecord
	GraphicalTargets map[string]cp.GraphicalTargetRecord
	// AuditHead is the latest audit-chain head; nil until first set. Non-durable.
	AuditHead *cp.AuditHead
}

// newState returns an empty State with all tables initialized.
func newState() *State {
	return &State{
		SessionTokens:    map[SessionTokenKey]cp.SessionTokenRecord{},
		ResumeTokens:     map[string]cp.ResumeTokenRecord{},
		Sessions:         map[string]cp.SessionRecord{},
		Approvals:        map[string]cp.ApprovalRecord{},
		Leases:           map[string]cp.LeaseRecord{},
		GraphicalTargets: map[string]cp.GraphicalTargetRecord{},
	}
}

// copyTables returns a copy of the six tables (not audit_head). Port of
// control.plane.memory.transaction._copy_state, which likewise omits
// audit_head from transaction snapshots.
func (s *State) copyTables() *State {
	out := &State{
		SessionTokens:    make(map[SessionTokenKey]cp.SessionTokenRecord, len(s.SessionTokens)),
		ResumeTokens:     make(map[string]cp.ResumeTokenRecord, len(s.ResumeTokens)),
		Sessions:         make(map[string]cp.SessionRecord, len(s.Sessions)),
		Approvals:        make(map[string]cp.ApprovalRecord, len(s.Approvals)),
		Leases:           make(map[string]cp.LeaseRecord, len(s.Leases)),
		GraphicalTargets: make(map[string]cp.GraphicalTargetRecord, len(s.GraphicalTargets)),
	}
	for k, v := range s.SessionTokens {
		out.SessionTokens[k] = v
	}
	for k, v := range s.ResumeTokens {
		out.ResumeTokens[k] = v
	}
	for k, v := range s.Sessions {
		out.Sessions[k] = v
	}
	for k, v := range s.Approvals {
		out.Approvals[k] = v
	}
	for k, v := range s.Leases {
		out.Leases[k] = v
	}
	for k, v := range s.GraphicalTargets {
		out.GraphicalTargets[k] = v
	}
	return out
}

// equalOpt reports whether two optional map values are value-equal, treating
// absence as a distinct state. Mirrors Python's “dict.get(k)“ (None on
// absence) compared with “==“.
func equalOpt[V comparable](v1 V, ok1 bool, v2 V, ok2 bool) bool {
	if ok1 != ok2 {
		return false
	}
	return !ok1 || v1 == v2
}

// detectConflict reports whether a key this transaction wrote was concurrently
// changed. Port of _detect_conflict: for every key whose value differs between
// snapshot and working, the current root value must still equal the snapshot
// value, else the transaction conflicts.
func detectConflict[K, V comparable](root, snapshot, working map[K]V) bool {
	for k := range unionKeys(snapshot, working) {
		before, bok := snapshot[k]
		after, aok := working[k]
		if equalOpt(after, aok, before, bok) {
			continue // untouched by this transaction
		}
		rootVal, rok := root[k]
		if !equalOpt(rootVal, rok, before, bok) {
			return true
		}
	}
	return false
}

// mergeTable applies only this transaction's key-level changes onto root. Port
// of _merge_table.
func mergeTable[K, V comparable](root, snapshot, working map[K]V) {
	for k := range unionKeys(snapshot, working) {
		before, bok := snapshot[k]
		after, aok := working[k]
		if equalOpt(after, aok, before, bok) {
			continue
		}
		if aok {
			root[k] = after
		} else {
			delete(root, k)
		}
	}
}

// unionKeys returns the set of keys present in either map.
func unionKeys[K, V comparable](a, b map[K]V) map[K]struct{} {
	out := make(map[K]struct{}, len(a)+len(b))
	for k := range a {
		out[k] = struct{}{}
	}
	for k := range b {
		out[k] = struct{}{}
	}
	return out
}
