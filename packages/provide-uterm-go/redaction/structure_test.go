//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package redaction

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

func testRedactor(t *testing.T) Redactor {
	t.Helper()
	r, err := MakeRedactor([]string{`S3cret\w*`})
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func nest(depth int, leaf any, kind string) any {
	value := leaf
	for i := 0; i < depth; i++ {
		if kind == "map" {
			value = map[string]any{"k": value}
		} else {
			value = []any{value}
		}
	}
	return value
}

func innermost(value any) any {
	for {
		switch v := value.(type) {
		case map[string]any:
			value = v["k"]
		case []any:
			value = v[0]
		default:
			return value
		}
	}
}

func levels(value any) int {
	n := 0
	for {
		switch v := value.(type) {
		case map[string]any:
			value = v["k"]
		case []any:
			value = v[0]
		default:
			return n
		}
		n++
	}
}

func TestStructureConstantsMatchTheReference(t *testing.T) {
	if RedactMaxDepth != 32 || DepthLimitMarker != "[NESTED_REDACTED]" || SensitiveKeyMarker != "[SECRET_REDACTED]" {
		t.Fatal("constants drifted from provide.uterm.redaction")
	}
}

func TestStructureRedactsStringsAndKeepsScalars(t *testing.T) {
	r := testRedactor(t)
	in := map[string]any{"a": []any{"S3cret1", map[string]any{"b": "x S3cret2"}, 1.5, true, nil}}
	got := RedactStructure(in, r, StructureOptions{})
	want := map[string]any{"a": []any{"[REDACTED]", map[string]any{"b": "x [REDACTED]"}, 1.5, true, nil}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
	if in["a"].([]any)[0] != "S3cret1" {
		t.Fatal("input mutated")
	}
	if RedactStructure(7, r, StructureOptions{}) != 7 || RedactStructure(nil, r, StructureOptions{}) != nil {
		t.Fatal("scalar changed")
	}
}

func TestStructureWalksTypedContainers(t *testing.T) {
	r := testRedactor(t)
	got := RedactStructure(map[string]any{
		"strs":  []string{"S3cretA", "ok"},
		"arr":   [2]string{"S3cretB", "ok"},
		"smap":  map[string]string{"k": "S3cretC"},
		"imap":  map[int]string{7: "S3cretD"},
		"bytes": []byte("S3cretE"),
	}, r, StructureOptions{})
	want := map[string]any{
		"strs":  []any{"[REDACTED]", "ok"},
		"arr":   []any{"[REDACTED]", "ok"},
		"smap":  map[string]any{"k": "[REDACTED]"},
		"imap":  map[string]any{"7": "[REDACTED]"},
		"bytes": []byte("S3cretE"),
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %#v", got)
	}
}

func TestStructureKeepsNilContainersNil(t *testing.T) {
	r := testRedactor(t)
	var nilSlice []string
	var nilMap map[string]any
	if got := RedactStructure(nilSlice, r, StructureOptions{}); !reflect.DeepEqual(got, nilSlice) {
		t.Fatalf("nil slice became %#v", got)
	}
	if got := RedactStructure(nilMap, r, StructureOptions{}); !reflect.DeepEqual(got, nilMap) {
		t.Fatalf("nil map became %#v", got)
	}
	// Even past the cap a nil container stays what it was: it holds nothing.
	if got := RedactStructure(nilSlice, r, StructureOptions{MaxDepth: depth(-1)}); !reflect.DeepEqual(got, nilSlice) {
		t.Fatalf("nil slice became %#v", got)
	}
}

func TestStructureDepthCapBoundary(t *testing.T) {
	r := testRedactor(t)
	for _, kind := range []string{"map", "list"} {
		inside := RedactStructure(nest(RedactMaxDepth, "S3cretLeaf", kind), r, StructureOptions{})
		if innermost(inside) != "[REDACTED]" || levels(inside) != RedactMaxDepth {
			t.Fatalf("%s: last level inside the cap not walked: %v", kind, innermost(inside))
		}
		past := RedactStructure(nest(RedactMaxDepth+1, "S3cretLeaf", kind), r, StructureOptions{})
		if innermost(past) != DepthLimitMarker || levels(past) != RedactMaxDepth {
			t.Fatalf("%s: container at the cap not replaced: %v (%d levels)", kind, innermost(past), levels(past))
		}
	}
	// A scalar at the cap is handled as usual, not replaced.
	if got := innermost(RedactStructure(nest(RedactMaxDepth, 5, "list"), r, StructureOptions{})); got != 5 {
		t.Fatalf("scalar at cap = %v", got)
	}
	arr := RedactStructure([1]any{[1]any{"x"}}, r, StructureOptions{MaxDepth: depth(1)})
	if !reflect.DeepEqual(arr, []any{DepthLimitMarker}) {
		t.Fatalf("array at cap = %#v", arr)
	}
}

func depth(n int) *int { return &n }

func TestStructureMaxDepthOption(t *testing.T) {
	r := testRedactor(t)
	if got := RedactStructure(map[string]any{"a": map[string]any{"b": "x"}}, r, StructureOptions{MaxDepth: depth(1)}); !reflect.DeepEqual(got, map[string]any{"a": DepthLimitMarker}) {
		t.Fatalf("MaxDepth 1 = %#v", got)
	}
	if got := RedactStructure(map[string]any{"a": map[string]any{"b": "S3cret"}}, r, StructureOptions{MaxDepth: depth(2)}); !reflect.DeepEqual(got, map[string]any{"a": map[string]any{"b": "[REDACTED]"}}) {
		t.Fatalf("MaxDepth 2 = %#v", got)
	}
	// 0 caps at the root, like the reference's max_depth=0; a string is still redacted.
	if got := RedactStructure([]any{"x"}, r, StructureOptions{MaxDepth: depth(0)}); got != DepthLimitMarker {
		t.Fatalf("MaxDepth 0 = %#v", got)
	}
	if got := RedactStructure("S3cret", r, StructureOptions{MaxDepth: depth(0)}); got != "[REDACTED]" {
		t.Fatalf("MaxDepth 0 string = %#v", got)
	}
	// nil selects the default cap.
	if got := RedactStructure(nest(RedactMaxDepth, "S3cretLeaf", "map"), r, StructureOptions{}); innermost(got) != "[REDACTED]" {
		t.Fatal("nil MaxDepth did not select the default")
	}
}

func TestStructureKeyAwareKeepsTypedNilsNull(t *testing.T) {
	r := testRedactor(t)
	var nilPtr *string
	var nilMap map[string]any
	var nilSlice []string
	var nilIface error
	got := RedactStructure(map[string]any{
		"token": nilPtr, "secret": nilMap, "password": nilSlice, "cookie": nilIface,
		"apiKey": "live", "accessToken": new(string),
	}, r, StructureOptions{KeyAware: true}).(map[string]any)
	for _, k := range []string{"token", "secret", "password", "cookie"} {
		if !isNil(got[k]) {
			t.Fatalf("%s: typed nil was masked: %#v", k, got[k])
		}
	}
	if got["apiKey"] != SensitiveKeyMarker || got["accessToken"] != SensitiveKeyMarker {
		t.Fatalf("non-nil values not masked: %#v", got)
	}
	if isNil(0) || isNil("") || isNil(struct{}{}) {
		t.Fatal("non-nilable kinds reported nil")
	}
}

func TestStructureVeryDeepInputIsCapped(t *testing.T) {
	r := testRedactor(t)
	got := RedactStructure(nest(100000, "S3cretLeaf", "map"), r, StructureOptions{KeyAware: true})
	encoded, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), "S3cretLeaf") || innermost(got) != DepthLimitMarker {
		t.Fatal("deep input not capped")
	}
}

