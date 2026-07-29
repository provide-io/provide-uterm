//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package client

import (
	"testing"
)

// A body is either entirely one JSON value or it is not JSON at all. Streaming
// json.Decoder.Decode stops after the first value and ignores whatever follows,
// so net/http's plain-text "404 page not found" used to be reported as the
// number 404 — text silently becoming a number, with no way for a caller to
// tell a JSON body from a non-JSON one.
func TestDecodeBodyRejectsTrailingGarbage(t *testing.T) {
	cases := []struct {
		name string
		raw  string
	}{
		{"net/http plain-text 404", "404 page not found\n"},
		{"number then words", "405 Method Not Allowed"},
		{"object then junk", `{"detail":"nope"} and then some`},
		{"array then junk", "[1,2,3] trailing"},
		{"two values", "1 2"},
		{"string then junk", `"ok" nope`},
		{"empty body", ""},
		{"not json at all", "boom-text"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := decodeBody([]byte(tc.raw))
			m, ok := got.(map[string]any)
			if !ok {
				t.Fatalf("decodeBody(%q) = %#v (%T), want the {\"raw\": ...} fallback", tc.raw, got, got)
			}
			if m["raw"] != tc.raw {
				t.Fatalf("decodeBody(%q) raw = %#v", tc.raw, m["raw"])
			}
		})
	}
}

// Well-formed bodies still decode, with integers preserved as json.Number.
func TestDecodeBodyAcceptsWholeJSONValues(t *testing.T) {
	m, ok := decodeBody([]byte(`{"detail":"nope","n":7}`)).(map[string]any)
	if !ok {
		t.Fatalf("object body did not decode")
	}
	if m["detail"] != "nope" {
		t.Fatalf("detail = %#v", m["detail"])
	}
	if got := m["n"]; got == nil || got.(interface{ String() string }).String() != "7" {
		t.Fatalf("n = %#v, want json.Number 7", got)
	}
	// Surrounding whitespace is still one JSON value.
	if _, ok := decodeBody([]byte("  [1,2]\n")).([]any); !ok {
		t.Fatalf("whitespace-padded array did not decode")
	}
}

// The server's /api refusals carry {"detail": ...}; the bridge REST routes
// carry {"error": ...}. APIError.Message must be the human message in both
// cases, not a re-marshalled blob of the whole body.
func TestExtractErrorPrefersDetailThenError(t *testing.T) {
	cases := []struct {
		name string
		body any
		want string
	}{
		{"detail envelope", map[string]any{"detail": "unknown session: no-such-session"}, "unknown session: no-such-session"},
		{"error envelope", map[string]any{"error": "no worker connected"}, "no worker connected"},
		{"detail wins over error", map[string]any{"detail": "d", "error": "e"}, "d"},
		{"non-string detail falls back to error", map[string]any{"detail": []any{"x"}, "error": "e"}, "e"},
		{"raw fallback", map[string]any{"raw": "boom-text"}, `{"raw":"boom-text"}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := extractError(tc.body); got != tc.want {
				t.Fatalf("extractError(%#v) = %q, want %q", tc.body, got, tc.want)
			}
		})
	}
}

// End to end: a 404 from the server's documented envelope surfaces as the
// server's own wording on APIError.
func TestAPIErrorMessageIsServerDetail(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("GET", "/api/sessions/no-such-session",
		fakeResponse{status: 404, body: map[string]any{"detail": "unknown session: no-such-session"}})
	c := fs.client()
	_, err := c.GetSession(ctx(), "no-such-session")
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("not *APIError: %T (%v)", err, err)
	}
	if !apiErr.IsNotFound() {
		t.Fatalf("status = %d", apiErr.StatusCode)
	}
	if apiErr.Message != "unknown session: no-such-session" {
		t.Fatalf("Message = %q", apiErr.Message)
	}
}

// A server that answers a plain-text 404 must not be reported as though it
// parsed: the body is the raw-text fallback, never the number 404.
func TestPlainTextNotFoundIsNotANumber(t *testing.T) {
	fs := newFakeServer(t)
	fs.on("GET", "/api/sessions/x", fakeResponse{status: 404, raw: "404 page not found\n"})
	c := fs.client()
	_, err := c.GetSession(ctx(), "x")
	apiErr, ok := err.(*APIError)
	if !ok {
		t.Fatalf("not *APIError: %T", err)
	}
	body, ok := apiErr.Body.(map[string]any)
	if !ok {
		t.Fatalf("Body = %#v (%T), want the raw fallback", apiErr.Body, apiErr.Body)
	}
	if body["raw"] != "404 page not found\n" {
		t.Fatalf("Body raw = %#v", body["raw"])
	}
}
