//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"path/filepath"
	"testing"

	cp "github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/controlplane/bootstrap"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/graphical"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
	_ "modernc.org/sqlite"
)

// openEngine builds an engine the way cli/server.go does — including Migrate,
// which the registry depends on for its table.
func openEngine(t *testing.T, backend cp.Backend, dbPath string) cp.Engine {
	t.Helper()
	ctx := context.Background()
	engine, err := bootstrap.New(cp.Config{Backend: backend, DatabaseURL: dbPath})
	if err != nil {
		t.Fatalf("bootstrap: %v", err)
	}
	if err := engine.Open(ctx); err != nil {
		t.Fatalf("open: %v", err)
	}
	if err := engine.Migrate(ctx); err != nil {
		t.Fatalf("migrate: %v", err)
	}
	return engine
}

func persistenceConfig() *serverconfig.UtermServerConfig {
	cfg := serverconfig.DefaultServerConfig()
	cfg.GraphicalTargets = []serverconfig.GraphicalTargetConfig{
		{TargetID: "gt-static", TenantID: "acme", Protocol: "memory", Enabled: true},
	}
	return cfg
}

// TestControlPlaneGraphicalTargetsSurviveRestart is the end-to-end point of the
// wiring: a runtime target created against a sqlite-backed server is still there
// after the process restarts, while the config-seeded static target is re-seeded
// rather than persisted.
func TestControlPlaneGraphicalTargetsSurviveRestart(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	dbPath := filepath.Join(t.TempDir(), "cp.db")
	cfg := persistenceConfig()
	scope, _ := graphical.ScopeForTenant("acme")

	first := openEngine(t, cp.BackendSQLite, dbPath)
	reg, err := NewControlPlaneGraphicalTargets(cfg, first)
	if err != nil {
		t.Fatalf("build registry: %v", err)
	}

	runtime := graphical.NewDefinition()
	runtime.TargetID = "gt-runtime"
	runtime.TenantID = "acme"
	runtime.DisplayName = "console"
	runtime.Protocol = graphical.ProtocolMemory
	if _, err := reg.Create(scope, runtime); err != nil {
		t.Fatalf("create: %v", err)
	}
	if err := first.Close(ctx); err != nil {
		t.Fatalf("close: %v", err)
	}

	// Restart: a fresh engine and registry over the same database.
	second := openEngine(t, cp.BackendSQLite, dbPath)
	defer func() { _ = second.Close(ctx) }()
	reopened, err := NewControlPlaneGraphicalTargets(cfg, second)
	if err != nil {
		t.Fatalf("rebuild registry: %v", err)
	}

	got, err := reopened.Get(scope, "gt-runtime")
	if err != nil {
		t.Fatalf("get after restart: %v", err)
	}
	if got == nil {
		t.Fatal("runtime target did not survive the restart")
	}
	if got.IsStatic || got.IsSystem {
		t.Fatalf("runtime target came back as static/system: %+v", got)
	}

	// The static target is present because it was re-seeded from config, and it
	// is still immutable.
	static, err := reopened.Get(scope, "gt-static")
	if err != nil || static == nil {
		t.Fatalf("static target missing after restart: %v %+v", err, static)
	}
	if !static.IsStatic || !static.IsSystem {
		t.Fatalf("re-seeded target lost its static/system flags: %+v", static)
	}

	list, err := reopened.List(scope)
	if err != nil {
		t.Fatalf("list: %v", err)
	}
	if len(list) != 2 {
		t.Fatalf("expected the runtime + static target, got %d", len(list))
	}
}

// A memory control plane keeps the previous behaviour: runtime targets are
// dropped on restart, so switching backends is the only thing that changes
// durability.
func TestControlPlaneGraphicalTargetsMemoryIsNotDurable(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	cfg := persistenceConfig()
	scope, _ := graphical.ScopeForTenant("acme")

	first := openEngine(t, cp.BackendMemory, ":memory:")
	reg, err := NewControlPlaneGraphicalTargets(cfg, first)
	if err != nil {
		t.Fatalf("build registry: %v", err)
	}
	runtime := graphical.NewDefinition()
	runtime.TargetID = "gt-runtime"
	runtime.TenantID = "acme"
	runtime.DisplayName = "console"
	runtime.Protocol = graphical.ProtocolMemory
	if _, err := reg.Create(scope, runtime); err != nil {
		t.Fatalf("create: %v", err)
	}
	_ = first.Close(ctx)

	second := openEngine(t, cp.BackendMemory, ":memory:")
	defer func() { _ = second.Close(ctx) }()
	reopened, _ := NewControlPlaneGraphicalTargets(cfg, second)
	got, err := reopened.Get(scope, "gt-runtime")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if got != nil {
		t.Fatal("memory backend should not persist runtime targets")
	}
	// The static target is still re-seeded from config.
	if static, _ := reopened.Get(scope, "gt-static"); static == nil {
		t.Fatal("static target should be re-seeded on a memory backend too")
	}
}

// A bad config target must fail registry construction rather than yielding a
// half-seeded registry.
func TestControlPlaneGraphicalTargetsSeedError(t *testing.T) {
	t.Parallel()
	ctx := context.Background()
	engine := openEngine(t, cp.BackendMemory, ":memory:")
	defer func() { _ = engine.Close(ctx) }()

	cfg := serverconfig.DefaultServerConfig()
	cfg.GraphicalTargets = []serverconfig.GraphicalTargetConfig{
		{TargetID: "gt-x", Protocol: "vnc", TargetAddress: "vm:5900", Enabled: true},
	}
	if _, err := NewControlPlaneGraphicalTargets(cfg, engine); err == nil {
		t.Fatal("expected an error for an unsupported protocol")
	}
}
