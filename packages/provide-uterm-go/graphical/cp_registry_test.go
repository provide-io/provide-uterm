//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package graphical_test

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/memory"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/sqlite"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/graphical"
	_ "modernc.org/sqlite"
)

// ControlPlaneRegistry must be usable anywhere a Registry is expected.
var _ graphical.Registry = (*graphical.ControlPlaneRegistry)(nil)

var fixedTime = time.Unix(1700000000, 0).UTC()

// newRegistry builds a registry over the named backend. Every behavioural test
// runs against BOTH so the two engines cannot drift apart behind the registry.
func newRegistry(t *testing.T, backend string) *graphical.ControlPlaneRegistry {
	t.Helper()
	ctx := context.Background()

	var engine cp.Engine
	switch backend {
	case "memory":
		engine = memory.New(cp.Config{DatabaseURL: ":memory:"})
	case "sqlite":
		engine = sqlite.New(cp.Config{DatabaseURL: filepath.Join(t.TempDir(), "cp.db")})
	default:
		t.Fatalf("unknown backend %q", backend)
	}
	if err := engine.Open(ctx); err != nil {
		t.Fatalf("open: %v", err)
	}
	if err := engine.Migrate(ctx); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	t.Cleanup(func() { _ = engine.Close(ctx) })

	r := graphical.NewControlPlaneRegistry(engine)
	r.SetClock(func() time.Time { return fixedTime })
	return r
}

func eachBackend(t *testing.T, fn func(t *testing.T, r *graphical.ControlPlaneRegistry)) {
	t.Helper()
	for _, backend := range []string{"memory", "sqlite"} {
		t.Run(backend, func(t *testing.T) {
			fn(t, newRegistry(t, backend))
		})
	}
}

func def(targetID, tenant string) *graphical.Definition {
	d := graphical.NewDefinition()
	d.TargetID = targetID
	d.TenantID = tenant
	d.DisplayName = "console"
	d.Protocol = graphical.ProtocolMemory
	return d
}

func scopeFor(t *testing.T, tenant string) graphical.Scope {
	t.Helper()
	s, ok := graphical.ScopeForTenant(tenant)
	if !ok {
		t.Fatalf("scope for %q", tenant)
	}
	return s
}

func TestCPRegistryCreateThenGet(t *testing.T) {
	eachBackend(t, func(t *testing.T, r *graphical.ControlPlaneRegistry) {
		scope := scopeFor(t, "acme")
		if _, err := r.Create(scope, def("gt-1", "acme")); err != nil {
			t.Fatalf("create: %v", err)
		}
		got, err := r.Get(scope, "gt-1")
		if err != nil {
			t.Fatalf("get: %v", err)
		}
		if got == nil || got.TargetID != "gt-1" {
			t.Fatalf("get returned %+v", got)
		}
		if !got.CreatedAt.Equal(fixedTime) {
			t.Fatalf("created_at = %v, want %v", got.CreatedAt, fixedTime)
		}
	})
}

// TestCPRegistryTenantIsolation is the security-critical one: a tenant must
// never see or mutate another tenant's target, through any verb.
func TestCPRegistryTenantIsolation(t *testing.T) {
	eachBackend(t, func(t *testing.T, r *graphical.ControlPlaneRegistry) {
		acme := scopeFor(t, "acme")
		other := scopeFor(t, "other")
		if _, err := r.Create(acme, def("gt-1", "acme")); err != nil {
			t.Fatalf("create: %v", err)
		}

		got, err := r.Get(other, "gt-1")
		if err != nil {
			t.Fatalf("cross-tenant get errored: %v", err)
		}
		if got != nil {
			t.Fatal("cross-tenant get leaked a target")
		}

		rows, err := r.List(other)
		if err != nil {
			t.Fatalf("cross-tenant list: %v", err)
		}
		if len(rows) != 0 {
			t.Fatalf("cross-tenant list leaked %d targets", len(rows))
		}

		if err := r.Delete(other, "gt-1"); err == nil {
			t.Fatal("cross-tenant delete should fail")
		}

		// The victim's row must still be there after the failed delete.
		if survived, _ := r.Get(acme, "gt-1"); survived == nil {
			t.Fatal("cross-tenant delete removed the owner's target")
		}
	})
}

// TestCPRegistryCreateRejectsForeignTenant covers the case where the payload
// claims a tenant the scope does not cover.
func TestCPRegistryCreateRejectsForeignTenant(t *testing.T) {
	eachBackend(t, func(t *testing.T, r *graphical.ControlPlaneRegistry) {
		_, err := r.Create(scopeFor(t, "acme"), def("gt-1", "other"))
		if err == nil {
			t.Fatal("expected a scope rejection")
		}
		var ge *graphical.Error
		if !asGraphicalError(err, &ge) || ge.Code != graphical.CodeForbidden {
			t.Fatalf("err = %+v, want CodeForbidden", err)
		}
	})
}

