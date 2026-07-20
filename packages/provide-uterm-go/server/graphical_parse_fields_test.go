//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/graphical"
)

// TestGetConfigField covers the optional config object parser used by create/update.
func TestGetConfigField(t *testing.T) {
	// Absent / null → empty map.
	m, err := getConfigField(map[string]any{})
	if err != nil || len(m) != 0 {
		t.Fatalf("absent: m=%v err=%v", m, err)
	}
	m, err = getConfigField(map[string]any{"config": nil})
	if err != nil || len(m) != 0 {
		t.Fatalf("nil: m=%v err=%v", m, err)
	}
	// Present object.
	m, err = getConfigField(map[string]any{"config": map[string]any{"vm_name": "v1"}})
	if err != nil || m["vm_name"] != "v1" {
		t.Fatalf("object: m=%v err=%v", m, err)
	}
	// Non-object → shape error.
	_, err = getConfigField(map[string]any{"config": "nope"})
	if err == nil {
		t.Fatal("expected shape error")
	}
	if ge, ok := err.(*graphical.Error); !ok || ge.Code != graphical.CodeInvalid {
		t.Fatalf("err = %v", err)
	}
}

// TestGetStringPtrField covers optional string pointer parsing.
func TestGetStringPtrField(t *testing.T) {
	p, err := getStringPtrField(map[string]any{}, "endpoint")
	if err != nil || p != nil {
		t.Fatalf("absent: %v %v", p, err)
	}
	p, err = getStringPtrField(map[string]any{"endpoint": nil}, "endpoint")
	if err != nil || p != nil {
		t.Fatalf("nil: %v %v", p, err)
	}
	p, err = getStringPtrField(map[string]any{"endpoint": "host:1"}, "endpoint")
	if err != nil || p == nil || *p != "host:1" {
		t.Fatalf("string: %v %v", p, err)
	}
	_, err = getStringPtrField(map[string]any{"endpoint": 3}, "endpoint")
	if err == nil {
		t.Fatal("expected type error")
	}
}
