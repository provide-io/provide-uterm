//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package fanout

import (
	"sort"
	"sync"
)

// Store persists fan-out groups. Port of the Python FanOutStore protocol.
type Store interface {
	// Save persists a group, creating or replacing any existing entry with the
	// same GroupID.
	Save(group *Group)
	// Get returns the group with the given ID, or (nil, false) when absent.
	Get(groupID string) (*Group, bool)
	// Delete removes the group with the given ID. No-op when absent.
	Delete(groupID string)
	// ListForPrincipal returns every group where principal is the creator or
	// appears in Grants.
	ListForPrincipal(principal string) []*Group
}

// InMemoryStore is an ephemeral, concurrency-safe [Store]. Groups are lost on
// restart. Port of InMemoryFanOutStore.
type InMemoryStore struct {
	mu     sync.Mutex
	groups map[string]*Group
}

// NewInMemoryStore builds an empty in-memory store.
func NewInMemoryStore() *InMemoryStore {
	return &InMemoryStore{groups: map[string]*Group{}}
}

// Save implements [Store].
func (s *InMemoryStore) Save(group *Group) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.groups[group.GroupID] = group
}

// Get implements [Store].
func (s *InMemoryStore) Get(groupID string) (*Group, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	g, ok := s.groups[groupID]
	return g, ok
}

// Delete implements [Store].
func (s *InMemoryStore) Delete(groupID string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.groups, groupID)
}

// ListForPrincipal implements [Store]. The result is deterministically ordered
// by CreatedAt then GroupID so callers (and tests) see a stable list despite
// the unordered backing map.
func (s *InMemoryStore) ListForPrincipal(principal string) []*Group {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]*Group, 0, len(s.groups))
	for _, g := range s.groups {
		if g.CreatedBy == principal || contains(g.Grants, principal) {
			out = append(out, g)
		}
	}
	sortGroups(out)
	return out
}

// sortGroups orders groups by CreatedAt then GroupID for a stable listing.
func sortGroups(groups []*Group) {
	sort.Slice(groups, func(i, j int) bool {
		if groups[i].CreatedAt != groups[j].CreatedAt {
			return groups[i].CreatedAt < groups[j].CreatedAt
		}
		return groups[i].GroupID < groups[j].GroupID
	})
}

// contains reports whether needle is in haystack.
func contains(haystack []string, needle string) bool {
	for _, v := range haystack {
		if v == needle {
			return true
		}
	}
	return false
}
