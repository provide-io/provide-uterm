//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import "testing"

func TestGraphicalTargetsDefaults(t *testing.T) {
	// Empty config → no graphical targets.
	cfg, err := ConfigFromMapping(map[string]any{})
	if err != nil {
		t.Fatalf("empty: %v", err)
	}
	if len(cfg.GraphicalTargets) != 0 {
		t.Fatalf("expected none, got %d", len(cfg.GraphicalTargets))
	}
	// Default auth tenant fields are populated.
	if cfg.Auth.TenantHeader != "x-uterm-tenant" || cfg.Auth.TenantCookie != "uterm_tenant" ||
		cfg.Auth.JWTTenantClaim != "tenant_id" {
		t.Fatalf("auth tenant defaults wrong: %+v", cfg.Auth)
	}
}

func TestGraphicalTargetsParsing(t *testing.T) {
	data := map[string]any{
		"graphical_targets": []any{
			map[string]any{
				"target_id":      "gt-console",
				"tenant_id":      "acme",
				"protocol":       "rfb",
				"target_address": "vm.local:5900",
				"vm_name":        "vm1",
				"name":           "Console",
				"description":    "primary",
				"width":          int64(800),
				"height":         int64(600),
				"is_static":      true,
			},
			// Minimal entry: defaults fill in (enabled true, 640x480, protocol rfb).
			map[string]any{"target_address": "other:5901"},
			// A disabled entry is still parsed into config (seeding drops it).
			map[string]any{"target_address": "off:5902", "enabled": false},
			// Non-table entries are skipped.
			"not-a-table",
			int64(7),
		},
	}
	cfg, err := ConfigFromMapping(data)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(cfg.GraphicalTargets) != 3 {
		t.Fatalf("expected 3 targets, got %d", len(cfg.GraphicalTargets))
	}

	full := cfg.GraphicalTargets[0]
	if full.TargetID != "gt-console" || full.TenantID != "acme" || full.TargetAddress != "vm.local:5900" ||
		full.VMName == nil || *full.VMName != "vm1" || full.Name != "Console" ||
		full.Description == nil || *full.Description != "primary" ||
		full.Width != 800 || full.Height != 600 || !full.Enabled || !full.IsStatic {
		t.Fatalf("full target wrong: %+v", full)
	}

	minimal := cfg.GraphicalTargets[1]
	if minimal.Protocol != "rfb" || minimal.Width != 640 || minimal.Height != 480 ||
		!minimal.Enabled || minimal.VMName != nil || minimal.Description != nil {
		t.Fatalf("minimal defaults wrong: %+v", minimal)
	}

	if cfg.GraphicalTargets[2].Enabled {
		t.Fatalf("disabled entry should keep enabled=false")
	}
}

func TestGraphicalTargetsWrongType(t *testing.T) {
	_, err := ConfigFromMapping(map[string]any{"graphical_targets": "nope"})
	if err == nil {
		t.Fatalf("expected error for non-list graphical_targets")
	}
}