func TestCPRegistryDuplicateCreateConflicts(t *testing.T) {
	eachBackend(t, func(t *testing.T, r *graphical.ControlPlaneRegistry) {
		scope := scopeFor(t, "acme")
		if _, err := r.Create(scope, def("gt-1", "acme")); err != nil {
			t.Fatalf("create: %v", err)
		}
		_, err := r.Create(scope, def("gt-1", "acme"))
		var ge *graphical.Error
		if !asGraphicalError(err, &ge) || ge.Code != graphical.CodeAlreadyExists {
			t.Fatalf("err = %+v, want CodeAlreadyExists", err)
		}
	})
}

func TestCPRegistryUpdatePreservesCreationStamps(t *testing.T) {
	eachBackend(t, func(t *testing.T, r *graphical.ControlPlaneRegistry) {
		scope := scopeFor(t, "acme")
		original := def("gt-1", "acme")
		creator := "alice"
		original.CreatedBy = &creator
		if _, err := r.Create(scope, original); err != nil {
			t.Fatalf("create: %v", err)
		}

		next := def("gt-1", "acme")
		next.DisplayName = "renamed"
		updated, err := r.Update(scope, next)
		if err != nil {
			t.Fatalf("update: %v", err)
		}
		if updated.DisplayName != "renamed" {
			t.Fatalf("display_name = %q", updated.DisplayName)
		}
		if updated.CreatedBy == nil || *updated.CreatedBy != "alice" {
			t.Fatalf("created_by not preserved: %+v", updated.CreatedBy)
		}
		if updated.UpdatedAt == nil {
			t.Fatal("updated_at should be stamped")
		}
	})
}

func TestCPRegistryUpdateAbsentIsNotFound(t *testing.T) {
	eachBackend(t, func(t *testing.T, r *graphical.ControlPlaneRegistry) {
		_, err := r.Update(scopeFor(t, "acme"), def("missing", "acme"))
		var ge *graphical.Error
		if !asGraphicalError(err, &ge) || ge.Code != graphical.CodeNotFound {
			t.Fatalf("err = %+v, want CodeNotFound", err)
		}
	})
}

func TestCPRegistryDelete(t *testing.T) {
	eachBackend(t, func(t *testing.T, r *graphical.ControlPlaneRegistry) {
		scope := scopeFor(t, "acme")
		if _, err := r.Create(scope, def("gt-1", "acme")); err != nil {
			t.Fatalf("create: %v", err)
		}
		if err := r.Delete(scope, "gt-1"); err != nil {
			t.Fatalf("delete: %v", err)
		}
		got, _ := r.Get(scope, "gt-1")
		if got != nil {
			t.Fatal("target survived delete")
		}
		var ge *graphical.Error
		if err := r.Delete(scope, "gt-1"); !asGraphicalError(err, &ge) || ge.Code != graphical.CodeNotFound {
			t.Fatalf("second delete = %+v, want CodeNotFound", err)
		}
	})
}

// TestCPRegistryStaticIsImmutableAndWins mirrors InMemoryRegistry: a seeded
// static target shadows a runtime id and cannot be mutated.
func TestCPRegistryStaticIsImmutableAndWins(t *testing.T) {
	eachBackend(t, func(t *testing.T, r *graphical.ControlPlaneRegistry) {
		scope := scopeFor(t, "acme")
		static := def("gt-static", "acme")
		static.DisplayName = "seeded"
		if err := r.AddStatic(static); err != nil {
			t.Fatalf("add static: %v", err)
		}

		var ge *graphical.Error
		if err := r.Delete(scope, "gt-static"); !asGraphicalError(err, &ge) || ge.Code != graphical.CodeImmutable {
			t.Fatalf("delete static = %+v, want CodeImmutable", err)
		}
		if _, err := r.Update(scope, def("gt-static", "acme")); !asGraphicalError(err, &ge) ||
			ge.Code != graphical.CodeImmutable {
			t.Fatalf("update static = %+v, want CodeImmutable", err)
		}
		if _, err := r.Create(scope, def("gt-static", "acme")); !asGraphicalError(err, &ge) ||
			ge.Code != graphical.CodeAlreadyExists {
			t.Fatalf("create over static = %+v, want CodeAlreadyExists", err)
		}

		got, _ := r.Get(scope, "gt-static")
		if got == nil || got.DisplayName != "seeded" || !got.IsSystem {
			t.Fatalf("static get = %+v", got)
		}
	})
}

