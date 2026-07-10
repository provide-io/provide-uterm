//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strconv"
	"testing"
)

func readAll(r io.Reader) ([]byte, error) { return io.ReadAll(r) }

const webhookSecret = "uterm-test-secret-32-byte-minimum-key" // pragma: allowlist secret

func TestVerifyWebhookSignatureFailClosed(t *testing.T) {
	body := []byte(`{"a":1}`)
	ts := "1700000000.0"
	now := 1700000000.0
	sig := BuildWebhookSignature(webhookSecret, body, ts)

	// Valid.
	if !VerifyWebhookSignature(webhookSecret, body, sig, ts, DefaultMaxAgeS, &now) {
		t.Errorf("valid signature rejected")
	}
	// Empty secret fails closed even with a matching-shaped signature.
	if VerifyWebhookSignature("", body, sig, ts, DefaultMaxAgeS, &now) {
		t.Errorf("empty secret validated")
	}
	// Missing headers.
	if VerifyWebhookSignature(webhookSecret, body, "", ts, DefaultMaxAgeS, &now) {
		t.Errorf("empty signature validated")
	}
	if VerifyWebhookSignature(webhookSecret, body, sig, "", DefaultMaxAgeS, &now) {
		t.Errorf("empty timestamp validated")
	}
	// Stale timestamp.
	stale := now + 10000
	if VerifyWebhookSignature(webhookSecret, body, sig, ts, DefaultMaxAgeS, &stale) {
		t.Errorf("stale timestamp validated")
	}
	// Tampered body.
	if VerifyWebhookSignature(webhookSecret, []byte(`{"a":2}`), sig, ts, DefaultMaxAgeS, &now) {
		t.Errorf("tampered body validated")
	}
	// Bad timestamp format.
	if VerifyWebhookSignature(webhookSecret, body, sig, "not-a-number", DefaultMaxAgeS, &now) {
		t.Errorf("bad timestamp format validated")
	}
	// Signature without sha256= prefix still verifies.
	bare := sig[len("sha256="):]
	if !VerifyWebhookSignature(webhookSecret, body, bare, ts, DefaultMaxAgeS, &now) {
		t.Errorf("bare hex signature rejected")
	}
}

// signedResponse builds an IdP response body signed under webhookSecret,
// optionally echoing a nonce, at frozen time `now`.
func signedResponse(w http.ResponseWriter, now float64, nonce string, obj map[string]any) {
	data := map[string]any{"subject_id": "user-1", "roles": []any{"viewer"}}
	if obj != nil {
		data = obj
	}
	if nonce != "" {
		data["nonce"] = nonce
	}
	body, _ := json.Marshal(data)
	ts := strconv.FormatFloat(now, 'f', -1, 64)
	w.Header().Set("X-Uterm-Signature", BuildWebhookSignature(webhookSecret, body, ts))
	w.Header().Set("X-Uterm-Timestamp", ts)
	_, _ = w.Write(body)
}

func newIDP(t *testing.T, url string, opts WebhookIDPOptions, now float64) *WebhookIdentityProvider {
	t.Helper()
	idp, err := NewWebhookIdentityProvider(url, opts)
	if err != nil {
		t.Fatal(err)
	}
	idp.now = func() float64 { return now }
	return idp
}

func TestWebhookIDPSuccess(t *testing.T) {
	falseVal := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{
			"subject_id": "user-123", "roles": []any{"admin"},
			"claims": map[string]any{"email": "user@example.com"}, "display_name": "Test User",
		})
	}))
	defer srv.Close()

	idp := newIDP(t, srv.URL, WebhookIDPOptions{Secret: webhookSecret, RequireSignedResponse: &falseVal}, 1e6)
	p, err := idp.Authenticate(context.Background(), &Request{})
	if err != nil || p == nil {
		t.Fatalf("resolve: %v %+v", err, p)
	}
	if p.SubjectID != "user-123" || !p.Roles.Has("admin") || p.Claims["email"] != "user@example.com" || p.Name() != "Test User" {
		t.Errorf("principal wrong: %+v", p)
	}
}

