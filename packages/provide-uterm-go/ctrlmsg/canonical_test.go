//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package ctrlmsg

import (
	"encoding/json"
	"math"
	"testing"
)

func TestPyFloatRepr(t *testing.T) {
	// Golden values captured from CPython repr()/json.dumps.
	cases := []struct {
		in   float64
		want string
	}{
		{1.5, "1.5"},
		{0.1, "0.1"},
		{1e20, "1e+20"},
		{1e16, "1e+16"},
		{1e15, "1000000000000000.0"},
		{3.14159, "3.14159"},
		{math.Copysign(0, -1), "-0.0"},
		{0.0, "0.0"},
		{2.0, "2.0"},
		{100.0, "100.0"},
		{1e-5, "1e-05"},
		{1e-4, "0.0001"},
		{0.0001, "0.0001"},
		{1.5e16, "1.5e+16"},
		{1e100, "1e+100"},
		{1e-10, "1e-10"},
		{123456789.0, "123456789.0"},
		{9999999999999998.0, "9999999999999998.0"},
		{-1.5, "-1.5"},
		{1e-300, "1e-300"},
		{math.NaN(), "NaN"},
		{math.Inf(1), "Infinity"},
		{math.Inf(-1), "-Infinity"},
	}
	for _, c := range cases {
		if got := pyFloatRepr(c.in); got != c.want {
			t.Errorf("pyFloatRepr(%v) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestCanonicalJSONScalars(t *testing.T) {
	cases := []struct {
		in   any
		want string
	}{
		{nil, "null"},
		{true, "true"},
		{false, "false"},
		{"hi", `"hi"`},
		{42, "42"},
		{int64(-7), "-7"},
		{2.5, "2.5"},
		{json.Number("255"), "255"},
		{json.Number("-9007199254740993"), "-9007199254740993"},
		{json.Number("1e20"), "1e+20"},
		{json.Number("1.5"), "1.5"},
		{json.Number("1E16"), "1e+16"},
	}
	for _, c := range cases {
		got, err := CanonicalJSON(c.in)
		if err != nil {
			t.Fatalf("CanonicalJSON(%v): %v", c.in, err)
		}
		if got != c.want {
			t.Errorf("CanonicalJSON(%v) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestCanonicalJSONStringEscaping(t *testing.T) {
	// Expectations use interpreted-string escapes (no literal glyphs/control
	// bytes in the source): e.g. "\"\\u00e9\"" is the six-character JSON string
	// `"é"` that CPython's ensure_ascii emits for U+00E9.
	cases := []struct {
		in   string
		want string
	}{
		{"é", "\"\\u00e9\""},                  // é
		{"→", "\"\\u2192\""},                  // →
		{"\U0001F600", "\"\\ud83d\\ude00\""},  // 😀 (surrogate pair)
		{"\x7f", "\"\\u007f\""},               // DEL is escaped (not S_CHAR)
		{"\x00", "\"\\u0000\""},               // NUL
		{"a<b>&", "\"a<b>&\""},                // HTML metacharacters are not escaped
		{"\n\t\r\b\f", "\"\\n\\t\\r\\b\\f\""}, // short control forms
		{"\x1f", "\"\\u001f\""},               // other control char -> \u00XX
		{"\U0001D11E", "\"\\ud834\\udd1e\""},  // 𝄞 (surrogate pair)
		{"a\"b\\c", "\"a\\\"b\\\\c\""},        // quote + backslash
		{"/", "\"/\""},                        // forward slash is not escaped
		{"ascii ~", "\"ascii ~\""},            // 0x7e tilde tops the S_CHAR range
	}
	for _, c := range cases {
		got, err := CanonicalJSON(c.in)
		if err != nil {
			t.Fatalf("CanonicalJSON(%q): %v", c.in, err)
		}
		if got != c.want {
			t.Errorf("CanonicalJSON(%q) = %s, want %s", c.in, got, c.want)
		}
	}
}

func TestCanonicalJSONContainers(t *testing.T) {
	cases := []struct {
		in   any
		want string
	}{
		{map[string]any{}, "{}"},
		{[]any{}, "[]"},
		{map[string]any{"b": 1, "a": 2}, `{"a":2,"b":1}`},
		// Keys sorted by code point: uppercase < lowercase < non-ASCII.
		{map[string]any{"z": 1, "a": 2, "é": 3, "A": 4}, "{\"A\":4,\"a\":2,\"z\":1,\"\\u00e9\":3}"},
		{[]any{1, "s", true, nil, 2.5}, `[1,"s",true,null,2.5]`},
		{map[string]any{"nested": map[string]any{"x": []any{1, 2}}}, `{"nested":{"x":[1,2]}}`},
	}
	for _, c := range cases {
		got, err := CanonicalJSON(c.in)
		if err != nil {
			t.Fatalf("CanonicalJSON(%v): %v", c.in, err)
		}
		if got != c.want {
			t.Errorf("CanonicalJSON(%v) = %s, want %s", c.in, got, c.want)
		}
	}
}

func TestCanonicalJSONErrors(t *testing.T) {
	// Unsupported top-level type.
	if _, err := CanonicalJSON(float32(1.5)); err == nil {
		t.Error("expected error for float32")
	}
	// Unsupported type nested in a map.
	if _, err := CanonicalJSON(map[string]any{"k": []string{"x"}}); err == nil {
		t.Error("expected error for nested []string")
	}
	// Unsupported type nested in a slice.
	if _, err := CanonicalJSON([]any{struct{}{}}); err == nil {
		t.Error("expected error for nested struct")
	}
	// Invalid json.Number (non-numeric).
	if _, err := CanonicalJSON(json.Number("not-a-number-1.2.3")); err == nil {
		t.Error("expected error for invalid json.Number")
	}
}