func TestCPRegistryListMergesAndSorts(t *testing.T) {
	eachBackend(t, func(t *testing.T, r *graphical.ControlPlaneRegistry) {
		scope := scopeFor(t, "acme")
		for _, id := range []string{"gt-c", "gt-a"} {
			if _, err := r.Create(scope, def(id, "acme")); err != nil {
				t.Fatalf("create %s: %v", id, err)
			}
		}
		if err := r.AddStatic(def("gt-b", "acme")); err != nil {
			t.Fatalf("add static: %v", err)
		}

		rows, err := r.List(scope)
		if err != nil {
			t.Fatalf("list: %v", err)
		}
		if len(rows) != 3 {
			t.Fatalf("list len = %d, want 3", len(rows))
		}
		if rows[0].TargetID != "gt-a" || rows[1].TargetID != "gt-b" || rows[2].TargetID != "gt-c" {
			t.Fatalf("order = %s %s %s", rows[0].TargetID, rows[1].TargetID, rows[2].TargetID)
		}
	})
}

func TestCPRegistryClosedRejects(t *testing.T) {
	eachBackend(t, func(t *testing.T, r *graphical.ControlPlaneRegistry) {
		scope := scopeFor(t, "acme")
		r.Close()
		var ge *graphical.Error
		if _, err := r.Get(scope, "gt-1"); !asGraphicalError(err, &ge) || ge.Code != graphical.CodeClosed {
			t.Fatalf("get on closed = %+v, want CodeClosed", err)
		}
		if _, err := r.List(scope); !asGraphicalError(err, &ge) || ge.Code != graphical.CodeClosed {
			t.Fatalf("list on closed = %+v, want CodeClosed", err)
		}
	})
}

func TestCPRegistryInvalidScopeRejected(t *testing.T) {
	eachBackend(t, func(t *testing.T, r *graphical.ControlPlaneRegistry) {
		var zero graphical.Scope // neither tenant nor system — invalid
		var ge *graphical.Error
		if _, err := r.Get(zero, "gt-1"); !asGraphicalError(err, &ge) || ge.Code != graphical.CodeForbidden {
			t.Fatalf("get with zero scope = %+v, want CodeForbidden", err)
		}
	})
}

// TestCPRegistrySurvivesReopen is the whole point of the feature: a target
// created before a restart is still there afterwards.
func TestCPRegistrySurvivesReopen(t *testing.T) {
	ctx := context.Background()
	dbPath := filepath.Join(t.TempDir(), "cp.db")
	scope, _ := graphical.ScopeForTenant("acme")

	first := sqlite.New(cp.Config{DatabaseURL: dbPath})
	if err := first.Open(ctx); err != nil {
		t.Fatalf("open: %v", err)
	}
	if err := first.Migrate(ctx); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	r1 := graphical.NewControlPlaneRegistry(first)
	target := def("gt-1", "acme")
	target.Config = map[string]any{"vm_name": "vm-1"}
	if _, err := r1.Create(scope, target); err != nil {
		t.Fatalf("create: %v", err)
	}
	if err := first.Close(ctx); err != nil {
		t.Fatalf("close: %v", err)
	}

	second := sqlite.New(cp.Config{DatabaseURL: dbPath})
	if err := second.Open(ctx); err != nil {
		t.Fatalf("reopen: %v", err)
	}
	t.Cleanup(func() { _ = second.Close(ctx) })
	got, err := graphical.NewControlPlaneRegistry(second).Get(scope, "gt-1")
	if err != nil {
		t.Fatalf("get after reopen: %v", err)
	}
	if got == nil {
		t.Fatal("target did not survive the restart")
	}
	if got.Config["vm_name"] != "vm-1" {
		t.Fatalf("config after reopen = %+v", got.Config)
	}
}

// TestCPRegistryStaticIsNotPersisted documents the deliberate split: static
// targets are re-seeded from config on every boot, never stored.
func TestCPRegistryStaticIsNotPersisted(t *testing.T) {
	ctx := context.Background()
	dbPath := filepath.Join(t.TempDir(), "cp.db")
	scope, _ := graphical.ScopeForTenant("acme")

	first := sqlite.New(cp.Config{DatabaseURL: dbPath})
	_ = first.Open(ctx)
	_ = first.Migrate(ctx)
	if err := graphical.NewControlPlaneRegistry(first).AddStatic(def("gt-static", "acme")); err != nil {
		t.Fatalf("add static: %v", err)
	}
	_ = first.Close(ctx)

	second := sqlite.New(cp.Config{DatabaseURL: dbPath})
	_ = second.Open(ctx)
	t.Cleanup(func() { _ = second.Close(ctx) })
	got, err := graphical.NewControlPlaneRegistry(second).Get(scope, "gt-static")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if got != nil {
		t.Fatal("static target was persisted; it must come from config seeding only")
	}
}

// asGraphicalError unwraps to *graphical.Error without pulling in errors.As at
// every call site.
func asGraphicalError(err error, out **graphical.Error) bool {
	ge, ok := err.(*graphical.Error)
	if ok {
		*out = ge
	}
	return ok
}
