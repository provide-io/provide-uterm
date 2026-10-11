//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/json"
	"errors"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	jwt "github.com/golang-jwt/jwt/v5"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// truncatedBodyServer declares a Content-Length it never fulfils, so the
// client's body read fails with an unexpected EOF.
func truncatedBodyServer(t *testing.T) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Length", "100")
		_, _ = w.Write([]byte("{"))
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestResolvedTokenPathWithoutHome(t *testing.T) {
	t.Setenv("UTERM_DEV_TOKEN_PATH", "")
	t.Setenv("HOME", "")
	if got := resolvedTokenPath(""); got != defaultDevTokenRelPath {
		t.Fatalf("resolvedTokenPath = %q, want relative default %q", got, defaultDevTokenRelPath)
	}
}

func TestSetupDevIDPSignError(t *testing.T) {
	sentinel := errors.New("sign failed")
	orig := signDevToken
	signDevToken = func(jwt.MapClaims, []byte) (string, error) { return "", sentinel }
	t.Cleanup(func() { signDevToken = orig })
	tokPath := filepath.Join(t.TempDir(), "tok")
	auth := serverconfig.DefaultServerConfig().Auth
	tok, err := SetupDevIDP(&auth, DevIDPOptions{TokenPath: tokPath})
	if !errors.Is(err, sentinel) || tok != "" {
		t.Fatalf("SetupDevIDP = %q, %v; want sentinel", tok, err)
	}
	if _, statErr := os.Stat(tokPath); !os.IsNotExist(statErr) {
		t.Fatalf("token file written despite sign failure: %v", statErr)
	}
}

func TestSetupDevIDPMkdirError(t *testing.T) {
	dir := t.TempDir()
	blocker := filepath.Join(dir, "file")
	if err := os.WriteFile(blocker, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	auth := serverconfig.DefaultServerConfig().Auth
	if tok, err := SetupDevIDP(&auth, DevIDPOptions{TokenPath: filepath.Join(blocker, "sub", "tok")}); err == nil || tok != "" {
		t.Fatalf("SetupDevIDP = %q, %v; want MkdirAll error", tok, err)
	}
}

func TestSetupDevIDPWriteError(t *testing.T) {
	// The token path is an existing directory, so WriteFile fails.
	dir := t.TempDir()
	auth := serverconfig.DefaultServerConfig().Auth
	if tok, err := SetupDevIDP(&auth, DevIDPOptions{TokenPath: dir}); err == nil || tok != "" {
		t.Fatalf("SetupDevIDP = %q, %v; want write error", tok, err)
	}
}

func TestDecodeEmbeddedIPv4MalformedLength(t *testing.T) {
	// A non-nil IP of neither 4 nor 16 bytes has no IPv4 or IPv6 form.
	if got := decodeEmbeddedIPv4(net.IP{1, 2, 3}); got != nil {
		t.Fatalf("decodeEmbeddedIPv4(3 bytes) = %v, want nil", got)
	}
}

func jwksServer(t *testing.T, key *rsa.PublicKey) *httptest.Server {
	t.Helper()
	n := base64.RawURLEncoding.EncodeToString(key.N.Bytes())
	e := base64.RawURLEncoding.EncodeToString(big.NewInt(int64(key.E)).Bytes())
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"keys": []any{
			map[string]any{"kty": "RSA", "kid": "k1", "n": n, "e": e},
		}})
	}))
	t.Cleanup(srv.Close)
	return srv
}

func TestResolveJWKSKeyEvictsWhenCacheFull(t *testing.T) {
	resetJWKSCache()
	t.Cleanup(resetJWKSCache)
	origMax := jwksCacheMax
	jwksCacheMax = 1
	t.Cleanup(func() { jwksCacheMax = origMax })
	jwksCacheMu.Lock()
	jwksCache["https://stale.example/jwks"] = map[string]any{"old": "key"}
	jwksCacheMu.Unlock()

	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	srv := jwksServer(t, &key.PublicKey)
	got, err := resolveJWKSKey(srv.URL, &jwt.Token{Header: map[string]any{"kid": "k1"}})
	if err != nil {
		t.Fatalf("resolveJWKSKey: %v", err)
	}
	if pub, ok := got.(*rsa.PublicKey); !ok || pub.N.Cmp(key.N) != 0 {
		t.Fatalf("resolved key mismatch: %T", got)
	}
	jwksCacheMu.Lock()
	defer jwksCacheMu.Unlock()
	if len(jwksCache) != 1 || jwksCache["https://stale.example/jwks"] != nil || jwksCache[srv.URL] == nil {
		t.Fatalf("cache after eviction = %v", jwksCache)
	}
}

func TestFetchJWKSInvalidURL(t *testing.T) {
	if _, err := fetchJWKS("http://example.com/\x7f"); err == nil {
		t.Fatal("expected request-construction error")
	}
}

