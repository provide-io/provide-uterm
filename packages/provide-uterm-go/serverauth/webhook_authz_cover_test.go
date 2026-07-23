//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// allowServer returns an httptest server that echoes a canned JSON body for
// every POST. No signature is emitted; use with an empty secret so the
// provider accepts unsigned responses.
func allowServer(t *testing.T, body map[string]any) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(body)
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestWebhookAuthzDecisionMethods(t *testing.T) {
	srv := allowServer(t, map[string]any{"allow": true})
	p := NewWebhookAuthorizationProvider(srv.URL, "", 2)
	prin := &Principal{
		SubjectID: "alice",
		Roles:     NewSet("admin"),
		Scopes:    NewSet("session.read"),
		Claims:    map[string]any{"k": "v"},
	}
	sess := &serverconfig.SessionDefinition{SessionID: "sess-1"}
	prof := &serverconfig.ConnectionProfile{ProfileID: "prof-1"}

	if !p.IsOwner(prin, sess) {
		t.Fatal("IsOwner allow")
	}
	if !p.CanReadSession(prin, sess) {
		t.Fatal("CanReadSession allow")
	}
	if !p.CanReadRecording(prin, sess) {
		t.Fatal("CanReadRecording allow")
	}
	if !p.CanCreateSession(prin) {
		t.Fatal("CanCreateSession allow")
	}
	if !p.CanMutateSession(prin, sess, "session.control.stop") {
		t.Fatal("CanMutateSession allow")
	}
	if !p.CanReadProfile(prin, prof) {
		t.Fatal("CanReadProfile allow")
	}
	if !p.CanMutateProfile(prin, prof) {
		t.Fatal("CanMutateProfile allow")
	}
}

// TestWebhookAuthzDecisionMethodsNilSession exercises the nil-session/profile
// branches (session_id / profile_id resolve to empty string).
func TestWebhookAuthzDecisionMethodsNilSession(t *testing.T) {
	srv := allowServer(t, map[string]any{"allow": false})
	p := NewWebhookAuthorizationProvider(srv.URL, "", 2)
	prin := &Principal{SubjectID: "bob", Roles: NewSet("viewer")}

	if p.IsOwner(prin, nil) {
		t.Fatal("IsOwner deny on nil session")
	}
	if p.CanReadSession(prin, nil) {
		t.Fatal("CanReadSession deny")
	}
	if p.CanReadRecording(prin, nil) {
		t.Fatal("CanReadRecording deny")
	}
	if p.CanMutateSession(prin, nil, "x") {
		t.Fatal("CanMutateSession deny")
	}
	if p.CanReadProfile(prin, nil) {
		t.Fatal("CanReadProfile deny")
	}
	if p.CanMutateProfile(prin, nil) {
		t.Fatal("CanMutateProfile deny")
	}
}

func TestWebhookAuthzCapabilitiesFor(t *testing.T) {
	srv := allowServer(t, map[string]any{"capabilities": []string{"a", "b"}})
	p := NewWebhookAuthorizationProvider(srv.URL, "", 2)

	// Nil principal -> empty set.
	if len(p.CapabilitiesFor(nil)) != 0 {
		t.Fatal("nil principal must yield empty capability set")
	}

	prin := &Principal{SubjectID: "alice"}
	caps := p.CapabilitiesFor(prin)
	if !caps.Has("a") || !caps.Has("b") {
		t.Fatalf("expected caps a,b got %v", caps.Sorted())
	}
}

// TestWebhookAuthzCapabilitiesForErrorPaths hits the non-200 / bad-status
// branch that returns an empty set.
func TestWebhookAuthzCapabilitiesForErrorPaths(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()
	p := NewWebhookAuthorizationProvider(srv.URL, "", 2)
	if len(p.CapabilitiesFor(&Principal{SubjectID: "z"})) != 0 {
		t.Fatal("500 status must yield empty set")
	}
}

