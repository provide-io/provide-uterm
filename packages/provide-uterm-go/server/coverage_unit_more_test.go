//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package server

import (
	"context"
	"crypto/tls"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverauth"
	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// errWriter is an http.ResponseWriter whose Write fails after failAfter
// successful writes, exercising the SSE write-failure branches without a live
// socket.
type errWriter struct {
	header    http.Header
	failAfter int
	writes    int
	flushed   bool
}

func (e *errWriter) Header() http.Header {
	if e.header == nil {
		e.header = http.Header{}
	}
	return e.header
}

func (e *errWriter) Write(b []byte) (int, error) {
	e.writes++
	if e.writes > e.failAfter {
		return 0, errors.New("broken pipe")
	}
	return len(b), nil
}

func (e *errWriter) WriteHeader(int) {}
func (e *errWriter) Flush()          { e.flushed = true }

func TestSSEWriteFailureBranches(t *testing.T) {
	// Success path (flushes).
	ok := &errWriter{failAfter: 3}
	if !sseWrite(ok, ok, true, []byte("x")) || !ok.flushed {
		t.Fatal("sseWrite success path")
	}
	// Fail on the "data: " prefix write.
	if sseWrite(&errWriter{failAfter: 0}, nil, false, []byte("x")) {
		t.Fatal("expected failure on prefix write")
	}
	// Fail on the payload write.
	if sseWrite(&errWriter{failAfter: 1}, nil, false, []byte("x")) {
		t.Fatal("expected failure on payload write")
	}
	// Fail on the trailing "\n\n" write.
	if sseWrite(&errWriter{failAfter: 2}, nil, false, []byte("x")) {
		t.Fatal("expected failure on terminator write")
	}
}

func TestSourceIPEdges(t *testing.T) {
	r := httptest.NewRequest("GET", "/", http.NoBody)
	r.RemoteAddr = "1.2.3.4:9999"
	if got := sourceIP(r); got != "1.2.3.4" {
		t.Fatalf("sourceIP host: %q", got)
	}
	r.RemoteAddr = ""
	if got := sourceIP(r); got != "unknown" {
		t.Fatalf("sourceIP empty: %q", got)
	}
	// Host that reduces to "" after stripping the port → "unknown".
	r.RemoteAddr = ":8080"
	if got := sourceIP(r); got != "unknown" {
		t.Fatalf("sourceIP portonly: %q", got)
	}
}

func TestDecodeJSONBodyEdges(t *testing.T) {
	// nil body → empty map, ok.
	r := &http.Request{}
	if m, ok := decodeJSONBody(r); !ok || len(m) != 0 {
		t.Fatalf("nil body: %v %v", m, ok)
	}
	// JSON null literal → normalized to empty map.
	rn := httptest.NewRequest("POST", "/", strings.NewReader("null"))
	if m, ok := decodeJSONBody(rn); !ok || m == nil {
		t.Fatalf("null body: %v %v", m, ok)
	}
	// Malformed JSON → ok=false.
	rb := httptest.NewRequest("POST", "/", strings.NewReader("{not json"))
	if _, ok := decodeJSONBody(rb); ok {
		t.Fatal("malformed body should be !ok")
	}
}

func TestStatusRecorderWriteWithoutHeader(t *testing.T) {
	rec := &statusRecorder{ResponseWriter: httptest.NewRecorder(), status: http.StatusOK}
	if _, err := rec.Write([]byte("hi")); err != nil {
		t.Fatalf("write: %v", err)
	}
	if !rec.written || rec.status != http.StatusOK {
		t.Fatalf("status recorder state: %+v", rec)
	}
}

func TestOriginAllowedBranches(t *testing.T) {
	// Wildcard short-circuit.
	wild := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
		cfg.Server.AllowedOrigins = []string{"*"}
	})
	if !wild.srv.originAllowed(httptest.NewRequest("GET", "/", http.NoBody)) {
		t.Fatal("wildcard should allow")
	}

	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
		cfg.Server.AllowedOrigins = []string{"https://app.example"}
	})
	// No Origin header → allowed (non-browser client).
	if !ts.srv.originAllowed(httptest.NewRequest("GET", "/", http.NoBody)) {
		t.Fatal("empty origin should allow")
	}
	// Same-origin over TLS (https scheme branch).
	rt := httptest.NewRequest("GET", "/", http.NoBody)
	rt.Host = "secure.example"
	rt.TLS = &tls.ConnectionState{}
	rt.Header.Set("Origin", "https://secure.example")
	if !ts.srv.originAllowed(rt) {
		t.Fatal("same-origin https should allow")
	}
	// Unlisted cross-origin → denied.
	rd := httptest.NewRequest("GET", "/", http.NoBody)
	rd.Host = "app.example"
	rd.Header.Set("Origin", "https://evil.example")
	if ts.srv.originAllowed(rd) {
		t.Fatal("unlisted origin should be denied")
	}
}

