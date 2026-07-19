//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package graphical

import (
	"testing"
	"time"
)

func tenantScope(t *testing.T, id string) Scope {
	t.Helper()
	s, ok := ScopeForTenant(id)
	if !ok {
		t.Fatalf("ScopeForTenant(%q) not ok", id)
	}
	return s
}

func runtimeTarget(id, tenant string) *Definition {
	d := NewDefinition()
	d.TargetID = id
	d.TenantID = tenant
	d.Endpoint = ptr("vm.local:5900")
	return d
}

func mustCode(t *testing.T, err error, code ErrorCode) {
	t.Helper()
	ge, ok := err.(*Error)
	if !ok {
		t.Fatalf("expected *Error, got %v", err)
	}
	if ge.Code != code {
		t.Fatalf("got code %d want %d (%s)", ge.Code, code, ge.Message)
	}
}

func TestScopeSemantics(t *testing.T) {
	if _, ok := ScopeForTenant("   "); ok {
		t.Fatalf("blank tenant produced a scope")
	}
	sys := SystemScope()
	if !sys.IsValid() || !sys.Permits("anything") {
		t.Fatalf("system scope should permit all")
	}
	acme := tenantScope(t, "acme")
	if !acme.Permits("acme") || acme.Permits("beta") || acme.Permits("") {
		t.Fatalf("tenant scope permit wrong")
	}
	if (Scope{}).IsValid() {
		t.Fatalf("zero scope should be invalid")
	}
}

func TestRegistryTenantIsolation(t *testing.T) {
	r := NewInMemoryRegistry()
	acme := tenantScope(t, "acme")
	beta := tenantScope(t, "beta")

	created, err := r.Create(acme, runtimeTarget("gt-a", "acme"))
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if created.TargetID != "gt-a" {
		t.Fatalf("bad created id")
	}

	// Cross-tenant Get is invisible (nil, no error) — beta cannot see acme's.
	got, err := r.Get(beta, "gt-a")
	if err != nil || got != nil {
		t.Fatalf("cross-tenant get leaked: %v %v", got, err)
	}
	// Same-tenant Get works.
	if got, err := r.Get(acme, "gt-a"); err != nil || got == nil {
		t.Fatalf("same-tenant get failed: %v %v", got, err)
	}

	// Cross-tenant List does not include acme's target.
	betaList, err := r.List(beta)
	if err != nil || len(betaList) != 0 {
		t.Fatalf("cross-tenant list leaked: %v %v", betaList, err)
	}
	acmeList, _ := r.List(acme)
	if len(acmeList) != 1 {
		t.Fatalf("own list wrong: %d", len(acmeList))
	}

	// Cross-tenant Update: beta's payload claims tenant beta (passes the scope
	// gate) but the stored gt-a is owned by acme → Forbidden (handler maps 404).
	_, err = r.Update(beta, runtimeTarget("gt-a", "beta"))
	mustCode(t, err, CodeForbidden)
	// acme creating a target that claims tenant beta is Forbidden.
	_, err = r.Create(acme, runtimeTarget("gt-x", "beta"))
	mustCode(t, err, CodeForbidden)

	// Cross-tenant Delete denied (beta cannot delete acme's runtime target).
	err = r.Delete(beta, "gt-a")
	mustCode(t, err, CodeForbidden)
	// Same-tenant delete works.
	if err := r.Delete(acme, "gt-a"); err != nil {
		t.Fatalf("own delete failed: %v", err)
	}
	if got, _ := r.Get(acme, "gt-a"); got != nil {
		t.Fatalf("target survived delete")
	}
}

func TestRegistryCreateValidationAndDuplicate(t *testing.T) {
	r := NewInMemoryRegistry()
	acme := tenantScope(t, "acme")

	// Invalid dimension → CodeInvalid.
	bad := runtimeTarget("gt-a", "acme")
	bad.Width = 0
	_, err := r.Create(acme, bad)
	mustCode(t, err, CodeInvalid)

	if _, err := r.Create(acme, runtimeTarget("gt-a", "acme")); err != nil {
		t.Fatalf("create: %v", err)
	}
	// Duplicate id.
	_, err = r.Create(acme, runtimeTarget("gt-a", "acme"))
	mustCode(t, err, CodeAlreadyExists)
}

func TestRegistryStaticImmutability(t *testing.T) {
	r := NewInMemoryRegistry()
	acme := tenantScope(t, "acme")

	stat := runtimeTarget("gt-s", "acme")
	if err := r.AddStatic(stat); err != nil {
		t.Fatalf("addstatic: %v", err)
	}
	// AddStatic marks IsSystem.
	got, _ := r.Get(acme, "gt-s")
	if got == nil || !got.IsSystem {
		t.Fatalf("static not system: %+v", got)
	}
	// Update of a static target → Immutable.
	_, err := r.Update(acme, runtimeTarget("gt-s", "acme"))
	mustCode(t, err, CodeImmutable)
	// Delete of a static target → Immutable.
	mustCode(t, r.Delete(acme, "gt-s"), CodeImmutable)

	// Duplicate static id → Conflict.
	mustCode(t, r.AddStatic(runtimeTarget("gt-s", "acme")), CodeConflict)

	// AddStatic validates.
	badStatic := runtimeTarget("bad id", "acme")
	mustCode(t, r.AddStatic(badStatic), CodeInvalid)
}

