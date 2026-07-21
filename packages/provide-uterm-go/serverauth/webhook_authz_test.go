//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
	"time"
)

func TestWebhookAuthzRejectsUnsignedWhenSecretSet(t *testing.T) {
	const secret = "authz-shared-secret-32-bytes!!" // pragma: allowlist secret
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"allow": true}) // no signature
	}))
	defer srv.Close()

	p := NewWebhookAuthorizationProvider(srv.URL, secret, 2)
	prin := &Principal{SubjectID: "alice", Roles: NewSet("admin")}
	if p.HasCapability(prin, "session.read") {
		t.Fatal("unsigned allow must fail closed when secret is set")
	}
}

func TestWebhookAuthzAcceptsSignedAllow(t *testing.T) {
	const secret = "authz-shared-secret-32-bytes!!" // pragma: allowlist secret
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body := []byte(`{"allow":true}`)
		ts := strconv.FormatFloat(float64(time.Now().Unix()), 'f', -1, 64)
		w.Header().Set("X-Uterm-Timestamp", ts)
		w.Header().Set("X-Uterm-Signature", BuildWebhookSignature(secret, body, ts))
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body)
	}))
	defer srv.Close()

	p := NewWebhookAuthorizationProvider(srv.URL, secret, 2)
	prin := &Principal{SubjectID: "alice", Roles: NewSet("admin")}
	if !p.HasCapability(prin, "session.read") {
		t.Fatal("signed allow must succeed")
	}
	if !p.IsAdmin(prin) {
		t.Fatal("signed admin check must succeed")
	}
}

func TestWebhookAuthzNoSecretAllowsUnsigned(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"allow": true})
	}))
	defer srv.Close()

	p := NewWebhookAuthorizationProvider(srv.URL, "", 2)
	if p.RequireSignedResponse {
		t.Fatal("empty secret must not require signed response")
	}
	prin := &Principal{SubjectID: "alice", Roles: NewSet("admin")}
	if !p.HasCapability(prin, "session.read") {
		t.Fatal("unsigned allow ok when no secret")
	}
}

func TestNewAuthorizationServiceFromConfigNilLocal(t *testing.T) {
	svc := NewAuthorizationServiceFromConfig(nil)
	if svc == nil {
		t.Fatal("nil")
	}
	p := &Principal{SubjectID: "a", Roles: NewSet("admin")}
	if !svc.IsAdmin(p) {
		t.Fatal("local admin")
	}
}
