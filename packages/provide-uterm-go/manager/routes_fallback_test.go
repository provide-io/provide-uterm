//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package manager

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// do issues a request against h and returns the recorder, so a test can assert
// on headers and the exact body bytes (doJSON discards both).
func do(t *testing.T, h http.Handler, method, target string) *httptest.ResponseRecorder {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(method, target, nil))
	return rec
}

// decodeMap unmarshals a response body, failing loudly on a non-JSON body —
// which is the whole point of these tests.
func decodeMap(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var m map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &m); err != nil {
		t.Fatalf("body is not a JSON object: %q (%v)", rec.Body.String(), err)
	}
	return m
}

// The manager carries two error envelopes, exactly like its FastAPI reference:
// routes raise JSONResponse({"error": ...}), while the two refusals the
// framework answers on its own — a path in no route table, and a known path
// with an unregistered method — are FastAPI's defaults, {"detail": ...}.
// Verified against the reference:
//
//	GET  /not-a-thing -> 404 {"detail":"Not Found"}
//	POST /health      -> 405 {"detail":"Method Not Allowed"}
//
// Before this, both fell through to net/http's plain text instead.
func TestRouteFallbackUsesDetailEnvelope(t *testing.T) {
	_, h := newTestServer(t, nil)

	cases := []struct {
		name   string
		method string
		target string
		status int
		detail string
	}{
		{"unrouted path", "GET", "/not-a-thing", http.StatusNotFound, "Not Found"},
		{"unrouted nested path", "GET", "/swarm/not-a-thing", http.StatusNotFound, "Not Found"},
		{"wrong method on health", "POST", "/health", http.StatusMethodNotAllowed, "Method Not Allowed"},
		{"wrong method on agents", "DELETE", "/agents", http.StatusMethodNotAllowed, "Method Not Allowed"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := do(t, h, tc.method, tc.target)
			if rec.Code != tc.status {
				t.Fatalf("%s %s: status = %d, want %d (body %q)",
					tc.method, tc.target, rec.Code, tc.status, rec.Body.String())
			}
			if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
				t.Fatalf("%s %s: Content-Type = %q, want application/json", tc.method, tc.target, ct)
			}
			body := decodeMap(t, rec)
			if body["detail"] != tc.detail {
				t.Fatalf("%s %s: detail = %v, want %q", tc.method, tc.target, body["detail"], tc.detail)
			}
			if len(body) != 1 {
				t.Fatalf("%s %s: body has extra keys: %v", tc.method, tc.target, body)
			}
		})
	}
}

// A 405 keeps net/http's computed Allow header.
func TestRouteFallbackKeepsAllowHeader(t *testing.T) {
	_, h := newTestServer(t, nil)
	if allow := do(t, h, "POST", "/health").Header().Get("Allow"); !strings.Contains(allow, "GET") {
		t.Fatalf("Allow = %q, want it to list GET", allow)
	}
}

// The fallback must not touch the manager's own {"error": ...} envelope, and
// must not swallow {wildcard} path values: the message naming "nope" proves
// agent_id still reached the handler.
func TestRouteFallbackLeavesManagerRefusalsAlone(t *testing.T) {
	_, h := newTestServer(t, nil)

	if rec := do(t, h, "GET", "/health"); rec.Code != http.StatusOK {
		t.Fatalf("routed health: %d %s", rec.Code, rec.Body.String())
	}
	rec := do(t, h, "GET", "/agent/nope/status")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("unknown agent: %d", rec.Code)
	}
	body := decodeMap(t, rec)
	msg, ok := body["error"].(string)
	if !ok {
		t.Fatalf("unknown agent lost the {\"error\": ...} envelope: %v", body)
	}
	if !strings.Contains(msg, "nope") {
		t.Fatalf("error = %q, want it to name the agent (path value lost?)", msg)
	}
	if _, leaked := body["detail"]; leaked {
		t.Fatalf("manager refusal must not carry a detail key: %v", body)
	}
}
