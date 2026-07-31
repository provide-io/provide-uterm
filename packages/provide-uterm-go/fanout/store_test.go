//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package fanout

import (
	"fmt"
	"sync"
	"testing"
)

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

func TestInMemoryStoreClonesSavedAndReturnedGroups(t *testing.T) {
	s := NewInMemoryStore()
	original := &Group{GroupID: "g1", Name: "original", WorkerIDs: []string{"w1"}, Grants: []string{"alice"}}
	s.Save(original)
	original.Name = "mutated-input"
	original.WorkerIDs[0] = "mutated-input-worker"
	original.Grants[0] = "mutated-input-grant"

	got, ok := s.Get("g1")
	if !ok {
		t.Fatal("saved group missing")
	}
	if got.Name != "original" || got.WorkerIDs[0] != "w1" || got.Grants[0] != "alice" {
		t.Fatalf("saved state aliased input: %+v", got)
	}
	got.Name = "mutated-get"
	got.WorkerIDs[0] = "mutated-get-worker"
	got.Grants[0] = "mutated-get-grant"
	listed := s.ListForPrincipal("alice")
	if len(listed) != 1 {
		t.Fatalf("list = %+v", listed)
	}
	listed[0].Name = "mutated-list"
	listed[0].WorkerIDs[0] = "mutated-list-worker"
	listed[0].Grants[0] = "mutated-list-grant"

	again, _ := s.Get("g1")
	if again.Name != "original" || again.WorkerIDs[0] != "w1" || again.Grants[0] != "alice" {
		t.Fatalf("stored state aliased returned group: %+v", again)
	}
}

func TestGrantAccessPreservesConcurrentDistinctGrants(t *testing.T) {
	store := NewInMemoryStore()
	controller := NewController(newFakeHub(nil), Config{Store: store, Authorizer: allowAllAuthorizer()})
	_, _ = controller.CreateGroup(&Group{GroupID: "g1"}, "owner")
	start := make(chan struct{})
	var wg sync.WaitGroup
	for _, grantee := range []string{"alice", "bob"} {
		grantee := grantee
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-start
			controller.GrantAccess("g1", grantee, "owner")
		}()
	}
	close(start)
	wg.Wait()

	group, ok := store.Get("g1")
	if !ok || !contains(group.Grants, "alice") || !contains(group.Grants, "bob") {
		t.Fatalf("concurrent grants = %v, want alice and bob", group.Grants)
	}
}

func TestInMemoryStoreCanListWhileGranting(t *testing.T) {
	store := NewInMemoryStore()
	store.Save(&Group{GroupID: "g1", CreatedBy: "owner"})
	start := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		<-start
		for i := 0; i < 100; i++ {
			store.GrantAccess("g1", fmt.Sprintf("member-%d", i), "owner")
		}
	}()
	go func() {
		defer wg.Done()
		<-start
		for i := 0; i < 100; i++ {
			groups := store.ListForPrincipal("owner")
			if len(groups) != 1 {
				t.Errorf("owner list length = %d, want 1", len(groups))
				return
			}
			// Returned values must remain detached while the stored grant slice
			// grows concurrently.
			groups[0].Grants = append(groups[0].Grants, "caller-only")
		}
	}()
	close(start)
	wg.Wait()

	group, ok := store.Get("g1")
	if !ok || len(group.Grants) != 100 {
		t.Fatalf("stored grants = %d, want 100", len(group.Grants))
	}
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
