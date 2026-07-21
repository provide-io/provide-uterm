//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"net/http"
	"testing"
)

// TestCFAccessEmailHeaderDoesNotOverrideAuth documents C1 de-scope for Go:
// self-hosted servers authenticate via JWT/dev_token/etc. A client-supplied
// Cf-Access-Authenticated-User-Email header must not mint or override identity.
func TestCFAccessEmailHeaderDoesNotOverrideAuth(t *testing.T) {
	ts := newTestServer(t, nil)
	// harness fakeAuth uses X-Subject; spoofed Access email must be ignored.
	hdr := map[string]string{
		"X-Subject": "real-user",
		"X-Role":    "admin",
		"Cf-Access-Authenticated-User-Email": "spoofed@evil.example",
	}
	// Authenticated route that echoes principal — health is open; use sessions list.
	rec := ts.do("GET", "/api/sessions", "", hdr)
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d %s", rec.Code, rec.Body.String())
	}
	// Ensure spoof header alone is not enough (no X-Subject → anonymous denied on protected routes).
	spoofOnly := map[string]string{
		"Cf-Access-Authenticated-User-Email": "spoofed@evil.example",
	}
	rec2 := ts.do("GET", "/api/sessions", "", spoofOnly)
	// Anonymous may get 401 or empty depending on auth mode in harness.
	if rec2.Code == http.StatusOK {
		// Harness may allow anonymous listing; spoof must not become admin create.
		rec3 := ts.do("POST", "/api/sessions", `{"session_id":"x","connector_type":"shell"}`, spoofOnly)
		if rec3.Code == http.StatusOK || rec3.Code == http.StatusCreated {
			t.Fatalf("spoofed Access email must not grant create: %d", rec3.Code)
		}
	}
}
