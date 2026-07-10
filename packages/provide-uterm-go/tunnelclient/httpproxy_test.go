//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package tunnelclient

import (
	"bytes"
	"encoding/base64"
	"testing"
)

func TestEncodeBodyEmpty(t *testing.T) {
	r := EncodeBody(nil, "text/plain")
	if r["body_size"] != 0 {
		t.Fatalf("body_size = %v", r["body_size"])
	}
	if len(r) != 1 {
		t.Fatalf("empty body should yield only body_size, got %v", r)
	}
}

func TestEncodeBodyText(t *testing.T) {
	body := []byte("hello")
	r := EncodeBody(body, "text/plain; charset=utf-8")
	if r["body_size"] != len(body) {
		t.Fatalf("body_size = %v", r["body_size"])
	}
	want := base64.StdEncoding.EncodeToString(body)
	if r["body_b64"] != want {
		t.Fatalf("body_b64 = %v, want %v", r["body_b64"], want)
	}
}

func TestEncodeBodyBinary(t *testing.T) {
	for _, ct := range []string{"image/png", "application/octet-stream", "font/woff2", "APPLICATION/PDF"} {
		r := EncodeBody([]byte{0x1, 0x2}, ct)
		if r["body_binary"] != true {
			t.Fatalf("%s should be binary, got %v", ct, r)
		}
		if _, ok := r["body_b64"]; ok {
			t.Fatalf("binary body must not include body_b64")
		}
	}
}

func TestEncodeBodyTruncated(t *testing.T) {
	big := bytes.Repeat([]byte{'a'}, bodyMaxBytes+1)
	r := EncodeBody(big, "text/plain")
	if r["body_truncated"] != true {
		t.Fatalf("oversized body should be truncated, got %v", r)
	}
	if _, ok := r["body_b64"]; ok {
		t.Fatal("truncated body must not include body_b64")
	}
}

func TestFormatLogLineRequest(t *testing.T) {
	got := FormatLogLine("GET", "/api", -1, -1, 12)
	if got != "→ GET /api (12B)" {
		t.Fatalf("request log = %q", got)
	}
}

func TestFormatLogLineResponse(t *testing.T) {
	got := FormatLogLine("GET", "/api", 200, 42.0, 2048)
	if got != "← 200 GET /api (42ms, 2.0KB)" {
		t.Fatalf("response log = %q", got)
	}
}

func TestFormatLogLineServerErrorWarn(t *testing.T) {
	got := FormatLogLine("POST", "/x", 500, -1, 3*1024*1024)
	if got != "← 500 POST /x (?, 3.0MB) ⚠" {
		t.Fatalf("500 log = %q", got)
	}
}

func TestHumanSize(t *testing.T) {
	cases := map[int]string{0: "0B", 512: "512B", 1024: "1.0KB", 1536: "1.5KB", 1024 * 1024: "1.0MB"}
	for n, want := range cases {
		if got := humanSize(n); got != want {
			t.Fatalf("humanSize(%d) = %q, want %q", n, got, want)
		}
	}
}