func TestWebhookIDPFailClosed(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(500)
	}))
	defer srv.Close()

	// deny (default) → nil
	idp := newIDP(t, srv.URL, WebhookIDPOptions{}, 1e6)
	p, err := idp.Authenticate(context.Background(), &Request{})
	if err != nil || p != nil {
		t.Errorf("deny fail-closed: %v %+v", err, p)
	}

	// viewer → anonymous viewer
	idp2 := newIDP(t, srv.URL, WebhookIDPOptions{OnFailure: "viewer"}, 1e6)
	p2, err := idp2.Authenticate(context.Background(), &Request{})
	if err != nil || p2 == nil || p2.SubjectID != "anonymous" || !p2.Roles.Has("viewer") {
		t.Errorf("viewer fail-open: %v %+v", err, p2)
	}
}

func TestWebhookIDPSignedRequestHeaders(t *testing.T) {
	falseVal := false
	var gotSig, gotTS string
	var gotBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotSig = r.Header.Get("X-Uterm-Signature")
		gotTS = r.Header.Get("X-Uterm-Timestamp")
		gotBody, _ = readAll(r.Body)
		_ = json.NewEncoder(w).Encode(map[string]any{"subject_id": "u", "roles": []any{"viewer"}})
	}))
	defer srv.Close()

	idp := newIDP(t, srv.URL, WebhookIDPOptions{Secret: webhookSecret, RequireSignedResponse: &falseVal}, 1e6)
	if _, err := idp.Authenticate(context.Background(), &Request{}); err != nil {
		t.Fatal(err)
	}
	if r := requestHeader(gotSig); r == "" {
		t.Fatal("no X-Uterm-Signature on request")
	}
	// The request signature must verify against the sent body — proving Go
	// signs identically to what the Python IdP would verify.
	tsFloat, _ := strconv.ParseFloat(gotTS, 64)
	if !VerifyWebhookSignature(webhookSecret, gotBody, gotSig, gotTS, DefaultMaxAgeS, &tsFloat) {
		t.Errorf("request signature does not verify over its body")
	}
}

func TestWebhookIDPFiltersRoles(t *testing.T) {
	falseVal := false
	cases := []struct {
		roles []any
		want  []string
	}{
		{[]any{"admin", "superuser", "root"}, []string{"admin"}},
		{[]any{"nonsense"}, []string{"viewer"}},
		{[]any{"operator"}, []string{"operator"}},
		{[]any{"Admin"}, []string{"admin"}},
	}
	for _, tc := range cases {
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			_ = json.NewEncoder(w).Encode(map[string]any{"subject_id": "x", "roles": tc.roles})
		}))
		idp := newIDP(t, srv.URL, WebhookIDPOptions{RequireSignedResponse: &falseVal}, 1e6)
		p, err := idp.Authenticate(context.Background(), &Request{})
		srv.Close()
		if err != nil || p == nil {
			t.Fatalf("roles %v: %v %+v", tc.roles, err, p)
		}
		if len(p.Roles) != len(tc.want) {
			t.Errorf("roles %v → %v, want %v", tc.roles, p.Roles.Sorted(), tc.want)
		}
		for _, r := range tc.want {
			if !p.Roles.Has(r) {
				t.Errorf("roles %v missing %q → %v", tc.roles, r, p.Roles.Sorted())
			}
		}
	}
}

func TestWebhookIDPAuditHook(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(500) }))
	defer srv.Close()

	var captured []map[string]any
	idp := newIDP(t, srv.URL, WebhookIDPOptions{OnFailure: "deny", AuditHook: func(action string, detail map[string]any) {
		if action == "auth.webhook_idp_failure" {
			captured = append(captured, detail)
		}
	}}, 1e6)
	if _, err := idp.Authenticate(context.Background(), &Request{}); err != nil {
		t.Fatal(err)
	}
	if len(captured) != 1 {
		t.Fatalf("audit events = %d", len(captured))
	}
	d := captured[0]
	if d["url"] != srv.URL || d["on_failure"] != "deny" || d["error"] == nil {
		t.Errorf("audit detail wrong: %+v", d)
	}
	if _, hasSecret := d["secret"]; hasSecret {
		t.Errorf("secret leaked into audit detail")
	}
}

// --- L9 replay + nonce binding ---

