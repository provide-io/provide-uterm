//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package tunnel

import (
	"strings"
	"sync"
)

// Store is the tunnel token + one-time invite persistence surface the server
// tunnel routes depend on. It combines the two Python maps (uterm_tunnel_tokens
// and uterm_tunnel_invites) behind one interface so it can be swapped for a
// durable backend later. All methods are safe for concurrent use.
type Store interface {
	// PutToken stores (or replaces) the at-rest token record for a tunnel.
	PutToken(tunnelID string, rec TokenRecord)
	// GetToken returns the token record for a tunnel; ok=false when absent.
	GetToken(tunnelID string) (TokenRecord, bool)
	// DeleteToken removes and returns the token record; ok=false when absent.
	DeleteToken(tunnelID string) (TokenRecord, bool)
	// ListTokens returns a snapshot copy of every tunnel id → token record.
	ListTokens() map[string]TokenRecord

	// IssueInvites mints single-use viewer + operator invites for a tunnel and
	// stores only their hashed keys. It returns the raw (unhashed) invite
	// strings, which are the only place the raw values ever exist. now is the
	// injected wall clock (seconds); the invite expiry is min(tunnelExpiresAt,
	// now+InviteTTLS).
	IssueInvites(sessionID, shareToken, controlToken string, tunnelExpiresAt, now float64, issuedIP *string) (shareInvite, controlInvite string)
	// ConsumeInvite atomically removes a matching invite (single-use) and
	// returns it, or nil when the invite is missing/expired/mismatched. The
	// invite is consumed even when subsequent validation fails.
	ConsumeInvite(invite, sessionID string, now float64) *Invite
	// DiscardInvitesForSession removes every pending invite for a session.
	DiscardInvitesForSession(sessionID string)
	// SweepExpired removes every invite whose expiry has passed.
	SweepExpired(now float64)
}

// MemStore is the in-memory Store implementation. The zero value is not usable;
// construct it with NewMemStore.
type MemStore struct {
	mu      sync.Mutex
	tokens  map[string]TokenRecord
	invites map[string]inviteRecord // key = HashToken(rawInvite)
}

// NewMemStore returns an empty, ready-to-use in-memory tunnel store.
func NewMemStore() *MemStore {
	return &MemStore{
		tokens:  make(map[string]TokenRecord),
		invites: make(map[string]inviteRecord),
	}
}

// compile-time assertion that MemStore satisfies Store.
var _ Store = (*MemStore)(nil)

// PutToken stores (or replaces) a tunnel's token record.
func (s *MemStore) PutToken(tunnelID string, rec TokenRecord) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.tokens[tunnelID] = rec
}

// GetToken returns a tunnel's token record.
func (s *MemStore) GetToken(tunnelID string) (TokenRecord, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	rec, ok := s.tokens[tunnelID]
	return rec, ok
}

// DeleteToken removes and returns a tunnel's token record.
func (s *MemStore) DeleteToken(tunnelID string) (TokenRecord, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	rec, ok := s.tokens[tunnelID]
	if ok {
		delete(s.tokens, tunnelID)
	}
	return rec, ok
}

// ListTokens returns a snapshot copy of the token map.
func (s *MemStore) ListTokens() map[string]TokenRecord {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make(map[string]TokenRecord, len(s.tokens))
	for k, v := range s.tokens {
		out[k] = v
	}
	return out
}

// IssueInvites mints and stores single-use viewer/operator invites.
func (s *MemStore) IssueInvites(
	sessionID, shareToken, controlToken string,
	tunnelExpiresAt, now float64,
	issuedIP *string,
) (shareInvite, controlInvite string) {
	inviteExpiresAt := now + InviteTTLS
	if tunnelExpiresAt < inviteExpiresAt {
		inviteExpiresAt = tunnelExpiresAt
	}
	shareInvite = GenerateToken()
	controlInvite = GenerateToken()
	s.mu.Lock()
	defer s.mu.Unlock()
	s.invites[HashToken(shareInvite)] = inviteRecord{
		sessionID:   sessionID,
		role:        RoleViewer,
		tunnelToken: shareToken,
		expiresAt:   inviteExpiresAt,
		issuedIP:    issuedIP,
	}
	s.invites[HashToken(controlInvite)] = inviteRecord{
		sessionID:   sessionID,
		role:        RoleOperator,
		tunnelToken: controlToken,
		expiresAt:   inviteExpiresAt,
		issuedIP:    issuedIP,
	}
	return shareInvite, controlInvite
}

// ConsumeInvite pops and validates a single-use invite.
func (s *MemStore) ConsumeInvite(invite, sessionID string, now float64) *Invite {
	inviteValue := strings.TrimSpace(invite)
	if inviteValue == "" {
		return nil
	}
	hash := HashToken(inviteValue)

	s.mu.Lock()
	rec, ok := s.invites[hash]
	if ok {
		// Single-use: remove before validating so a failed validation still
		// burns the invite, matching the Python invite_store.pop().
		delete(s.invites, hash)
	}
	s.mu.Unlock()

	if !ok {
		return nil
	}
	if now > rec.expiresAt {
		return nil
	}
	if rec.sessionID != sessionID {
		return nil
	}
	if rec.role != RoleViewer && rec.role != RoleOperator {
		return nil
	}
	token := strings.TrimSpace(rec.tunnelToken)
	if token == "" {
		return nil
	}
	return &Invite{
		SessionID:   sessionID,
		Role:        rec.role,
		TunnelToken: token,
		ExpiresAt:   rec.expiresAt,
		IssuedIP:    rec.issuedIP,
	}
}

// DiscardInvitesForSession removes all pending invites for a session.
func (s *MemStore) DiscardInvitesForSession(sessionID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for key, rec := range s.invites {
		if rec.sessionID == sessionID {
			delete(s.invites, key)
		}
	}
}

// SweepExpired removes every invite past its expiry.
func (s *MemStore) SweepExpired(now float64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for key, rec := range s.invites {
		if now > rec.expiresAt {
			delete(s.invites, key)
		}
	}
}
