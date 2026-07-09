//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package ansi

import (
	"slices"
	"strings"
	"testing"
)

// registerTemp registers a dialect and unregisters it at test cleanup.
func registerTemp(t *testing.T, name string, handler func(string) string) {
	t.Helper()
	if err := RegisterColorDialect(name, handler); err != nil {
		t.Fatalf("RegisterColorDialect(%q) = %v", name, err)
	}
	t.Cleanup(func() {
		if err := UnregisterColorDialect(name); err != nil {
			t.Fatalf("cleanup UnregisterColorDialect(%q) = %v", name, err)
		}
	})
}

func TestBuiltinsRegistered(t *testing.T) {
	want := []string{"brace_tokens", "extended_tokens", "tilde_codes", "pipe_codes"}
	if got := RegisteredDialects(); !slices.Equal(got, want) {
		t.Fatalf("RegisteredDialects() = %v, want %v", got, want)
	}
}

func TestRegisterAndList(t *testing.T) {
	registerTemp(t, "test_dialect", func(s string) string { return s })
	if !slices.Contains(RegisteredDialects(), "test_dialect") {
		t.Fatal("test_dialect not in RegisteredDialects()")
	}
}

func TestRegisterDuplicateErrors(t *testing.T) {
	err := RegisterColorDialect("pipe_codes", func(s string) string { return s })
	if err == nil {
		t.Fatal("expected error registering duplicate dialect")
	}
	if !strings.Contains(err.Error(), "already registered") {
		t.Fatalf("error %q does not mention 'already registered'", err)
	}
}

func TestUnregister(t *testing.T) {
	if err := RegisterColorDialect("temp", func(s string) string { return s }); err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(RegisteredDialects(), "temp") {
		t.Fatal("temp not registered")
	}
	if err := UnregisterColorDialect("temp"); err != nil {
		t.Fatal(err)
	}
	if slices.Contains(RegisteredDialects(), "temp") {
		t.Fatal("temp still registered after unregister")
	}
}

func TestUnregisterMissingErrors(t *testing.T) {
	err := UnregisterColorDialect("nonexistent")
	if err == nil {
		t.Fatal("expected error unregistering missing dialect")
	}
	if !strings.Contains(err.Error(), "not registered") {
		t.Fatalf("error %q does not mention 'not registered'", err)
	}
}

func TestDialectsCalledInOrder(t *testing.T) {
	var calls []string
	registerTemp(t, "a", func(s string) string {
		calls = append(calls, "a")
		return s
	})
	registerTemp(t, "b", func(s string) string {
		calls = append(calls, "b")
		return s
	})
	NormalizeColors("test")
	// Built-ins run first, then a, then b.
	if !slices.Equal(calls, []string{"a", "b"}) {
		t.Fatalf("calls = %v, want [a b]", calls)
	}
}

func TestDialectsTransformChaining(t *testing.T) {
	registerTemp(t, "upper_x", func(s string) string { return strings.ReplaceAll(s, "x", "X") })
	registerTemp(t, "bang", func(s string) string { return s + "!" })
	if got := NormalizeColors("xyz"); got != "Xyz!" {
		t.Fatalf("NormalizeColors chained = %q, want %q", got, "Xyz!")
	}
}

func TestMustRegisterPanicsOnDuplicate(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("mustRegister did not panic on duplicate name")
		}
	}()
	mustRegister("pipe_codes", func(s string) string { return s })
}

func TestPreviewANSIAliasesNormalizeColors(t *testing.T) {
	in := "|04Red{+g}Go~0"
	if got, want := PreviewANSI(in), NormalizeColors(in); got != want {
		t.Fatalf("PreviewANSI = %q, NormalizeColors = %q", got, want)
	}
}
