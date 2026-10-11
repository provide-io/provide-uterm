//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package graphical_test

import (
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/graphical"
)

// Every mutating verb refuses an invalid (zero) scope before reading the store.
func TestCPRegistryMutationsRejectInvalidScope(t *testing.T) {
	t.Parallel()
	engine := newFakeEngine(failGet) // any store read would surface as CodeBackend
	r := graphical.NewControlPlaneRegistry(engine)
	var zero graphical.Scope

	_, err := r.Create(zero, def("gt-1", "acme"))
	wantCode(t, err, graphical.CodeForbidden, "Create")
	_, err = r.Update(zero, def("gt-1", "acme"))
	wantCode(t, err, graphical.CodeForbidden, "Update")
	wantCode(t, r.Delete(zero, "gt-1"), graphical.CodeForbidden, "Delete")
}

// Update checks the PAYLOAD tenant against the caller's scope before anything
// else, so a caller cannot move a target into a tenant it does not hold.
func TestCPRegistryUpdateRejectsForeignPayloadTenant(t *testing.T) {
	t.Parallel()
	r, scope := failingRegistry(t, failGet)
	_, err := r.Update(scope, def("gt-1", "other"))
	wantCode(t, err, graphical.CodeForbidden, "Update")
}

func seededEngine(mode failMode) *fakeEngine {
	engine := newFakeEngine(mode)
	engine.store.rows["gt-1"] = cp.GraphicalTargetRecord{
		TargetID: "gt-1", TenantID: "acme", DisplayName: "c",
		Protocol: graphical.ProtocolMemory, Width: 640, Height: 480, CreatedAt: 100, Config: "{}",
	}
	return engine
}

// An unserializable config on Update is rejected as invalid and the stored row
// is left untouched.
func TestCPRegistryUpdateUnserializableConfigIsInvalid(t *testing.T) {
	t.Parallel()
	engine := seededEngine(failNone)
	scope, _ := graphical.ScopeForTenant("acme")
	target := def("gt-1", "acme")
	target.Config = map[string]any{"bad": make(chan int)}

	_, err := graphical.NewControlPlaneRegistry(engine).Update(scope, target)
	wantCode(t, err, graphical.CodeInvalid, "Update")
	if got := engine.store.rows["gt-1"].Config; got != "{}" {
		t.Fatalf("stored config changed to %q", got)
	}
}

// A store write failure on Update is a backend error, not success.
func TestCPRegistryUpdateWriteFailureIsBackendError(t *testing.T) {
	t.Parallel()
	engine := seededEngine(failPut)
	scope, _ := graphical.ScopeForTenant("acme")
	_, err := graphical.NewControlPlaneRegistry(engine).Update(scope, def("gt-1", "acme"))
	wantCode(t, err, graphical.CodeBackend, "Update")
}

// Deleting a static target owned by another tenant is forbidden, not merely
// immutable: the caller must not learn that the target exists.
func TestCPRegistryDeleteForeignStaticIsForbidden(t *testing.T) {
	t.Parallel()
	r, scope := failingRegistry(t, failNone)
	if err := r.AddStatic(def("gt-static", "other")); err != nil {
		t.Fatal(err)
	}
	wantCode(t, r.Delete(scope, "gt-static"), graphical.CodeForbidden, "Delete")
}

// The in-memory registry applies the same payload-tenant check on Update and
// reports a missing runtime target on Delete.
func TestInMemoryRegistryUpdateForeignTenantAndDeleteMissing(t *testing.T) {
	t.Parallel()
	r := graphical.NewInMemoryRegistry()
	scope, _ := graphical.ScopeForTenant("acme")

	_, err := r.Update(scope, def("gt-1", "other"))
	wantCode(t, err, graphical.CodeForbidden, "Update")
	wantCode(t, r.Delete(scope, "gt-missing"), graphical.CodeNotFound, "Delete")
}
