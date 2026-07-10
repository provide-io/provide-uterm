//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package client

import (
	"reflect"
	"strings"
	"testing"
)

func TestSanitizeLongListTruncated(t *testing.T) {
	in := make([]any, 25)
	for i := range in {
		in[i] = i
	}
	got := sanitize(in).([]any)
	if len(got) != 11 {
		t.Fatalf("len = %d", len(got))
	}
	if got[10] != "..." {
		t.Fatalf("last elem = %v", got[10])
	}
}

func TestSanitizeShortListUnchanged(t *testing.T) {
	in := []any{1, 2, 3}
	got := sanitize(in).([]any)
	if !reflect.DeepEqual(got, in) {
		t.Fatalf("short list changed: %v", got)
	}
}

func TestSanitizeLongStringTruncated(t *testing.T) {
	got := sanitize(strings.Repeat("a", 600)).(string)
	if got != strings.Repeat("a", 500)+"..." {
		t.Fatalf("string trunc wrong len %d", len(got))
	}
}

func TestSanitizeCompositeKeysRedacted(t *testing.T) {
	got := sanitize(map[string]any{"api_key": "hidden", "access_token": "hidden"}).(map[string]any)
	if got["api_key"] != "***" || got["access_token"] != "***" {
		t.Fatalf("composite keys not redacted: %v", got)
	}
}

func TestSanitizeNestedRedaction(t *testing.T) {
	got := sanitize(map[string]any{"credentials": map[string]any{"api_key": "hidden", "password": "p"}}).(map[string]any)
	creds := got["credentials"].(map[string]any)
	if creds["api_key"] != "***" || creds["password"] != "***" {
		t.Fatalf("nested not redacted: %v", creds)
	}
}

func TestSanitizeNonSensitivePassthrough(t *testing.T) {
	got := sanitize(map[string]any{"status": "ok", "count": 3}).(map[string]any)
	if got["status"] != "ok" || got["count"] != 3 {
		t.Fatalf("non-sensitive changed: %v", got)
	}
}

func TestSanitizeAllSensitiveSubstrings(t *testing.T) {
	for _, k := range []string{"token", "secret", "password", "api_key", "authorization", "session_id"} {
		got := sanitize(map[string]any{k: "sensitive"}).(map[string]any)
		if got[k] != "***" {
			t.Fatalf("key %q not redacted: %v", k, got)
		}
	}
}

func TestSanitizePrimitivePassthrough(t *testing.T) {
	if sanitize(42) != 42 {
		t.Fatal("int changed")
	}
	if sanitize(nil) != nil {
		t.Fatal("nil changed")
	}
	if sanitize("short") != "short" {
		t.Fatal("short string changed")
	}
}
