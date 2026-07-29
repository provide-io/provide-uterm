//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"strings"
	"testing"
)

// The reference (FastAPI) server answers every refusal with {"detail": ...}.
// The two cases that used to fall through to net/http's own defaults — a path
// in no route table and a known path with the wrong method — must use the same
// envelope, status, and wording. Pinned by conformance/live scenario
// 003_error_shapes.
func TestRouteFallbackUsesDetailEnvelope(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.srv.MarkReady()

	cases := []struct {
		name   string
		method string
		target string
		status int
		detail string
	}{
		{"unrouted path", "GET", "/api/not-a-thing", http.StatusNotFound, "Not Found"},
		{"unrouted root path", "GET", "/definitely-not-a-route", http.StatusNotFound, "Not Found"},
		{"wrong method", "POST", "/api/health", http.StatusMethodNotAllowed, "Method Not Allowed"},
		{"wrong method on healthz", "DELETE", "/healthz", http.StatusMethodNotAllowed, "Method Not Allowed"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := ts.do(tc.method, tc.target, "", adminHeaders())
			if rec.Code != tc.status {
				t.Fatalf("%s %s: status = %d, want %d (body %q)",
					tc.method, tc.target, rec.Code, tc.status, rec.Body.String())
			}
			if ct := rec.Header().Get("Content-Type"); ct != "application/json" {
				t.Fatalf("%s %s: Content-Type = %q, want application/json", tc.method, tc.target, ct)
			}
			body := decode(t, rec.Body.Bytes())
			if body["detail"] != tc.detail {
				t.Fatalf("%s %s: detail = %v, want %q", tc.method, tc.target, body["detail"], tc.detail)
			}
			if len(body) != 1 {
				t.Fatalf("%s %s: body has extra keys: %v", tc.method, tc.target, body)
			}
		})
	}
}

// A 405 keeps net/http's computed Allow header — the point of the status is to
// tell a client which verb to use instead.
func TestRouteFallbackKeepsAllowHeader(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.srv.MarkReady()

	rec := ts.do("POST", "/api/health", "", adminHeaders())
	allow := rec.Header().Get("Allow")
	if !strings.Contains(allow, "GET") {
		t.Fatalf("Allow = %q, want it to list GET", allow)
	}
}

// The fallback must not shadow real routes: a registered path still reaches its
// handler, and a handler's own 404 keeps its own message.
func TestRouteFallbackLeavesRoutedRequestsAlone(t *testing.T) {
	ts := newTestServer(t, nil)
	ts.srv.MarkReady()

	if rec := ts.do("GET", "/api/health", "", nil); rec.Code != http.StatusOK {
		t.Fatalf("routed health: %d %s", rec.Code, rec.Body.String())
	}
	rec := ts.do("GET", "/api/sessions/no-such-session", "", adminHeaders())
	if rec.Code != http.StatusNotFound {
		t.Fatalf("unknown session: %d", rec.Code)
	}
	if got := decode(t, rec.Body.Bytes())["detail"]; got != "unknown session: no-such-session" {
		t.Fatalf("unknown session detail = %v", got)
	}
}
