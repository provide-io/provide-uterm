//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package annotation

import (
	"errors"
	"strings"
	"testing"
)

func TestCompilePatternOK(t *testing.T) {
	re, err := CompilePattern(`\bsudo\b`)
	if err != nil || re == nil {
		t.Fatalf("expected clean compile, got re=%v err=%v", re, err)
	}
}

func TestCompilePatternUnsupportedYieldsTypedError(t *testing.T) {
	// Lookahead is unsupported under RE2 -> typed *RE2TranslateError.
	_, err := CompilePattern(`foo(?=bar)`)
	if err == nil {
		t.Fatal("expected error for lookahead pattern")
	}
	var re2 *RE2TranslateError
	if !errors.As(err, &re2) {
		t.Fatalf("want *RE2TranslateError, got %T", err)
	}
	if re2.Pattern != `foo(?=bar)` {
		t.Fatalf("error should name the pattern, got %q", re2.Pattern)
	}
	if !strings.Contains(re2.Error(), "not RE2-compatible") {
		t.Fatalf("unexpected error text: %q", re2.Error())
	}
	if re2.Unwrap() == nil {
		t.Fatal("expected wrapped underlying error")
	}
}

func TestMustCompilePanicsOnBadPattern(t *testing.T) {
	defer func() {
		if r := recover(); r == nil {
			t.Fatal("expected panic on unsupported pattern")
		}
	}()
	_ = mustCompile(`foo(?=bar)`)
}
