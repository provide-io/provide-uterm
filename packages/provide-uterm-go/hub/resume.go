//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package hub

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"sync"
)

// ResumeSession is the state preserved for a disconnected browser session so a
// reconnecting browser can prove identity and reclaim its role / hijack
// ownership within the TTL. Port of resume.ResumeSession.
type ResumeSession struct {
	Token          string
	WorkerID       string
	Role           string
	CreatedAt      float64 // monotonic
	ExpiresAt      float64 // monotonic
	WasHijackOwner bool
	WallCreatedAt  float64 // wall clock at token creation (session-identity checks)
}

// ResumeTokenStore is the async resume-token persistence surface. Port of the
// resume.ResumeTokenStore protocol. Every method takes a context for the future
// control-plane-backed implementation; the in-memory store ignores it.
type ResumeTokenStore interface {
	Create(ctx context.Context, workerID, role string, ttlS float64) (string, error)
	Get(ctx context.Context, token string) (*ResumeSession, error)
	Consume(ctx context.Context, token string) (*ResumeSession, error)
	MarkHijackOwner(ctx context.Context, token string, isOwner bool) error
	Revoke(ctx context.Context, token string) error
}

// tokenGen mints a fresh opaque resume token. The default generator mirrors
// Python's secrets.token_urlsafe(32) (32 random bytes, URL-safe base64, no
// padding). Injectable so tests get deterministic tokens.
type tokenGen func() string

// defaultTokenGen returns 32 random bytes as URL-safe base64 without padding.
func defaultTokenGen() string {
	var b [32]byte
	if _, err := rand.Read(b[:]); err != nil { //nolint:staticcheck // rand.Read never errors on supported platforms
		panic(err)
	}
	return base64.RawURLEncoding.EncodeToString(b[:])
}

// InMemoryResumeStore is a lightweight single-process resume-token store with
// automatic expiry pruning. Port of resume.InMemoryResumeStore.
//
// Deviation: the Python store relies on the event loop for mutual exclusion;
// this port guards its map with a mutex so it is safe under -race. The
// control-plane-backed ControlPlaneResumeStore is NOT ported (it depends on the
// async control-plane token store, which is outside the wave-B perimeter).
type InMemoryResumeStore struct {
	mu     sync.Mutex
	tokens map[string]*ResumeSession
	clock  Clock
	gen    tokenGen
}

// NewInMemoryResumeStore builds an empty store. clock nil selects the real
// clock; gen nil selects the cryptographic default token generator.
func NewInMemoryResumeStore(clock Clock, gen tokenGen) *InMemoryResumeStore {
	if gen == nil {
		gen = defaultTokenGen
	}
	return &InMemoryResumeStore{
		tokens: map[string]*ResumeSession{},
		clock:  orDefaultClock(clock),
		gen:    gen,
	}
}

// Create mints and stores a new resume token, opportunistically pruning expired
// entries first (matching the Python create()'s cleanup-on-create). Port of
// InMemoryResumeStore.create.
func (s *InMemoryResumeStore) Create(_ context.Context, workerID, role string, ttlS float64) (string, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cleanupLocked()
	token := s.gen()
	now := s.clock.Monotonic()
	s.tokens[token] = &ResumeSession{
		Token:         token,
		WorkerID:      workerID,
		Role:          role,
		CreatedAt:     now,
		ExpiresAt:     now + ttlS,
		WallCreatedAt: s.clock.Wall(),
	}
	return token, nil
}

// Get looks up a token, pruning and returning nil if expired. Port of get.
func (s *InMemoryResumeStore) Get(_ context.Context, token string) (*ResumeSession, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	session := s.tokens[token]
	if session == nil {
		return nil, nil
	}
	if s.clock.Monotonic() > session.ExpiresAt {
		delete(s.tokens, token)
		return nil, nil
	}
	return session, nil
}

// Consume atomically validates and revokes a token (single-use). Returns nil if
// already used or expired. Port of consume.
func (s *InMemoryResumeStore) Consume(_ context.Context, token string) (*ResumeSession, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	session, ok := s.tokens[token]
	if !ok {
		return nil, nil
	}
	delete(s.tokens, token)
	if s.clock.Monotonic() > session.ExpiresAt {
		return nil, nil
	}
	return session, nil
}

// MarkHijackOwner flags whether the session held hijack ownership at
// disconnect. Port of mark_hijack_owner (a no-op for an unknown token).
func (s *InMemoryResumeStore) MarkHijackOwner(_ context.Context, token string, isOwner bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if session, ok := s.tokens[token]; ok {
		session.WasHijackOwner = isOwner
	}
	return nil
}

// Revoke invalidates a token immediately. Port of revoke.
func (s *InMemoryResumeStore) Revoke(_ context.Context, token string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.tokens, token)
	return nil
}

// CleanupExpired removes all expired tokens and returns the count removed. Port
// of cleanup_expired.
func (s *InMemoryResumeStore) CleanupExpired() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.cleanupLocked()
}

func (s *InMemoryResumeStore) cleanupLocked() int {
	now := s.clock.Monotonic()
	removed := 0
	for t, sess := range s.tokens {
		if now > sess.ExpiresAt {
			delete(s.tokens, t)
			removed++
		}
	}
	return removed
}

// Len returns the number of stored (possibly expired) tokens. Port of __len__.
func (s *InMemoryResumeStore) Len() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return len(s.tokens)
}