func TestWebhookAuthzResolveBrowserRole(t *testing.T) {
	sess := &serverconfig.SessionDefinition{SessionID: "s1"}

	// Nil principal defaults to viewer.
	p0 := NewWebhookAuthorizationProvider("http://127.0.0.1:1", "", 2)
	if got := p0.ResolveBrowserRole(nil, sess); got != "viewer" {
		t.Fatalf("nil principal should default viewer, got %q", got)
	}

	// Recognized role returned as-is (lowercased).
	srv := allowServer(t, map[string]any{"role": "Operator"})
	p := NewWebhookAuthorizationProvider(srv.URL, "", 2)
	prin := &Principal{SubjectID: "alice", Roles: NewSet("x")}
	if got := p.ResolveBrowserRole(prin, sess); got != "operator" {
		t.Fatalf("expected operator, got %q", got)
	}

	// Unknown role falls back to viewer.
	srv2 := allowServer(t, map[string]any{"role": "wizard"})
	p2 := NewWebhookAuthorizationProvider(srv2.URL, "", 2)
	if got := p2.ResolveBrowserRole(prin, sess); got != "viewer" {
		t.Fatalf("unknown role should fall back to viewer, got %q", got)
	}

	// Nil session -> empty session id branch.
	srv3 := allowServer(t, map[string]any{"role": "admin"})
	p3 := NewWebhookAuthorizationProvider(srv3.URL, "", 2)
	if got := p3.ResolveBrowserRole(prin, nil); got != "admin" {
		t.Fatalf("expected admin, got %q", got)
	}
}

// TestWebhookAuthzResolveBrowserRoleError hits the error path (unreachable URL).
func TestWebhookAuthzResolveBrowserRoleError(t *testing.T) {
	p := NewWebhookAuthorizationProvider("http://127.0.0.1:1", "", 2)
	prin := &Principal{SubjectID: "alice"}
	if got := p.ResolveBrowserRole(prin, &serverconfig.SessionDefinition{SessionID: "s"}); got != "viewer" {
		t.Fatalf("transport error should yield viewer, got %q", got)
	}
}

func TestWebhookAuthzString(t *testing.T) {
	p := NewWebhookAuthorizationProvider("http://example/authz", "", 2)
	if got := p.String(); got != "WebhookAuthorizationProvider(http://example/authz)" {
		t.Fatalf("unexpected String(): %q", got)
	}
}

func TestNewAuthorizationServiceFromConfigWebhook(t *testing.T) {
	url := "http://example/authz"
	secret := "s3cr3t" // pragma: allowlist secret
	cfg := &serverconfig.UtermServerConfig{
		Governance: serverconfig.GovernanceConfig{
			AuthzWebhookURL:      &url,
			AuthzWebhookSecret:   &secret,
			AuthzWebhookTimeoutS: 3.0,
		},
	}
	svc := NewAuthorizationServiceFromConfig(cfg)
	if svc == nil {
		t.Fatal("nil service")
	}

	// Empty (whitespace) URL falls back to the local RBAC service.
	blank := "   "
	cfg2 := &serverconfig.UtermServerConfig{
		Governance: serverconfig.GovernanceConfig{AuthzWebhookURL: &blank},
	}
	if NewAuthorizationServiceFromConfig(cfg2) == nil {
		t.Fatal("blank url should still return a service")
	}
}

// unreachable is a URL guaranteed to fail the HTTP round-trip.
const unreachable = "http://127.0.0.1:1/authz"

func TestWebhookAuthzTransportErrorPaths(t *testing.T) {
	p := NewWebhookAuthorizationProvider(unreachable, "", 1)
	prin := &Principal{SubjectID: "a"}
	// check() -> client.Do error -> false
	if p.HasCapability(prin, "x") {
		t.Fatal("transport error should deny")
	}
	// CapabilitiesFor() -> client.Do error -> empty
	if len(p.CapabilitiesFor(prin)) != 0 {
		t.Fatal("transport error should yield empty caps")
	}
}

func TestWebhookAuthzAssertURLAllowedRejects(t *testing.T) {
	srv := allowServer(t, map[string]any{"allow": true, "capabilities": []string{"a"}, "role": "admin"})
	p := NewWebhookAuthorizationProvider(srv.URL, "", 2)
	p.AssertURLAllowed = func(ctx context.Context, rawURL string) error {
		return errUnroutable
	}
	prin := &Principal{SubjectID: "a"}
	if p.HasCapability(prin, "x") {
		t.Fatal("egress rejection must deny check()")
	}
	if len(p.CapabilitiesFor(prin)) != 0 {
		t.Fatal("egress rejection must empty CapabilitiesFor")
	}
	if got := p.ResolveBrowserRole(prin, nil); got != "viewer" {
		t.Fatalf("egress rejection must default role to viewer, got %q", got)
	}
}