func TestWebhookIDPReplayRejected(t *testing.T) {
	const frozen = 1_000_000.0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		signedResponse(w, frozen, "", nil)
	}))
	defer srv.Close()

	idp := newIDP(t, srv.URL, WebhookIDPOptions{Secret: webhookSecret}, frozen)
	first, err := idp.Authenticate(context.Background(), &Request{})
	if err != nil || first == nil || first.SubjectID != "user-1" {
		t.Fatalf("first delivery: %v %+v", err, first)
	}
	// Identical (signature, timestamp) replayed within the window → rejected.
	replay, err := idp.Authenticate(context.Background(), &Request{})
	if err != nil || replay != nil {
		t.Errorf("replay not rejected: %v %+v", err, replay)
	}
}

func TestWebhookIDPNonceBinding(t *testing.T) {
	const frozen = 1_000_000.0

	// Echoes the request nonce → accepted.
	echo := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		signedResponse(w, frozen, r.Header.Get("X-Uterm-Nonce"), nil)
	}))
	defer echo.Close()
	idp := newIDP(t, echo.URL, WebhookIDPOptions{Secret: webhookSecret}, frozen)
	if p, err := idp.Authenticate(context.Background(), &Request{}); err != nil || p == nil {
		t.Errorf("matching nonce echo rejected: %v %+v", err, p)
	}

	// Wrong echoed nonce → rejected even when not required.
	wrong := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		signedResponse(w, frozen, "not-the-sent-nonce", nil)
	}))
	defer wrong.Close()
	idp2 := newIDP(t, wrong.URL, WebhookIDPOptions{Secret: webhookSecret}, frozen)
	if p, err := idp2.Authenticate(context.Background(), &Request{}); err != nil || p != nil {
		t.Errorf("wrong nonce not rejected: %v %+v", err, p)
	}

	// require_response_nonce=true, missing echo → rejected.
	noEcho := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		signedResponse(w, frozen, "", nil)
	}))
	defer noEcho.Close()
	idp3 := newIDP(t, noEcho.URL, WebhookIDPOptions{Secret: webhookSecret, RequireResponseNonce: true}, frozen)
	if p, err := idp3.Authenticate(context.Background(), &Request{}); err != nil || p != nil {
		t.Errorf("required-but-missing nonce not rejected: %v %+v", err, p)
	}
}

func TestWebhookIDPResponseSignatureRequired(t *testing.T) {
	const frozen = 1_000_000.0
	// Unsigned response but signing required → rejected (deny → nil).
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"subject_id": "u", "roles": []any{"viewer"}})
	}))
	defer srv.Close()
	idp := newIDP(t, srv.URL, WebhookIDPOptions{Secret: webhookSecret}, frozen)
	if p, err := idp.Authenticate(context.Background(), &Request{}); err != nil || p != nil {
		t.Errorf("unsigned response accepted under require_signed_response: %v %+v", err, p)
	}
}

func TestWebhookIDPInvalidOnFailure(t *testing.T) {
	if _, err := NewWebhookIdentityProvider("https://x", WebhookIDPOptions{OnFailure: "bogus"}); err == nil {
		t.Error("invalid on_failure accepted")
	}
}

func TestWebhookIDPForwardFilters(t *testing.T) {
	falseVal := false
	var gotBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotBody, _ = readAll(r.Body)
		_ = json.NewEncoder(w).Encode(map[string]any{"subject_id": "u", "roles": []any{"viewer"}})
	}))
	defer srv.Close()

	idp := newIDP(t, srv.URL, WebhookIDPOptions{
		RequireSignedResponse: &falseVal,
		ForwardHeaders:        NewSet("authorization"),
		ForwardCookies:        NewSet("session"),
	}, 1e6)
	_, err := idp.Authenticate(context.Background(), &Request{
		Headers: map[string]string{"authorization": "Bearer tok", "x-secret": "leak"},
		Cookies: map[string]string{"session": "abc", "other": "leak"},
	})
	if err != nil {
		t.Fatal(err)
	}
	var payload webhookRequestPayload
	if err := json.Unmarshal(gotBody, &payload); err != nil {
		t.Fatal(err)
	}
	if payload.Headers["authorization"] != "Bearer tok" || payload.Headers["x-secret"] != "" {
		t.Errorf("header forward filter wrong: %+v", payload.Headers)
	}
	if payload.Cookies["session"] != "abc" || payload.Cookies["other"] != "" {
		t.Errorf("cookie forward filter wrong: %+v", payload.Cookies)
	}
	if payload.Nonce == "" || payload.Action != "resolve_principal" {
		t.Errorf("payload nonce/action wrong: %+v", payload)
	}
}

func requestHeader(v string) string { return v }
