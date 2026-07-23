//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverconfig

import (
	"os"
	"path/filepath"
	"testing"
)

func TestPyTypeNameDefault(t *testing.T) {
	// A type not enumerated in the switch hits the fmt.Sprintf("%T") default.
	if got := pyTypeName([]byte("x")); got != "[]uint8" {
		t.Fatalf("pyTypeName default = %q", got)
	}
}

func TestStructToMapErrors(t *testing.T) {
	// json.Marshal fails on a channel value.
	if _, err := structToMap(make(chan int)); err == nil {
		t.Fatal("expected marshal error")
	}
	// Marshals fine to "123" but does not unmarshal into a map.
	if _, err := structToMap(123); err == nil {
		t.Fatal("expected unmarshal-into-map error")
	}
}

func TestDecodeSectionExtraInputs(t *testing.T) {
	type mini struct {
		A int `json:"a"`
	}
	var m mini
	// Unknown key must be rejected (extra="forbid").
	if err := decodeSection(&m, map[string]any{"zzz": 1}); err == nil {
		t.Fatal("expected extra-inputs error")
	}
	// Non-map userVal is a no-op.
	if err := decodeSection(&m, "not-a-map"); err != nil {
		t.Fatalf("non-map userVal should be a no-op: %v", err)
	}
}

func TestApplyGraphicalTargetsWithConfigTable(t *testing.T) {
	cfg := DefaultServerConfig()
	data := map[string]any{
		"graphical_targets": []any{
			map[string]any{
				"target_id":      "t1",
				"target_address": "127.0.0.1:5900",
				"config":         map[string]any{"k": "v"},
			},
			"not-a-table", // skipped
		},
	}
	if err := applyGraphicalTargets(cfg, data); err != nil {
		t.Fatalf("applyGraphicalTargets: %v", err)
	}
	if len(cfg.GraphicalTargets) != 1 {
		t.Fatalf("expected 1 target, got %d", len(cfg.GraphicalTargets))
	}
	if cfg.GraphicalTargets[0].Config["k"] != "v" {
		t.Fatalf("config table not applied: %+v", cfg.GraphicalTargets[0].Config)
	}

	// Non-list graphical_targets is an error.
	if err := applyGraphicalTargets(cfg, map[string]any{"graphical_targets": "x"}); err == nil {
		t.Fatal("expected list-type error")
	}
}

func TestLoadServerConfigBadTOML(t *testing.T) {
	path := filepath.Join(t.TempDir(), "bad.toml")
	if err := os.WriteFile(path, []byte("this is = = not valid toml ]["), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadServerConfig(path); err == nil {
		t.Fatal("expected TOML parse error")
	}
}

func TestLoadServerConfigMappingError(t *testing.T) {
	path := filepath.Join(t.TempDir(), "extra.toml")
	// Unknown top-level key -> ConfigFromMapping returns an extra-inputs error.
	if err := os.WriteFile(path, []byte("totally_unknown_key = 1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadServerConfig(path); err == nil {
		t.Fatal("expected mapping validation error")
	}
}

func TestLoadServerConfigMissingFile(t *testing.T) {
	if _, err := LoadServerConfig(filepath.Join(t.TempDir(), "nope.toml")); err == nil {
		t.Fatal("expected read error for missing file")
	}
}
