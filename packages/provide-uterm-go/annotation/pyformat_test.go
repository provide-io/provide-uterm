//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package annotation

import "testing"

func TestPyFormatNamedFields(t *testing.T) {
	got, err := pyFormat("AWS access key detected in {event_type}", map[string]string{"event_type": "read"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != "AWS access key detected in read" {
		t.Fatalf("got %q", got)
	}
}

func TestPyFormatMatchAndEventType(t *testing.T) {
	got, err := pyFormat("{match} in {event_type}", map[string]string{"match": "sudo", "event_type": "send"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != "sudo in send" {
		t.Fatalf("got %q", got)
	}
}

func TestPyFormatEscapedBraces(t *testing.T) {
	got, err := pyFormat("literal {{ and }} braces {match}", map[string]string{"match": "X"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != "literal { and } braces X" {
		t.Fatalf("got %q", got)
	}
}

func TestPyFormatConversionAndSpecIgnored(t *testing.T) {
	got, err := pyFormat("{match!r} {event_type:>4}", map[string]string{"match": "a", "event_type": "b"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != "a b" {
		t.Fatalf("got %q", got)
	}
}

func TestPyFormatAttributeAndIndexAccessBaseName(t *testing.T) {
	// The base arg name is resolved; attribute/index chains are ignored.
	got, err := pyFormat("{match.foo} {event_type[0]}", map[string]string{"match": "m", "event_type": "e"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if got != "m e" {
		t.Fatalf("got %q", got)
	}
}

func TestPyFormatKeyError(t *testing.T) {
	_, err := pyFormat("desc with {unknown_key}", map[string]string{"match": "x"})
	fe, ok := err.(*pyFormatError)
	if !ok || fe.Kind != keyError || !fe.caught() {
		t.Fatalf("expected caught keyError, got %v", err)
	}
}

func TestPyFormatIndexErrorPositional(t *testing.T) {
	_, err := pyFormat("{0} positional", map[string]string{})
	fe, ok := err.(*pyFormatError)
	if !ok || fe.Kind != indexError || !fe.caught() {
		t.Fatalf("expected caught indexError, got %v", err)
	}
}

func TestPyFormatIndexErrorAutoNumber(t *testing.T) {
	_, err := pyFormat("{} auto", map[string]string{})
	fe, ok := err.(*pyFormatError)
	if !ok || fe.Kind != indexError {
		t.Fatalf("expected indexError, got %v", err)
	}
}

func TestPyFormatValueErrorSingleOpen(t *testing.T) {
	_, err := pyFormat("unbalanced { brace", map[string]string{})
	fe, ok := err.(*pyFormatError)
	if !ok || fe.Kind != valueError || fe.caught() {
		t.Fatalf("expected uncaught valueError, got %v", err)
	}
	if fe.Error() == "" {
		t.Fatal("expected non-empty error message")
	}
}

func TestPyFormatValueErrorSingleClose(t *testing.T) {
	_, err := pyFormat("unbalanced } brace", map[string]string{})
	fe, ok := err.(*pyFormatError)
	if !ok || fe.Kind != valueError {
		t.Fatalf("expected valueError, got %v", err)
	}
}

func TestIsAllDigits(t *testing.T) {
	cases := map[string]bool{"": false, "0": true, "42": true, "4a": false, "a": false}
	for in, want := range cases {
		if got := isAllDigits(in); got != want {
			t.Errorf("isAllDigits(%q) = %v, want %v", in, got, want)
		}
	}
}

func TestRuneTruncate(t *testing.T) {
	if got := runeTruncate("abc", 80); got != "abc" {
		t.Fatalf("short string changed: %q", got)
	}
	if got := runeTruncate("ababab", 3); got != "aba" {
		t.Fatalf("expected 'aba', got %q", got)
	}
}
