//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package cli

import (
	"encoding/json"
	"math"
	"strings"
	"testing"
)

// TestPyAuditFloatRepr pins the float layout against CPython repr() outputs
// (hand-verified with the differential parity test's uv run).
func TestPyAuditFloatRepr(t *testing.T) {
	cases := map[float64]string{
		0.1:                  "0.1",
		2.5:                  "2.5",
		100000.0:             "100000.0",
		3.0:                  "3.0",
		1720000000.123456:    "1720000000.123456",
		1e-5:                 "1e-05",
		2.5e16:               "2.5e+16",
		math.Copysign(0, -1): "-0.0",
		1e17:                 "1e+17",
		0.0:                  "0.0",
		1234567890.0:         "1234567890.0",
		math.Inf(1):          "Infinity",
		math.Inf(-1):         "-Infinity",
	}
	for in, want := range cases {
		if got := pyAuditFloatRepr(in); got != want {
			t.Errorf("pyAuditFloatRepr(%v) = %q, want %q", in, got, want)
		}
	}
	if got := pyAuditFloatRepr(math.NaN()); got != "NaN" {
		t.Errorf("NaN repr = %q", got)
	}
}

// TestAuditEncodeSortsKeysCompact checks sorted keys and compact separators.
func TestAuditEncodeSortsKeysCompact(t *testing.T) {
	var b strings.Builder
	if err := auditEncode(&b, map[string]any{"b": num("1"), "a": "x", "c": true}); err != nil {
		t.Fatal(err)
	}
	if got, want := b.String(), `{"a":"x","b":1,"c":true}`; got != want {
		t.Errorf("got %q want %q", got, want)
	}
}

// TestAuditEncodeEnsureAsciiFalse checks non-ASCII passes through literally, C0
// control chars are escaped as \uXXXX, and 0x7f passes through unescaped —
// exactly CPython's ensure_ascii=False contract.
func TestAuditEncodeEnsureAsciiFalse(t *testing.T) {
	var b strings.Builder
	input := "héllo→世界\n\t" + string(rune(0x01)) + string(rune(0x7f))
	_ = auditEncode(&b, map[string]any{"s": input})
	got := b.String()
	if !strings.Contains(got, "héllo→世界") {
		t.Errorf("non-ASCII not literal: %q", got)
	}
	if !strings.Contains(got, `\n`) || !strings.Contains(got, `\t`) || !strings.Contains(got, `\u0001`) {
		t.Errorf("control escaping wrong: %q", got)
	}
	if !strings.ContainsRune(got, 0x7f) {
		t.Errorf("0x7f should pass through literally: %q", got)
	}
}

// TestAuditEncodeNestedAndNull exercises slice, nested map, and null.
func TestAuditEncodeNestedAndNull(t *testing.T) {
	var b strings.Builder
	_ = auditEncode(&b, map[string]any{"list": []any{num("1"), num("2.0"), nil, false}})
	if got, want := b.String(), `{"list":[1,2.0,null,false]}`; got != want {
		t.Errorf("got %q want %q", got, want)
	}
}

// TestAuditEncodeUnsupportedType errors on a non-JSON value type.
func TestAuditEncodeUnsupportedType(t *testing.T) {
	var b strings.Builder
	if err := auditEncode(&b, struct{}{}); err == nil {
		t.Fatal("expected error for unsupported type")
	}
}

// TestAuditEncodeNumberExponential renders an exponential json.Number via repr.
func TestAuditEncodeNumberExponential(t *testing.T) {
	var b strings.Builder
	_ = auditEncodeNumber(&b, json.Number("1e-05"))
	if got := b.String(); got != "1e-05" {
		t.Errorf("got %q", got)
	}
}

// TestAuditEncodeNumberInvalid errors on a malformed float literal.
func TestAuditEncodeNumberInvalid(t *testing.T) {
	var b strings.Builder
	if err := auditEncodeNumber(&b, json.Number("1.2.3")); err == nil {
		t.Fatal("expected error")
	}
}
