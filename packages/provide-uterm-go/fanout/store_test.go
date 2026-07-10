//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package fanout

import "testing"

func TestInMemoryStoreCRUD(t *testing.T) {
	s := NewInMemoryStore()
	g := &Group{GroupID: "g1", CreatedBy: "admin", CreatedAt: 1.0}
	s.Save(g)

	got, ok := s.Get("g1")
	if !ok || got.GroupID != "g1" {
		t.Fatalf("Get after Save: %v %v", got, ok)
	}
	if _, ok := s.Get("missing"); ok {
		t.Fatal("Get missing should be false")
	}

	s.Delete("g1")
	if _, ok := s.Get("g1"); ok {
		t.Fatal("Get after Delete should be false")
	}
	// Delete of a missing group is a no-op.
	s.Delete("nope")
}

func TestInMemoryStoreListForPrincipal(t *testing.T) {
	s := NewInMemoryStore()
	s.Save(&Group{GroupID: "a", CreatedBy: "alice", CreatedAt: 1.0})
	s.Save(&Group{GroupID: "b", CreatedBy: "bob", CreatedAt: 2.0, Grants: []string{"alice"}})
	s.Save(&Group{GroupID: "c", CreatedBy: "carol", CreatedAt: 3.0})

	// alice is creator of "a" and grantee on "b"; not visible: "c".
	got := s.ListForPrincipal("alice")
	if len(got) != 2 {
		t.Fatalf("alice sees %d groups, want 2: %+v", len(got), got)
	}
	// Deterministic order by CreatedAt: a (1.0) then b (2.0).
	if got[0].GroupID != "a" || got[1].GroupID != "b" {
		t.Fatalf("order = %s,%s want a,b", got[0].GroupID, got[1].GroupID)
	}

	if len(s.ListForPrincipal("nobody")) != 0 {
		t.Fatal("nobody should see no groups")
	}
}

func TestSortGroupsTieBreakByID(t *testing.T) {
	groups := []*Group{
		{GroupID: "z", CreatedAt: 5.0},
		{GroupID: "a", CreatedAt: 5.0},
	}
	sortGroups(groups)
	if groups[0].GroupID != "a" || groups[1].GroupID != "z" {
		t.Fatalf("tie-break order = %s,%s want a,z", groups[0].GroupID, groups[1].GroupID)
	}
}

func TestContains(t *testing.T) {
	if !contains([]string{"x", "y"}, "y") {
		t.Fatal("contains true")
	}
	if contains([]string{"x"}, "z") {
		t.Fatal("contains false")
	}
}