func TestRegistryStaticCrossTenantDelete(t *testing.T) {
	r := NewInMemoryRegistry()
	if err := r.AddStatic(runtimeTarget("gt-s", "acme")); err != nil {
		t.Fatalf("addstatic: %v", err)
	}
	beta := tenantScope(t, "beta")
	// A static target owned by acme: beta sees Forbidden (not Immutable) on delete.
	mustCode(t, r.Delete(beta, "gt-s"), CodeForbidden)
}

func TestRegistryUpdateTimestamps(t *testing.T) {
	r := NewInMemoryRegistry()
	fixed := time.Unix(1000, 0).UTC()
	r.SetClock(func() time.Time { return fixed })
	acme := tenantScope(t, "acme")

	created, err := r.Create(acme, runtimeTarget("gt-a", "acme"))
	if err != nil {
		t.Fatalf("create: %v", err)
	}
	if !created.CreatedAt.Equal(fixed) || created.UpdatedAt != nil {
		t.Fatalf("create timestamps wrong: %+v", created)
	}
	upd := runtimeTarget("gt-a", "acme")
	upd.DisplayName = "renamed"
	updated, err := r.Update(acme, upd)
	if err != nil {
		t.Fatalf("update: %v", err)
	}
	if updated.UpdatedAt == nil || !updated.UpdatedAt.Equal(fixed) {
		t.Fatalf("update did not stamp UpdatedAt: %+v", updated)
	}
	if !updated.CreatedAt.Equal(fixed) {
		t.Fatalf("update lost CreatedAt")
	}
}

func TestRegistryUpdateNotFound(t *testing.T) {
	r := NewInMemoryRegistry()
	acme := tenantScope(t, "acme")
	_, err := r.Update(acme, runtimeTarget("ghost", "acme"))
	mustCode(t, err, CodeNotFound)
}

func TestRegistryListMergeAndOrder(t *testing.T) {
	r := NewInMemoryRegistry()
	acme := tenantScope(t, "acme")
	// Static + runtime; static wins on id collision; sorted by id.
	if err := r.AddStatic(runtimeTarget("gt-b", "acme")); err != nil {
		t.Fatalf("addstatic: %v", err)
	}
	if _, err := r.Create(acme, runtimeTarget("gt-a", "acme")); err != nil {
		t.Fatalf("create: %v", err)
	}
	list, _ := r.List(acme)
	if len(list) != 2 || list[0].TargetID != "gt-a" || list[1].TargetID != "gt-b" {
		t.Fatalf("list order/merge wrong: %+v", list)
	}
}

func TestRegistryClosed(t *testing.T) {
	r := NewInMemoryRegistry()
	acme := tenantScope(t, "acme")
	r.Close()
	if _, err := r.Get(acme, "x"); err == nil {
		mustCode(t, err, CodeClosed)
	} else {
		mustCode(t, err, CodeClosed)
	}
	_, lerr := r.List(acme)
	mustCode(t, lerr, CodeClosed)
	_, cerr := r.Create(acme, runtimeTarget("gt-a", "acme"))
	mustCode(t, cerr, CodeClosed)
	_, uerr := r.Update(acme, runtimeTarget("gt-a", "acme"))
	mustCode(t, uerr, CodeClosed)
	mustCode(t, r.Delete(acme, "gt-a"), CodeClosed)
}

func TestRegistryInvalidScope(t *testing.T) {
	r := NewInMemoryRegistry()
	var invalid Scope // zero value: neither system nor tenant
	_, err := r.Get(invalid, "x")
	mustCode(t, err, CodeForbidden)
	// An invalid scope permits nothing.
	if invalid.Permits("anything") {
		t.Fatalf("invalid scope permitted a tenant")
	}
}

func TestErrorImplementsError(t *testing.T) {
	e := newError(CodeInvalid, "boom")
	if e.Error() != "boom" {
		t.Fatalf("Error() = %q", e.Error())
	}
}

func TestRegistryCreateCollidesWithStatic(t *testing.T) {
	r := NewInMemoryRegistry()
	acme := tenantScope(t, "acme")
	if err := r.AddStatic(runtimeTarget("gt-s", "acme")); err != nil {
		t.Fatalf("addstatic: %v", err)
	}
	// A runtime create with the same id as a static target → AlreadyExists.
	_, err := r.Create(acme, runtimeTarget("gt-s", "acme"))
	mustCode(t, err, CodeAlreadyExists)
}

func TestRegistryUpdateInvalidPayload(t *testing.T) {
	r := NewInMemoryRegistry()
	acme := tenantScope(t, "acme")
	if _, err := r.Create(acme, runtimeTarget("gt-a", "acme")); err != nil {
		t.Fatalf("create: %v", err)
	}
	bad := runtimeTarget("gt-a", "acme")
	bad.Height = 0
	_, err := r.Update(acme, bad)
	mustCode(t, err, CodeInvalid)
}
