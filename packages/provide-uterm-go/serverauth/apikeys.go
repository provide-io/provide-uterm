//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/hex"
	"sync"
	"time"
)

// ApiKey ports api_keys.ApiKey — a single API key record; the raw key is never
// stored.
type ApiKey struct {
	KeyID      string
	KeyHash    string
	Name       string
	Scopes     Set
	CreatedAt  float64
	ExpiresAt  *float64
	LastUsedAt *float64
	Revoked    bool
}

// HashKey ports api_keys._hash_key: SHA-256 hex digest of the raw key.
func HashKey(rawKey string) string {
	digest := sha256.Sum256([]byte(rawKey))
	return hex.EncodeToString(digest[:])
}

// ApiKeyStore ports api_keys.ApiKeyStore — an in-memory registry with
// timing-safe validation. Safe for concurrent use.
type ApiKeyStore struct {
	mu   sync.Mutex
	keys map[string]*ApiKey // key_id -> record
	// now returns POSIX seconds; overridable in tests (mirrors monkeypatching
	// api_keys.time in the Python suite).
	now func() float64
}

// NewApiKeyStore constructs an empty store.
func NewApiKeyStore() *ApiKeyStore {
	return &ApiKeyStore{keys: map[string]*ApiKey{}, now: wallClock}
}

func wallClock() float64 { return float64(time.Now().UnixNano()) / 1e9 }

// SetClock overrides the store's time source (test hook).
func (s *ApiKeyStore) SetClock(now func() float64) { s.now = now }

// tokenURLSafe returns secrets.token_urlsafe(nbytes)-equivalent output:
// nbytes of randomness, base64url-encoded without padding.
func tokenURLSafe(nbytes int) string {
	buf := make([]byte, nbytes)
	if _, err := rand.Read(buf); err != nil {
		panic(err) // crypto/rand failure is unrecoverable
	}
	return base64.RawURLEncoding.EncodeToString(buf)
}

// Create ports ApiKeyStore.create: returns (rawKey, record). expiresInS nil =
// never expires.
func (s *ApiKeyStore) Create(name string, scopes Set, expiresInS *int) (string, *ApiKey) {
	rawKey := tokenURLSafe(32)
	keyHash := HashKey(rawKey)
	if scopes == nil {
		scopes = NewSet()
	}
	var expiresAt *float64
	if expiresInS != nil {
		v := s.now() + float64(*expiresInS)
		expiresAt = &v
	}
	record := &ApiKey{
		KeyID:     keyHash[:16],
		KeyHash:   keyHash,
		Name:      name,
		Scopes:    scopes,
		CreatedAt: s.now(),
		ExpiresAt: expiresAt,
	}
	s.mu.Lock()
	s.keys[record.KeyID] = record
	s.mu.Unlock()
	return rawKey, record
}

// Validate ports ApiKeyStore.validate: returns the record or nil. Uses a
// constant-time hash comparison; skips revoked/expired keys.
func (s *ApiKeyStore) Validate(rawKey string) *ApiKey {
	keyHash := HashKey(rawKey)
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, record := range s.keys {
		if record.Revoked {
			continue
		}
		if record.ExpiresAt != nil && s.now() > *record.ExpiresAt {
			continue
		}
		if subtle.ConstantTimeCompare([]byte(record.KeyHash), []byte(keyHash)) == 1 {
			t := s.now()
			record.LastUsedAt = &t
			return record
		}
	}
	return nil
}

// Revoke ports ApiKeyStore.revoke: returns true if the key existed.
func (s *ApiKeyStore) Revoke(keyID string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if record, ok := s.keys[keyID]; ok {
		record.Revoked = true
		return true
	}
	return false
}

// ListKeys ports ApiKeyStore.list_keys.
func (s *ApiKeyStore) ListKeys() []*ApiKey {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]*ApiKey, 0, len(s.keys))
	for _, record := range s.keys {
		out = append(out, record)
	}
	return out
}
