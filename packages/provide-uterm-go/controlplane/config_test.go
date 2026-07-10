//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package controlplane

import (
	"errors"
	"testing"
)

func TestDefaultConfig(t *testing.T) {
	t.Parallel()
	c := DefaultConfig()
	if c.Backend != BackendMemory || c.DatabaseURL != ":memory:" {
		t.Fatalf("unexpected defaults: %+v", c)
	}
	if c.Capabilities != DefaultCapabilities() {
		t.Fatalf("unexpected capabilities: %+v", c.Capabilities)
	}
}

func TestDefaultCapabilitiesAllTrue(t *testing.T) {
	t.Parallel()
	caps := DefaultCapabilities()
	if !caps.SupportsTransactions || !caps.SupportsMigrations || !caps.SupportsRetries {
		t.Fatalf("expected all capability flags true, got %+v", caps)
	}
}

func TestConfigNormalized(t *testing.T) {
	t.Parallel()
	tests := []struct {
		name string
		in   Config
		want Config
	}{
		{
			name: "empty gets all defaults",
			in:   Config{},
			want: DefaultConfig(),
		},
		{
			name: "only url set keeps sqlite backend when given",
			in:   Config{Backend: BackendSQLite, DatabaseURL: "/tmp/x.db"},
			want: Config{Backend: BackendSQLite, DatabaseURL: "/tmp/x.db", Capabilities: DefaultCapabilities()},
		},
		{
			name: "custom capabilities preserved",
			in:   Config{Capabilities: EngineCapabilities{SupportsTransactions: true}},
			want: Config{
				Backend:      BackendMemory,
				DatabaseURL:  ":memory:",
				Capabilities: EngineCapabilities{SupportsTransactions: true},
			},
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			if got := tc.in.Normalized(); got != tc.want {
				t.Fatalf("Normalized() = %+v, want %+v", got, tc.want)
			}
		})
	}
}

func TestErrorKinds(t *testing.T) {
	t.Parallel()
	cfg := ConfigurationError("bad")
	if cfg.Error() != "bad" || cfg.Kind != "configuration" {
		t.Fatalf("configuration error mismatch: %+v", cfg)
	}
	if CapabilityError("c").Kind != "capability" {
		t.Fatal("capability kind mismatch")
	}
	conflict := ConflictError("boom")
	if conflict.Kind != "conflict" {
		t.Fatal("conflict kind mismatch")
	}
	if !IsConflict(conflict) {
		t.Fatal("IsConflict should be true for a conflict error")
	}
	if IsConflict(cfg) {
		t.Fatal("IsConflict should be false for a configuration error")
	}
	if IsConflict(errors.New("plain")) {
		t.Fatal("IsConflict should be false for a non-control-plane error")
	}
	if IsConflict(nil) {
		t.Fatal("IsConflict(nil) should be false")
	}
}
