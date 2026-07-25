//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package memory

import (
	"context"
	"sort"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
)

// sessionStore is the memory SessionStore. Port of MemorySessionStore. It reads
// and writes the transaction's working state directly.
type sessionStore struct{ state *State }

func (s *sessionStore) Upsert(_ context.Context, rec cp.SessionRecord) error {
	s.state.Sessions[rec.SessionID] = rec
	return nil
}

func (s *sessionStore) Get(_ context.Context, sessionID string) (*cp.SessionRecord, error) {
	rec, ok := s.state.Sessions[sessionID]
	if !ok {
		return nil, nil
	}
	return &rec, nil
}

func (s *sessionStore) MarkDeleted(_ context.Context, sessionID string, deletedAt float64) error {
	cur, ok := s.state.Sessions[sessionID]
	if !ok {
		return nil // no-op on a missing session (Python early return)
	}
	cur.DeletedAt = cp.Float(deletedAt)
	cur.LifecycleState = "deleted"
	s.state.Sessions[sessionID] = cur
	return nil
}

// tokenStore is the memory TokenStore. Port of MemoryTokenStore.
type tokenStore struct{ state *State }

func (s *tokenStore) PutSessionToken(_ context.Context, rec cp.SessionTokenRecord) error {
	s.state.SessionTokens[SessionTokenKey{rec.SessionID, rec.TokenKind}] = rec
	return nil
}

func (s *tokenStore) GetSessionToken(_ context.Context, sessionID, tokenKind string) (*cp.SessionTokenRecord, error) {
	rec, ok := s.state.SessionTokens[SessionTokenKey{sessionID, tokenKind}]
	if !ok {
		return nil, nil
	}
	return &rec, nil
}

func (s *tokenStore) CreateResumeToken(_ context.Context, rec cp.ResumeTokenRecord) error {
	s.state.ResumeTokens[rec.TokenValue] = rec
	return nil
}

func (s *tokenStore) GetResumeToken(_ context.Context, tokenValue string) (*cp.ResumeTokenRecord, error) {
	rec, ok := s.state.ResumeTokens[tokenValue]
	if !ok || rec.RevokedAt.Valid {
		return nil, nil
	}
	return &rec, nil
}

func (s *tokenStore) RevokeResumeToken(_ context.Context, tokenValue string, revokedAt float64) error {
	rec, ok := s.state.ResumeTokens[tokenValue]
	if !ok {
		return nil // no-op on a missing token (Python early return)
	}
	rec.RevokedAt = cp.Float(revokedAt)
	s.state.ResumeTokens[tokenValue] = rec
	return nil
}

func (s *tokenStore) ConsumeResumeToken(
	_ context.Context, tokenValue string, revokedAt float64,
) (*cp.ResumeTokenRecord, error) {
	rec, ok := s.state.ResumeTokens[tokenValue]
	if !ok || rec.RevokedAt.Valid {
		return nil, nil
	}
	revoked := rec
	revoked.RevokedAt = cp.Float(revokedAt)
	s.state.ResumeTokens[tokenValue] = revoked
	// Return the record as it stood BEFORE revocation, matching Python which
	// returns the pre-revoke record.
	return &rec, nil
}

// approvalStore is the memory ApprovalStore. Port of MemoryApprovalStore.
type approvalStore struct{ state *State }

func (s *approvalStore) PutApproval(_ context.Context, rec cp.ApprovalRecord) error {
	s.state.Approvals[rec.ApprovalID] = rec
	return nil
}

func (s *approvalStore) GetApproval(_ context.Context, approvalID string) (*cp.ApprovalRecord, error) {
	rec, ok := s.state.Approvals[approvalID]
	if !ok {
		return nil, nil
	}
	return &rec, nil
}

func (s *approvalStore) ListPending(_ context.Context) ([]cp.ApprovalRecord, error) {
	var pending []cp.ApprovalRecord
	for _, rec := range s.state.Approvals {
		if rec.State == "pending" {
			pending = append(pending, rec)
		}
	}
	// Match the sqlite backend's ORDER BY created_at ASC, approval_id ASC so
	// FIFO consumers see the same order regardless of backend.
	sortApprovals(pending)
	return pending, nil
}

// leaseStore is the memory LeaseStore. Port of MemoryLeaseStore.
type leaseStore struct{ state *State }

func (s *leaseStore) PutLease(_ context.Context, rec cp.LeaseRecord) error {
	s.state.Leases[rec.SessionID] = rec
	return nil
}

func (s *leaseStore) GetLease(_ context.Context, sessionID string) (*cp.LeaseRecord, error) {
	rec, ok := s.state.Leases[sessionID]
	if !ok {
		return nil, nil
	}
	return &rec, nil
}

func (s *leaseStore) ClearLease(_ context.Context, sessionID string) error {
	delete(s.state.Leases, sessionID)
	return nil
}

// graphicalTargetStore is the in-memory GraphicalTargetStore. Port of
// control.plane.memory.graphical_target_store.MemoryGraphicalTargetStore.
type graphicalTargetStore struct{ state *State }

func (s *graphicalTargetStore) PutGraphicalTarget(_ context.Context, rec cp.GraphicalTargetRecord) error {
	s.state.GraphicalTargets[rec.TargetID] = rec
	return nil
}

func (s *graphicalTargetStore) GetGraphicalTarget(
	_ context.Context, targetID string,
) (*cp.GraphicalTargetRecord, error) {
	rec, ok := s.state.GraphicalTargets[targetID]
	if !ok {
		return nil, nil
	}
	return &rec, nil
}

// ListGraphicalTargets sorts by target_id so this backend agrees with the
// SQLite one, which gets the order from ORDER BY. Go map iteration is randomized,
// so without the sort the two backends would disagree nondeterministically.
func (s *graphicalTargetStore) ListGraphicalTargets(_ context.Context) ([]cp.GraphicalTargetRecord, error) {
	ids := make([]string, 0, len(s.state.GraphicalTargets))
	for id := range s.state.GraphicalTargets {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	out := make([]cp.GraphicalTargetRecord, 0, len(ids))
	for _, id := range ids {
		out = append(out, s.state.GraphicalTargets[id])
	}
	return out, nil
}

func (s *graphicalTargetStore) DeleteGraphicalTarget(_ context.Context, targetID string) (bool, error) {
	if _, ok := s.state.GraphicalTargets[targetID]; !ok {
		return false, nil
	}
	delete(s.state.GraphicalTargets, targetID)
	return true, nil
}
