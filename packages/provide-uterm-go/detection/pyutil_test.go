//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package detection

import (
	"errors"
	"testing"
)

func TestPyTruthy(t *testing.T) {
	cases := []struct {
		v    any
		want bool
	}{
		{nil, false}, {false, false}, {true, true}, {"", false}, {"x", true},
		{0, false}, {5, true}, {int64(0), false}, {int64(3), true},
		{0.0, false}, {1.5, true}, {[]any{}, false}, {[]any{1}, true},
		{map[string]any{}, false}, {map[string]any{"a": 1}, true},
		{struct{}{}, true},
	}
	for _, c := range cases {
		if got := pyTruthy(c.v); got != c.want {
			t.Errorf("pyTruthy(%#v) = %v, want %v", c.v, got, c.want)
		}
	}
}

func TestPyIntOr0(t *testing.T) {
	cases := []struct {
		v    any
		want int
	}{
		{nil, 0}, {false, 0}, {true, 1}, {"", 0}, {0, 0}, {7, 7},
		{int64(9), 9}, {2.9, 2}, {-3.9, -3}, {"5", 5}, {"not_an_int", 0},
		{" 8 ", 8}, {"5.0", 0}, {[]any{1}, 0},
	}
	for _, c := range cases {
		if got := pyIntOr0(c.v); got != c.want {
			t.Errorf("pyIntOr0(%#v) = %d, want %d", c.v, got, c.want)
		}
	}
}

func TestAsStringAndPatternHelpers(t *testing.T) {
	if asString(nil) != "" {
		t.Error("asString(nil)")
	}
	if asString(42) != "" {
		t.Error("asString(non-string)")
	}
	if asString("hi") != "hi" {
		t.Error("asString(string)")
	}
	p := Pattern{"a": "x", "n": 5}
	if patternString(p, "a", "d") != "x" {
		t.Error("patternString present")
	}
	if patternString(p, "missing", "d") != "d" {
		t.Error("patternString default")
	}
	if patternString(p, "n", "d") != "d" {
		t.Error("patternString non-string default")
	}
	if !patternBoolDefaultTrue(p, "missing") {
		t.Error("bool default true")
	}
	if patternBoolDefaultTrue(Pattern{"k": false}, "k") {
		t.Error("bool present false")
	}
}

func TestPyRegexEscape(t *testing.T) {
	if got := pyRegexEscape("Enter your name"); got != `Enter\ your\ name` {
		t.Errorf("got %q", got)
	}
	if got := pyRegexEscape("[Press ENTER]"); got != `\[Press\ ENTER\]` {
		t.Errorf("got %q", got)
	}
	if got := pyRegexEscape("a-b.c#d&e~f g"); got != `a\-b\.c\#d\&e\~f\ g` {
		t.Errorf("got %q", got)
	}
	if got := pyRegexEscape("plain"); got != "plain" {
		t.Errorf("got %q", got)
	}
}

func TestTranslatePyRegex(t *testing.T) {
	if got := translatePyRegex(`Command \[.*\Z`); got != `Command \[.*\z` {
		t.Errorf("got %q", got)
	}
	if got := translatePyRegex(`no anchor`); got != `no anchor` {
		t.Errorf("passthrough got %q", got)
	}
	// escaped backslash before Z is left as-is (not our anchor)
	if got := translatePyRegex(`x\\Z`); got != `x\\Z` {
		t.Errorf("escaped backslash got %q", got)
	}
	// trailing lone backslash is preserved
	if got := translatePyRegex(`\Zab\`); got != `\zab\` {
		t.Errorf("got %q", got)
	}
}

func TestCompilePyRegexErrors(t *testing.T) {
	// lookahead is unsupported under RE2 -> typed error naming the pattern
	_, err := compilePyRegex(`foo(?=bar)`, "m")
	if err == nil {
		t.Fatal("expected error for lookahead")
	}
	var re2 *RE2TranslateError
	if !errors.As(err, &re2) {
		t.Fatalf("want *RE2TranslateError, got %T", err)
	}
	if re2.Pattern != `foo(?=bar)` {
		t.Errorf("pattern = %q", re2.Pattern)
	}
	if re2.Unwrap() == nil {
		t.Error("Unwrap nil")
	}
	if re2.Error() == "" {
		t.Error("empty Error()")
	}
	// backreference is unsupported
	if _, err := compilePyRegex(`(a)\1`, "m"); err == nil {
		t.Error("expected error for backreference")
	}
	// \Z translated then compiles
	if _, err := compilePyRegex(`x\Z`, "m"); err != nil {
		t.Errorf("unexpected error: %v", err)
	}
	// empty flags path
	if _, err := compilePyRegex(`abc`, ""); err != nil {
		t.Errorf("unexpected error: %v", err)
	}
}
