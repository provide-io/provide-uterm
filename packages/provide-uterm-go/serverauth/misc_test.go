//
// SPDX-FileCopyrightText: Copyright (c) 2025-2026 provide.io llc. All rights reserved.
// SPDX-License-Identifier: AGPL-3.0-or-later
//

package serverauth

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	jwt "github.com/golang-jwt/jwt/v5"

	"github.com/provide-io/provide-uterm/packages/provide-uterm-go/serverconfig"
)

// --- replay cache ---

func TestReplayCacheAgesOut(t *testing.T) {
	c := newBoundedReplayCache(300.0, 128)
	if c.seenOrRecord("sig-A", 1000.0) {
		t.Errorf("first record reported replay")
	}
	if !c.seenOrRecord("sig-A", 1200.0) {
		t.Errorf("within-window repeat not reported as replay")
	}
	if c.seenOrRecord("sig-A", 1000.0+301.0) {
		t.Errorf("aged-out signature still blocked")
	}
}

func TestReplayCacheBounded(t *testing.T) {
	c := newBoundedReplayCache(300.0, 8)
	for i := 0; i < 100; i++ {
		c.seenOrRecord("sig-"+itoa(i), 1000.0+float64(i))
	}
	if c.len() > 8 {
		t.Errorf("cache size %d exceeds bound 8", c.len())
	}
}

func TestReplayCachePurgesStaleOnInsert(t *testing.T) {
	c := newBoundedReplayCache(10.0, 128)
	c.seenOrRecord("old", 1000.0)
	c.seenOrRecord("new", 1000.0+50.0)
	if c.contains("old") || !c.contains("new") {
		t.Errorf("stale purge wrong: old=%v new=%v", c.contains("old"), c.contains("new"))
	}
}

func itoa(i int) string {
	if i == 0 {
		return "0"
	}
	digits := ""
	for i > 0 {
		digits = string(rune('0'+i%10)) + digits
		i /= 10
	}
	return digits
}

// --- dev idp ---

func TestSetupDevIDP(t *testing.T) {
	dir := t.TempDir()
	tokPath := filepath.Join(dir, "tok")
	auth := serverconfig.DefaultServerConfig().Auth // mode dev_token

	token, err := SetupDevIDP(&auth, DevIDPOptions{TokenPath: tokPath})
	if err != nil {
		t.Fatal(err)
	}
	if auth.Mode != "jwt" || len(auth.JWTAlgorithms) != 1 || auth.JWTAlgorithms[0] != "HS256" {
		t.Errorf("auth not mutated to jwt/HS256: %+v", auth)
	}
	if auth.JWTPublicKeyPEM == nil || len(*auth.JWTPublicKeyPEM) < 32 {
		t.Errorf("secret too short")
	}
	if auth.WorkerBearerToken == nil || len(*auth.WorkerBearerToken) < 32 {
		t.Errorf("worker token too short")
	}

	// File written, 0600 on POSIX, content == token.
	got, err := os.ReadFile(tokPath)
	if err != nil || string(got) != token {
		t.Fatalf("token file: %v content-match=%v", err, string(got) == token)
	}
	if runtime.GOOS != "windows" {
		fi, _ := os.Stat(tokPath)
		if fi.Mode().Perm() != 0o600 {
			t.Errorf("token file perms = %o", fi.Mode().Perm())
		}
	}

	// The token validates via the mutated config (round-trip through the jwt path).
	idp := NewLocalIdentityProvider(&auth, nil)
	p, err := idp.PrincipalFromJWTToken(token)
	if err != nil || p.SubjectID != "dev-user" || !p.Roles.Has("admin") {
		t.Fatalf("dev token not valid via config: %v %+v", err, p)
	}

	// ReadDevToken round-trip.
	if rt, ok := ReadDevToken(tokPath); !ok || rt != token {
		t.Errorf("read dev token round-trip failed")
	}
	if _, ok := ReadDevToken(filepath.Join(dir, "absent")); ok {
		t.Errorf("read missing token returned ok")
	}
}

func TestSetupDevIDPCustomSubjectRoles(t *testing.T) {
	dir := t.TempDir()
	auth := serverconfig.DefaultServerConfig().Auth
	token, err := SetupDevIDP(&auth, DevIDPOptions{
		TokenPath: filepath.Join(dir, "t"), Subject: "alice", Roles: []string{"viewer", "operator"},
	})
	if err != nil {
		t.Fatal(err)
	}
	claims := jwt.MapClaims{}
	_, _, err = jwt.NewParser().ParseUnverified(token, claims)
	if err != nil {
		t.Fatal(err)
	}
	if claims["sub"] != "alice" {
		t.Errorf("sub = %v", claims["sub"])
	}
}

func TestDevTokenTTL(t *testing.T) {
	if DevTokenTTLS != 24*3600 {
		t.Errorf("DevTokenTTLS = %d", DevTokenTTLS)
	}
}