func TestStructureKeyAware(t *testing.T) {
	r := testRedactor(t)
	frame := map[string]any{
		"type":  "resume",
		"token": "abc",
		"wrap":  []any{map[string]any{"resume_token": "x", "password": nil, "keep": "S3cret", "secret": map[string]any{"a": 1}}},
	}
	off := RedactStructure(frame, r, StructureOptions{})
	wantOff := map[string]any{
		"type":  "resume",
		"token": "abc",
		"wrap":  []any{map[string]any{"resume_token": "x", "password": nil, "keep": "[REDACTED]", "secret": map[string]any{"a": 1}}},
	}
	if !reflect.DeepEqual(off, wantOff) {
		t.Fatalf("key-aware off = %#v", off)
	}
	on := RedactStructure(frame, r, StructureOptions{KeyAware: true})
	wantOn := map[string]any{
		"type":  "resume",
		"token": SensitiveKeyMarker,
		"wrap":  []any{map[string]any{"resume_token": SensitiveKeyMarker, "password": nil, "keep": "[REDACTED]", "secret": SensitiveKeyMarker}},
	}
	if !reflect.DeepEqual(on, wantOn) {
		t.Fatalf("key-aware on = %#v", on)
	}
}

func TestIsSensitiveKey(t *testing.T) {
	for _, key := range []string{
		"token", "TOKEN", "Resume-Token", "session_token", "password", "passwd", "passphrase",
		"ssh_passphrase", "secret", "client_secret", "api_key", "api-key", "apikey", "openai_api_key",
		"Authorization", "cookie", "private_key", "db_password", "accessToken", "clientSecret",
		"privateKey", "Set-Cookie", "Proxy-Authorization", "credentials", "credential",
		"aws_secret_access_key", "APIKey", "x-api-key", "access_key_id", "HTTPCookie", "refresh.token",
		"Session Token",
	} {
		if !IsSensitiveKey(key) {
			t.Errorf("%q should be sensitive", key)
		}
	}
	for _, key := range []string{
		"", "_", "tokens", "token_count", "max_tokens", "secretary", "type", "tokenizer", "token_type",
		"password_required", "secret_hash", "session_id", "api", "key", "keyApi", "privately_key",
		"TOKEN_TTL", "token2",
	} {
		if IsSensitiveKey(key) {
			t.Errorf("%q should be ordinary", key)
		}
	}
}

func TestKeySegments(t *testing.T) {
	cases := map[string][]string{
		"accessToken":  {"access", "token"},
		"APIKey":       {"api", "key"},
		"HTTPCookie":   {"http", "cookie"},
		"RESUME_TOKEN": {"resume", "token"},
		"a.b c-d__e":   {"a", "b", "c", "d", "e"},
		"token2Fa":     {"token2", "fa"},
		"X":            {"x"},
		"aB":           {"a", "b"},
		"AB":           {"ab"},
		"ABc":          {"a", "bc"},
		"ABC":          {"abc"},
		// Character-class boundaries, each pinned by a key that splits on it.
		"aZ":  {"a", "z"},
		"zA":  {"z", "a"},
		"0A":  {"0", "a"},
		"9A":  {"9", "a"},
		"YZ":  {"yz"},
		"@Z":  {"@z"},
		"[Z":  {"[z"},
		"ABa": {"a", "ba"},
		"ABz": {"a", "bz"},
		"AB{": {"ab{"},
		"AB`": {"ab`"},
		"/A":  {"/a"},
		":A":  {":a"},
		"":    {},
	}
	for key, want := range cases {
		if got := KeySegments(key); !reflect.DeepEqual(got, want) {
			t.Errorf("KeySegments(%q) = %#v, want %#v", key, got, want)
		}
	}
}