func TestFetchJWKSTransportError(t *testing.T) {
	srv := httptest.NewServer(http.NotFoundHandler())
	url := srv.URL
	srv.Close() // nothing listens any more
	if _, err := fetchJWKS(url); err == nil {
		t.Fatal("expected transport error against closed server")
	}
}

func TestFetchJWKSTruncatedBody(t *testing.T) {
	srv := truncatedBodyServer(t)
	if _, err := fetchJWKS(srv.URL); err == nil || !strings.Contains(err.Error(), "EOF") {
		t.Fatalf("fetchJWKS err = %v; want unexpected EOF", err)
	}
}

func TestFetchJWKSBadRSAKey(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte(`{"keys":[{"kty":"RSA","kid":"a","n":"!!!","e":"AQAB"}]}`))
	}))
	t.Cleanup(srv.Close)
	if keys, err := fetchJWKS(srv.URL); err == nil || keys != nil {
		t.Fatalf("fetchJWKS = %v, %v; want decode error", keys, err)
	}
}

func TestJWTKeyFuncUnknownAlgFamilyReturnsPEM(t *testing.T) {
	pem := "raw-key-material"
	p := &LocalIdentityProvider{Auth: &serverconfig.AuthConfig{JWTPublicKeyPEM: &pem}}
	got, err := p.jwtKeyFunc(&jwt.Token{Method: jwt.SigningMethodEdDSA})
	if err != nil {
		t.Fatal(err)
	}
	if b, ok := got.([]byte); !ok || string(b) != pem {
		t.Fatalf("jwtKeyFunc = %#v, want raw PEM bytes", got)
	}
}

func TestSecurityOverrideUnknownField(t *testing.T) {
	v := "x"
	cfg := &serverconfig.SecurityConfig{CSP: &v}
	if got := securityOverride(cfg, "not_a_header"); got != nil {
		t.Fatalf("securityOverride(unknown) = %v, want nil", *got)
	}
}

func TestWebhookAuthzTruncatedResponseDenies(t *testing.T) {
	srv := truncatedBodyServer(t)
	p := NewWebhookAuthorizationProvider(srv.URL, "", 2)
	if p.CanCreateSession(&Principal{SubjectID: "alice", Roles: NewSet(), Scopes: NewSet()}) {
		t.Fatal("truncated authz response must deny")
	}
}

func TestWebhookAuthzCapabilitiesNilPrincipal(t *testing.T) {
	p := NewWebhookAuthorizationProvider("http://127.0.0.1:1", "", 2)
	if got := p.CapabilitiesFor(nil); len(got) != 0 {
		t.Fatalf("CapabilitiesFor(nil) = %v, want empty", got)
	}
}

func TestWebhookIDPInvalidURLFailsClosed(t *testing.T) {
	var audited []map[string]any
	idp := newIDP(t, "http://example.com/\x7f", WebhookIDPOptions{
		AuditHook: func(_ string, d map[string]any) { audited = append(audited, d) },
	}, 1e6)
	p, err := idp.Authenticate(context.Background(), &Request{})
	if err != nil || p != nil {
		t.Fatalf("Authenticate = %+v, %v; want fail-closed nil", p, err)
	}
	if len(audited) != 1 || !strings.Contains(audited[0]["error"].(string), "control character") {
		t.Fatalf("audit = %v; want request-construction error", audited)
	}
}

func TestWebhookIDPTruncatedResponseFailsClosed(t *testing.T) {
	srv := truncatedBodyServer(t)
	var audited []map[string]any
	idp := newIDP(t, srv.URL, WebhookIDPOptions{
		AuditHook: func(_ string, d map[string]any) { audited = append(audited, d) },
	}, 1e6)
	p, err := idp.Authenticate(context.Background(), &Request{})
	if err != nil || p != nil {
		t.Fatalf("Authenticate = %+v, %v; want fail-closed nil", p, err)
	}
	if len(audited) != 1 || !strings.Contains(audited[0]["error"].(string), "EOF") {
		t.Fatalf("audit = %v; want body read error", audited)
	}
}

func TestWebhookIDPMalformedResponseFailsClosed(t *testing.T) {
	falseVal := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write([]byte("{not json"))
	}))
	t.Cleanup(srv.Close)
	var audited []map[string]any
	idp := newIDP(t, srv.URL, WebhookIDPOptions{
		RequireSignedResponse: &falseVal,
		AuditHook:             func(_ string, d map[string]any) { audited = append(audited, d) },
	}, 1e6)
	p, err := idp.Authenticate(context.Background(), &Request{})
	if err != nil || p != nil {
		t.Fatalf("Authenticate = %+v, %v; want fail-closed nil", p, err)
	}
	if len(audited) != 1 || !strings.Contains(audited[0]["error"].(string), "invalid character") {
		t.Fatalf("audit = %v; want JSON decode error", audited)
	}
}