func TestWebhookAuthzNilHTTPClientFallback(t *testing.T) {
	// A nil HTTPClient forces the http.DefaultClient fallback branch; the
	// unreachable URL then fails the round-trip.
	p := &WebhookAuthorizationProvider{URL: unreachable, TimeoutS: 1, Now: wallClock}
	prin := &Principal{SubjectID: "a"}
	if p.HasCapability(prin, "x") {
		t.Fatal("expected deny")
	}
	if len(p.CapabilitiesFor(prin)) != 0 {
		t.Fatal("expected empty caps")
	}
	if p.ResolveBrowserRole(prin, nil) != "viewer" {
		t.Fatal("expected viewer")
	}
}

func TestWebhookAuthzNon200AndBadJSON(t *testing.T) {
	// Non-200 -> deny.
	srv500 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusForbidden)
	}))
	defer srv500.Close()
	p500 := NewWebhookAuthorizationProvider(srv500.URL, "", 2)
	if p500.HasCapability(&Principal{SubjectID: "a"}, "x") {
		t.Fatal("non-200 must deny")
	}
	if p500.ResolveBrowserRole(&Principal{SubjectID: "a"}, nil) != "viewer" {
		t.Fatal("non-200 role must be viewer")
	}

	// 200 but malformed JSON body -> deny / empty / viewer.
	srvBad := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("not json"))
	}))
	defer srvBad.Close()
	pBad := NewWebhookAuthorizationProvider(srvBad.URL, "", 2)
	prin := &Principal{SubjectID: "a"}
	if pBad.HasCapability(prin, "x") {
		t.Fatal("bad json must deny check")
	}
	if len(pBad.CapabilitiesFor(prin)) != 0 {
		t.Fatal("bad json must empty caps")
	}
	if pBad.ResolveBrowserRole(prin, nil) != "viewer" {
		t.Fatal("bad json role must be viewer")
	}
}

func TestNewWebhookAuthorizationProviderTimeoutDefault(t *testing.T) {
	p := NewWebhookAuthorizationProvider("http://x", "", 0)
	if p.TimeoutS != 2.0 {
		t.Fatalf("non-positive timeout must default to 2.0, got %v", p.TimeoutS)
	}
	p2 := NewWebhookAuthorizationProvider("http://x", "", -5)
	if p2.TimeoutS != 2.0 {
		t.Fatalf("negative timeout must default to 2.0, got %v", p2.TimeoutS)
	}
}

func TestWebhookAuthzNilPrincipalAndMarshalError(t *testing.T) {
	p := NewWebhookAuthorizationProvider("http://example/authz", "", 2)
	// check() short-circuits on a nil principal.
	if p.HasCapability(nil, "x") {
		t.Fatal("nil principal must deny")
	}
	// A non-marshalable claim value makes json.Marshal fail inside check().
	bad := &Principal{SubjectID: "a", Claims: map[string]any{"ch": make(chan int)}}
	if p.HasCapability(bad, "x") {
		t.Fatal("marshal error must deny")
	}
}

func TestWebhookAuthzNewRequestError(t *testing.T) {
	// A URL with an invalid percent-escape fails http.NewRequestWithContext.
	p := NewWebhookAuthorizationProvider("http://%zz/authz", "", 2)
	prin := &Principal{SubjectID: "a"}
	if p.HasCapability(prin, "x") {
		t.Fatal("bad URL must deny check()")
	}
	if len(p.CapabilitiesFor(prin)) != 0 {
		t.Fatal("bad URL must empty CapabilitiesFor")
	}
	if p.ResolveBrowserRole(prin, nil) != "viewer" {
		t.Fatal("bad URL must default role to viewer")
	}
}

var errUnroutable = errUnroutableT("unroutable")

type errUnroutableT string

func (e errUnroutableT) Error() string { return string(e) }

func TestDerefStr(t *testing.T) {
	if derefStr(nil) != "" {
		t.Fatal("nil deref must be empty")
	}
	v := "hello"
	if derefStr(&v) != "hello" {
		t.Fatal("deref value")
	}
}
