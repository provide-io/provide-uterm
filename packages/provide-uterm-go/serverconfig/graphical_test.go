// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later

package serverconfig

import "testing"

func TestGraphicalDefaultsAndValidation(t *testing.T) {
	cfg := DefaultServerConfig()
	if cfg.Graphical.AllowDynamicTargets || len(cfg.Graphical.DynamicAllowedCIDRs) != 0 {
		t.Fatalf("unsafe graphical defaults: %#v", cfg.Graphical)
	}
	_, err := ConfigFromMapping(map[string]any{"graphical": map[string]any{"allow_dynamic_targets": true}})
	if err == nil {
		t.Fatal("production dynamic targets must be rejected")
	}
	cfg, err = ConfigFromMapping(map[string]any{
		"environment": "dev",
		"graphical":   map[string]any{"allow_dynamic_targets": true, "dynamic_allowed_cidrs": []any{"10.0.0.0/8"}},
	})
	if err != nil || !cfg.Graphical.AllowDynamicTargets {
		t.Fatalf("dev graphical config: %v", err)
	}
}

func TestGraphicalTargetIdentityTLSAndDuplicateValidation(t *testing.T) {
	base := map[string]any{"target_id": "one", "endpoint": "dns:///[2001:db8::1]:443"}
	_, err := ConfigFromMapping(map[string]any{"graphical_targets": []any{base, base}})
	if err == nil {
		t.Fatal("duplicate static target accepted")
	}
	for _, endpoint := range []string{"dns:///2001:db8::1:443", "dns:///tärget:443", "dns:///host:0"} {
		_, err = ConfigFromMapping(map[string]any{"graphical_targets": []any{map[string]any{"target_id": "bad", "endpoint": endpoint}}})
		if err == nil {
			t.Fatalf("invalid endpoint accepted: %q", endpoint)
		}
	}
	_, err = ConfigFromMapping(map[string]any{"graphical_targets": []any{map[string]any{
		"target_id": "mtls", "endpoint": "dns:///host:443", "tls_mode": "mtls",
	}}})
	if err == nil {
		t.Fatal("mtls without client references accepted")
	}
}
