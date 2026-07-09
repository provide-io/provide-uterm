//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package redaction

import "testing"

func TestMakeRedactorIdentityWhenEmpty(t *testing.T) {
	for _, patterns := range [][]string{nil, {}} {
		r, err := MakeRedactor(patterns)
		if err != nil {
			t.Fatal(err)
		}
		if got := r("secret token=abc"); got != "secret token=abc" {
			t.Fatalf("got %q", got)
		}
	}
}

func TestMakeRedactorReplacesAllPatterns(t *testing.T) {
	r, err := MakeRedactor([]string{`token=\S+`, `\d{4}-\d{4}`})
	if err != nil {
		t.Fatal(err)
	}
	got := r("token=abc card 1234-5678 token=def")
	want := "[REDACTED] card [REDACTED] [REDACTED]"
	if got != want {
		t.Fatalf("got %q want %q", got, want)
	}
}

func TestMakeRedactorInvalidPattern(t *testing.T) {
	if _, err := MakeRedactor([]string{`(`}); err == nil {
		t.Fatal("expected compile error")
	}
}

func TestRedactText(t *testing.T) {
	if got := RedactText("plain", nil); got != "plain" {
		t.Fatalf("got %q", got)
	}
	r, err := MakeRedactor([]string{"a+"})
	if err != nil {
		t.Fatal(err)
	}
	if got := RedactText("baab", r); got != "b[REDACTED]b" {
		t.Fatalf("got %q", got)
	}
}
