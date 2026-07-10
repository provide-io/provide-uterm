//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"math/big"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	jwt "github.com/golang-jwt/jwt/v5"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

func rsPublicPEM(t *testing.T, pub *rsa.PublicKey) string {
	t.Helper()
	der, err := x509.MarshalPKIXPublicKey(pub)
	if err != nil {
		t.Fatal(err)
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der}))
}

func ecPublicPEM(t *testing.T, pub *ecdsa.PublicKey) string {
	t.Helper()
	der, err := x509.MarshalPKIXPublicKey(pub)
	if err != nil {
		t.Fatal(err)
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "PUBLIC KEY", Bytes: der}))
}

func signRS256(t *testing.T, key *rsa.PrivateKey, kid string) string {
	t.Helper()
	now := time.Now().Unix()
	tok := jwt.NewWithClaims(jwt.SigningMethodRS256, jwt.MapClaims{
		"sub": "rsa-user", "roles": []string{"admin"}, "iss": "provide-uterm",
		"aud": "provide-uterm-server", "iat": now, "exp": now + 600,
	})
	if kid != "" {
		tok.Header["kid"] = kid
	}
	s, err := tok.SignedString(key)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func TestJWTWithRSAPublicKeyPEM(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	pemStr := rsPublicPEM(t, &key.PublicKey)
	idp := NewLocalIdentityProvider(&serverconfig.AuthConfig{
		Mode: "jwt", JWTPublicKeyPEM: &pemStr, JWTAlgorithms: []string{"RS256"},
		JWTIssuer: "provide-uterm", JWTAudience: "provide-uterm-server", JWTRolesClaim: "roles", ClockSkewSeconds: 15,
	}, nil)
	p, err := idp.PrincipalFromJWTToken(signRS256(t, key, ""))
	if err != nil || p.SubjectID != "rsa-user" || !p.Roles.Has("admin") {
		t.Fatalf("RS256 PEM validation: %v %+v", err, p)
	}
}

func TestJWTWithECPublicKeyPEM(t *testing.T) {
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	pemStr := ecPublicPEM(t, &key.PublicKey)
	now := time.Now().Unix()
	tok := jwt.NewWithClaims(jwt.SigningMethodES256, jwt.MapClaims{
		"sub": "ec-user", "roles": []string{"operator"}, "iss": "provide-uterm",
		"aud": "provide-uterm-server", "iat": now, "exp": now + 600,
	})
	signed, err := tok.SignedString(key)
	if err != nil {
		t.Fatal(err)
	}
	idp := NewLocalIdentityProvider(&serverconfig.AuthConfig{
		Mode: "jwt", JWTPublicKeyPEM: &pemStr, JWTAlgorithms: []string{"ES256"},
		JWTIssuer: "provide-uterm", JWTAudience: "provide-uterm-server", JWTRolesClaim: "roles", ClockSkewSeconds: 15,
	}, nil)
	p, err := idp.PrincipalFromJWTToken(signed)
	if err != nil || p.SubjectID != "ec-user" || !p.Roles.Has("operator") {
		t.Fatalf("ES256 PEM validation: %v %+v", err, p)
	}
}

func TestJWTNoKeyConfigured(t *testing.T) {
	idp := NewLocalIdentityProvider(&serverconfig.AuthConfig{
		Mode: "jwt", JWTAlgorithms: []string{"HS256"}, JWTIssuer: "provide-uterm",
		JWTAudience: "provide-uterm-server", ClockSkewSeconds: 15,
	}, nil)
	if _, err := idp.PrincipalFromJWTToken(makeToken(t, "u", []string{"admin"}, 600)); err == nil {
		t.Error("validation succeeded with no key configured")
	}
}

func jwksHandler(t *testing.T, pub *rsa.PublicKey, kid string) http.Handler {
	t.Helper()
	n := base64.RawURLEncoding.EncodeToString(pub.N.Bytes())
	e := base64.RawURLEncoding.EncodeToString(big.NewInt(int64(pub.E)).Bytes())
	doc := map[string]any{"keys": []any{map[string]any{"kty": "RSA", "kid": kid, "n": n, "e": e}}}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(doc)
	})
}