func TestSetupDevIDPFreshSecretEachCall(t *testing.T) {
	dir := t.TempDir()
	a1 := serverconfig.DefaultServerConfig().Auth
	a2 := serverconfig.DefaultServerConfig().Auth
	if _, err := SetupDevIDP(&a1, DevIDPOptions{TokenPath: filepath.Join(dir, "t1")}); err != nil {
		t.Fatal(err)
	}
	if _, err := SetupDevIDP(&a2, DevIDPOptions{TokenPath: filepath.Join(dir, "t2")}); err != nil {
		t.Fatal(err)
	}
	if *a1.JWTPublicKeyPEM == *a2.JWTPublicKeyPEM {
		t.Errorf("two setups shared a secret")
	}
}

// --- security headers ---

func TestSecurityHeadersStrict(t *testing.T) {
	headers := ResolveSecurityHeaders(&serverconfig.SecurityConfig{Mode: "strict"})
	if len(headers) != len(StrictSecurityDefaults) {
		t.Fatalf("strict headers = %d, want %d", len(headers), len(StrictSecurityDefaults))
	}
	m := headerMap(headers)
	for k, v := range StrictSecurityDefaults {
		if m[k] != v {
			t.Errorf("header %q = %q, want %q", k, m[k], v)
		}
	}
}

func TestSecurityHeadersDev(t *testing.T) {
	headers := ResolveSecurityHeaders(&serverconfig.SecurityConfig{Mode: "dev"})
	if len(headers) != 1 || headers[0].Name != "X-Content-Type-Options" || headers[0].Value != "nosniff" {
		t.Errorf("dev headers = %+v", headers)
	}
}

func TestSecurityHeadersOverridesAndSuppress(t *testing.T) {
	csp := "default-src 'none'"
	empty := ""
	cfg := &serverconfig.SecurityConfig{Mode: "strict", CSP: &csp, HSTS: &empty}
	m := headerMap(ResolveSecurityHeaders(cfg))
	if m["Content-Security-Policy"] != csp {
		t.Errorf("csp override wrong: %q", m["Content-Security-Policy"])
	}
	if _, present := m["Strict-Transport-Security"]; present {
		t.Errorf("empty-string HSTS not suppressed")
	}
	// dev-mode default suppressed by empty override.
	x := ""
	dev := &serverconfig.SecurityConfig{Mode: "dev", XContentTypeOptions: &x}
	if len(ResolveSecurityHeaders(dev)) != 0 {
		t.Errorf("dev nosniff not suppressed")
	}
}

func headerMap(h []HeaderPair) map[string]string {
	m := map[string]string{}
	for _, p := range h {
		m[p.Name] = p.Value
	}
	return m
}

// --- egress webhook guard ---

func TestAssertWebhookTargetAllowed(t *testing.T) {
	ctx := context.Background()

	// Metadata IP literal → blocked.
	err := AssertWebhookTargetAllowed(ctx, "http://169.254.169.254/latest", nil)
	var blocked *EgressBlockedError
	if !errors.As(err, &blocked) {
		t.Errorf("metadata IP not blocked: %v", err)
	}

	// Private IP literal → allowed.
	if err := AssertWebhookTargetAllowed(ctx, "https://10.0.0.5/hook", nil); err != nil {
		t.Errorf("private IP blocked: %v", err)
	}

	// DNS name resolving to a metadata IP → blocked.
	metaResolver := func(context.Context, string) ([]string, error) { return []string{"169.254.169.254"}, nil }
	if err := AssertWebhookTargetAllowed(ctx, "https://evil.example.com/x", metaResolver); !errors.As(err, &blocked) {
		t.Errorf("DNS→metadata not blocked: %v", err)
	}

	// Resolution failure → fail closed.
	failResolver := func(context.Context, string) ([]string, error) { return nil, errors.New("nxdomain") }
	if err := AssertWebhookTargetAllowed(ctx, "https://nope.example.com/x", failResolver); !errors.As(err, &blocked) {
		t.Errorf("resolution failure not fail-closed: %v", err)
	}

	// Empty resolve → fail closed.
	emptyResolver := func(context.Context, string) ([]string, error) { return nil, nil }
	if err := AssertWebhookTargetAllowed(ctx, "https://nope.example.com/x", emptyResolver); !errors.As(err, &blocked) {
		t.Errorf("empty resolve not fail-closed: %v", err)
	}

	// No host → allowed (noop).
	if err := AssertWebhookTargetAllowed(ctx, "not a url", nil); err != nil {
		t.Errorf("hostless url errored: %v", err)
	}

	// NAT64-wrapped metadata IP → blocked (embedded decode).
	if err := AssertWebhookTargetAllowed(ctx, "https://[64:ff9b::a9fe:a9fe]/x", nil); !errors.As(err, &blocked) {
		t.Errorf("NAT64-wrapped metadata not blocked: %v", err)
	}
}
