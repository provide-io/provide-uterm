//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import (
	"testing"
)

func sampleProfile(id, owner, visibility string) ConnectionProfile {
	return ConnectionProfile{
		ProfileID: id, Owner: owner, Name: "P", ConnectorType: "ssh",
		Visibility: visibility, CreatedAt: 1, UpdatedAt: 1,
	}
}

func TestProfileStoreCRUD(t *testing.T) {
	store := NewFileProfileStore(t.TempDir())

	if _, err := store.CreateProfile(sampleProfile("p1", "alice", "private")); err != nil {
		t.Fatal(err)
	}
	if _, err := store.CreateProfile(sampleProfile("p2", "bob", "shared")); err != nil {
		t.Fatal(err)
	}

	got, err := store.GetProfile("p1")
	if err != nil || got == nil || got.Owner != "alice" {
		t.Fatalf("get p1: %v %+v", err, got)
	}
	missing, err := store.GetProfile("nope")
	if err != nil || missing != nil {
		t.Fatalf("get missing: %v %+v", err, missing)
	}

	alice := "alice"
	visible, err := store.ListProfiles(&alice)
	if err != nil {
		t.Fatal(err)
	}
	// alice sees her own (p1) + shared (p2).
	if len(visible) != 2 {
		t.Errorf("alice visible = %d, want 2", len(visible))
	}

	all, err := store.ListProfiles(nil)
	if err != nil || len(all) != 2 {
		t.Fatalf("list all: %v len=%d", err, len(all))
	}

	// Update only mutable fields; connector_type (immutable) is ignored.
	updated, err := store.UpdateProfile("p1", map[string]any{"name": "Renamed", "connector_type": "telnet"})
	if err != nil || updated == nil {
		t.Fatalf("update: %v %+v", err, updated)
	}
	if updated.Name != "Renamed" || updated.ConnectorType != "ssh" {
		t.Errorf("update applied wrong fields: %+v", updated)
	}
	if updated.UpdatedAt == 1 {
		t.Errorf("updated_at not refreshed")
	}

	if noUpdate, err := store.UpdateProfile("nope", map[string]any{"name": "x"}); err != nil || noUpdate != nil {
		t.Fatalf("update missing: %v %+v", err, noUpdate)
	}

	ok, err := store.DeleteProfile("p1")
	if err != nil || !ok {
		t.Fatalf("delete p1: %v %v", err, ok)
	}
	ok2, err := store.DeleteProfile("p1")
	if err != nil || ok2 {
		t.Fatalf("second delete should be false: %v %v", err, ok2)
	}
}

func TestProfileValidateRejectsBadConnectorType(t *testing.T) {
	store := NewFileProfileStore(t.TempDir())
	bad := sampleProfile("p", "a", "private")
	bad.ConnectorType = "bogus"
	if _, err := store.CreateProfile(bad); err == nil {
		t.Fatal("expected connector_type validation error")
	}
}

func TestProfileEmptyStoreReturnsEmpty(t *testing.T) {
	store := NewFileProfileStore(t.TempDir())
	all, err := store.ListProfiles(nil)
	if err != nil || len(all) != 0 {
		t.Fatalf("empty store: %v len=%d", err, len(all))
	}
}