func TestJWTViaJWKS(t *testing.T) {
	resetJWKSCache()
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	srv := httptest.NewServer(jwksHandler(t, &key.PublicKey, "kid-1"))
	defer srv.Close()

	url := srv.URL
	idp := NewLocalIdentityProvider(&serverconfig.AuthConfig{
		Mode: "jwt", JWTJWKSURL: &url, JWTAlgorithms: []string{"RS256"},
		JWTIssuer: "provide-uterm", JWTAudience: "provide-uterm-server", JWTRolesClaim: "roles", ClockSkewSeconds: 15,
	}, nil)

	// with matching kid
	p, err := idp.PrincipalFromJWTToken(signRS256(t, key, "kid-1"))
	if err != nil || p.SubjectID != "rsa-user" {
		t.Fatalf("JWKS validation: %v %+v", err, p)
	}
	// second call hits the cache (same key)
	if _, err := idp.PrincipalFromJWTToken(signRS256(t, key, "kid-1")); err != nil {
		t.Fatalf("cached JWKS validation: %v", err)
	}
	// unknown kid on a multi-key doc would fail; single-key doc accepts no-kid token
	resetJWKSCache()
	if _, err := idp.PrincipalFromJWTToken(signRS256(t, key, "")); err != nil {
		t.Fatalf("single-key no-kid JWKS: %v", err)
	}
}

func TestJWKSFetchErrors(t *testing.T) {
	resetJWKSCache()
	key, _ := rsa.GenerateKey(rand.Reader, 2048)

	// non-200
	bad := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(500) }))
	defer bad.Close()
	badURL := bad.URL
	idp := NewLocalIdentityProvider(&serverconfig.AuthConfig{
		Mode: "jwt", JWTJWKSURL: &badURL, JWTAlgorithms: []string{"RS256"},
		JWTIssuer: "provide-uterm", JWTAudience: "provide-uterm-server", ClockSkewSeconds: 15,
	}, nil)
	if _, err := idp.PrincipalFromJWTToken(signRS256(t, key, "k")); err == nil {
		t.Error("non-200 JWKS accepted")
	}

	// no RSA keys
	resetJWKSCache()
	empty := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"keys": []any{map[string]any{"kty": "oct", "kid": "x"}}})
	}))
	defer empty.Close()
	emptyURL := empty.URL
	idp2 := NewLocalIdentityProvider(&serverconfig.AuthConfig{
		Mode: "jwt", JWTJWKSURL: &emptyURL, JWTAlgorithms: []string{"RS256"},
		JWTIssuer: "provide-uterm", JWTAudience: "provide-uterm-server", ClockSkewSeconds: 15,
	}, nil)
	if _, err := idp2.PrincipalFromJWTToken(signRS256(t, key, "k")); err == nil {
		t.Error("no-RSA-key JWKS accepted")
	}
}

func TestRSAFromJWK(t *testing.T) {
	key, _ := rsa.GenerateKey(rand.Reader, 2048)
	n := base64.RawURLEncoding.EncodeToString(key.N.Bytes())
	e := base64.RawURLEncoding.EncodeToString(big.NewInt(int64(key.E)).Bytes())
	pub, err := rsaFromJWK(jwkKey{Kty: "RSA", N: n, E: e})
	if err != nil || pub.E != key.E || pub.N.Cmp(key.N) != 0 {
		t.Fatalf("rsaFromJWK round-trip: %v", err)
	}
	if _, err := rsaFromJWK(jwkKey{N: "!!!", E: e}); err == nil {
		t.Error("bad n base64 accepted")
	}
	if _, err := rsaFromJWK(jwkKey{N: n, E: "!!!"}); err == nil {
		t.Error("bad e base64 accepted")
	}
}