func TestApplyCORSBranches(t *testing.T) {
	ts := newTestServer(t, func(cfg *serverconfig.UtermServerConfig, _ *Deps) {
		cfg.Server.AllowedOrigins = []string{"https://app.example"}
	})
	// Empty origin → no CORS headers.
	rec := httptest.NewRecorder()
	ts.srv.applyCORS(rec, httptest.NewRequest("GET", "/", http.NoBody))
	if rec.Header().Get("Access-Control-Allow-Origin") != "" {
		t.Fatal("empty origin should not set CORS")
	}
	// Unlisted origin (no wildcard) → no CORS headers.
	rec2 := httptest.NewRecorder()
	r2 := httptest.NewRequest("GET", "/", http.NoBody)
	r2.Header.Set("Origin", "https://other.example")
	ts.srv.applyCORS(rec2, r2)
	if rec2.Header().Get("Access-Control-Allow-Origin") != "" {
		t.Fatal("unlisted origin should not set CORS")
	}
}

func TestDeckPrincipalForAndUtermPrincipal(t *testing.T) {
	// nil principal → nil.
	if deckPrincipalFor(&browserConn{}) != nil {
		t.Fatal("nil principal should map to nil deck principal")
	}
	// anonymous subject → nil.
	anon := &browserConn{principal: &serverauth.Principal{SubjectID: "anonymous"}}
	if deckPrincipalFor(anon) != nil {
		t.Fatal("anonymous should map to nil deck principal")
	}
	// Non-anonymous with display name → carries display.
	name := "Ada"
	bc := &browserConn{principal: &serverauth.Principal{SubjectID: "ada", DisplayName: &name}}
	dp, ok := deckPrincipalFor(bc).(deckPrincipalT)
	if !ok || dp.subject != "ada" || dp.display != "Ada" {
		t.Fatalf("deck principal: %#v", deckPrincipalFor(bc))
	}
	// UtermPrincipal: nil principal → nil.
	if (&browserConn{}).UtermPrincipal() != nil {
		t.Fatal("UtermPrincipal nil principal should be nil")
	}
}

func TestPostureCallerPrivilegedNil(t *testing.T) {
	ts := newTestServer(t, nil)
	if ts.srv.postureCallerPrivileged(nil) {
		t.Fatal("nil principal is never privileged")
	}
}

func TestResolverPrincipalNil(t *testing.T) {
	// A request with no principal in context → nil hub principal.
	if resolverPrincipal(httptest.NewRequest("GET", "/", http.NoBody)) != nil {
		t.Fatal("resolverPrincipal without a principal should be nil")
	}
}

// erroringAuth always fails authentication, exercising resolvePrincipal's
// error → anonymous normalization.
type erroringAuth struct{}

func (erroringAuth) Authenticate(context.Context, *serverauth.Request) (*serverauth.Principal, error) {
	return nil, errors.New("auth backend down")
}

func TestResolvePrincipalErrorNormalizesAnonymous(t *testing.T) {
	ts := newTestServer(t, func(_ *serverconfig.UtermServerConfig, deps *Deps) {
		deps.Auth = erroringAuth{}
	})
	p := ts.srv.resolvePrincipal(httptest.NewRequest("GET", "/", http.NoBody))
	if !isAnonymous(p) {
		t.Fatalf("auth error should normalize to anonymous, got %+v", p)
	}
}

func TestAuthRequestCarriesCookies(t *testing.T) {
	r := httptest.NewRequest("GET", "/", http.NoBody)
	r.AddCookie(&http.Cookie{Name: "sid", Value: "abc"})
	r.Header.Set("X-Custom", "v")
	got := authRequest(r)
	if got.Cookies["sid"] != "abc" {
		t.Fatalf("cookie not carried: %v", got.Cookies)
	}
}

func TestMustCIDRPanics(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("mustCIDR should panic on a bad CIDR")
		}
	}()
	_ = mustCIDR("not-a-cidr")
}

func TestDecodeEmbeddedIPv4Forms(t *testing.T) {
	if decodeEmbeddedIPv4(nil) != nil {
		t.Fatal("nil ip")
	}
	// 6to4 2002::/16 carries 1.2.3.4.
	if got := decodeEmbeddedIPv4(net.ParseIP("2002:0102:0304::1")); got == nil || got.String() != "1.2.3.4" {
		t.Fatalf("6to4: %v", got)
	}
	// NAT64 well-known 64:ff9b::a.b.c.d.
	if got := decodeEmbeddedIPv4(net.ParseIP("64:ff9b::0808:0808")); got == nil || got.String() != "8.8.8.8" {
		t.Fatalf("nat64: %v", got)
	}
	// IPv4-compatible ::5.6.7.8 (deprecated, non ::/::1).
	if got := decodeEmbeddedIPv4(net.ParseIP("::0506:0708")); got == nil || got.String() != "5.6.7.8" {
		t.Fatalf("v4-compat: %v", got)
	}
	// :: and ::1 fall through to nil (handled by normal v6 branches).
	if decodeEmbeddedIPv4(net.ParseIP("::1")) != nil {
		t.Fatal("::1 should not decode to an embedded v4")
	}
	// A plain global v6 with no embedding → nil.
	if decodeEmbeddedIPv4(net.ParseIP("2606:4700::1111")) != nil {
		t.Fatal("global v6 should not decode")
	}
}
