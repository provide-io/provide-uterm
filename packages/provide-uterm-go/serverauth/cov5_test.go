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
	"math/big"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

func TestJWKSWrongKidRejected(t *testing.T) {
	resetJWKSCache()
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	n := base64.RawURLEncoding.EncodeToString(key.N.Bytes())
	e := base64.RawURLEncoding.EncodeToString(big.NewInt(int64(key.E)).Bytes())
	// two keys → no single-key fallback, so a wrong kid must fail.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"keys": []any{
			map[string]any{"kty": "RSA", "kid": "a", "n": n, "e": e},
			map[string]any{"kty": "RSA", "kid": "b", "n": n, "e": e},
		}})
	}))
	defer srv.Close()
	url := srv.URL
	idp := NewLocalIdentityProvider(&serverconfig.AuthConfig{
		Mode: "jwt", JWTJWKSURL: &url, JWTAlgorithms: []string{"RS256"},
		JWTIssuer: "provide-uterm", JWTAudience: "provide-uterm-server", ClockSkewSeconds: 15,
	}, nil)
	if _, err := idp.PrincipalFromJWTToken(signRS256(t, key, "unknown-kid")); err == nil {
		t.Error("unknown kid accepted against multi-key JWKS")
	}
}

func TestJWKSMalformedJSON(t *testing.T) {
	resetJWKSCache()
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("{not json"))
	}))
	defer srv.Close()
	url := srv.URL
	idp := NewLocalIdentityProvider(&serverconfig.AuthConfig{
		Mode: "jwt", JWTJWKSURL: &url, JWTAlgorithms: []string{"RS256"},
		JWTIssuer: "provide-uterm", JWTAudience: "provide-uterm-server", ClockSkewSeconds: 15,
	}, nil)
	if _, err := idp.PrincipalFromJWTToken(signRS256(t, key, "k")); err == nil {
		t.Error("malformed JWKS accepted")
	}
}

func TestWebhookPostConnectionError(t *testing.T) {
	// Point at a closed server → the POST fails → deny → nil.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	url := srv.URL
	srv.Close() // now refused
	idp := newIDP(t, url, WebhookIDPOptions{}, 1e6)
	if p, err := idp.Authenticate(context.Background(), &Request{}); err != nil || p != nil {
		t.Errorf("connection error not fail-closed: %v %+v", err, p)
	}
}

func TestWebhookResponseNoRolesDefaultsViewer(t *testing.T) {
	falseVal := false
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"subject_id": "u"}) // no roles
	}))
	defer srv.Close()
	idp := newIDP(t, srv.URL, WebhookIDPOptions{RequireSignedResponse: &falseVal}, 1e6)
	p, err := idp.Authenticate(context.Background(), &Request{})
	if err != nil || p == nil || !p.Roles.Has("viewer") {
		t.Errorf("no-roles default: %v %+v", err, p)
	}
}

func TestVerifyWebhookSignatureEmptyAfterPrefix(t *testing.T) {
	now := 1700000000.0
	if VerifyWebhookSignature(webhookSecret, []byte("x"), "sha256=", "1700000000.0", DefaultMaxAgeS, &now) {
		t.Error("empty-after-prefix signature validated")
	}
}
